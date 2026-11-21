"""Shared fixtures. Every test runs the bridge against ``fixtures/stub_agent.py``,
a stand-in agent CLI: no model, no login, no network beyond 127.0.0.1."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from meetstream_agent_kit.agents import AGENTS
from meetstream_agent_kit.config import BridgeConfig
from meetstream_agent_kit.login import LoginChecker
from meetstream_agent_kit.pairing import PairingStore
from meetstream_agent_kit.server import BridgeServer
from meetstream_agent_kit.session import MeetingSession

STUB = Path(__file__).parent / "fixtures" / "stub_agent.py"

TOOLS = [
    {
        "name": "send_chat_message",
        "description": "Post in the meeting chat",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
    }
]


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty bridge home per test; no desktop notifications."""
    path = tmp_path / "home"
    monkeypatch.setenv("MEETSTREAM_AGENT_KIT_HOME", str(path))
    monkeypatch.setenv("MEETSTREAM_AGENT_KIT_WORKSPACE", str(tmp_path / "MeetStream Agent"))

    def offline(_url: str, _timeout: float) -> bytes:
        raise OSError("tests never reach the network")

    monkeypatch.setattr("meetstream_agent_kit.shared_estimates._fetch", offline)
    monkeypatch.setenv("MIA_BRIDGE_HOME", str(tmp_path / "legacy"))
    monkeypatch.setattr("meetstream_agent_kit.console.shutil.which", lambda name: None)
    for name in (
        "STUB_AUTH_FAIL",
        "STUB_FIRST_S",
        "STUB_TOTAL_S",
        "STUB_STARTUP_S",
        "STUB_NO_TOOLS",
        "STUB_MCP_AT_START",
    ):
        monkeypatch.delenv(name, raising=False)
    STUB.chmod(0o755)
    return path


def make_config(
    workdir: Path,
    agent: str = "claude",
    max_access: str = "readonly",
    confirm: Callable[[str], bool] | None = None,
) -> BridgeConfig:
    return BridgeConfig(
        agent=AGENTS[agent],
        binary=str(STUB),
        workdir=workdir,
        max_access=max_access,  # type: ignore[arg-type]
        owner="Nav",
        timeout_s=20,
        prime=False,
        quiet=True,
        confirm=confirm or (lambda action: False),
    )


def make_session(workdir: Path, agent: str = "claude", **kwargs: Any) -> MeetingSession:
    access = kwargs.pop("access", "readonly")
    confirm = kwargs.pop("confirm", None)
    config = make_config(workdir, agent, max_access=access, confirm=confirm)
    return MeetingSession("bot1", config, access=access, **kwargs)


def run(session: MeetingSession, text: str) -> list[dict[str, Any]]:
    from meetstream_agent_kit.prompts import format_turn

    return list(session.run_turn(format_turn([{"speaker": "Nav", "text": text}])))


class Bridge:
    """A running bridge on a free port, with a pairing token."""

    def __init__(self, server: BridgeServer, token: str) -> None:
        self.server = server
        self.token = token

    @property
    def sessions(self) -> dict[str, MeetingSession]:
        return self.server.sessions

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.port}{path}"

    def post(
        self, path: str, body: dict[str, Any], token: str | None = "", stream: bool = False
    ) -> tuple[int, Any]:
        token = self.token if token == "" else token
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            self.url(path), data=json.dumps(body).encode(), method="POST", headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode()
                if stream:
                    return response.status, [json.loads(line) for line in raw.splitlines() if line.strip()]
                return response.status, json.loads(raw or "{}")
        except urllib.error.HTTPError as err:
            with err:
                return err.code, json.loads(err.read() or b"{}")

    def get(self, path: str, token: str | None = "") -> tuple[int, Any]:
        token = self.token if token == "" else token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        request = urllib.request.Request(self.url(path), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as err:
            with err:
                return err.code, json.loads(err.read() or b"{}")


@pytest.fixture
def start_bridge(tmp_path: Path) -> Iterator[Callable[..., Bridge]]:
    started: list[BridgeServer] = []

    def start(max_access: str = "readonly", agent: str = "claude", binary: str | None = None) -> Bridge:
        config = make_config(tmp_path, agent, max_access=max_access)
        if binary is not None:
            config = BridgeConfig(**{**config.__dict__, "binary": binary})
        _, token = PairingStore().create("test")
        login = LoginChecker(config.agent, config.binary, notify=lambda title, text: None)
        server = BridgeServer(("127.0.0.1", 0), config, login=login)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return Bridge(server, token)

    yield start
    for server in started:
        server.close_all()
        server.shutdown()
        server.server_close()


@pytest.fixture
def bridge(start_bridge: Callable[..., Bridge]) -> Bridge:
    return start_bridge()
