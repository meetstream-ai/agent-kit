"""Run an agent CLI once for one turn (agents without a warm mode, and ``bench``)."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from meetstream_agent_kit.access import AccessLevel
from meetstream_agent_kit.agents import AgentSpec
from meetstream_agent_kit.constants import MAX_ANSWER_CHARS, STOP_GRACE_S
from meetstream_agent_kit.parsers import PARSERS
from meetstream_agent_kit.usage import Usage, usage_from_lines


@dataclass
class RunResult:
    answer: str = ""
    error: str | None = None
    exit_code: int | None = None
    spawn_ms: float = 0.0
    first_output_ms: float | None = None
    total_ms: float = 0.0
    deltas: list[str] = field(default_factory=list)
    usage: Usage | None = None
    """Token usage the CLI reported for the run, when it prints one."""
    stderr_tail: str = ""
    """The last lines the CLI wrote to stderr, to explain an empty or failed turn."""
    first_line_ms: float | None = None


def build_oneshot_argv(
    agent: AgentSpec,
    prompt: str,
    access: AccessLevel,
    binary: str | None = None,
    model: str = "",
    effort: str = "",
    ignore_rules: bool = False,
) -> tuple[list[str], str | None]:
    """Return ``(argv, stdin_text)`` for one non-interactive run.

    One-shot runs cannot ask before acting, so ``confirm`` runs read-only.
    """
    via_arg = agent.prompt_via == "arg"
    argv = [binary or agent.binary]
    argv += [prompt if (via_arg and part == "{prompt}") else part for part in agent.oneshot_args]
    argv += agent.full_args if access == "full" else agent.readonly_args
    argv += agent.model_args(model)
    argv += agent.effort_flags(effort)
    if ignore_rules and agent.ignore_rules_args:
        # After a subcommand (codex exec) the options go right behind it; otherwise at the end.
        at = 2 if agent.oneshot_args and not agent.oneshot_args[0].startswith("-") else len(argv)
        argv[at:at] = list(agent.ignore_rules_args)
    return argv, (None if via_arg else prompt)


def kill_process_group(proc: subprocess.Popen[str], sig: int = signal.SIGTERM) -> None:
    """Signal the agent and everything it started (it runs in its own process group)."""
    with contextlib.suppress(OSError):  # already gone
        os.killpg(proc.pid, sig)


def stop_process(proc: subprocess.Popen[str], grace_s: float = STOP_GRACE_S) -> None:
    """Stop an agent for good: SIGTERM, SIGKILL after ``grace_s``, then reap it.

    Returns at once; the waiting happens on a background thread. Pipes are
    closed by whoever reads them, at end of file.
    """
    kill_process_group(proc)
    if proc.stdin is not None:
        with contextlib.suppress(OSError):
            proc.stdin.close()

    def reap() -> None:
        try:
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            kill_process_group(proc, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=grace_s)

    threading.Thread(target=reap, daemon=True).start()


def close_stream(stream: object) -> None:
    """Close a pipe we finished reading."""
    close = getattr(stream, "close", None)
    if close is not None:
        with contextlib.suppress(OSError):
            close()


def run_once(
    agent: AgentSpec,
    argv: list[str],
    stdin_text: str | None,
    workdir: str,
    timeout_s: float,
    on_delta: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
    on_stage: Callable[[str, float], None] | None = None,
) -> RunResult:
    """Spawn the CLI once and stream its output through the agent's parser."""
    parse = PARSERS[agent.parser]
    result = RunResult()
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=workdir,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except FileNotFoundError:
        result.error = f"agent CLI not found: {argv[0]}"
        result.total_ms = (time.monotonic() - t0) * 1000
        if on_stage:
            on_stage("done", result.total_ms)
        return result
    assert proc.stdout is not None  # noqa: S101 - set by PIPE above
    assert proc.stderr is not None  # noqa: S101 - set by PIPE above
    result.spawn_ms = (time.monotonic() - t0) * 1000
    stderr_tail: list[str] = []
    stderr = proc.stderr

    def drain_stderr() -> None:
        for line in stderr:
            stderr_tail.append(line)
            del stderr_tail[:-20]

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()
    timer = threading.Timer(timeout_s, kill_process_group, args=(proc,))
    timer.start()
    if cancel is not None:

        def stop_on_cancel() -> None:
            cancel.wait()
            if proc.poll() is None:
                kill_process_group(proc)

        threading.Thread(target=stop_on_cancel, daemon=True).start()
    final: str | None = None
    try:
        if stdin_text is not None and proc.stdin is not None:
            try:
                proc.stdin.write(stdin_text)
                proc.stdin.close()
            except BrokenPipeError:
                pass
        for raw in proc.stdout:
            if result.first_line_ms is None:
                result.first_line_ms = (time.monotonic() - t0) * 1000
                if on_stage:
                    on_stage("output", result.first_line_ms)
            if '"usage"' in raw:
                result.usage = usage_from_lines([raw]) or result.usage
            line = parse(raw.rstrip("\n"))
            if line.error:
                result.error = line.error
            if line.final is not None:
                final = line.final
            if line.delta:
                if result.first_output_ms is None:
                    result.first_output_ms = (time.monotonic() - t0) * 1000
                    if on_stage:
                        on_stage("first_delta", result.first_output_ms)
                result.deltas.append(line.delta)
                if on_delta:
                    on_delta(line.delta)
        result.exit_code = proc.wait()
    finally:
        timer.cancel()
        if cancel is not None:
            cancel.set()  # release the watcher thread
        stderr_thread.join(timeout=1)
        close_stream(proc.stdout)
        close_stream(proc.stderr)
        if proc.stdin is not None:
            close_stream(proc.stdin)
    result.total_ms = (time.monotonic() - t0) * 1000
    joiner = "\n" if agent.parser == "text" else ""
    answer = final if final is not None else joiner.join(result.deltas)
    result.answer = answer.strip()[:MAX_ANSWER_CHARS]
    tail = "".join(stderr_tail).strip()[-300:]
    result.stderr_tail = tail
    if result.exit_code not in (0, None) and not result.answer and not result.error:
        result.error = f"agent exited with {result.exit_code}" + (f": {tail}" if tail else "")
    if result.total_ms >= timeout_s * 1000 and not result.answer:
        result.error = f"agent timed out after {int(timeout_s)} s"
    if on_stage:
        on_stage("done", result.total_ms)
    return result
