"""MS-2366: where the MeetStream sign-in is kept on this computer.

Only the refresh token, the device id and the two server addresses are stored;
the short-lived access token stays in memory. Backends, in order:

- macOS Keychain through ``security -i``: the commands go to its stdin, so the
  secret never shows in ``ps`` (argv).
- Linux Secret Service through ``secret-tool`` (the secret on stdin), when it
  is installed and works.
- ``<home>/credentials.json``, mode 0600 in a 0700 directory, written
  atomically (the same rules as ``pairings.json``).

``MEETSTREAM_AGENT_KIT_CREDENTIALS=file`` forces the file (tests, headless).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

from meetstream_agent_kit.pairing import default_home

SERVICE: Final = "ai.meetstream.agent-kit"
ACCOUNT: Final = "default"
_ENV: Final = "MEETSTREAM_AGENT_KIT_CREDENTIALS"
_TIMEOUT_S: Final = 10


class CredentialError(Exception):
    """The credential store cannot be read or written."""


@dataclass
class Credentials:
    device_id: str
    refresh_token: str
    auth_url: str
    relay_url: str
    signed_in_at: str = ""


def _quote(value: str) -> str:
    """Quote for ``security -i`` (it splits on spaces and honours double quotes)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class CredentialStore:
    def __init__(self, home: Path | None = None, backend: str = "") -> None:
        self.home = home if home is not None else default_home()
        self.backend = backend or self._pick()

    @staticmethod
    def _pick() -> str:
        forced = os.environ.get(_ENV, "").strip().lower()
        if forced in ("file", "keychain", "secret-tool"):
            return forced
        if sys.platform == "darwin" and shutil.which("security"):
            return "keychain"
        if sys.platform.startswith("linux") and shutil.which("secret-tool"):
            return "secret-tool"
        return "file"

    @property
    def where(self) -> str:
        return {"keychain": "macOS Keychain", "secret-tool": "Secret Service"}.get(
            self.backend, str(self.home / "credentials.json")
        )

    # Public API ---------------------------------------------------------------

    def load(self) -> Credentials | None:
        raw = self._read()
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return Credentials(**{k: str(data.get(k) or "") for k in Credentials.__dataclass_fields__})
        except (ValueError, TypeError):
            return None

    def save(self, creds: Credentials) -> None:
        self._write(json.dumps(asdict(creds), separators=(",", ":")))

    def delete(self) -> None:
        if self.backend == "keychain":
            self._security(f"delete-generic-password -s {SERVICE} -a {ACCOUNT}\n", check=False)
        elif self.backend == "secret-tool":
            subprocess.run(
                ["secret-tool", "clear", "service", SERVICE, "account", ACCOUNT],
                capture_output=True,
                timeout=_TIMEOUT_S,
                check=False,
            )
        (self.home / "credentials.json").unlink(missing_ok=True)

    # Backends -----------------------------------------------------------------

    def _security(self, commands: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["security", "-i"],
            input=commands,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
        )
        if check and result.returncode != 0:
            raise CredentialError("could not write to the macOS Keychain")
        return result

    def _read(self) -> str:
        if self.backend == "keychain":
            result = subprocess.run(
                ["security", "find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_S,
                check=False,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        if self.backend == "secret-tool":
            result = subprocess.run(
                ["secret-tool", "lookup", "service", SERVICE, "account", ACCOUNT],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_S,
                check=False,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        path = self.home / "credentials.json"
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        except OSError as exc:
            raise CredentialError(f"cannot read {path}: {exc.strerror}") from exc

    def _write(self, secret: str) -> None:
        if self.backend == "keychain":
            # -U updates an existing item. The secret travels on stdin only.
            self._security(
                f"add-generic-password -U -s {SERVICE} -a {ACCOUNT} -l {_quote('MeetStream Agent Kit')} "
                f"-w {_quote(secret)}\n"
            )
            return
        if self.backend == "secret-tool":
            result = subprocess.run(
                [
                    "secret-tool",
                    "store",
                    "--label=MeetStream Agent Kit",
                    "service",
                    SERVICE,
                    "account",
                    ACCOUNT,
                ],
                input=secret,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_S,
                check=False,
            )
            if result.returncode != 0:
                raise CredentialError("could not write to the Secret Service")
            return
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.home.chmod(0o700)
        path = self.home / "credentials.json"
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(secret)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
