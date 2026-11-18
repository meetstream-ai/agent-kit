"""One meeting: one warm agent process, its turns, its meeting tools.

When a MIA bot starts a meeting, the bridge starts ONE long-lived agent process
for it (when the CLI has a warm mode) and keeps it for every turn, so the agent
never pays its start-up time while someone waits for an answer. CLIs without a
warm mode run once per turn.

A turn is a stream of events, each sent to MIA as one NDJSON line:
``delta`` (text as it is written), ``tool_call`` (the agent called a meeting
tool; MIA runs it and answers on ``/v1/tool_result``), then ``done`` or
``error``.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import secrets
import subprocess
import threading
import time
from collections.abc import Iterator, Sequence
from typing import Any, Final

from meetstream_agent_kit.access import AccessLevel
from meetstream_agent_kit.activation import ActivationGate
from meetstream_agent_kit.config import BridgeConfig
from meetstream_agent_kit.console import say
from meetstream_agent_kit.constants import (
    APPROVE_TOOL,
    CANCEL_GRACE_S,
    MAX_ANSWER_CHARS,
    MCP_SECRET_ENV,
    PREEMPT_WAIT_S,
    TIMEOUT_REPLY,
    TOOL_RESULT_TIMEOUT_S,
)
from meetstream_agent_kit.drivers import Driver, make_driver
from meetstream_agent_kit.oneshot import RunResult, build_oneshot_argv, close_stream, run_once, stop_process
from meetstream_agent_kit.parsers import classify_error
from meetstream_agent_kit.prompts import system_prompt

TURN_END: Final = object()
"""Queued by a driver when the current turn is over."""

Event = dict[str, Any]


class _ToolWaiter:
    __slots__ = ("done", "is_error", "output")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.output = ""
        self.is_error = True


class MeetingSession:
    """One MIA session (one bot in one meeting)."""

    def __init__(
        self,
        session_id: str,
        config: BridgeConfig,
        *,
        instructions: str = "",
        tools: Sequence[dict[str, Any]] = (),
        model: str = "",
        port: int = 0,
        access: AccessLevel | None = None,
        activation_required: bool = False,
    ) -> None:
        self.session_id = session_id
        self.config = config
        self.access: AccessLevel = access or config.max_access
        self.instructions = instructions
        self.tools = list(tools)
        self.model = model
        self.port = port
        self.activation_required = activation_required
        self.activation: ActivationGate | None = None
        self.mcp_secret = secrets.token_urlsafe(24)
        self.started_turns = 0
        self._warmup_running = False
        self.mcp_tools_listed: int | None = None
        """How many meeting tools the agent loaded over MCP (None until it asks)."""
        self.proc: subprocess.Popen[str] | None = None
        self.spawn_ms: float | None = None
        self.ready = threading.Event()
        self.events: queue.Queue[Any] = queue.Queue()
        self._turn_lock = threading.Lock()
        self._proc_lock = threading.Lock()
        self._cancel = threading.Event()
        self._pending: dict[str, _ToolWaiter] = {}
        self._pending_lock = threading.Lock()
        self.driver: Driver | None = make_driver(self)

    # Facts the drivers and the server use --------------------------------

    @property
    def warm(self) -> bool:
        return self.driver is not None

    @property
    def mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp/{self.session_id}"

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def turn_running(self) -> bool:
        return self._turn_lock.locked()

    def rules(self, access: AccessLevel | None = None) -> str:
        """The agent's instructions, for the access level the agent really runs at."""
        return system_prompt(self.config.owner, self.instructions, access or self.access)

    def changed_fields(self, other: MeetingSession) -> list[str]:
        """Which parts of the meeting's setup differ from ``other`` (for the restart log line)."""
        pairs = (
            ("instructions", self.instructions, other.instructions),
            ("tools", self.tools, other.tools),
            ("model", self.model, other.model),
            ("access", self.access, other.access),
            ("activation", self.activation_required, other.activation_required),
        )
        return [name for name, mine, theirs in pairs if mine != theirs]

    def config_key(self) -> str:
        """Changes when anything that shapes the agent process changes."""
        key = [self.instructions, self.tools, self.model, self.access, self.activation_required]
        return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()

    def tool_names(self) -> set[str]:
        return {t["name"] for t in self.tools}

    def allow_action(self, description: str) -> bool:
        """May the agent run a shell command or change a file right now?"""
        if self.cancelled:
            return False
        if self.access == "full":
            return True
        if self.access == "confirm":
            return self.config.confirm(description)
        return False

    # Events from drivers ----------------------------------------------------

    def emit(self, kind: str, value: Any = None) -> None:
        self.events.put((kind, value))

    def end_turn(self) -> None:
        self.events.put(TURN_END)

    def is_current(self, proc: subprocess.Popen[str]) -> bool:
        return proc is self.proc

    def log(self, text: str) -> None:
        """One line in the bridge terminal (silent in tests)."""
        if not self.config.quiet:
            say(f"[session] {self.session_id}: {text}")

    def debug(self, text: str) -> None:
        """A protocol trace line, only with ``--debug`` / ``MS_AGENT_KIT_DEBUG=1``."""
        if self.config.debug:
            say(f"[debug] {self.session_id}: {text}")

    def startup_error(self, default: str) -> str:
        """The error a failed start queued, or ``default``."""
        if not self.events.empty():
            item = self.events.get(timeout=1)
            if isinstance(item, tuple):
                return str(item[1])
        return default

    def _drop_queued_events(self) -> None:
        while not self.events.empty():
            self.events.get_nowait()

    # Process lifecycle --------------------------------------------------------

    def start(self, resume: bool = False) -> None:
        """Start the warm agent process (no-op for one-shot agents or a running one)."""
        driver = self.driver
        if driver is None:
            self.ready.set()
            return
        with self._proc_lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            t0 = time.monotonic()
            self.ready.clear()
            proc = subprocess.Popen(
                driver.argv(resume),
                cwd=self.config.workdir,
                env=self._spawn_env(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            self.proc = proc
            self.spawn_ms = (time.monotonic() - t0) * 1000
        threading.Thread(target=driver.read, args=(proc,), daemon=True).start()
        threading.Thread(target=_drain, args=(proc,), daemon=True).start()
        driver.handshake()

    def _spawn_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env[MCP_SECRET_ENV] = self.mcp_secret
        model_env = self.config.agent.model_env
        if self.model and model_env:
            env[model_env] = self.model
        return env

    def restart(self, resume: bool = True) -> None:
        with self._proc_lock:
            proc, self.proc = self.proc, None
        if proc is not None:
            stop_process(proc)
        if self.warm:
            self.start(resume=resume and self.started_turns > 0)

    def close(self) -> None:
        self._fail_pending("the meeting ended")
        with self._proc_lock:
            proc, self.proc = self.proc, None
        if proc is not None:
            stop_process(proc)
        if self.driver is not None:
            self.driver.cleanup()

    # Turns --------------------------------------------------------------------

    def run_turn(self, text: str, *, warmup: bool = False) -> Iterator[Event]:
        """Run one turn; yield its events. Never two at once.

        MIA runs one turn at a time, so a turn that arrives while another runs
        means MIA gave up on that one: it is stopped, not queued behind. The
        warm-up turn at meeting start is waited out instead (up to 15 s).
        """
        if not self._turn_lock.acquire(timeout=PREEMPT_WAIT_S):
            if not self._warmup_running:
                self.log("a new turn arrived while the last one was still running: stopped the last one")
                self.cancel()
            if not self._turn_lock.acquire(timeout=15):
                self.log("the last turn did not stop in 15 s: this turn was dropped")
                yield {"type": "error", "code": "busy", "message": "a turn is already running"}
                return
        self._warmup_running = warmup
        try:
            self._cancel.clear()
            if self.driver is not None:
                yield from self._warm_turn(self.driver, text)
            else:
                yield from self._oneshot_turn(text)
        finally:
            self._warmup_running = False
            self._turn_lock.release()

    def _warm_turn(self, driver: Driver, text: str) -> Iterator[Event]:
        t0 = time.monotonic()
        if self.proc is None or self.proc.poll() is not None:
            self.start(resume=self.started_turns > 0)
        not_ready = driver.check_ready()
        if not_ready:
            yield _error(not_ready)
            return
        self._drop_queued_events()  # leftovers of a cancelled turn
        try:
            driver.send_turn(text)
        except (BrokenPipeError, AttributeError, ValueError):
            yield {"type": "error", "code": "agent_error", "message": "the agent process exited"}
            return
        except (RuntimeError, TimeoutError) as exc:
            yield _error(str(exc))
            return
        self.started_turns += 1
        first_ms: float | None = None
        chunks: list[str] = []
        idle_limit = self.config.timeout_s
        last_activity = t0
        while True:
            if self.cancelled:
                yield _cancelled()
                return
            try:
                wait = last_activity + idle_limit - time.monotonic()
                item = self.events.get(timeout=max(0.05, min(0.5, wait)))
            except queue.Empty:
                if self.pending_tool_calls():
                    last_activity = time.monotonic()  # waiting on MIA, not on the agent
                elif time.monotonic() >= last_activity + idle_limit:
                    yield from self._timed_out(driver, chunks, first_ms, t0)
                    return
                continue
            if item is TURN_END:
                break
            last_activity = time.monotonic()
            kind, value = item
            if kind == "wake":  # cancel() woke the loop; the check at the top ends the turn
                continue
            if kind == "delta":
                if first_ms is None:
                    first_ms = (time.monotonic() - t0) * 1000
                chunks.append(value)
                yield {"type": "delta", "text": value}
            elif kind == "tool_call":
                yield {"type": "tool_call", **value}
            elif kind == "rpc_done":
                if value == "cancelled":
                    yield _cancelled()
                    return
                break
            elif kind == "final":
                if not chunks and value:  # no partial messages: send it whole
                    first_ms = (time.monotonic() - t0) * 1000
                    chunks.append(value)
                    yield {"type": "delta", "text": value}
            elif kind in ("error", "exit"):
                message = str(value) if kind == "error" else f"the agent process exited ({value})"
                yield _error(message)
                return
        yield {
            "type": "done",
            "text": "".join(chunks).strip()[:MAX_ANSWER_CHARS],
            "first_output_ms": None if first_ms is None else round(first_ms, 1),
            "total_ms": round((time.monotonic() - t0) * 1000, 1),
            "warm": True,
        }

    def _timed_out(
        self, driver: Driver, chunks: list[str], first_ms: float | None, t0: float
    ) -> Iterator[Event]:
        """No output for too long: ask the agent to stop, keep the session if it does, say so."""
        self.log(f"no output for {int(self.config.timeout_s)} s: stopping the turn")
        proc = self.proc
        self.cancel()
        if not self._wait_turn_end(CANCEL_GRACE_S, proc):
            self.log("the agent did not stop the turn: restarting it (the conversation is kept)")
            self.restart()
        yield from _timeout_reply(chunks, first_ms, t0, warm=True)

    def _wait_turn_end(self, timeout_s: float, proc: subprocess.Popen[str] | None) -> bool:
        """After a cancel: wait for the agent to end its turn (or for the driver's own restart,
        which is how stream-json agents stop). False if neither happens in time."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.proc is not proc:
                return True
            try:
                item = self.events.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is TURN_END or (isinstance(item, tuple) and item[0] in ("rpc_done", "error", "exit")):
                return True
        return False

    def _oneshot_turn(self, text: str) -> Iterator[Event]:
        cfg = self.config
        agent = cfg.agent
        access: AccessLevel = "full" if self.access == "full" else "readonly"
        argv, stdin_text = build_oneshot_argv(
            agent, self.rules(access) + "\n\n" + text, access, cfg.binary, self.model, cfg.effort
        )
        deltas: queue.Queue[Any] = queue.Queue()
        box: dict[str, RunResult] = {}

        def work() -> None:
            box["result"] = run_once(
                agent, argv, stdin_text, str(cfg.workdir), cfg.timeout_s, deltas.put, cancel=self._cancel
            )
            deltas.put(TURN_END)

        threading.Thread(target=work, daemon=True).start()
        suffix = "\n" if agent.parser == "text" else ""
        while (item := deltas.get()) is not TURN_END:
            yield {"type": "delta", "text": item + suffix}
        result = box["result"]
        if self.cancelled and not result.answer:
            yield _cancelled()
        elif result.answer:
            yield {
                "type": "done",
                "text": result.answer,
                "spawn_ms": round(result.spawn_ms, 1),
                "first_output_ms": None
                if result.first_output_ms is None
                else round(result.first_output_ms, 1),
                "total_ms": round(result.total_ms, 1),
                "warm": False,
            }
        elif result.error and "timed out" in result.error:
            self.log(f"no answer within {int(cfg.timeout_s)} s: stopped the run")
            yield from _timeout_reply([], None, time.monotonic() - result.total_ms / 1000, warm=False)
        else:
            yield _error(result.error or "the agent returned no answer")

    def cancel(self) -> None:
        """Barge-in: stop the turn now and keep the conversation."""
        self._cancel.set()
        self.emit("wake")  # end the turn now, not at the next 0.5 s poll
        self._fail_pending("cancelled by the speaker")
        if self.driver is not None:
            self.driver.cancel()

    # Meeting tools over MCP ----------------------------------------------------

    def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """The agent called a meeting tool: hand it to MIA and wait for the result."""
        call_id = "call_" + secrets.token_hex(8)
        waiter = _ToolWaiter()
        with self._pending_lock:
            self._pending[call_id] = waiter
        self.emit("tool_call", {"call_id": call_id, "name": name, "arguments": json.dumps(arguments or {})})
        if not waiter.done.wait(TOOL_RESULT_TIMEOUT_S):
            with self._pending_lock:
                self._pending.pop(call_id, None)
            return "MIA did not return a result in time.", True
        return waiter.output, waiter.is_error

    def resolve_tool(self, call_id: str, output: str, is_error: bool) -> bool:
        with self._pending_lock:
            waiter = self._pending.pop(call_id, None)
        if waiter is None:
            return False
        waiter.output, waiter.is_error = output, is_error
        waiter.done.set()
        return True

    def pending_tool_calls(self) -> list[str]:
        with self._pending_lock:
            return list(self._pending)

    def _fail_pending(self, reason: str) -> None:
        with self._pending_lock:
            pending, self._pending = self._pending, {}
        for waiter in pending.values():
            waiter.output, waiter.is_error = reason, True
            waiter.done.set()

    def mcp_tools(self) -> list[dict[str, Any]]:
        """The tools this meeting offers the agent, in MCP form."""
        tools = [
            {
                "name": t["name"],
                "description": t.get("description") or "",
                "inputSchema": t.get("parameters") or {"type": "object", "properties": {}},
            }
            for t in self.tools
        ]
        if self.access == "confirm":
            tools.append(
                {
                    "name": APPROVE_TOOL,
                    "description": "Internal: asks the owner to approve an action.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"tool_name": {"type": "string"}, "input": {"type": "object"}},
                    },
                }
            )
        return tools


def _drain(proc: subprocess.Popen[str]) -> None:
    """Read and drop stderr so a chatty agent never blocks on a full pipe."""
    if proc.stderr is not None:
        for _ in proc.stderr:
            pass
        close_stream(proc.stderr)


def _timeout_reply(chunks: list[str], first_ms: float | None, t0: float, *, warm: bool) -> Iterator[Event]:
    """What MIA gets when a turn is stopped for taking too long: a short sentence to speak."""
    said = "".join(chunks).strip()
    yield {"type": "delta", "text": (" " if said else "") + TIMEOUT_REPLY}
    yield {
        "type": "done",
        "text": f"{said} {TIMEOUT_REPLY}".strip(),
        "timed_out": True,
        "first_output_ms": None if first_ms is None else round(first_ms, 1),
        "total_ms": round((time.monotonic() - t0) * 1000, 1),
        "warm": warm,
    }


def _error(message: str) -> Event:
    return {"type": "error", "code": classify_error(message), "message": message[:300]}


def _cancelled() -> Event:
    return {"type": "error", "code": "cancelled", "message": "cancelled"}
