"""Pre-flight checks for ``serve`` and ``doctor``, each with the fix to show the user.

They catch what otherwise fails later, in the middle of a meeting: a busy port,
a second bridge already running, a missing CLI, a work folder that is not there,
a config folder that cannot be written.
"""

from __future__ import annotations

import errno
import json
import os
import shlex
import shutil
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from meetstream_agent_kit.agents import AgentSpec
from meetstream_agent_kit.parsers import looks_like_auth_error


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    message: str
    fix: str = ""


def check_python() -> Check:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.platform == "win32":
        return Check("python", False, "Windows is not supported yet", "Run it in WSL, or on macOS or Linux.")
    return Check("python", True, f"Python {version}")


def resolve_binary(agent: AgentSpec, override: str | None = None) -> str | None:
    """Full path of the agent CLI, or None when it is not installed."""
    candidate = override or agent.binary
    if os.sep in candidate:
        path = Path(candidate).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(candidate)


def check_binary(agent: AgentSpec, override: str | None = None) -> Check:
    found = resolve_binary(agent, override)
    if found:
        return Check("agent", True, f"{agent.label}: {found}")
    shown = override or agent.binary
    return Check("agent", False, f"{agent.label} not found ({shown})", agent.install_hint)


def check_workdir(path: Path) -> Check:
    if not path.exists():
        return Check("workdir", False, f"{path} does not exist", "Pass an existing folder with --workdir.")
    if not path.is_dir():
        return Check("workdir", False, f"{path} is not a folder", "Pass a folder with --workdir.")
    if not os.access(path, os.R_OK | os.X_OK):
        return Check("workdir", False, f"{path} cannot be read", "Pass a folder you can read with --workdir.")
    return Check("workdir", True, str(path))


def check_home(home: Path) -> Check:
    """The config folder exists, or can be created, and is writable."""
    probe = home if home.exists() else next((p for p in home.parents if p.exists()), Path("/"))
    if os.access(probe, os.W_OK | os.X_OK):
        return Check("config", True, str(home))
    return Check(
        "config",
        False,
        f"{home} cannot be written",
        "Fix the folder's permissions, or set MEETSTREAM_AGENT_KIT_HOME to a folder you own.",
    )


# ── Port and the "already running" lock ───────────────────────────────────────


def lock_path(home: Path, port: int) -> Path:
    return home / "run" / f"serve-{port}.json"


def write_lock(home: Path, port: int) -> Path:
    path = lock_path(home, port)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pid": os.getpid(), "port": port, "started_at": time.time()}), encoding="utf-8"
    )
    return path


def _running_bridge(home: Path, port: int) -> dict[str, Any] | None:
    """The bridge recorded as serving on ``port``, if its process is still alive."""
    try:
        info = json.loads(lock_path(home, port).read_text(encoding="utf-8"))
        pid = int(info["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass  # alive, owned by someone else
    return {"pid": pid}


def port_in_use_message(home: Path, port: int) -> Check:
    running = _running_bridge(home, port)
    if running:
        return Check(
            "port",
            False,
            f"the bridge is already running on port {port} (pid {running['pid']})",
            "Use that one, stop it first, or start this one with another --port.",
        )
    return Check(
        "port",
        False,
        f"port {port} is used by another program",
        f"Start the bridge with another --port (and point your tunnel at it), or free port {port}.",
    )


def check_port(home: Path, port: int) -> Check:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return port_in_use_message(home, port)
            return Check("port", False, f"cannot listen on 127.0.0.1:{port}: {exc.strerror}")
    return Check("port", True, f"127.0.0.1:{port} is free")


# ── Why a test turn failed ────────────────────────────────────────────────────

TurnProblem = Literal["not_installed", "signed_out", "timeout", "failed", "empty"]


@dataclass(frozen=True)
class TurnDiagnosis:
    kind: TurnProblem
    message: str
    fix: str
    detail: str = ""
    """What the CLI itself said (error text or its last stderr lines)."""


def login_argv(agent: AgentSpec) -> list[str] | None:
    """The sign-in command as argv, when it can be run as is (no placeholders)."""
    text = agent.relogin.split("(")[0].strip()
    if not text or "<" in text:
        return None
    return shlex.split(text)


def diagnose_turn(
    agent: AgentSpec, answer: str, error: str | None, stderr_tail: str = "", timeout_s: float = 0
) -> TurnDiagnosis | None:
    """None when the agent answered; otherwise the most likely reason and its fix."""
    if answer.strip() and not error:
        return None
    said = (error or "").strip()
    tail = stderr_tail.strip()
    detail = "\n".join([said] * bool(said) + [tail] * bool(tail and tail not in said))
    sign_in = f"Sign in with: {agent.relogin}" if agent.relogin else "Sign in to the CLI, then try again."
    if said.startswith("agent CLI not found"):
        return TurnDiagnosis("not_installed", f"{agent.label} is not installed", agent.install_hint)
    if looks_like_auth_error(f"{said}\n{tail}"):
        return TurnDiagnosis("signed_out", f"{agent.label} is signed out", sign_in, detail)
    if "timed out" in said:
        limit = f" within {int(timeout_s)} s" if timeout_s else ""
        return TurnDiagnosis(
            "timeout",
            f"{agent.label} did not answer{limit}",
            f"It may be waiting for a sign-in or a permission prompt. Run `{agent.binary}` on its own once "
            "to see what it asks for, then try again.",
            tail,
        )
    if said:
        return TurnDiagnosis("failed", f"{agent.label} failed", sign_in, detail)
    return TurnDiagnosis(
        "empty",
        f"{agent.label} ran but sent back no answer",
        f"Often a sign-in it is waiting for. {sign_in}"
        + ("" if tail else f" If it still says nothing, run `{agent.binary}` on its own once."),
        tail,
    )
