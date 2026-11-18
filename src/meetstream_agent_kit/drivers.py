"""Warm drivers: how the bridge talks to a long-lived agent process.

* :class:`StreamJsonDriver`: Claude Code, one JSON line per turn in and
  stream-json events out.
* :class:`AcpDriver`: Agent Client Protocol (JSON-RPC 2.0 over stdio): Antigravity,
  Hermes, Copilot CLI, Muse Code, OpenClaw.
* :class:`CodexDriver`: ``codex app-server`` (typed JSON-RPC: threads and turns).

A driver builds the agent's command line, reads its output into the session's
event queue, writes each turn, and stops a turn on barge-in. Process lifecycle,
the turn loop and meeting-tool plumbing live in :mod:`meetstream_agent_kit.session`.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import secrets
import subprocess
import sys
import threading
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from meetstream_agent_kit import __version__
from meetstream_agent_kit.constants import APPROVE_TOOL, MCP_SECRET_ENV, MCP_SERVER_NAME
from meetstream_agent_kit.oneshot import close_stream
from meetstream_agent_kit.pairing import default_home
from meetstream_agent_kit.parsers import parse_agy, parse_claude

if TYPE_CHECKING:
    from meetstream_agent_kit.session import MeetingSession

_CLIENT_INFO: Final = {
    "name": "meetstream-agent-kit",
    "title": "MeetStream Agent Kit",
    "version": __version__,
}
_SAFE_ACP_KINDS: Final = frozenset({"read", "search", "think", "fetch"})


def self_command() -> list[str]:
    """How to run this package again (for the MCP stdio shim), installed or as a ``.pyz``."""
    spec = importlib.util.find_spec("meetstream_agent_kit")
    archive = getattr(spec.loader, "archive", None) if spec else None  # set by zipimport
    if isinstance(archive, str):
        return [sys.executable, archive]
    return [sys.executable, "-m", "meetstream_agent_kit"]


def make_driver(session: MeetingSession) -> Driver | None:
    """The driver for the session's agent, or None when it runs once per turn."""
    warm = session.config.agent.warm
    if warm in ("claude_stream_json", "agy_stream_json"):
        return StreamJsonDriver(session)
    if warm == "acp":
        return AcpDriver(session)
    if warm == "codex_app_server":
        return CodexDriver(session)
    return None


class Driver(ABC):
    """One warm agent protocol."""

    def __init__(self, session: MeetingSession) -> None:
        self.session = session

    @property
    def binary(self) -> str:
        return self.session.config.binary or self.session.config.agent.binary

    @abstractmethod
    def argv(self, resume: bool) -> list[str]:
        """The agent's command line. ``resume`` continues the conversation after a restart."""

    def handshake(self) -> None:  # noqa: B027 - optional hook
        """Runs right after the process starts (protocol initialisation)."""

    @abstractmethod
    def read(self, proc: subprocess.Popen[str]) -> None:
        """Read the process's stdout until it exits (runs on its own thread)."""

    def check_ready(self) -> str | None:
        """An error message when the agent cannot take a turn yet, else None."""
        return None

    @abstractmethod
    def send_turn(self, text: str) -> None:
        """Hand one turn to the agent."""

    @abstractmethod
    def cancel(self) -> None:
        """Stop the running turn and keep the conversation."""

    def cleanup(self) -> None:  # noqa: B027 - optional hook
        """Remove anything the driver created for this meeting."""

    def _finish(self, proc: subprocess.Popen[str]) -> None:
        """The process's stdout closed: release its pipe and unblock anyone waiting on it."""
        close_stream(proc.stdout)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=1)  # reap it; a process still running is reaped by stop_process
        self.session.ready.set()
        if self.session.is_current(proc):
            self.session.emit("exit", proc.poll())
            self.session.end_turn()


# ── Claude Code (and a custom agy stream-json spec): stream-json ─────────────────


class StreamJsonDriver(Driver):
    """One JSON line per turn on stdin, stream-json events on stdout."""

    def __init__(self, session: MeetingSession) -> None:
        super().__init__(session)
        self.is_claude = session.config.agent.warm == "claude_stream_json"
        self._parse = parse_claude if self.is_claude else parse_agy
        self.conversation_id = str(uuid.uuid4())
        self._mcp_config: Path | None = None

    def argv(self, resume: bool) -> list[str]:
        session, cfg = self.session, self.session.config
        argv = [self.binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json"]
        if self.is_claude:
            argv += [
                "--include-partial-messages",
                "--verbose",
                "--append-system-prompt",
                session.rules(),
                "--mcp-config",
                str(self._write_mcp_config()),
                "--strict-mcp-config",
            ]
            argv += ["--resume" if resume else "--session-id", self.conversation_id]
            meeting_tools = f"mcp__{MCP_SERVER_NAME}"
            if session.access == "confirm":
                argv += [
                    "--tools=default",
                    f"--allowedTools=Read,Grep,Glob,{meeting_tools}",
                    "--permission-prompt-tool",
                    f"{meeting_tools}__{APPROVE_TOOL}",
                ]
            elif session.access == "full":
                argv += [
                    "--tools=default",
                    "--permission-mode",
                    "acceptEdits",
                    f"--allowedTools=Bash,{meeting_tools}",
                ]
            else:
                argv += [
                    "--tools=Read,Grep,Glob",
                    "--permission-mode",
                    "dontAsk",
                    f"--allowedTools={meeting_tools}",
                ]
        else:  # Antigravity has no system-prompt or MCP flags: the rules go in the first turn
            argv += cfg.agent.full_args if session.access == "full" else cfg.agent.readonly_args
        argv += cfg.agent.model_args(session.model)
        if cfg.ignore_rules and cfg.agent.ignore_rules_warm:
            argv += cfg.agent.ignore_rules_args
        argv += cfg.agent.effort_flags(cfg.effort)
        return argv

    def _write_mcp_config(self) -> Path:
        """Claude's MCP config, in a 0600 file so the meeting secret never shows in ``ps``."""
        if self._mcp_config is not None and self._mcp_config.exists():
            return self._mcp_config
        session = self.session
        directory = (session.config.runtime_dir or default_home()) / "run"
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        path = directory / f"mcp-{session.session_id}-{secrets.token_hex(4)}.json"
        server = {
            "type": "http",
            "url": session.mcp_url,
            "headers": {"Authorization": f"Bearer {session.mcp_secret}"},
        }
        with path.open("x", encoding="utf-8") as handle:
            path.chmod(0o600)
            json.dump({"mcpServers": {MCP_SERVER_NAME: server}}, handle)
        self._mcp_config = path
        return path

    def cleanup(self) -> None:
        if self._mcp_config is not None:
            self._mcp_config.unlink(missing_ok=True)
            self._mcp_config = None

    def read(self, proc: subprocess.Popen[str]) -> None:
        session = self.session
        assert proc.stdout is not None  # noqa: S101 - spawned with stdout=PIPE
        for raw in proc.stdout:
            if not session.is_current(proc):
                # Barge-in replaced this process: what it still prints belongs to
                # the cancelled turn and must not leak into the next one.
                close_stream(proc.stdout)
                return
            line = raw.rstrip("\n")
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if (
                isinstance(event, dict)
                and event.get("type") in ("system", "init")
                and (event.get("subtype", "init") == "init")
            ):
                session.ready.set()
                continue
            parsed = self._parse(line)
            if parsed.delta:
                session.emit("delta", parsed.delta)
            if parsed.error is not None:
                session.emit("error", parsed.error)
                session.end_turn()
            elif parsed.final is not None:
                session.emit("final", parsed.final)
                session.end_turn()
        self._finish(proc)

    def send_turn(self, text: str) -> None:
        proc = self.session.proc
        if proc is None or proc.stdin is None:
            raise BrokenPipeError("the agent process is not running")
        proc.stdin.write(self._input_line(text) + "\n")
        proc.stdin.flush()

    def _input_line(self, text: str) -> str:
        if self.is_claude:
            return json.dumps(
                {"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None}
            )
        if self.session.started_turns == 0:
            text = self.session.rules() + "\n\n" + text
        return json.dumps({"event": "user", "message": {"content": text}})

    def cancel(self) -> None:
        # stream-json has no "stop": restart the process and resume the conversation.
        if self.session.turn_running:
            threading.Thread(target=self.session.restart, daemon=True).start()


# ── JSON-RPC over stdio (shared by ACP and Codex app-server) ──────────────────


class _Waiter:
    __slots__ = ("done", "error", "result")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: Any = None
        self.error: Any = None


class _JsonRpcDriver(Driver):
    """Request/response plumbing for line-delimited JSON-RPC on stdio."""

    jsonrpc_field: bool = True
    """ACP sends ``"jsonrpc": "2.0"``; Codex app-server omits it."""

    def __init__(self, session: MeetingSession) -> None:
        super().__init__(session)
        self._lock = threading.Lock()
        self._next_id = 0
        self._waiters: dict[int, _Waiter] = {}

    def next_id(self) -> int:
        with self._lock:
            self._next_id += 1
            return self._next_id

    def send(self, message: dict[str, Any]) -> None:
        if self.jsonrpc_field:
            message = {"jsonrpc": "2.0", **message}
        with self._lock:
            proc = self.session.proc
            if proc is None or proc.stdin is None:
                raise BrokenPipeError("the agent process is not running")
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None, timeout_s: float = 60.0) -> dict[str, Any]:
        msg_id = self.next_id()
        waiter = _Waiter()
        with self._lock:
            self._waiters[msg_id] = waiter
        self.send({"id": msg_id, "method": method, "params": params})
        if not waiter.done.wait(timeout_s):
            raise TimeoutError(f"{method} timed out")
        if waiter.error is not None:
            error = waiter.error
            raise RuntimeError(str(error.get("message") if isinstance(error, dict) else error))
        return waiter.result if isinstance(waiter.result, dict) else {}

    def resolve(self, message: dict[str, Any]) -> bool:
        """A response to one of our requests: wake its waiter."""
        with self._lock:
            waiter = self._waiters.pop(message.get("id"), None)  # type: ignore[arg-type]
        if waiter is None:
            return False
        waiter.result, waiter.error = message.get("result"), message.get("error")
        waiter.done.set()
        return True

    def reply_unsupported(self, message: dict[str, Any]) -> None:
        self.send({"id": message["id"], "error": {"code": -32601, "message": "not supported by the bridge"}})

    @staticmethod
    def messages(proc: subprocess.Popen[str]) -> Any:
        """JSON objects from stdout, skipping anything else."""
        assert proc.stdout is not None  # noqa: S101 - spawned with stdout=PIPE
        for raw in proc.stdout:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if isinstance(message, dict):
                yield message


# ── ACP: Antigravity, Hermes, Copilot CLI, Muse Code, OpenClaw ────────────────


class AcpDriver(_JsonRpcDriver):
    """Agent Client Protocol (https://agentclientprotocol.com) over stdio."""

    def __init__(self, session: MeetingSession) -> None:
        super().__init__(session)
        self.acp_session_id: str | None = None
        self.session_info: dict[str, Any] = {}
        """What ``session/new`` returned (models, modes, config options)."""
        self._prompt_id: int | None = None

    def argv(self, resume: bool) -> list[str]:
        cfg = self.session.config
        extra = cfg.agent.ignore_rules_args if cfg.ignore_rules and cfg.agent.ignore_rules_warm else ()
        return [self.binary, *cfg.agent.acp_args, *extra]

    def mcp_server(self, capabilities: dict[str, Any]) -> dict[str, Any]:
        """The meeting tools as an MCP server the agent can reach; the secret never goes in argv."""
        session = self.session
        if (capabilities.get("mcpCapabilities") or {}).get("http"):
            return {
                "type": "http",
                "name": MCP_SERVER_NAME,
                "url": session.mcp_url,
                "headers": [{"name": "Authorization", "value": f"Bearer {session.mcp_secret}"}],
            }
        command = self_command()
        return {
            "name": MCP_SERVER_NAME,
            "command": command[0],
            "args": [*command[1:], "mcp-stdio", "--url", session.mcp_url],
            "env": [{"name": MCP_SECRET_ENV, "value": session.mcp_secret}],
        }

    def handshake(self) -> None:
        session = self.session
        try:
            init = self.request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                    "clientInfo": _CLIENT_INFO,
                },
            )
            offer_mcp = bool(session.tools) and session.config.agent.acp_mcp
            servers = [self.mcp_server(init.get("agentCapabilities") or {})] if offer_mcp else []
            created = self.request("session/new", {"cwd": str(session.config.workdir), "mcpServers": servers})
            self.acp_session_id = str(created.get("sessionId") or "")
            self.session_info = created
            advertised = {
                str(m.get("modelId"))
                for m in _as_dict(created.get("models")).get("availableModels") or []
                if isinstance(m, dict)
            }
            if session.model and (session.config.agent.acp_set_model or session.model in advertised):
                self._set_model(session.model)
            if session.config.effort:
                self._set_thought_level(created, session.config.effort)
            session.ready.set()
        except Exception as exc:
            self.acp_session_id = None
            session.emit("error", f"ACP start failed: {exc}")
            session.end_turn()

    def _set_thought_level(self, created: dict[str, Any], effort: str) -> None:
        """The effort as the session's ``thought_level`` option, when the agent offers one with that value."""
        for option in created.get("configOptions") or []:
            if not isinstance(option, dict) or option.get("category") != "thought_level":
                continue
            values = {str(o.get("value")) for o in option.get("options") or [] if isinstance(o, dict)}
            if effort not in values:
                self.session.log(
                    f"effort {effort!r} is not offered here ({', '.join(sorted(values))}); kept its own"
                )
                return
            try:
                self.request(
                    "session/set_config_option",
                    {"sessionId": self.acp_session_id, "configId": option.get("id"), "value": effort},
                    15.0,
                )
            except (RuntimeError, TimeoutError) as exc:
                self.session.log(f"effort {effort!r} not set ({exc}); the agent's own level answers")
            return

    def _set_model(self, model: str) -> None:
        """Ask for MIA's model; an agent that cannot switch keeps its own and the meeting goes on."""
        try:
            self.request("session/set_model", {"sessionId": self.acp_session_id, "modelId": model}, 15.0)
        except (RuntimeError, TimeoutError) as exc:
            self.session.log(f"model {model!r} not set ({exc}); the agent's own model answers")

    def read(self, proc: subprocess.Popen[str]) -> None:
        session = self.session
        for message in self.messages(proc):
            method, msg_id = message.get("method"), message.get("id")
            session.debug("acp <- " + acp_summary(message))
            if method is None and msg_id is not None:  # a response to us
                if msg_id == self._prompt_id:
                    self._prompt_id = None
                    if "error" in message:
                        error = message["error"] or {}
                        session.emit("error", str(error.get("message") if isinstance(error, dict) else error))
                    else:
                        session.emit("rpc_done", str((message.get("result") or {}).get("stopReason") or ""))
                    session.end_turn()
                else:
                    self.resolve(message)
            elif method == "session/update":
                update = (message.get("params") or {}).get("update") or {}
                content = update.get("content") or {}
                is_text = (
                    update.get("sessionUpdate") == "agent_message_chunk" and content.get("type") == "text"
                )
                if is_text and content.get("text"):
                    session.emit("delta", str(content["text"]))
            elif method is not None and msg_id is not None:  # the agent asks us something
                threading.Thread(target=self._answer, args=(message,), daemon=True).start()
        self._finish(proc)

    def _answer(self, message: dict[str, Any]) -> None:
        """Every request from the agent gets an answer: an unanswered one stalls the turn for good."""
        try:
            if message.get("method") != "session/request_permission":
                self.session.debug(f"acp -> error: {message.get('method')} is not supported")
                self.reply_unsupported(message)
                return
            params = _as_dict(message.get("params"))
            allowed = self.permission_decision(_as_dict(params.get("toolCall")))
            options = [o for o in params.get("options") or [] if isinstance(o, dict)]
            choice = _permission_option(options, allowed)
            outcome = (
                {"outcome": "selected", "optionId": choice["optionId"]}
                if choice
                else {"outcome": "cancelled"}
            )
            self.session.debug(
                f"acp -> permission {'allowed' if allowed else 'denied'} "
                f"({self.session.access}): {outcome.get('optionId') or 'cancelled'}"
            )
            self.send({"id": message["id"], "result": {"outcome": outcome}})
        except Exception as exc:  # a malformed request must still be answered
            self.session.log(f"could not answer the agent's {message.get('method')}: {exc}")
            with contextlib.suppress(Exception):
                self.send({"id": message["id"], "error": {"code": -32603, "message": "the bridge failed"}})

    def permission_decision(self, tool_call: dict[str, Any]) -> bool:
        """Reads and meeting tools are fine; edits, deletes, moves and shell follow the access level."""
        session = self.session
        if session.cancelled:
            return False
        kind = str(tool_call.get("kind") or "other")
        title = str(tool_call.get("title") or "")
        if kind in _SAFE_ACP_KINDS or any(name and name in title for name in session.tool_names()):
            return True
        raw_input = json.dumps(tool_call.get("rawInput") or {})[:200]
        return session.allow_action(f"{kind}: {title} {raw_input}")

    def check_ready(self) -> str | None:
        if self.acp_session_id:
            return None
        return self.session.startup_error("ACP session not ready")

    def send_turn(self, text: str) -> None:
        session = self.session
        if session.started_turns == 0:  # ACP has no system-prompt field: the rules lead the first turn
            text = session.rules() + "\n\n" + text
        self._prompt_id = self.next_id()
        session.debug(f"acp -> session/prompt id={self._prompt_id}")
        self.send(
            {
                "id": self._prompt_id,
                "method": "session/prompt",
                "params": {"sessionId": self.acp_session_id, "prompt": [{"type": "text", "text": text}]},
            }
        )

    def cancel(self) -> None:
        if self.acp_session_id and self._prompt_id is not None:
            self.session.debug("acp -> session/cancel")
            with contextlib.suppress(BrokenPipeError, ValueError, OSError):  # gone: the turn ends anyway
                self.send({"method": "session/cancel", "params": {"sessionId": self.acp_session_id}})


def _permission_option(options: list[dict[str, Any]], allowed: bool) -> dict[str, Any] | None:
    """The option that says yes (or no): by ACP kind, then by a kind or name that starts that way."""
    exact = ("allow_once", "allow_always") if allowed else ("reject_once", "reject_always")
    prefixes = ("allow", "approve", "yes", "accept") if allowed else ("reject", "deny", "no", "decline")
    usable = [o for o in options if o.get("optionId")]
    for field, test in (("kind", exact), ("kind", prefixes), ("name", prefixes)):
        for option in usable:
            value = str(option.get(field) or "").lower()
            if (value in test) if test is exact else value.startswith(test):
                return option
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def acp_summary(message: dict[str, Any]) -> str:
    """One line about an ACP message, for ``--debug``: its kind, never the text people or the agent wrote."""
    method, msg_id = message.get("method"), message.get("id")
    params = _as_dict(message.get("params"))
    if method == "session/update":
        update = _as_dict(params.get("update"))
        kind = str(update.get("sessionUpdate") or "?")
        if kind.startswith("tool_call"):
            return (
                f"update {kind} {str(update.get('title') or '')[:80]} {update.get('status') or ''}".rstrip()
            )
        return f"update {kind}"
    if method is not None and msg_id is not None:
        call = _as_dict(params.get("toolCall"))
        what = f" {call.get('kind') or ''} {str(call.get('title') or '')[:80]}".rstrip() if call else ""
        return f"request {method} id={msg_id}{what}"
    if method is not None:
        return f"notification {method}"
    if "error" in message:
        error = _as_dict(message.get("error"))
        return f"response id={msg_id} error {str(error.get('message') or '')[:120]}"
    result = _as_dict(message.get("result"))
    stop = f" stopReason={result['stopReason']}" if "stopReason" in result else ""
    return f"response id={msg_id}{stop}"


# ── Codex app-server ──────────────────────────────────────────────────────────


class CodexDriver(_JsonRpcDriver):
    """``codex app-server``: thread/start once per meeting, turn/start per turn."""

    jsonrpc_field = False

    def __init__(self, session: MeetingSession) -> None:
        super().__init__(session)
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self._old_turns: set[str] = set()

    def argv(self, resume: bool) -> list[str]:
        argv = [self.binary, "app-server"]
        cfg = self.session.config
        if cfg.ignore_rules and cfg.agent.ignore_rules_warm:
            argv += cfg.agent.ignore_rules_args
        if self.session.tools:
            server = f"mcp_servers.{MCP_SERVER_NAME}"
            argv += [
                "-c",
                f"{server}.url={json.dumps(self.session.mcp_url)}",
                "-c",
                f'{server}.bearer_token_env_var="{MCP_SECRET_ENV}"',
                "-c",
                f'{server}.default_tools_approval_mode="approve"',
            ]
        return argv

    def handshake(self) -> None:
        session, cfg = self.session, self.session.config
        try:
            self.request("initialize", {"clientInfo": _CLIENT_INFO, "capabilities": None})
            self.send({"method": "initialized"})
            params: dict[str, Any] = {
                "cwd": str(cfg.workdir),
                "approvalPolicy": "on-request" if session.access == "confirm" else "never",
                "sandbox": "workspace-write" if session.access == "full" else "read-only",
                "developerInstructions": session.rules(),
                "ephemeral": True,
                "config": {"model_reasoning_effort": cfg.effort} if cfg.effort else None,
            }
            if session.model:
                params["model"] = session.model
            started = self.request("thread/start", params)
            self.thread_id = str((started.get("thread") or {}).get("id") or "") or None
            session.ready.set()
        except Exception as exc:
            self.thread_id = None
            session.emit("error", f"Codex app-server start failed: {exc}")
            session.end_turn()

    def read(self, proc: subprocess.Popen[str]) -> None:
        session = self.session
        for message in self.messages(proc):
            method, msg_id = message.get("method"), message.get("id")
            params = message.get("params") or {}
            if method is None and msg_id is not None:
                self.resolve(message)
            elif method in ("item/agentMessage/delta", "turn/completed") and self._is_stale(params):
                continue
            elif method == "item/agentMessage/delta":
                if params.get("delta"):
                    session.emit("delta", str(params["delta"]))
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                status = turn.get("status")
                self.turn_id = None
                if status == "failed":
                    session.emit("error", self._error_text(turn.get("error") or {}))
                else:
                    session.emit("rpc_done", "cancelled" if status == "interrupted" else "end_turn")
                session.end_turn()
            elif method == "error":
                error = params.get("error") or {}
                if not params.get("willRetry") and error.get("codexErrorInfo") == "unauthorized":
                    session.emit("error", self._error_text(error))
                    session.end_turn()
            elif method is not None and msg_id is not None:
                threading.Thread(target=self._answer, args=(message,), daemon=True).start()
        self._finish(proc)

    def _is_stale(self, params: dict[str, Any]) -> bool:
        """An event from an older turn (such as the interrupted turn's completion)."""
        turn_id = params.get("turnId") or (params.get("turn") or {}).get("id")
        return bool(turn_id) and turn_id in self._old_turns

    @staticmethod
    def _error_text(error: dict[str, Any]) -> str:
        text = str(error.get("message") or "Codex turn failed")
        if error.get("codexErrorInfo") == "unauthorized":
            text = "unauthorized: Codex login required. " + text
        return text

    def _answer(self, message: dict[str, Any]) -> None:
        method, params = message.get("method"), message.get("params") or {}
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            what = str(params.get("command") or params.get("reason") or method)[:200]
            label = "shell" if "command" in str(method) else "file change"
            decision = "accept" if self.session.allow_action(f"{label}: {what}") else "decline"
            self.send({"id": message["id"], "result": {"decision": decision}})
        elif method in ("execCommandApproval", "applyPatchApproval"):
            decision = "approved" if self.session.allow_action(str(method)) else "denied"
            self.send({"id": message["id"], "result": {"decision": decision}})
        elif method == "mcpServer/elicitation/request":
            ours = params.get("serverName") == MCP_SERVER_NAME
            self.send(
                {
                    "id": message["id"],
                    "result": {
                        "action": "accept" if ours else "decline",
                        "content": {} if ours else None,
                        "_meta": None,
                    },
                }
            )
        else:
            self.reply_unsupported(message)

    def check_ready(self) -> str | None:
        if self.thread_id:
            return None
        return self.session.startup_error("Codex thread not ready")

    def send_turn(self, text: str) -> None:
        started = self.request(
            "turn/start",
            {"threadId": self.thread_id, "input": [{"type": "text", "text": text, "text_elements": []}]},
            timeout_s=30,
        )
        self.turn_id = str((started.get("turn") or {}).get("id") or "") or None

    def cancel(self) -> None:
        if not (self.thread_id and self.turn_id):
            return
        self._old_turns.add(self.turn_id)
        with contextlib.suppress(BrokenPipeError, ValueError, OSError):  # gone: the turn ends anyway
            self.send(
                {
                    "id": self.next_id(),
                    "method": "turn/interrupt",
                    "params": {"threadId": self.thread_id, "turnId": self.turn_id},
                }
            )
