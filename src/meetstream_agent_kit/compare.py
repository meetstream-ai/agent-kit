"""Compare models by token use: one real, short meeting turn per model, projected to an hour.

Used by setup ("Measure live") and ``meetstream-agent-kit bench --compare``.
Each model answers the same small meeting turn, read-only, through the CLI's
one-run mode, in its own process and its own empty folder. A small work queue
runs a few models at once (``DEFAULT_PARALLEL``): when one finishes, its worker
takes the next model straight away. A transient failure (timeout, crash, rate
limit, network) is retried once after a short pause; a sign-in or unknown-model
error is not. At a terminal a live table shows every model; Esc stops what is
left and keeps the results so far.

The CLI's own usage numbers become a one-hour estimate (see
:mod:`meetstream_agent_kit.usage` for the assumption) and a bar chart.
"""

from __future__ import annotations

import contextlib
import math
import queue
import re
import sys
import tempfile
import textwrap
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from meetstream_agent_kit import estimates, pricing, ui
from meetstream_agent_kit.agents import AgentSpec
from meetstream_agent_kit.config import BridgeConfig
from meetstream_agent_kit.console import say
from meetstream_agent_kit.oneshot import RunResult, build_oneshot_argv, run_once
from meetstream_agent_kit.prompts import format_turn, system_prompt
from meetstream_agent_kit.usage import (
    Assumptions,
    HourEstimate,
    Usage,
    bar,
    human_tokens,
    project_hour,
    usage_from_lines,
)

SAMPLE_TURN = [
    {"speaker": "Priya", "text": "Okay, next item is the release."},
    {"speaker": "Sam", "text": "We're mostly done, two bugs left."},
    {"speaker": "{owner}", "text": "Agent, in one sentence: are we on track to ship this week?"},
]
COMPARE_TIMEOUT_S = 120.0
DEFAULT_PARALLEL: Final = 4
RETRY_BACKOFF_S: Final = 3.0
CANCELLED: Final = "cancelled"

_NO_RETRY = re.compile(
    r"log ?in|sign(ed)? ?in|auth|unauthori[sz]ed|forbidden|\b40[13]\b|api key|credential|"
    r"model\b.*\b(not found|unknown|invalid|not supported|not available|does not exist)|"
    r"(unknown|invalid|unsupported) model|cli not found",
    re.IGNORECASE,
)
_TRANSIENT = re.compile(
    r"timed? ?out|timeout|exited with|crash|killed|signal|rate.?limit|\b429\b|too many requests|"
    r"overloaded|\b50[234]\b|unavailable|network|connection|econnreset|socket|temporar|try again",
    re.IGNORECASE,
)


def retryable(error: str) -> bool:
    """A transient failure worth one more try; never a sign-in or unknown-model error."""
    return bool(error) and not _NO_RETRY.search(error) and bool(_TRANSIENT.search(error))


@dataclass(frozen=True)
class Measured:
    model: str
    usage: Usage | None
    seconds: float
    error: str = ""
    first_output_ms: float | None = None


_hooks = threading.local()
"""Set by the queue's worker for the run on that thread: ``on_stage`` and ``cancel``."""


def measure(config: BridgeConfig, model: str, effort: str) -> Measured:
    """One read-only sample turn with ``model`` in a fresh folder; the usage the CLI reports for it."""
    owner = config.owner or "the user"
    turn = format_turn([{**m, "speaker": m["speaker"].replace("{owner}", owner)} for m in SAMPLE_TURN])
    argv, stdin_text = build_oneshot_argv(
        config.agent,
        system_prompt(owner, "Be brief.") + "\n\n" + turn,
        "readonly",
        config.binary,
        model,
        effort,
        config.ignore_rules,
    )
    try:
        with tempfile.TemporaryDirectory(prefix="msak-measure-") as folder:
            report: Path | None = None
            if config.agent.usage_file_flag:
                report = Path(folder) / "usage.json"
                argv += [config.agent.usage_file_flag, str(report)]
            result: RunResult = run_once(
                config.agent,
                argv,
                stdin_text,
                folder,  # each model its own folder: parallel runs never share CLI state
                min(config.timeout_s, COMPARE_TIMEOUT_S),
                cancel=getattr(_hooks, "cancel", None),
                on_stage=getattr(_hooks, "on_stage", None),
            )
            usage = result.usage
            if report is not None and report.exists():
                usage = usage_from_lines([report.read_text(encoding="utf-8").replace("\n", " ")]) or usage
    except OSError as exc:
        return Measured(model, None, 0.0, str(exc))
    error = result.error or ("" if result.answer else "the CLI returned no answer")
    # An answer without token counts is not a failure: some CLIs (Copilot) never report them.
    measured = Measured(model, usage, result.total_ms / 1000, error, result.first_output_ms)
    estimates.save(config.agent.name, model, measured)
    return measured


# ── The work queue ────────────────────────────────────────────────────────────

_STAGES: Final = {"output": "thinking", "first_delta": "first words", "done": "answered"}


@dataclass
class Job:
    model: str
    state: str = "queued"
    """queued, running, retrying, done, failed or cancelled."""
    stage: str = ""
    """booting, thinking, first words or answered, while running."""
    retried: bool = False
    started: float | None = None
    ended: float | None = None
    reason: str = ""
    measured: Measured | None = None
    attempts: list[float] = field(default_factory=list)

    def elapsed(self, now: float | None = None) -> float:
        if self.started is None:
            return 0.0
        return (self.ended or now or time.monotonic()) - self.started


def run_queue(
    config: BridgeConfig,
    models: Sequence[str],
    effort: str,
    *,
    run: Callable[[BridgeConfig, str, str], Measured] = measure,
    parallel: int = DEFAULT_PARALLEL,
    backoff_s: float = RETRY_BACKOFF_S,
    stop: threading.Event | None = None,
    on_change: Callable[[Job], None] | None = None,
    jobs: Sequence[Job] | None = None,
) -> list[Job]:
    """Measure every model with ``parallel`` workers; a worker takes the next model as soon as it is free.

    ``jobs`` (one per model, made by the caller) lets a live view read them while they change.
    """
    jobs = list(jobs) if jobs is not None else [Job(model) for model in models]
    pending: queue.SimpleQueue[Job] = queue.SimpleQueue()
    for job in jobs:
        pending.put(job)
    stop = stop or threading.Event()
    lock = threading.Lock()
    running: set[threading.Event] = set()
    finished = threading.Event()

    def changed(job: Job) -> None:
        if on_change is not None:
            on_change(job)

    def attempt(job: Job) -> Measured:
        own_stop = threading.Event()  # run_once sets its cancel event when done: never share one
        with lock:
            running.add(own_stop)
        if stop.is_set():
            own_stop.set()

        def on_stage(event: str, _ms: float) -> None:
            if event in _STAGES and job.stage != "answered":
                job.stage = _STAGES[event]
                changed(job)

        _hooks.on_stage, _hooks.cancel = on_stage, own_stop
        try:
            return run(config, job.model, effort)
        except Exception as exc:  # one model's crash must not stop the others
            return Measured(job.model, None, 0.0, f"crashed: {exc}")
        finally:
            _hooks.on_stage = _hooks.cancel = None
            with lock:
                running.discard(own_stop)

    def work() -> None:
        while not stop.is_set():
            try:
                job = pending.get_nowait()
            except queue.Empty:
                return
            job.state, job.stage, job.started = "running", "booting", time.monotonic()
            changed(job)
            while True:
                t0 = time.monotonic()
                measured = attempt(job)
                job.attempts.append(time.monotonic() - t0)
                cut_short = measured.usage is None and config.agent.reports_usage  # killed before usage
                if stop.is_set() and (measured.error or cut_short):
                    job.state, job.reason = CANCELLED, "stopped"
                    break
                if measured.error and not job.retried and retryable(measured.error):
                    job.state, job.retried, job.reason, job.stage = (
                        "retrying",
                        True,
                        measured.error,
                        "waiting",
                    )
                    changed(job)
                    if stop.wait(backoff_s):
                        job.state, job.reason = CANCELLED, "stopped"
                        break
                    job.stage = "booting"
                    changed(job)
                    continue
                job.measured = measured
                job.state = "failed" if measured.error else "done"
                job.reason = measured.error
                job.stage = "" if measured.error else "answered"
                estimates.save(config.agent.name, job.model, measured)
                break
            job.ended = time.monotonic()
            changed(job)

    def cancel_runs() -> None:
        while not finished.is_set():
            if stop.wait(0.1):
                with lock:
                    for event in running:
                        event.set()
                return

    workers = [threading.Thread(target=work, daemon=True) for _ in range(max(1, min(parallel, len(jobs))))]
    canceller = threading.Thread(target=cancel_runs, daemon=True)
    canceller.start()
    for worker in workers:
        worker.start()
    try:
        for worker in workers:
            while worker.is_alive():
                worker.join(0.1)
    except KeyboardInterrupt:
        stop.set()
        for worker in workers:
            worker.join()
    finally:
        finished.set()
    for job in jobs:
        if job.state == "queued":
            job.state, job.reason = CANCELLED, "not started"
    return jobs


def results(jobs: Sequence[Job]) -> list[Measured]:
    return [job.measured or Measured(job.model, None, job.elapsed(), job.reason or CANCELLED) for job in jobs]


def time_left(jobs: Sequence[Job], workers: int, now: float | None = None) -> float | None:
    """Seconds until the last model should finish, from the ones done so far; None before any is."""
    now = now or time.monotonic()
    took = [job.elapsed() for job in jobs if job.state in ("done", "failed")]
    if not took:
        return None
    each = sum(took) / len(took)
    queued = sum(job.state == "queued" for job in jobs)
    running = [max(0.0, each - job.elapsed(now)) for job in jobs if job.state in ("running", "retrying")]
    return max(running, default=0.0) + math.ceil(queued / max(1, workers)) * each


# ── Showing it ────────────────────────────────────────────────────────────────


def _model_name(model: str) -> str:
    return model or "default"


def _hour_text(agent: AgentSpec, measured: Measured | None) -> tuple[str, str]:
    """Tokens per hour and the hour at API rates, for a finished model."""
    if measured is None or measured.error:
        return "", ""
    if measured.usage is None:
        return "usage n/a", ""
    cost, _ = estimates.hour_cost(agent, measured.model, measured.usage)
    tag = "free" if pricing.plan_kind(agent.name, measured.model) == pricing.FREE else ""
    return (
        f"{human_tokens(project_hour(measured.usage).total)}/h",
        tag or (f"≈ {pricing.money(cost)}/h" if cost is not None else "—"),
    )


def _status(job: Job, frame: str) -> tuple[str, str, str]:
    """(mark, text, style) for one row."""
    retried = " (retried once)" if job.retried else ""
    if job.state == "done":
        return "✓", "answered" + retried, "green"
    if job.state == "failed":
        return "✗", ui.clip((job.reason.splitlines()[0] if job.reason else "failed") + retried, 36), "red"
    if job.state == CANCELLED:
        return "·", job.reason or CANCELLED, "grey50"
    if job.state == "retrying":
        why = job.reason.splitlines()[0] if job.reason else ""
        return frame, ui.clip(f"retrying (1/1) · {job.stage}" + (f" · {why}" if why else ""), 36), "yellow"
    if job.state == "running":
        return frame, job.stage, "cyan"
    return "·", "queued", "grey50"


_OUTCOMES: Final = (("done", "answered"), ("failed", "failed"), (CANCELLED, "stopped"))


def _table(agent: AgentSpec, jobs: Sequence[Job], workers: int, frame: str) -> Any:
    from rich.console import Group
    from rich.table import Table
    from rich.text import Text

    table = Table.grid(padding=(0, 2))
    for justify in ("left", "left", "left", "right", "right", "right"):
        table.add_column(justify=justify)
    now = time.monotonic()
    for job in jobs:
        mark, text, style = _status(job, frame)
        tokens, cost = _hour_text(agent, job.measured)
        seconds = f"{job.elapsed(now):.1f} s" if job.started is not None else ""
        table.add_row(
            Text(mark, style=style),
            Text(ui.clip(_model_name(job.model), 34)),
            Text(text, style=style),
            Text(seconds, style="grey50"),
            Text(tokens),
            Text(cost, style="yellow" if cost.startswith("≈") else "grey50"),
        )
    done = sum(job.state in ("done", "failed", CANCELLED) for job in jobs)
    left = time_left(jobs, workers, now)
    if done < len(jobs):
        summary = f"{done}/{len(jobs)} done"
        summary += f" · {sum(job.state in ('running', 'retrying') for job in jobs)} running"
        summary += f" · ~{left:.0f} s left" if left is not None else ""
    else:
        counts = [(sum(job.state == state for job in jobs), word) for state, word in _OUTCOMES]
        summary = " · ".join(f"{count} {word}" for count, word in counts if count)
    footer = Text("  " + summary, style="bold")
    keys = Text("  Esc stop (keeps the results so far)" if done < len(jobs) else "", style="grey42")
    return Group(table, footer, keys)


@contextlib.contextmanager
def _keys_pressed(stop: threading.Event) -> Iterator[None]:
    """Esc or q sets ``stop`` while the table is up (Unix terminals; elsewhere Ctrl-C does)."""
    try:
        import termios
        import tty

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
    except (ImportError, OSError, ValueError, AttributeError):
        yield
        return
    done = threading.Event()

    def read() -> None:
        import os
        import select

        while not done.is_set():
            if select.select([fd], [], [], 0.1)[0]:
                key = os.read(fd, 32)
                if key[:1] in (b"\x1b", b"q", b"\x03"):
                    stop.set()
                    return

    tty.setcbreak(fd)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        yield
    finally:
        done.set()
        reader.join(1)
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _live(
    config: BridgeConfig,
    models: Sequence[str],
    effort: str,
    run: Callable[[BridgeConfig, str, str], Measured],
    parallel: int,
    backoff_s: float,
) -> list[Job]:
    from rich.live import Live

    stop = threading.Event()
    jobs = [Job(model) for model in models]
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    worker = threading.Thread(
        target=run_queue,
        args=(config, models, effort),
        kwargs={"run": run, "parallel": parallel, "backoff_s": backoff_s, "stop": stop, "jobs": jobs},
        daemon=True,
    )
    with _keys_pressed(stop), Live(console=ui._rich(), refresh_per_second=10) as live:
        worker.start()
        tick = 0
        while worker.is_alive():
            live.update(_table(config.agent, jobs, parallel, frames[tick % len(frames)]))
            tick += 1
            try:
                worker.join(0.1)
            except KeyboardInterrupt:
                stop.set()
        live.update(_table(config.agent, jobs, parallel, ""))
    return jobs


def _plain_line(agent: AgentSpec, job: Job) -> str | None:
    """One line per finished (or retried) model, for logs and pipes."""
    name = _model_name(job.model)
    if job.state == "retrying" and job.stage == "waiting":
        return f"  ↻ {name}: {ui.clip(job.reason.splitlines()[0], 100)}; retrying once"
    measured = job.measured
    if job.state == "done" and measured is not None:
        if measured.usage is None:
            return (
                f"  ✓ {name} answered in {measured.seconds:.1f} s · {agent.label} doesn't report token usage"
            )
        tokens, cost = _hour_text(agent, measured)
        return (
            f"  ✓ {name}  {human_tokens(measured.usage.input)} in · {human_tokens(measured.usage.output)} out"
            f"  ({measured.seconds:.1f} s) · {tokens} {cost}"
        )
    if job.state == "failed":
        return f"  ✗ {name}: {job.reason[:160]}"
    return None


def compare(
    config: BridgeConfig,
    models: Sequence[str],
    effort: str,
    *,
    run: Callable[[BridgeConfig, str, str], Measured] = measure,
    parallel: int = DEFAULT_PARALLEL,
    backoff_s: float = RETRY_BACKOFF_S,
) -> list[Measured]:
    """Measure the models a few at a time: a live table at a terminal, else a line per model."""
    parallel = max(1, parallel)
    if ui._rich() is not None and ui.is_interactive():
        jobs = _live(config, models, effort, run, parallel, backoff_s)
    else:
        printing = threading.Lock()

        def on_change(job: Job) -> None:
            line = _plain_line(config.agent, job)
            if line is not None and job.state != "running":
                with printing:
                    say(line)

        say(f"  Measuring {len(models)} model(s), {min(parallel, len(models))} at a time.")
        jobs = run_queue(
            config, models, effort, run=run, parallel=parallel, backoff_s=backoff_s, on_change=on_change
        )
        stopped = sum(job.state == CANCELLED for job in jobs)
        if stopped:
            say(f"  Stopped: {stopped} model(s) not measured.")
    return results(jobs)


def chart_lines(
    results: Sequence[Measured],
    assumptions: Assumptions | None = None,
    *,
    selected: str = "",
    width: int = 0,
    agent: AgentSpec | None = None,
) -> list[str]:
    """The estimate as horizontal bars: model, tokens per hour, and the hour at API rates where known."""
    a = assumptions or Assumptions()
    rows: list[tuple[str, HourEstimate, float, Usage]] = [
        (m.model, project_hour(m.usage, a), m.seconds, m.usage) for m in results if m.usage and not m.error
    ]
    if not rows:
        if any(m.usage is None and not m.error for m in results):
            return ["  This CLI doesn't report token usage, so there is no usage to chart (times are above)."]
        return ["  No model reported token usage, so there is nothing to chart."]
    name_w = min(32, max(len(name or "default") for name, _, _, _ in rows))
    largest = max(estimate.total for _, estimate, _, _ in rows)
    costs = []
    for name, estimate, _, turn in rows:
        if estimate.cost_usd is not None:
            costs.append(f"≈ ${estimate.cost_usd:.2f}/h")
        elif agent is not None and (found := estimates.hour_cost(agent, name, turn)[0]) is not None:
            costs.append(f"≈ {pricing.money(found)}/h")
        else:
            costs.append("—")
    if not width:  # fit the terminal: everything but the bar is fixed width
        fixed = name_w + 27 + max(len(c) for c in costs)  # " ★ name  ▕bar▏  123k tok/h  12 s  cost"
        width = max(8, min(28, ui.width() - fixed - 1))
    lines = [
        ui.paint("Estimated tokens for one hour of meeting", "bold") + ui.paint("  (an estimate)", "dim"),
        "",
    ]
    for (name, estimate, seconds, _), cost in zip(rows, costs, strict=True):
        label = (name or "default")[:name_w].ljust(name_w)
        mark = ui.paint("★", "orange") if name == selected else " "
        filled = bar(estimate.total, largest, width)
        lines.append(
            f" {mark} {label}  {ui.paint(filled.ljust(width), 'cyan')}  "
            f"{human_tokens(estimate.total).rjust(6)} tok/h  {f'{seconds:.0f} s'.rjust(4)}  "
            f"{ui.paint(cost, 'dim')}"
        )
    notes = [
        f"Assumes {a.describe()}.",
        "Measured from one real short turn per model on this computer. The seconds are that turn, "
        "started cold; warm meeting turns answer faster. Real meetings vary.",
        "Prices are API-rate estimates: the CLI's own where it reports one, else the vendor's published "
        "per-token list price (— when unknown). On a subscription or free plan the same tokens count "
        "against your plan instead of a bill.",
    ]
    lines.append("")
    for note in notes:
        lines += [
            ui.paint(line, "dim")
            for line in textwrap.wrap(
                note, max(40, ui.width() - 4), initial_indent="  ", subsequent_indent="  "
            )
        ]
    return lines
