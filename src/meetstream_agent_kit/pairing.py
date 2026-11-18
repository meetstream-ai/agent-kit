"""Pairing tokens: how MIA proves it may use this bridge.

``meetstream-agent-kit pair`` creates a token and prints it once. Only its SHA-256 hash is
stored (``~/.config/meetstream-agent-kit/pairings.json``, mode 0600, in a 0700
directory). The user saves the token in the MeetStream dashboard; MIA sends it
as ``Authorization: Bearer <token>``. Revoking a pairing makes its token fail at
once, because every request reads the file again.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from meetstream_agent_kit.constants import HOME_ENV

TOKEN_PREFIX: Final = "msak_"  # noqa: S105 - a public prefix, not a secret
_LEGACY_HOME_ENV: Final = "MIA_BRIDGE_HOME"
_LEGACY_DIR_NAME: Final = "meetstream-agent-bridge"


class PairingError(Exception):
    """The pairings file cannot be read or written."""


@dataclass(frozen=True)
class Pairing:
    id: str
    label: str
    created_at: str


def default_home() -> Path:
    """``$MEETSTREAM_AGENT_KIT_HOME``, else ``~/.config/meetstream-agent-kit``."""
    override = os.environ.get(HOME_ENV)
    return Path(override) if override else Path.home() / ".config" / "meetstream-agent-kit"


def _legacy_home() -> Path:
    override = os.environ.get(_LEGACY_HOME_ENV)
    return Path(override) if override else Path.home() / ".config" / _LEGACY_DIR_NAME


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class PairingStore:
    """The pairings file. Reads it fresh on every call."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = home if home is not None else default_home()

    @property
    def path(self) -> Path:
        return self.home / "pairings.json"

    def _read(self) -> dict[str, dict[str, Any]]:
        path = self.path
        if not path.exists():
            # Pairings made before the rename (pre-release bridge) keep working;
            # they move to the new file on the next write.
            legacy = _legacy_home() / "pairings.json"
            if self.home == default_home() and legacy.exists():
                path = legacy
            else:
                return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PairingError(f"cannot read {path}: {exc.strerror}") from exc
        except ValueError as exc:
            raise PairingError(f"{path} is not valid JSON; fix it or delete it and pair again") from exc
        if not isinstance(data, dict):
            raise PairingError(f"{path} has an unexpected format; delete it and pair again")
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}

    def _write(self, pairings: dict[str, dict[str, Any]]) -> None:
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            self.home.chmod(0o700)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(pairings, handle, indent=2)
            tmp.replace(self.path)
        except OSError as exc:
            raise PairingError(f"cannot write {self.path}: {exc.strerror}") from exc

    def create(self, label: str) -> tuple[Pairing, str]:
        """Create a pairing. Returns it and its token; the token is not stored."""
        pairing_id = secrets.token_hex(4)
        token = f"{TOKEN_PREFIX}{pairing_id}_{secrets.token_urlsafe(32)}"
        pairing = Pairing(pairing_id, label[:80], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        pairings = self._read()
        pairings[pairing_id] = {
            "label": pairing.label,
            "sha256": hash_token(token),
            "created_at": pairing.created_at,
        }
        self._write(pairings)
        return pairing, token

    def revoke(self, pairing_id: str) -> bool:
        pairings = self._read()
        if pairings.pop(pairing_id, None) is None:
            return False
        self._write(pairings)
        return True

    def list(self) -> list[Pairing]:
        return [
            Pairing(pid, str(entry.get("label") or ""), str(entry.get("created_at") or "?"))
            for pid, entry in sorted(self._read().items())
        ]

    def verify(self, authorization: str | None) -> str | None:
        """The pairing id an ``Authorization: Bearer`` header belongs to, or None.

        Compares every stored hash in constant time, so timing does not reveal
        which pairing came close.
        """
        if not authorization or not authorization.startswith("Bearer "):
            return None
        digest = hash_token(authorization[len("Bearer ") :].strip())
        match = None
        for pairing_id, entry in self._read().items():
            if hmac.compare_digest(digest, str(entry.get("sha256") or "")):
                match = pairing_id
        return match
