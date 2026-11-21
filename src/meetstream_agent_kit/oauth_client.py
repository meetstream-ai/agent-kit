"""MS-2366: sign in to MeetStream with MCP OAuth (authorization code + PKCE).

The browser opens MeetStream's sign-in page; MeetStream sends the code back to
a one-shot listener on ``127.0.0.1:<random port>`` (RFC 8252). The code is
exchanged for a 15-minute access token and a rotating refresh token bound to
this computer (``meetstream_device_id``). Tokens never go in URLs, argv or logs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import platform
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Final

from meetstream_agent_kit import __version__

CLIENT_ID: Final = "meetstream-agent-kit"
SCOPE: Final = "local_agent"
DEFAULT_AUTH_URL: Final = "https://mcp.meetstream.ai"
DEFAULT_RELAY_URL: Final = "wss://relay.meetstream.ai/v1/connect"
_TIMEOUT_S: Final = 15
_CALLBACK_WAIT_S: Final = 300
_LOOPBACK: Final = ("127.0.0.1", "localhost", "::1")


class OAuthError(Exception):
    """Sign-in or refresh failed (network, server or protocol)."""


class SignInRequiredError(OAuthError):
    """The refresh token is no longer valid: run ``meetstream-agent-kit login``."""


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float  # time.time() when the access token expires
    device_id: str

    @property
    def lifetime_left(self) -> float:
        return self.expires_at - time.time()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def check_url(url: str) -> str:
    """https, or http only for this computer (local testing)."""
    parts = urllib.parse.urlsplit(url.rstrip("/"))
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in _LOOPBACK):
        return url.rstrip("/")
    raise OAuthError(f"{url} must start with https://")


def _post(url: str, form: dict[str, str]) -> dict[str, Any]:
    data = urllib.parse.urlencode(form).encode("ascii")
    request = urllib.request.Request(  # noqa: S310 - checked scheme
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        with _OPENER.open(request, timeout=_TIMEOUT_S) as response:
            body = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except ValueError:
            body = {}
        error = str(body.get("error") or f"HTTP {exc.code}")
        desc = str(body.get("error_description") or "")
        if error == "invalid_grant":
            raise SignInRequiredError(desc or "your MeetStream sign-in expired") from None
        raise OAuthError(f"{error}: {desc}".rstrip(": ")) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OAuthError(f"cannot reach {urllib.parse.urlsplit(url).netloc}: {exc}") from None
    if not isinstance(body, dict):
        raise OAuthError("unexpected answer from the sign-in server")
    return body


def discover(auth_url: str) -> dict[str, Any]:
    url = check_url(auth_url) + "/.well-known/oauth-authorization-server"
    try:
        with _OPENER.open(url, timeout=_TIMEOUT_S) as response:
            meta = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OAuthError(f"cannot read MeetStream's sign-in settings: {exc}") from None
    if not isinstance(meta, dict) or SCOPE not in (meta.get("scopes_supported") or []):
        raise OAuthError("this MeetStream server does not offer sign-in for Agent Kit yet")
    return meta


def device_name() -> str:
    name = ""
    if sys.platform == "darwin":
        try:
            name = subprocess.run(
                ["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=5, check=False
            ).stdout.strip()
        except OSError:
            name = ""
    name = " ".join((name or socket.gethostname()).split())
    return name[:40] or "my computer"


def platform_name() -> str:
    return "macOS" if sys.platform == "darwin" else platform.system() or "unknown"


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class _Callback(BaseHTTPRequestHandler):
    result: dict[str, str] = {}  # noqa: RUF012 - one sign-in at a time

    def log_message(self, format: str, *args: Any) -> None:  # never log the code
        pass

    def do_GET(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        if parts.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        query = dict(urllib.parse.parse_qsl(parts.query))
        type(self).result = query
        ok = "code" in query
        page = (
            "<h2>Signed in to MeetStream.</h2><p>You can close this tab and go back to the terminal.</p>"
            if ok
            else "<h2>Sign-in did not finish.</h2><p>Go back to the terminal.</p>"
        )
        data = f"<!doctype html><meta charset=utf-8><title>MeetStream</title>{page}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _token_from(body: dict[str, Any], fallback_device: str = "") -> Tokens:
    access = str(body.get("access_token") or "")
    refresh = str(body.get("refresh_token") or "")
    if not access or not refresh:
        raise OAuthError("the sign-in server answered without tokens")
    return Tokens(
        access_token=access,
        refresh_token=refresh,
        expires_at=time.time() + float(body.get("expires_in") or 900),
        device_id=str(body.get("meetstream_device_id") or fallback_device),
    )


def login(auth_url: str, resource: str = "", *, say: Any = print, open_browser: bool = True) -> Tokens:
    """Browser sign-in. Returns the tokens; the caller stores the refresh token."""
    meta = discover(auth_url)
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    _Callback.result = {}
    server = HTTPServer(("127.0.0.1", 0), _Callback)
    server.timeout = 1
    redirect = f"http://127.0.0.1:{server.server_address[1]}/callback"
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect,
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "device_name": device_name(),
        "platform": platform_name(),
        "kit_version": __version__,
    }
    if resource:
        params["resource"] = resource
    url = str(meta["authorization_endpoint"]) + "?" + urllib.parse.urlencode(params)
    say("Opening your browser to sign in to MeetStream. If it does not open, visit:")
    say(f"  {url}")
    if open_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    deadline = time.monotonic() + _CALLBACK_WAIT_S
    try:
        while not _Callback.result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    result = _Callback.result
    if not result:
        raise OAuthError("sign-in timed out after 5 minutes")
    if not secrets.compare_digest(result.get("state", ""), state):
        raise OAuthError("sign-in answer did not match this request (state); try again")
    if "code" not in result:
        raise OAuthError(result.get("error_description") or result.get("error") or "sign-in was cancelled")
    body = _post(
        str(meta["token_endpoint"]),
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": result["code"],
            "redirect_uri": redirect,
            "code_verifier": verifier,
        },
    )
    return _token_from(body)


def refresh(auth_url: str, refresh_token: str, device_id: str) -> Tokens:
    meta = discover(auth_url)
    body = _post(
        str(meta["token_endpoint"]),
        {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
        },
    )
    return _token_from(body, device_id)


def revoke(auth_url: str, token: str) -> None:
    try:
        meta = discover(auth_url)
        endpoint = str(meta.get("revocation_endpoint") or check_url(auth_url) + "/oauth/revoke")
        _post(endpoint, {"token": token, "client_id": CLIENT_ID})
    except OAuthError:
        pass  # signing out locally still happens
