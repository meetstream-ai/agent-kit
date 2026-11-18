"""Is the agent CLI installed and signed in?

Checked when pairing, when ``serve`` starts, and every time a meeting starts,
with each CLI's own side-effect-free status command. When the answer turns bad
the user gets one desktop notification with the exact command to run.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from meetstream_agent_kit.agents import AgentSpec
from meetstream_agent_kit.console import notify as desktop_notify
from meetstream_agent_kit.constants import LOGIN_CACHE_S
from meetstream_agent_kit.parsers import looks_like_auth_error

LoginState = Literal["ok", "expired", "unknown", "not_installed"]
Notify = Callable[[str, str], None]


@dataclass(frozen=True)
class LoginStatus:
    state: LoginState
    detail: str = ""

    @property
    def usable(self) -> bool:
        """False when MIA cannot use the agent until the user acts."""
        return self.state in ("ok", "unknown")


class LoginChecker:
    """Cached login check for one agent; notifies once when it turns bad."""

    def __init__(self, agent: AgentSpec, binary: str, notify: Notify = desktop_notify) -> None:
        self.agent = agent
        self.binary = binary
        self._notify = notify
        self._cached: tuple[float, LoginStatus] | None = None
        self._lock = threading.Lock()
        self._notified = False

    def check(self, *, force: bool = False) -> LoginStatus:
        with self._lock:
            now = time.monotonic()
            if not force and self._cached and now - self._cached[0] < LOGIN_CACHE_S:
                return self._cached[1]
            status = self._run()
            self._cached = (now, status)
        if not status.usable:
            self._tell(status)
        elif status.state == "ok":
            self._notified = False
        return status

    def mark_expired(self, detail: str = "") -> None:
        """A turn failed with a sign-in error: remember it and tell the user."""
        status = LoginStatus("expired", detail)
        with self._lock:
            self._cached = (time.monotonic(), status)
        self._tell(status)

    def _tell(self, status: LoginStatus) -> None:
        if self._notified:
            return
        self._notified = True
        if status.state == "not_installed":
            self._notify(f"{self.agent.label} is not installed", self.agent.install_hint)
        else:
            self._notify(
                f"{self.agent.label} needs sign-in",
                f"MIA cannot use your agent. Run: {self.agent.relogin or 'your CLI login'}",
            )

    def _run(self) -> LoginStatus:
        argv = self.agent.login_check
        if not argv:
            return LoginStatus("unknown", "this CLI has no login check; the first turn will tell")
        try:
            proc = subprocess.run(
                [self.binary, *argv], capture_output=True, text=True, timeout=15, check=False
            )
        except FileNotFoundError:
            return LoginStatus("not_installed", f"{self.binary} was not found")
        except PermissionError:
            return LoginStatus("not_installed", f"{self.binary} is not executable")
        except subprocess.TimeoutExpired:
            return LoginStatus("unknown", "login check timed out")
        output = f"{proc.stdout}\n{proc.stderr}".strip()
        auth_error = looks_like_auth_error(output)
        if proc.returncode not in self.agent.login_ok_codes or (proc.returncode != 0 and auth_error):
            return LoginStatus("expired", output[:200])
        if proc.returncode == 0 and auth_error and "loggedIn" not in output:
            return LoginStatus("expired", output[:200])
        return LoginStatus("ok")
