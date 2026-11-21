"""share_local_app: show an app on the owner's computer in the meeting through a quick tunnel."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import pytest

from meetstream_agent_kit.local_apps import LOCAL_TOOLS, MAX_SHARES, SHARE_TOOL, STOP_TOOL, LocalShares
from meetstream_agent_kit.prompts import system_prompt
from meetstream_agent_kit.tunnel import Tunnel, TunnelSpec

from .conftest import TOOLS, Bridge, make_session


class FakeTunnel:
    started: ClassVar[list[int]] = []
    stopped: ClassVar[list[int]] = []

    def __init__(self, spec: TunnelSpec, port: int) -> None:
        self.spec, self.port = spec, port
        self.start_timeout_s = self.reach_timeout_s = 0.0

    def start(self) -> str:
        FakeTunnel.started.append(self.port)
        return f"https://app-{self.port}.trycloudflare.com"

    def stop(self) -> None:
        FakeTunnel.stopped.append(self.port)


def _shares(logs: list[str], *, listening: bool = True) -> LocalShares:
    FakeTunnel.started, FakeTunnel.stopped = [], []
    return LocalShares(
        which=lambda p: f"/usr/bin/{p}" if p == "cloudflared" else None,
        make_tunnel=lambda spec, port: FakeTunnel(spec, port),  # type: ignore[arg-type,return-value]
        is_open=lambda _port: listening,
        log=logs.append,
    )


def test_share_returns_a_public_url_and_logs_it() -> None:
    logs: list[str] = []
    shares = _shares(logs)
    text, is_error = shares.share(3000, "/dash")
    assert not is_error
    assert text.startswith("https://app-3000.trycloudflare.com/dash now shows localhost:3000")
    assert "screen_share" in text
    assert logs == ["[tunnel] 3000 -> https://app-3000.trycloudflare.com"]
    assert shares.share(3000)[0].endswith("(already shared)")  # no second tunnel


def test_limits_and_checks() -> None:
    shares = _shares([])
    for port in range(4000, 4000 + MAX_SHARES):
        assert not shares.share(port)[1]
    text, is_error = shares.share(5000)
    assert is_error
    assert f"at most {MAX_SHARES}" in text
    assert shares.share(0)[1]
    assert shares.share(70000)[1]
    closed = _shares([], listening=False).share(3000)
    assert closed[1]
    assert "nothing is listening on localhost:3000" in closed[0]
    none = LocalShares(which=lambda _p: None, is_open=lambda _p: True).share(3000)
    assert none[1]
    assert "cloudflared" in none[0]


def test_stop_one_or_all_and_close_at_meeting_end() -> None:
    shares = _shares([])
    shares.share(3000)
    shares.share(8080)
    assert shares.stop(3000) == ("closed: 3000", False)
    assert FakeTunnel.stopped == [3000]
    shares.close_all()
    assert FakeTunnel.stopped == [3000, 8080]
    assert shares.stop(1234)[1]


def test_tools_only_at_full_or_ask_first(tmp_path: Path) -> None:
    for access, offered in (("readonly", False), ("confirm", True), ("full", True)):
        session = make_session(tmp_path, "claude", access=access, tools=TOOLS)
        names = {t["name"] for t in session.mcp_tools()}
        assert (SHARE_TOOL in names) is offered
        assert (STOP_TOOL in names) is offered
        assert session.tool_names() == {"send_chat_message"}  # MIA's tools are unchanged
        if not offered:
            assert session.local_tool(SHARE_TOOL, {"port": 3000}) == (
                "not allowed at this access level",
                True,
            )


def test_ask_first_needs_the_owners_yes(tmp_path: Path) -> None:
    asked: list[str] = []
    session = make_session(tmp_path, "claude", access="confirm", confirm=lambda a: asked.append(a) or False)
    text, is_error = session.local_tool(SHARE_TOOL, {"port": 3000})
    assert is_error
    assert "did not allow" in text
    assert "localhost:3000" in asked[0]


def test_rules_tell_the_agent_how_to_show_local_apps() -> None:
    assert "share_local_app" in system_prompt("Nav", "", "full")
    assert "share_local_app" in system_prompt("Nav", "", "confirm")
    assert "share_local_app" not in system_prompt("Nav", "", "readonly")


def test_bridge_runs_local_tools_itself_not_at_mia(
    start_bridge: Callable[..., Bridge], monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = start_bridge(max_access="full")
    bridge.post(
        "/v1/session/start", {"session_id": "bot1", "tools": TOOLS, "access": "full", "owner_verified": True}
    )
    session = bridge.sessions["bot1"]
    logs: list[str] = []
    session.local_apps = _shares(logs)
    headers_secret = session.mcp_secret
    import urllib.request

    def mcp(method: str, params: dict[str, Any]) -> Any:
        req = urllib.request.Request(
            bridge.url("/mcp/bot1"),
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {headers_secret}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())["result"]

    listed = {t["name"] for t in mcp("tools/list", {})["tools"]}
    assert listed >= LOCAL_TOOLS
    result = mcp("tools/call", {"name": SHARE_TOOL, "arguments": {"port": 3000}})
    assert result["isError"] is False
    assert "https://app-3000.trycloudflare.com" in result["content"][0]["text"]
    assert session.pending_tool_calls() == []  # never sent to MIA
    bridge.post("/v1/session/end", {"session_id": "bot1"})
    assert FakeTunnel.stopped == [3000]  # closed with the meeting


@pytest.mark.skipif(sys.platform == "win32", reason="Unix only")
def test_a_real_tunnel_object_gets_the_short_timeouts() -> None:
    tunnel = Tunnel(TunnelSpec("cloudflare"), 3000)
    assert tunnel.reach_timeout_s == 60.0  # the bridge's own tunnel keeps its long wait


def test_ngrok_is_tried_when_cloudflared_cannot_reach_its_edge() -> None:
    """Seen 2026-11-18: cloudflared timed out on QUIC and DNS for its edge; ngrok worked."""
    from meetstream_agent_kit.tunnel import TunnelError

    tried: list[str] = []

    class Picky(FakeTunnel):
        def start(self) -> str:
            tried.append(self.spec.kind)
            if self.spec.kind == "cloudflare":
                raise TunnelError("cloudflared did not report an address within 30 s")
            return "https://abc.ngrok-free.app"

    shares = LocalShares(
        which=lambda p: f"/usr/bin/{p}",
        make_tunnel=lambda spec, port: Picky(spec, port),  # type: ignore[arg-type,return-value]
        is_open=lambda _p: True,
        log=lambda _t: None,
    )
    text, is_error = shares.share(3000)
    assert not is_error
    assert text.startswith("https://abc.ngrok-free.app")
    assert tried == ["cloudflare", "ngrok"]
