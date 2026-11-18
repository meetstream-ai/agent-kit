"""Pre-flight checks for ``serve`` and ``doctor``, each with the fix to show the user.

They catch what otherwise fails later, in the middle of a meeting: a busy port,
a second bridge already running, a missing CLI, a work folder that is not there,
a config folder that cannot be written.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meetstream_agent_kit.agents import AgentSpec


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
