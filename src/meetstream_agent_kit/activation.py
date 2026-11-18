"""Activation code: the agent answers nobody until its owner proves who they are.

When MIA asks for it, the bridge shows a one-time code on this machine. MIA
answers nobody until someone pastes that code in the meeting chat; that
participant is then the only owner for the meeting. MeetStream never sees the
code before it is pasted, so it cannot claim the agent for anyone.

* A wrong code 3 times, or 5 minutes, shows a new code.
* After 12 wrong codes the meeting stays locked.
* When the owner leaves, the agent locks again and a new code is shown.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from typing import Final, Literal, NamedTuple

from meetstream_agent_kit.console import notify, say

CODE_DIGITS: Final = 6
CODE_TTL_S: Final = 300.0
TRIES_PER_CODE: Final = 3
MAX_FAILS: Final = 12

ActivationState = Literal["pending", "claimed", "locked"]


class ClaimResult(NamedTuple):
    ok: bool
    reason: str
    """``ok``, ``wrong_code``, ``expired``, ``locked``, ``already_claimed`` or ``no_participant_id``."""


class ActivationGate:
    """The activation code for one meeting."""

    def __init__(
        self,
        agent_label: str,
        session_id: str,
        show: Callable[[str, str], None] = notify,
        log: Callable[[str], None] = say,
    ) -> None:
        self.agent_label = agent_label
        self.session_id = session_id
        self._show = show
        self._log = log
        self._lock = threading.Lock()
        self.owner_id: str | None = None
        self.owner_name = ""
        self.fails = 0
        self._code = ""
        self._expires = 0.0
        self._tries = 0
        self._new_code()

    @property
    def state(self) -> ActivationState:
        if self.owner_id:
            return "claimed"
        return "locked" if self.fails >= MAX_FAILS else "pending"

    def claim(self, code: str, participant_id: str, name: str) -> ClaimResult:
        with self._lock:
            if self.owner_id:
                return ClaimResult(False, "already_claimed")
            if self.fails >= MAX_FAILS:
                return ClaimResult(False, "locked")
            if not participant_id:
                return ClaimResult(False, "no_participant_id")
            if time.monotonic() > self._expires:
                self._new_code()
                return ClaimResult(False, "expired")
            if self._code and hmac.compare_digest(code.strip(), self._code):
                self.owner_id, self.owner_name = participant_id, name[:80]
                self._code = ""
                self._note(f"activated for {self.owner_name or 'the owner'}")
                return ClaimResult(True, "ok")
            self.fails += 1
            self._tries += 1
            if self.fails >= MAX_FAILS:
                self._code = ""
                self._note("too many wrong codes: locked for this meeting")
                return ClaimResult(False, "locked")
            if self._tries >= TRIES_PER_CODE:
                self._new_code()
            return ClaimResult(False, "wrong_code")

    def release(self, participant_ids: Iterable[str]) -> bool:
        """The owner left the meeting: lock again and show a new code."""
        with self._lock:
            if not self.owner_id or self.owner_id not in set(participant_ids):
                return False
            self._note(f"{self.owner_name or 'the owner'} left: locked")
            self.owner_id, self.owner_name = None, ""
            if self.fails < MAX_FAILS:
                self._new_code()
            return True

    def _new_code(self) -> None:
        self._code = f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"
        self._expires = time.monotonic() + CODE_TTL_S
        self._tries = 0
        self._show(
            "MIA activation code",
            f"{self._code}: paste it in the meeting chat to let {self.agent_label} answer you (valid 5 min)",
        )

    def _note(self, text: str) -> None:
        self._log(f"[activate] {self.session_id}: {text}")
