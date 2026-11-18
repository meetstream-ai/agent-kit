"""Public https address for the bridge: a Cloudflare quick tunnel, ngrok, or one you already have.

MeetStream reaches the bridge over https, so the bridge on 127.0.0.1 needs a
tunnel. ``Tunnel.start`` runs the tunnel program, reads the address it prints,
and checks that the address really reaches this bridge before handing it back.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from meetstream_agent_kit.constants import STOP_GRACE_S
from meetstream_agent_kit.oneshot import close_stream

TunnelKind = Literal["cloudflare", "ngrok", "url", "none"]
TUNNEL_KINDS: Final[tuple[TunnelKind, ...]] = ("cloudflare", "ngrok", "url", "none")

START_TIMEOUT_S: Final = 40.0
"""How long to wait for the tunnel program to print its address."""
REACH_TIMEOUT_S: Final = 60.0
"""How long to keep trying the public address (new quick-tunnel DNS can lag a few seconds)."""

_CLOUDFLARE_URL: Final = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_CLOUDFLARE_READY: Final = "Registered tunnel connection"
"""cloudflared prints the address before Cloudflare can route it; probing earlier can cache a DNS miss."""

_NGROK_ERRORS: Final[dict[str, str]] = {
    "ERR_NGROK_4018": (
        "ngrok needs a free account on this computer. Sign up at https://dashboard.ngrok.com/signup, "
        "then run: ngrok config add-authtoken <your token>"
    ),
    "ERR_NGROK_108": (
        "ngrok is already running on this computer (a free account allows one at a time). "
        "Stop the other ngrok, or pick the Cloudflare quick tunnel."
    ),
    "ERR_NGROK_334": (
        "That ngrok domain is already in use by another ngrok. Stop it, or leave the domain blank."
    ),
    "ERR_NGROK_320": "That ngrok domain is not on your ngrok account. Check it at https://dashboard.ngrok.com/domains",
}


class TunnelError(Exception):
    """The tunnel could not start or its address does not reach the bridge. The message says how to fix it."""


@dataclass(frozen=True)
class TunnelSpec:
    kind: TunnelKind
    url: str = ""
    """``url``: the address the user already has. ``ngrok``: optional static domain."""


def install_hint(program: str) -> str:
    if program == "cloudflared":
        if platform.system() == "Darwin":
            return "Install it with: brew install cloudflared"
        return "Install it: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
    if platform.system() == "Darwin":
        return "Install it with: brew install ngrok"
    return "Install it: https://ngrok.com/download"


def make_spec(kind: str, url: str = "") -> TunnelSpec:
    """A checked TunnelSpec from user input."""
    for known in TUNNEL_KINDS:
        if kind == known:
            if known == "url" and not url.startswith("https://"):
                raise TunnelError("An address of your own must start with https://")
            return TunnelSpec(known, url)
    raise TunnelError(f"unknown tunnel {kind!r}; choose one of: {', '.join(TUNNEL_KINDS)}")


def program_for(kind: str) -> str | None:
    return {"cloudflare": "cloudflared", "ngrok": "ngrok"}.get(kind)


def find_cloudflare_url(line: str) -> str | None:
    match = _CLOUDFLARE_URL.search(line)
    return match.group(0) if match else None


def parse_ngrok_line(line: str) -> tuple[str | None, str | None]:
    """Return (public url, error message) from one ngrok ``--log-format json`` line."""
    try:
        event = json.loads(line)
    except ValueError:
        return None, None
    if not isinstance(event, dict):
        return None, None
    url = event.get("url")
    if isinstance(url, str) and url.startswith("https://"):
        return url, None
    err = event.get("err")
    if isinstance(err, str) and err and err != "<nil>":
        for code, message in _NGROK_ERRORS.items():
            if code in err:
                return None, message
        return None, f"ngrok: {err.splitlines()[0][:300]}"
    return None, None


_DOH_URL: Final = "https://1.1.1.1/dns-query?type=A&name="
DOH_FALLBACK_WAIT_S: Final = 15.0
"""Without DNS-over-HTTPS, wait this long before the first local lookup."""


def dns_published(url: str, timeout: float = 5.0) -> bool | None:
    """Ask public DNS-over-HTTPS whether the tunnel's host exists yet.

    New tunnel names take a few seconds to appear. Asking the computer's own
    resolver too early makes it cache "no such host" (macOS keeps that for a
    while), so every later check fails. Public DNS-over-HTTPS has no such
    local cache. Returns None when it cannot be asked.
    """
    host = urllib.parse.urlsplit(url).hostname or ""
    request = urllib.request.Request(  # noqa: S310 - fixed https host
        _DOH_URL + host, headers={"accept": "application/dns-json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https host
            answer = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return bool(isinstance(answer, dict) and answer.get("Status") == 0 and answer.get("Answer"))


def reaches_bridge(url: str, timeout: float = 5.0) -> bool:
    """True when ``url`` answers like this bridge: /v1/health without a token is 401."""
    request = urllib.request.Request(url.rstrip("/") + "/v1/health", method="GET")  # noqa: S310 - https only
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return bool(response.status == 200)
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code == 401
    except (urllib.error.URLError, OSError, ValueError):
        return False


class Tunnel:
    """Runs one tunnel program for the bridge's port and knows its public address."""

    def __init__(
        self,
        spec: TunnelSpec,
        port: int,
        *,
        which: Callable[[str], str | None] = shutil.which,
        check: Callable[[str], bool] = reaches_bridge,
        published: Callable[[str], bool | None] = dns_published,
    ) -> None:
        self.spec = spec
        self.port = port
        self.url = ""
        self._which = which
        self._check = check
        self._published = published
        self._proc: subprocess.Popen[str] | None = None
        self.start_timeout_s = START_TIMEOUT_S

    def argv(self, binary: str) -> list[str]:
        local = f"http://127.0.0.1:{self.port}"
        if self.spec.kind == "cloudflare":
            return [binary, "tunnel", "--no-autoupdate", "--url", local]
        argv = [binary, "http", local, "--log", "stdout", "--log-format", "json"]
        if self.spec.url:
            argv += ["--url", self.spec.url]
        return argv

    def start(self) -> str:
        kind = self.spec.kind
        if kind == "none":
            return ""
        if kind == "url":
            self.url = self.spec.url.rstrip("/")
        else:
            program = program_for(kind) or ""
            binary = self._which(program)
            if binary is None:
                raise TunnelError(f"{program} is not installed. {install_hint(program)}")
            self.url = self._run(binary)
        self._wait_reachable()
        return self.url

    def _run(self, binary: str) -> str:
        stream_is_stderr = self.spec.kind == "cloudflare"
        proc = subprocess.Popen(
            self.argv(binary),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL if stream_is_stderr else subprocess.PIPE,
            stderr=subprocess.PIPE if stream_is_stderr else subprocess.DEVNULL,
            text=True,
        )
        self._proc = proc
        stream = proc.stderr if stream_is_stderr else proc.stdout
        assert stream is not None  # noqa: S101 - piped above
        found: dict[str, str] = {}
        done = threading.Event()

        def read() -> None:
            for line in stream:
                if "url" not in found and "error" not in found:
                    if stream_is_stderr:
                        url = find_cloudflare_url(line)
                        if url:
                            found["pending"] = url
                        if "pending" in found and _CLOUDFLARE_READY in line:
                            found["url"] = found["pending"]
                            done.set()
                    else:
                        url, error = parse_ngrok_line(line)
                        if url:
                            found["url"] = url
                            done.set()
                        elif error:
                            found["error"] = error
                            done.set()
            done.set()  # the program exited

        threading.Thread(target=read, daemon=True, name="tunnel-log").start()
        done.wait(self.start_timeout_s)
        if "url" in found:
            return found["url"]
        self.stop()
        if "error" in found:
            raise TunnelError(found["error"])
        name = "cloudflared" if stream_is_stderr else "ngrok"
        raise TunnelError(
            f"{name} did not report an address within {self.start_timeout_s:.0f} s; "
            "run it by hand to see why."
        )

    def _wait_published(self, deadline: float) -> None:
        """Hold the first local lookup until public DNS has the name (see dns_published)."""
        if self.spec.kind == "url":
            return
        started = time.monotonic()
        while time.monotonic() < deadline:
            state = self._published(self.url)
            if state:
                return
            if state is None:
                time.sleep(max(0.0, DOH_FALLBACK_WAIT_S - (time.monotonic() - started)))
                return
            time.sleep(1.0)

    def _wait_reachable(self) -> None:
        deadline = time.monotonic() + REACH_TIMEOUT_S
        self._wait_published(deadline)
        while time.monotonic() < deadline:
            if self._check(self.url):
                return
            time.sleep(1.0)
        self.stop()
        raise TunnelError(
            f"{self.url} does not reach this bridge on port {self.port}. "
            "Check that the address forwards to http://127.0.0.1:" + str(self.port) + "."
        )

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        # A tunnel stops quickly; wait for it here so it never outlives the bridge.
        proc.terminate()
        try:
            proc.wait(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        close_stream(proc.stdout)
        close_stream(proc.stderr)


# ── ngrok on this computer: installed? connected to an account? which domain? ──

NGROK_AUTHTOKEN_URL: Final = "https://dashboard.ngrok.com/get-started/your-authtoken"
NGROK_DOMAINS_URL: Final = "https://dashboard.ngrok.com/domains"
_NGROK_CONFIG_AT: Final = re.compile(r"configuration file at (.+?)\s*$", re.MULTILINE)
_YAML_KEY: Final = r"^\s*(?:-\s*)?{key}:\s*[\"']?([^\"'\s#]+)"
_NGROK_HOST_KEYS: Final = ("domain", "hostname", "url")
_PUBLIC_HOST: Final = re.compile(r"^(?:https?://)?([a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,})/?$", re.IGNORECASE)
_NGROK_RANDOM_HOST: Final = re.compile(r"^https://[0-9a-f]{4,}-\d+-\d+-\d+-\d+\.ngrok", re.IGNORECASE)
"""Old-style per-start addresses, e.g. https://67ed-203-0-113-5.ngrok-free.app."""

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class NgrokSetup:
    """What the wizard can learn about ngrok without asking."""

    binary: str | None
    config_path: str = ""
    connected: bool = False
    """An authtoken is saved in the config, or NGROK_AUTHTOKEN is set."""
    domains: tuple[str, ...] = ()
    """Domains named in the config's endpoints/tunnels, or reserved on the account (needs an API key)."""


def _default_ngrok_configs() -> list[Path]:
    home = Path.home()
    return [
        home / "Library" / "Application Support" / "ngrok" / "ngrok.yml",
        Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "ngrok" / "ngrok.yml",
        home / ".ngrok2" / "ngrok.yml",
    ]


def ngrok_config_path(binary: str, run: Runner = subprocess.run) -> Path | None:
    """The config file ngrok itself uses (``ngrok config check``), else the first default that exists."""
    try:
        proc = run([binary, "config", "check"], capture_output=True, text=True, timeout=10, check=False)
        match = _NGROK_CONFIG_AT.search(f"{proc.stdout}\n{proc.stderr}")
        if match:
            return Path(match.group(1).strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return next((p for p in _default_ngrok_configs() if p.is_file()), None)


def _yaml_values(text: str, key: str) -> list[str]:
    return re.findall(_YAML_KEY.format(key=key), text, re.MULTILINE)


def _config_domains(text: str) -> list[str]:
    found: list[str] = []
    for key in _NGROK_HOST_KEYS:
        for value in _yaml_values(text, key):
            match = _PUBLIC_HOST.match(value)
            host = match.group(1).lower() if match else ""
            if host and host not in found and not host.startswith(("localhost", "127.")):
                found.append(host)
    return found


def _reserved_domains(binary: str, run: Runner) -> list[str]:
    """Domains on the account, through the ngrok API. Works only when an API key is configured."""
    try:
        proc = run(
            [binary, "api", "reserved-domains", "list"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        data = json.loads(proc.stdout) if proc.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    items = data.get("reserved_domains") if isinstance(data, dict) else None
    return [str(i["domain"]) for i in items or [] if isinstance(i, dict) and i.get("domain")]


def read_ngrok_setup(
    which: Callable[[str], str | None] = shutil.which,
    run: Runner = subprocess.run,
    env: Mapping[str, str] = os.environ,
) -> NgrokSetup:
    binary = which("ngrok")
    if binary is None:
        return NgrokSetup(None)
    path = ngrok_config_path(binary, run)
    try:
        text = path.read_text(encoding="utf-8") if path else ""
    except OSError:
        text = ""
    connected = bool(env.get("NGROK_AUTHTOKEN") or _yaml_values(text, "authtoken"))
    domains = _config_domains(text)
    if connected and (env.get("NGROK_API_KEY") or _yaml_values(text, "api_key")):
        domains += [d for d in _reserved_domains(binary, run) if d not in domains]
    return NgrokSetup(binary, str(path or ""), connected, tuple(domains))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def probe_ngrok_address(binary: str, timeout_s: float = 20.0) -> tuple[str, str]:
    """Start ngrok once, read the address it is given, stop it. Returns (url, error message).

    It points at a free local port with nothing behind it, so nothing is exposed.
    """
    tun = Tunnel(TunnelSpec("ngrok"), _free_port(), which=lambda _p: binary)
    tun.start_timeout_s = timeout_s
    try:
        return tun._run(binary), ""
    except TunnelError as exc:
        return "", str(exc)
    finally:
        tun.stop()


def ngrok_address_is_random(url: str) -> bool:
    """True for the per-start addresses ngrok gives accounts without a dev domain."""
    return bool(_NGROK_RANDOM_HOST.match(url))
