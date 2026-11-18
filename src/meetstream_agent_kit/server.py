"""The bridge's HTTP server: what MIA calls (``/v1/*``) and what the agent calls (``/mcp/*``).

It listens on 127.0.0.1 only. The user's tunnel (cloudflared, ngrok, Tailscale
Funnel) makes it reachable for MIA. Every ``/v1`` call needs a pairing token;
``/mcp/<session>`` needs that meeting's own secret, which only the local agent
process gets. Request lines and headers are never logged.

The wire protocol is documented in ``docs/protocol.md``.
"""

from __future__ import annotations

import hmac
import json
import re
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final

from meetstream_agent_kit import __version__
from meetstream_agent_kit.access import effective_access
from meetstream_agent_kit.activation import ActivationGate
from meetstream_agent_kit.config import BridgeConfig
from meetstream_agent_kit.console import say
from meetstream_agent_kit.constants import (
    APPROVE_TOOL,
    MAX_ANSWER_CHARS,
    MAX_BODY_BYTES,
    MAX_TOOLS,
    MCP_SERVER_NAME,
    TIMEOUT_REPLY,
)
from meetstream_agent_kit.login import LoginChecker
from meetstream_agent_kit.pairing import PairingError, PairingStore
from meetstream_agent_kit.prompts import format_turn
from meetstream_agent_kit.session import MeetingSession

_SESSION_ID_RE: Final = re.compile(r"[A-Za-z0-9_\-]{1,80}")
_TOOL_NAME_RE: Final = re.compile(r"[A-Za-z0-9_\-]{1,64}")
_PRIME_TURN: Final = (
    "<meeting_turns>\n[system]: The meeting is starting. Reply with the single word: ready\n</meeting_turns>"
)

Body = dict[str, Any]


class BridgeServer(ThreadingHTTPServer):
    """Holds the bridge's state: config, pairings, login checks and live meetings."""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        config: BridgeConfig,
        pairings: PairingStore | None = None,
        login: LoginChecker | None = None,
    ) -> None:
        super().__init__(address, BridgeHandler)
        self.config = config
        self.pairings = pairings or PairingStore(config.runtime_dir)
        self.login = login or LoginChecker(config.agent, config.binary)
        self.sessions: dict[str, MeetingSession] = {}
        self.sessions_lock = threading.Lock()

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def start_session(
        self,
        session_id: str,
        *,
        instructions: str,
        tools: list[dict[str, Any]],
        model: str,
        access: Any,
        activation_required: bool,
    ) -> tuple[MeetingSession, bool]:
        """Start (or reuse) a meeting. Returns ``(session, reused)``.

        The same session id with the same settings reuses the warm agent; any
        change (instructions, tools, model, access) restarts it.
        """
        new = MeetingSession(
            session_id,
            self.config,
            instructions=instructions,
            tools=tools,
            model=model or self.config.model,
            port=self.port,
            access=access,
            activation_required=activation_required,
        )
        with self.sessions_lock:
            old = self.sessions.get(session_id)
            if old is not None and old.config_key() == new.config_key():
                return old, True
            if activation_required:
                # The activation belongs to the meeting, not to one agent process:
                # a restart for a changed setup keeps the owner who already pasted the code.
                kept = old.activation if old is not None else None
                new.activation = kept or ActivationGate(self.config.agent.label, session_id)
            if old is not None and not self.config.quiet:
                changed = ", ".join(old.changed_fields(new)) or "setup"
                say(f"[session] {session_id}: restarting the agent: {changed} changed")
            self.sessions[session_id] = new
        if old is not None:
            old.close()
        new.start()
        return new, False

    def end_session(self, session_id: str) -> bool:
        with self.sessions_lock:
            session = self.sessions.pop(session_id, None)
        if session is None:
            return False
        session.close()
        return True

    def close_all(self) -> None:
        for session_id in list(self.sessions):
            self.end_session(session_id)


_IMAGE_RESULT_RE: Final = re.compile(r"MIA_IMAGE (image/(?:jpeg|png|webp)) ([A-Za-z0-9+/]+={0,2})")


def mcp_tool_content(output: str) -> list[dict[str, Any]]:
    """A meeting tool's result as MCP content.

    MIA's screen "look" tools return the screenshot itself
    (``MIA_IMAGE <mime> <base64>``) so the agent can see it.
    """
    match = _IMAGE_RESULT_RE.fullmatch(output or "")
    if match:
        return [
            {"type": "image", "data": match.group(2), "mimeType": match.group(1)},
            {"type": "text", "text": "Screenshot of the meeting screen."},
        ]
    return [{"type": "text", "text": output}]


def valid_tools(raw: Any) -> list[dict[str, Any]]:
    """MIA's meeting tools, with names and sizes checked."""
    tools = []
    for tool in raw if isinstance(raw, list) else []:
        if isinstance(tool, dict) and _TOOL_NAME_RE.fullmatch(str(tool.get("name") or "")):
            params = tool.get("parameters")
            tools.append(
                {
                    "name": tool["name"],
                    "description": str(tool.get("description") or "")[:1000],
                    "parameters": params if isinstance(params, dict) else None,
                }
            )
    return tools[:MAX_TOOLS]


class BridgeHandler(BaseHTTPRequestHandler):
    server: BridgeServer
    protocol_version = "HTTP/1.1"
    server_version = "meetstream-agent-kit"
    sys_version = ""  # do not advertise the Python version

    def log_message(self, format: str, *args: Any) -> None:
        """Never log request lines or headers (they can carry tokens)."""

    # Helpers ------------------------------------------------------------------

    def _json(self, status: int, body: Body | None, headers: dict[str, str] | None = None) -> None:
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        try:
            pairing = self.server.pairings.verify(self.headers.get("Authorization"))
        except PairingError as exc:
            say(f"[agent-kit] {exc}", err=True)
            pairing = None
        if pairing is None:
            self._json(401, {"error": "unauthorized"})
            return False
        return True

    def _read_body(self) -> Body | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(413, {"error": f"body must be 1..{MAX_BODY_BYTES} bytes"})
            return None
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            self._json(400, {"error": "invalid JSON"})
            return None
        if not isinstance(body, dict):
            self._json(400, {"error": "body must be an object"})
            return None
        return body

    def _session(self, body: Body) -> MeetingSession | None:
        session = self.server.sessions.get(str(body.get("session_id") or ""))
        if session is None:
            self._json(404, {"error": "unknown session; call /v1/session/start first"})
        return session

    def _login_payload(self, *, force: bool = False) -> Body:
        status = self.server.login.check(force=force)
        agent = self.server.config.agent
        payload: Body = {"login": status.state, "relogin": agent.relogin}
        if status.state == "not_installed":
            payload["install"] = agent.install_hint
        if status.state != "ok" and status.detail:
            payload["login_detail"] = status.detail[:200]
        return payload

    def _log(self, text: str) -> None:
        if not self.server.config.quiet:
            say(text)

    # Routes -------------------------------------------------------------------

    def do_GET(self) -> None:
        if self.path.startswith("/mcp/"):
            self._json(405, {"error": "use POST"}, {"Allow": "POST"})
            return
        if self.path != "/v1/health":
            self._json(404, {"error": "not found"})
            return
        if not self._authorized():
            return
        cfg = self.server.config
        self._json(
            200,
            {
                "ok": True,
                "agent": cfg.agent.name,
                "label": cfg.agent.label,
                "mode": cfg.max_access,  # older name of max_access, kept for compatibility
                "max_access": cfg.max_access,
                "warm": cfg.agent.warm is not None,
                "sessions": len(self.server.sessions),
                "version": __version__,
                **self._login_payload(),
            },
        )

    def do_POST(self) -> None:
        if self.path.startswith("/mcp/"):
            self._mcp()
            return
        routes: dict[str, Callable[[Body], None]] = {
            "/v1/session/start": self._session_start,
            "/v1/turn": self._turn,
            "/v1/tool_result": self._tool_result,
            "/v1/cancel": self._cancel,
            "/v1/session/end": self._session_end,
            "/v1/claim": self._claim,
            "/v1/release": self._release,
        }
        route = routes.get(self.path)
        if route is None:
            self._json(404, {"error": "not found"})
            return
        if not self._authorized():
            return
        body = self._read_body()
        if body is not None:
            route(body)

    def _session_start(self, body: Body) -> None:
        cfg = self.server.config
        session_id = str(body.get("session_id") or "")
        if not _SESSION_ID_RE.fullmatch(session_id):
            self._json(400, {"error": "session_id is required"})
            return
        login = self._login_payload(force=True)
        if login["login"] in ("expired", "not_installed"):
            self._json(200, {"agent": cfg.agent.name, "warm": False, **login})
            return
        activation_required = body.get("claim_required") is True
        # With the activation code only the person who pasted it can start
        # turns, and this bridge checks that itself.
        decision = effective_access(
            cfg.agent,
            cfg.max_access,
            str(body.get("access") or ""),
            body.get("owner_verified") is True or activation_required,
        )
        session, reused = self.server.start_session(
            session_id,
            instructions=str(body.get("instructions") or ""),
            tools=valid_tools(body.get("tools")),
            model=str(body.get("model") or "")[:80],
            access=decision.level,
            activation_required=activation_required,
        )
        self._log(
            f"[session] {session_id}: {'reused' if reused else 'started'} ({cfg.agent.label}, "
            f"{'warm' if session.warm else 'one run per turn'}, {len(session.tools)} meeting tools, "
            f"access {decision.level}" + (f": {decision.reason}" if decision.reason else "") + ")"
        )
        if session.warm and cfg.prime and not reused:
            threading.Thread(target=_prime, args=(session,), daemon=True).start()
        self._json(
            200,
            {
                "agent": cfg.agent.name,
                "warm": session.warm,
                "reused": reused,
                "spawn_ms": session.spawn_ms,
                "access": decision.level,
                "max_access": cfg.max_access,
                "access_reason": decision.reason,
                "claim": session.activation.state if session.activation else "off",
                **login,
            },
        )

    def _claim(self, body: Body) -> None:
        session = self._session(body)
        if session is None:
            return
        if session.activation is None:
            self._json(409, {"ok": False, "reason": "no_activation_code"})
            return
        result = session.activation.claim(
            str(body.get("code") or "")[:12],
            str(body.get("participant_id") or "")[:200],
            str(body.get("name") or ""),
        )
        self._json(200, {"ok": result.ok, "reason": result.reason, "claim": session.activation.state})

    def _release(self, body: Body) -> None:
        session = self._session(body)
        if session is None:
            return
        raw_ids = body.get("participant_ids")
        ids = [str(i)[:200] for i in (raw_ids if isinstance(raw_ids, list) else []) if i]
        released = bool(session.activation and session.activation.release(ids))
        self._json(
            200, {"released": released, "claim": session.activation.state if session.activation else "off"}
        )

    def _turn(self, body: Body) -> None:
        session = self._session(body)
        if session is None:
            return
        if session.activation is not None and session.activation.state != "claimed":
            self._json(403, {"error": "locked", "claim": session.activation.state})
            return
        raw = body.get("messages")
        messages = [m for m in (raw if isinstance(raw, list) else []) if isinstance(m, dict)]
        if not messages:
            self._json(400, {"error": "messages are required"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = messages[-1]
        self._log(f"[turn] {str(last.get('speaker') or '?')[:40]}: {str(last.get('text') or '')[:160]}")
        agent = self.server.config.agent
        try:
            for event in session.run_turn(format_turn(messages)):
                if event["type"] == "error" and event.get("code") == "auth_required":
                    self.server.login.mark_expired(str(event.get("message") or ""))
                    event["relogin"] = agent.relogin
                if event["type"] == "tool_call":
                    self._log(f"[tool] {event['name']}")
                elif event["type"] == "done":
                    first, total = event.get("first_output_ms"), event.get("total_ms")
                    if event.get("timed_out"):
                        self._log(f"[timeout] stopped after {total} ms; MIA says: {TIMEOUT_REPLY}")
                    else:
                        self._log(f"[done] first output {first} ms, total {total} ms")
                elif event["type"] == "error":  # never silent: every failed turn shows here
                    self._log(f"[error] {event.get('code')}: {str(event.get('message') or '')[:200]}")
                self._chunk(event)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            session.cancel()  # MIA hung up: stop the agent too

    def _chunk(self, event: Body) -> None:
        line = (json.dumps(event) + "\n").encode("utf-8")
        self.wfile.write(f"{len(line):X}\r\n".encode("ascii") + line + b"\r\n")
        self.wfile.flush()

    def _tool_result(self, body: Body) -> None:
        session = self._session(body)
        if session is None:
            return
        output = str(body.get("output") or "")
        if not _IMAGE_RESULT_RE.fullmatch(output):  # screenshots pass whole; text is capped
            output = output[:MAX_ANSWER_CHARS]
        ok = session.resolve_tool(str(body.get("call_id") or ""), output, bool(body.get("is_error")))
        self._json(200 if ok else 404, {"ok": ok})

    def _cancel(self, body: Body) -> None:
        session = self._session(body)
        if session is None:
            return
        session.cancel()
        self._json(200, {"ok": True})

    def _session_end(self, body: Body) -> None:
        self._json(200, {"ok": self.server.end_session(str(body.get("session_id") or ""))})

    # MCP (Streamable HTTP, JSON responses), for the local agent only -----------

    def _mcp(self) -> None:
        session = self.server.sessions.get(self.path[len("/mcp/") :].strip("/"))
        authorization = self.headers.get("Authorization") or ""
        if session is None or not hmac.compare_digest(authorization, f"Bearer {session.mcp_secret}"):
            self._json(401, {"error": "unauthorized"})
            return
        body = self._read_body()
        if body is None:
            return
        method, msg_id, params = body.get("method"), body.get("id"), body.get("params") or {}
        if msg_id is None:  # a notification: nothing to answer
            self._json(202, None)
            return
        result: Body
        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion") or "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": f"meetstream-{MCP_SERVER_NAME}", "version": __version__},
            }
        elif method == "tools/list":
            result = {"tools": session.mcp_tools()}
            session.mcp_tools_listed = len(result["tools"])
        elif method == "ping":
            result = {}
        elif method == "tools/call":
            name = str(params.get("name") or "")
            raw_args = params.get("arguments")
            args: Body = raw_args if isinstance(raw_args, dict) else {}
            if name == APPROVE_TOOL and session.access == "confirm":
                result = self._approve(args)
            elif name in session.tool_names():
                output, is_error = session.call_tool(name, args)
                result = {"content": mcp_tool_content(output), "isError": is_error}
            else:
                result = {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
        else:
            self._json(
                200,
                {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "method not found"}},
            )
            return
        self._json(200, {"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _approve(self, args: Body) -> Body:
        """Claude Code asks before acting (``confirm`` access): ask the owner here."""
        tool = str(args.get("tool_name") or "?")
        tool_input = args.get("input") if isinstance(args.get("input"), dict) else {}
        allowed = self.server.config.confirm(f"{tool} {json.dumps(tool_input)[:240]}")
        decision = (
            {"behavior": "allow", "updatedInput": tool_input}
            if allowed
            else {"behavior": "deny", "message": "The owner did not approve this action."}
        )
        return {"content": [{"type": "text", "text": json.dumps(decision)}]}


def _prime(session: MeetingSession) -> None:
    """One tiny turn so the agent's prompt cache is warm before anyone speaks."""
    for _ in session.run_turn(_PRIME_TURN, warmup=True):
        pass
