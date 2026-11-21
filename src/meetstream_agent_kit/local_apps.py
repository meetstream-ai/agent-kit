"""Show an app running on the owner's computer in the meeting: ``share_local_app``.

MIA's ``screen_share`` opens a URL on the meeting bot's own machine, so
``http://localhost:3000`` there is not the owner's app. The bridge serves two
meeting tools of its own: ``share_local_app`` opens a quick tunnel to a local
port in the background and returns its public https address (which the agent
then gives to ``screen_share``), and ``stop_local_app`` closes it. Only at Full
or Ask-first access (Ask first asks the owner here), at most two per meeting,
all closed when the meeting ends.
"""

from __future__ import annotations

import shutil
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Final

from meetstream_agent_kit.console import say
from meetstream_agent_kit.tunnel import Tunnel, TunnelError, TunnelSpec

SHARE_TOOL: Final = "share_local_app"
STOP_TOOL: Final = "stop_local_app"
LOCAL_TOOLS: Final = frozenset({SHARE_TOOL, STOP_TOOL})
MAX_SHARES: Final = 2
START_TIMEOUT_S: Final = 30.0
"""cloudflared can take 15-25 s to register a quick tunnel (measured 2026-11-18)."""
REACH_TIMEOUT_S: Final = 15.0
"""Then how long the new address gets to reach the app."""

TOOL_DEFS: Final[list[dict[str, Any]]] = [
    {
        "name": SHARE_TOOL,
        "description": (
            "Make an app running on the owner's computer (localhost) viewable in the meeting. Opens a "
            "temporary public https address for the local port and returns it; then call screen_share "
            "with that url. screen_share alone cannot open localhost: that is the meeting bot's machine."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "port": {"type": "integer", "minimum": 1, "maximum": 65535, "description": "local port"},
                "path": {"type": "string", "description": "optional path, for example /dashboard"},
            },
            "required": ["port"],
        },
    },
    {
        "name": STOP_TOOL,
        "description": "Close the address share_local_app opened (one port, or all when no port is given).",
        "inputSchema": {"type": "object", "properties": {"port": {"type": "integer"}}},
    },
]


def port_open(port: int, timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def answers(url: str, timeout: float = 5.0) -> bool:
    """The tunnel reaches the app: any HTTP answer except the tunnel's own "origin down" pages."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - https from the tunnel
            return bool(response.status)
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code not in (502, 503, 504, 530)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _tunnel_kinds(which: Callable[[str], str | None]) -> list[str]:
    """cloudflared first (no account, runs beside the bridge's own ngrok), then ngrok."""
    return [kind for kind, program in (("cloudflare", "cloudflared"), ("ngrok", "ngrok")) if which(program)]


class LocalShares:
    """The tunnels one meeting opened. Thread-safe; ``close_all`` at meeting end."""

    def __init__(
        self,
        *,
        which: Callable[[str], str | None] = shutil.which,
        make_tunnel: Callable[[TunnelSpec, int], Tunnel] | None = None,
        is_open: Callable[[int], bool] = port_open,
        log: Callable[[str], None] = say,
    ) -> None:
        self._which = which
        self._make = make_tunnel or (lambda spec, port: Tunnel(spec, port, which=which, check=answers))
        self._is_open = is_open
        self._log = log
        self._lock = threading.Lock()
        self.open: dict[int, tuple[Tunnel, str]] = {}

    def share(self, port: int, path: str = "") -> tuple[str, bool]:
        """(text for the agent, is_error)."""
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            return "port must be a number from 1 to 65535", True
        suffix = "/" + path.lstrip("/") if path else ""
        with self._lock:
            if port in self.open:
                return f"{self.open[port][1]}{suffix} (already shared)", False
            if len(self.open) >= MAX_SHARES:
                return f"at most {MAX_SHARES} local apps per meeting; stop one with {STOP_TOOL} first", True
        if not self._is_open(port):
            return (
                f"nothing is listening on localhost:{port} on the owner's computer; start the app first",
                True,
            )
        kinds = _tunnel_kinds(self._which)
        if not kinds:
            return "no tunnel program on this computer (install cloudflared: brew install cloudflared)", True
        errors = []
        for kind in kinds:  # cloudflared first; ngrok when it fails (some networks block its edge)
            tunnel = self._make(TunnelSpec(kind), port)  # type: ignore[arg-type]
            tunnel.start_timeout_s = START_TIMEOUT_S
            tunnel.reach_timeout_s = REACH_TIMEOUT_S
            try:
                url = tunnel.start()
                break
            except TunnelError as exc:
                errors.append(f"{kind}: {exc}")
        else:
            return f"could not open an address for port {port}: {'; '.join(errors)}", True
        with self._lock:
            self.open[port] = (tunnel, url)
        self._log(f"[tunnel] {port} -> {url}")
        return (
            f"{url}{suffix} now shows localhost:{port}. Call screen_share with this url. "
            f"It stays open until the meeting ends or {STOP_TOOL}."
        ), False

    def stop(self, port: int | None = None) -> tuple[str, bool]:
        with self._lock:
            ports = [port] if port is not None else list(self.open)
            closed = [(p, self.open.pop(p)) for p in ports if p in self.open]
        for p, (tunnel, _url) in closed:
            tunnel.stop()
            self._log(f"[tunnel] {p} closed")
        if not closed:
            return "no local app was shared" + (
                f" on port {port}" if port is not None else ""
            ), port is not None
        return f"closed: {', '.join(str(p) for p, _ in closed)}", False

    def close_all(self) -> None:
        self.stop(None)
