"""MS-2366: relay mode — one outbound connection to MeetStream, no tunnel.

The bridge still runs on ``127.0.0.1`` exactly as in tunnel mode (the local
agent's MCP endpoint needs it), but nothing outside this computer can reach
it. Instead this client keeps one WebSocket to the MeetStream relay, signed in
with the MeetStream login (``meetstream-agent-kit login``). MIA's calls arrive
as ``req`` frames and are replayed against the local bridge with a
per-process secret, so the bridge's handlers and answers are the same in both
modes. ``/v1/turn`` streams back as ``res_head`` + ``chunk``… + ``end``.

Token handling: the access token (15 min) lives in memory and is renewed in
band at 80 % of its life; every rotated refresh token is saved before the new
access token is used. Frames: ``src/containers/agent_relay/PROTOCOL.md`` in the
MeetStream repo (summary in ``docs/protocol.md``).
"""

from __future__ import annotations

import contextlib
import http.client
import json
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from meetstream_agent_kit import __version__
from meetstream_agent_kit.credentials import Credentials, CredentialStore
from meetstream_agent_kit.oauth_client import (
    OAuthError,
    SignInRequiredError,
    Tokens,
    device_name,
    platform_name,
)
from meetstream_agent_kit.oauth_client import refresh as oauth_refresh
from meetstream_agent_kit.ws_client import ClosedError, HandshakeRejectedError, WebSocket, WebSocketError

SUBPROTOCOL: Final = "meetstream.relay.v1"
PING_S: Final = 30.0
SILENCE_S: Final = 75.0
_BACKOFF_BASE_S: Final = 1.0
_BACKOFF_CAP_S: Final = 60.0
_ALLOWED: Final = {
    ("GET", "/v1/health"),
    ("POST", "/v1/session/start"),
    ("POST", "/v1/turn"),
    ("POST", "/v1/tool_result"),
    ("POST", "/v1/cancel"),
    ("POST", "/v1/claim"),
    ("POST", "/v1/release"),
    ("POST", "/v1/session/end"),
}
_STOP_CODES: Final = {4403}  # revoked: never reconnect on our own


class StopRelayError(Exception):
    """Relay mode cannot continue without the user (sign in again)."""


@dataclass
class RelayStatus:
    connected: bool = False
    since: float = 0.0
    last_error: str = ""
    connects: int = 0
    events: list[str] = field(default_factory=list)


class RelayClient:
    def __init__(
        self,
        store: CredentialStore,
        creds: Credentials,
        bridge_port: int,
        internal_token: str,
        hello: dict[str, Any],
        *,
        say: Callable[[str], None] = print,
        alert: Callable[[str], None] | None = None,
        refresh: Callable[[str, str, str], Tokens] = oauth_refresh,
    ) -> None:
        self.store = store
        self.creds = creds
        self.port = bridge_port
        self.internal_token = internal_token
        self.hello = hello
        self.say = say
        self.alert = alert
        self._refresh = refresh
        self.tokens: Tokens | None = None
        self.status = RelayStatus()
        self._stop = threading.Event()
        self._ws: WebSocket | None = None
        self._streams: dict[int, dict[str, Any]] = {}
        self._streams_lock = threading.Lock()

    # Tokens -------------------------------------------------------------------

    def access_token(self, *, force: bool = False) -> str:
        if not force and self.tokens is not None and self.tokens.lifetime_left > 120:
            return self.tokens.access_token
        tokens = self._refresh(self.creds.auth_url, self.creds.refresh_token, self.creds.device_id)
        # Save the rotated refresh token first: the old one is dead from now on.
        self.creds.refresh_token = tokens.refresh_token
        if tokens.device_id:
            self.creds.device_id = tokens.device_id
        self.store.save(self.creds)
        self.tokens = tokens
        return tokens.access_token

    # Run loop -----------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            ws.close(1001, "going away")

    def run_forever(self) -> None:
        """Connect, serve, reconnect with backoff. Raises StopRelayError when the user must act."""
        attempt = 0
        while not self._stop.is_set():
            try:
                self._run_once()
                attempt = 0
            except StopRelayError:
                raise
            except SignInRequiredError as exc:
                raise StopRelayError(
                    f"MeetStream sign-in is no longer valid ({exc}). Run: meetstream-agent-kit login"
                ) from exc
            except HandshakeRejectedError as exc:
                self.status.last_error = str(exc)
                if exc.status == 401:
                    self.tokens = None  # next try refreshes
                elif exc.status == 403:
                    raise StopRelayError(
                        "The relay refused this computer. Run: meetstream-agent-kit login"
                    ) from exc
            except ClosedError as exc:
                if exc.code in _STOP_CODES:
                    raise StopRelayError(
                        "This computer was disconnected from MeetStream (dashboard or a security "
                        "check). Run: meetstream-agent-kit login"
                    ) from exc
                if exc.code == 4401:
                    self.tokens = None
                self.status.last_error = str(exc)
            except (WebSocketError, OAuthError, OSError) as exc:
                self.status.last_error = str(exc)
            if self.status.connected:
                self.status.connected = False
                self.say("[relay] disconnected; reconnecting")
            if self._stop.is_set():
                break
            attempt += 1
            delay = random.uniform(0, min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * 2 ** min(attempt, 6)))  # noqa: S311
            self._stop.wait(delay)

    def _run_once(self) -> None:
        token = self.access_token()
        ws = WebSocket.connect(
            self.creds.relay_url,
            {"Authorization": f"Bearer {token}", "X-Agent-Kit-Version": __version__},
            SUBPROTOCOL,
        )
        self._ws = ws
        try:
            self._send({"t": "hello", **self.hello})
            welcome = json.loads(ws.recv())
            if welcome.get("t") != "welcome":
                raise WebSocketError("the relay did not welcome this connection")
            self.status.connected, self.status.since = True, time.time()
            self.status.connects += 1
            self.say("[relay] Connected to MeetStream (no tunnel needed)")
            threading.Thread(target=self._heartbeat, args=(ws,), daemon=True, name="relay-heartbeat").start()
            while not self._stop.is_set():
                self._handle(json.loads(ws.recv()))
        finally:
            ws.close()
            self._abort_all()

    def _heartbeat(self, ws: WebSocket) -> None:
        """Ping every 30 s, renew the token in band at 80 % of its life, drop a silent link."""
        while not ws.closed and not self._stop.wait(5):
            try:
                if time.monotonic() - ws.last_rx > SILENCE_S:
                    ws.close(1001, "silent")
                    return
                if time.monotonic() - ws.last_rx > PING_S:
                    ws.ping()
                tokens = self.tokens
                if tokens is not None and tokens.lifetime_left < 0.2 * 900:
                    self._send({"t": "auth", "token": self.access_token(force=True)})
            except SignInRequiredError:
                ws.close(1000, "signed out")
                return
            except (OSError, WebSocketError, OAuthError):
                continue

    # Frames -------------------------------------------------------------------

    def _send(self, frame: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise WebSocketError("not connected")
        ws.send_text(json.dumps({"v": 1, **frame}, separators=(",", ":")))

    def _handle(self, frame: dict[str, Any]) -> None:
        kind = frame.get("t")
        if kind == "req":
            threading.Thread(target=self._serve_request, args=(frame,), daemon=True, name="relay-req").start()
        elif kind == "abort":
            self._abort(int(frame.get("s") or 0))
        elif kind == "drain":
            self.say("[relay] MeetStream asked to reconnect (maintenance)")
            if self._ws is not None:
                self._ws.close(1001, "drain")
        elif kind == "notice":
            message = str(frame.get("message") or frame.get("kind") or "")
            self.say(f"[relay] {message}")
            if self.alert is not None and frame.get("kind") in ("revoked", "replaced"):
                self.alert(message)
        elif kind == "auth_err":
            self.tokens = None
        # welcome, auth_ok, agents: nothing to do

    def _serve_request(self, frame: dict[str, Any]) -> None:
        stream = int(frame.get("s") or 0)
        method = str(frame.get("method") or "POST").upper()
        path = str(frame.get("path") or "")
        raw_body = frame.get("body")
        body: dict[str, Any] = raw_body if isinstance(raw_body, dict) else {}
        if (method, path) not in _ALLOWED:
            self._reply(stream, 404, {"error": "not found"})
            return
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=None)
        with self._streams_lock:
            self._streams[stream] = {"conn": conn, "session_id": str(body.get("session_id") or "")}
        try:
            data = json.dumps(body).encode("utf-8") if method == "POST" else None
            headers = {"Authorization": f"Bearer {self.internal_token}"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            streaming = response.getheader("Content-Type", "").startswith("application/x-ndjson")
            if not streaming:
                raw = response.read()
                try:
                    payload = json.loads(raw or b"{}")
                except ValueError:
                    payload = {"error": "bad answer from the bridge"}
                self._reply(stream, response.status, payload if isinstance(payload, dict) else {})
                return
            self._send({"t": "res_head", "s": stream, "status": response.status})
            while True:
                line = response.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                self._send({"t": "chunk", "s": stream, "event": event})
            self._send({"t": "end", "s": stream})
        except (OSError, http.client.HTTPException, WebSocketError):
            pass  # aborted, or the relay link dropped: MIA sees a cut stream
        finally:
            with self._streams_lock:
                self._streams.pop(stream, None)
            conn.close()

    def _reply(self, stream: int, status: int, body: dict[str, Any]) -> None:
        with contextlib.suppress(OSError, WebSocketError):
            self._send({"t": "res", "s": stream, "status": status, "body": body})

    def _abort(self, stream: int) -> None:
        with self._streams_lock:
            info = self._streams.pop(stream, None)
        if info is None:
            return
        with contextlib.suppress(OSError):
            info["conn"].close()  # the bridge sees the hang-up and stops the agent's turn
        if info.get("session_id"):
            threading.Thread(target=self._local_cancel, args=(info["session_id"],), daemon=True).start()

    def _local_cancel(self, session_id: str) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(
                "POST",
                "/v1/cancel",
                body=json.dumps({"session_id": session_id}).encode(),
                headers={
                    "Authorization": f"Bearer {self.internal_token}",
                    "Content-Type": "application/json",
                },
            )
            conn.getresponse().read()
        except (OSError, http.client.HTTPException):
            pass
        finally:
            conn.close()

    def _abort_all(self) -> None:
        with self._streams_lock:
            streams = list(self._streams)
        for stream in streams:
            self._abort(stream)


def hello_frame(agent_name: str, agent_label: str, login_state: str, max_access: str) -> dict[str, Any]:
    return {
        "kit_version": __version__,
        "device_name": device_name(),
        "platform": platform_name(),
        "agents": [{"id": agent_name, "label": agent_label, "login": login_state}],
        "max_access": max_access,
        "caps": ["bridge.v1"],
    }
