"""The bridge end to end: HTTP routes, warm sessions, drivers, meeting tools, activation."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from meetstream_agent_kit import activation
from meetstream_agent_kit.activation import ActivationGate
from meetstream_agent_kit.agents import custom_agent
from meetstream_agent_kit.config import BridgeConfig
from meetstream_agent_kit.drivers import AcpDriver, CodexDriver, StreamJsonDriver
from meetstream_agent_kit.session import MeetingSession

from .conftest import STUB, TOOLS, Bridge, make_session, run

TURN = {"session_id": "bot1", "messages": [{"speaker": "Nav", "text": "what is the answer?"}]}


def wait_for(condition: Callable[[], bool], timeout_s: float = 4.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.02)


# ── HTTP routes ───────────────────────────────────────────────────────────────


def test_turns_stream_on_one_warm_process(bridge: Bridge) -> None:
    status, body = bridge.post(
        "/v1/session/start", {"session_id": "bot1", "instructions": "Be brief.", "tools": TOOLS}
    )
    assert status == 200
    assert body["warm"] is True
    assert body["login"] == "ok"
    assert not body["reused"]
    proc = bridge.sessions["bot1"].proc
    assert proc is not None
    for _ in range(2):
        status, events = bridge.post("/v1/turn", TURN, stream=True)
        assert status == 200
        assert [e["type"] for e in events if e["type"] != "delta"] == ["done"]
        assert "".join(e["text"] for e in events if e["type"] == "delta").strip() == "The answer is 42."
        assert events[-1]["warm"] is True
        assert events[-1]["first_output_ms"] is not None
    assert bridge.sessions["bot1"].proc is proc  # same process: warm
    _, body = bridge.post(
        "/v1/session/start", {"session_id": "bot1", "instructions": "Be brief.", "tools": TOOLS}
    )
    assert body["reused"] is True


def test_meeting_tool_round_trip_through_mia(bridge: Bridge) -> None:
    bridge.post("/v1/session/start", {"session_id": "bot1", "tools": TOOLS})
    result: dict[str, Any] = {}
    turn = {"session_id": "bot1", "messages": [{"speaker": "Nav", "text": "TOOL:send_chat_message"}]}
    thread = threading.Thread(target=lambda: result.update(turn=bridge.post("/v1/turn", turn, stream=True)))
    thread.start()
    session = bridge.sessions["bot1"]
    wait_for(lambda: bool(session.pending_tool_calls()))
    call_id = session.pending_tool_calls()[0]
    status, body = bridge.post(
        "/v1/tool_result", {"session_id": "bot1", "call_id": call_id, "output": "posted"}
    )
    assert status == 200
    assert body["ok"]
    thread.join(10)
    _, events = result["turn"]
    calls = [e for e in events if e["type"] == "tool_call"]
    assert calls[0]["name"] == "send_chat_message"
    assert json.loads(calls[0]["arguments"]) == {"text": "hi"}
    assert events[-1] == {**events[-1], "type": "done", "text": "Tool said: posted"}


def test_mcp_endpoint_needs_the_meeting_secret(bridge: Bridge) -> None:
    bridge.post("/v1/session/start", {"session_id": "bot1", "tools": TOOLS})
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert bridge.post("/mcp/bot1", request)[0] == 401  # the pairing token is not the MCP secret
    status, body = bridge.post("/mcp/bot1", request, token=bridge.sessions["bot1"].mcp_secret)
    assert status == 200
    assert [t["name"] for t in body["result"]["tools"]] == ["send_chat_message"]


def test_cancel_stops_the_turn_and_keeps_the_session(bridge: Bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUB_FIRST_S", "5")
    bridge.post("/v1/session/start", {"session_id": "bot1"})
    result: dict[str, Any] = {}
    thread = threading.Thread(target=lambda: result.update(turn=bridge.post("/v1/turn", TURN, stream=True)))
    thread.start()
    time.sleep(0.5)
    t0 = time.monotonic()
    assert bridge.post("/v1/cancel", {"session_id": "bot1"})[0] == 200
    thread.join(5)
    assert time.monotonic() - t0 < 3
    assert result["turn"][1][-1] == {"type": "error", "code": "cancelled", "message": "cancelled"}
    assert "bot1" in bridge.sessions


def test_an_expired_login_is_reported_with_the_sign_in_command(
    bridge: Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_AUTH_FAIL", "1")
    status, body = bridge.post("/v1/session/start", {"session_id": "bot1"})
    assert status == 200
    assert body["login"] == "expired"
    assert body["relogin"] == "claude auth login"
    assert "bot1" not in bridge.sessions


def test_a_missing_cli_is_reported_with_the_install_command(start_bridge: Callable[..., Bridge]) -> None:
    bridge = start_bridge(binary="/nonexistent/claude")
    _, body = bridge.post("/v1/session/start", {"session_id": "bot1"})
    assert body["login"] == "not_installed"
    assert "npm install" in body["install"]
    assert "bot1" not in bridge.sessions
    _, health = bridge.get("/v1/health")
    assert health["login"] == "not_installed"


def test_sign_in_error_in_the_middle_of_a_meeting(bridge: Bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge.post("/v1/session/start", {"session_id": "bot1"})
    bridge.sessions["bot1"].close()
    monkeypatch.setenv("STUB_AUTH_FAIL", "1")  # the restarted process is signed out
    _, events = bridge.post("/v1/turn", {**TURN, "messages": [{"speaker": "Nav", "text": "hi"}]}, stream=True)
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "auth_required"
    assert events[-1]["relogin"] == "claude auth login"


def test_auth_and_input_limits(bridge: Bridge) -> None:
    assert bridge.post("/v1/session/start", {"session_id": "x"}, token=None)[0] == 401
    assert bridge.post("/v1/session/start", {"session_id": "x"}, token="msak_wrong")[0] == 401
    assert bridge.post("/v1/session/start", {"session_id": "../x"})[0] == 400
    assert bridge.post("/v1/turn", {"session_id": "nope", "messages": [{"text": "x"}]})[0] == 404
    assert bridge.post("/v1/nope", {})[0] == 404
    assert bridge.get("/v1/health", token=None)[0] == 401
    status, health = bridge.get("/v1/health")
    assert status == 200
    assert health["ok"]
    assert health["warm"]
    assert health["login"] == "ok"
    assert health["max_access"] == health["mode"] == "readonly"


def test_the_bridge_does_not_advertise_its_python_version(bridge: Bridge) -> None:
    import urllib.error
    import urllib.request

    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(bridge.url("/v1/health"), timeout=5)
    with err.value:
        assert err.value.headers["Server"].strip() == "meetstream-agent-kit"


def test_session_start_reports_and_caps_access(start_bridge: Callable[..., Bridge]) -> None:
    bridge = start_bridge(max_access="confirm")
    _, body = bridge.post(
        "/v1/session/start", {"session_id": "bot1", "access": "full", "owner_verified": True}
    )
    assert body["access"] == "confirm"
    assert body["max_access"] == "confirm"
    assert "--max-access" in body["access_reason"]
    assert bridge.sessions["bot1"].access == "confirm"
    proc = bridge.sessions["bot1"].proc
    _, body = bridge.post("/v1/session/start", {"session_id": "bot1", "access": "readonly"})
    assert body["access"] == "readonly"
    assert not body["reused"]  # a new level restarts the agent
    assert bridge.sessions["bot1"].proc is not proc


# ── Activation code ───────────────────────────────────────────────────────────


def make_gate() -> tuple[ActivationGate, list[str]]:
    shown: list[str] = []
    gate = ActivationGate(
        "Claude Code", "bot1", show=lambda title, text: shown.append(text), log=lambda text: None
    )
    return gate, shown


def test_activation_binds_one_participant() -> None:
    gate, shown = make_gate()
    code = gate._code
    assert len(code) == 6
    assert code.isdigit()
    assert code in shown[0]
    assert gate.state == "pending"
    assert gate.claim(code, "", "Nav") == (False, "no_participant_id")
    assert gate.claim(code, "dev-1", "Nav") == (True, "ok")
    assert gate.state == "claimed"
    assert gate.owner_id == "dev-1"
    assert gate.claim(code, "dev-2", "Guest") == (False, "already_claimed")  # single use


def test_wrong_codes_rotate_then_lock() -> None:
    gate, shown = make_gate()
    first = gate._code
    wrong = f"{(int(first) + 1) % 10**6:06d}"
    for _ in range(3):
        assert gate.claim(wrong, "dev-9", "Guest").reason == "wrong_code"
    assert gate._code != first
    assert len(shown) == 2  # a new code after 3 wrong tries
    while gate.state == "pending":
        gate.claim("x", "dev-9", "Guest")
    assert gate.state == "locked"
    assert gate.fails == activation.MAX_FAILS
    assert gate.claim(gate._code, "dev-1", "Nav") == (False, "locked")


def test_codes_expire_and_lock_again_when_the_owner_leaves(monkeypatch: pytest.MonkeyPatch) -> None:
    gate, shown = make_gate()
    now = time.monotonic()
    monkeypatch.setattr(activation.time, "monotonic", lambda: now + 301)
    assert gate.claim(gate._code, "dev-1", "Nav") == (False, "expired")
    assert len(shown) == 2
    monkeypatch.setattr(activation.time, "monotonic", lambda: now + 302)
    assert gate.claim(gate._code, "dev-1", "Nav") == (True, "ok")
    assert gate.release(["dev-7"]) is False
    assert gate.release(["dev-1"]) is True
    assert gate.state == "pending"
    assert len(shown) == 3


def test_turns_are_locked_until_the_code_is_pasted(bridge: Bridge) -> None:
    _, body = bridge.post("/v1/session/start", {"session_id": "bot1", "claim_required": True})
    assert body["claim"] == "pending"
    status, body = bridge.post("/v1/turn", TURN)
    assert status == 403
    assert body["error"] == "locked"
    _, body = bridge.post("/v1/claim", {"session_id": "bot1", "code": "x", "participant_id": "dev-1"})
    assert body["reason"] == "wrong_code"
    gate = bridge.sessions["bot1"].activation
    assert gate is not None
    claim = {"session_id": "bot1", "code": gate._code, "participant_id": "dev-1", "name": "Nav"}
    assert bridge.post("/v1/claim", claim)[1] == {"ok": True, "reason": "ok", "claim": "claimed"}
    status, events = bridge.post("/v1/turn", TURN, stream=True)
    assert status == 200
    assert events[-1]["type"] == "done"
    released = bridge.post("/v1/release", {"session_id": "bot1", "participant_ids": ["dev-1"]})[1]
    assert released == {"released": True, "claim": "pending"}
    assert bridge.post("/v1/turn", TURN)[0] == 403
    assert bridge.post("/v1/session/start", {"session_id": "bot2"})[1]["claim"] == "off"
    assert bridge.post("/v1/claim", {"session_id": "bot2", "code": "1"})[0] == 409


def test_the_activation_code_allows_full_access(start_bridge: Callable[..., Bridge]) -> None:
    bridge = start_bridge(max_access="full")
    _, body = bridge.post(
        "/v1/session/start", {"session_id": "bot1", "access": "full", "claim_required": True}
    )
    assert body["access"] == "full"
    assert body["claim"] == "pending"


# ── Claude Code stream-json driver ────────────────────────────────────────────


def test_claude_argv_per_access_level(tmp_path: Path) -> None:
    session = make_session(tmp_path, model="haiku", port=9999, instructions="Be brief.")
    driver = session.driver
    assert isinstance(driver, StreamJsonDriver)
    argv = driver.argv(resume=False)
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert "--tools=Read,Grep,Glob" in argv
    assert "dontAsk" in argv
    assert "--allowedTools=mcp__meeting" in argv
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--session-id") + 1] == driver.conversation_id
    assert argv[argv.index("--model") + 1] == "haiku"
    resumed = driver.argv(resume=True)
    assert resumed[resumed.index("--resume") + 1] == driver.conversation_id
    confirm = make_session(tmp_path, access="confirm", port=9999).driver
    assert confirm is not None
    assert "mcp__meeting__approve_action" in confirm.argv(False)
    full = make_session(tmp_path, access="full", port=9999).driver
    assert full is not None
    full_argv = full.argv(False)
    assert "acceptEdits" in full_argv
    assert "--allowedTools=Bash,mcp__meeting" in full_argv
    assert "dontAsk" not in full_argv
    session.close()


def test_the_meeting_secret_never_appears_in_the_claude_command_line(tmp_path: Path) -> None:
    session = make_session(tmp_path, port=9999)
    assert session.driver is not None
    argv = session.driver.argv(resume=False)
    assert session.mcp_secret not in json.dumps(argv)
    config_file = Path(argv[argv.index("--mcp-config") + 1])
    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600
    server = json.loads(config_file.read_text())["mcpServers"]["meeting"]
    assert server["url"] == "http://127.0.0.1:9999/mcp/bot1"
    assert server["headers"]["Authorization"] == f"Bearer {session.mcp_secret}"
    session.close()
    assert not config_file.exists()  # removed when the meeting ends


def test_barge_in_output_never_leaks_into_the_next_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Barge-in restarts Claude's process; what the old one still prints must not reach the next turn."""
    monkeypatch.setenv("STUB_FIRST_S", "0.3")  # the next turn answers after the stale words arrive
    session = make_session(tmp_path)
    session.start()
    first_turn: list[dict[str, Any]] = []
    thread = threading.Thread(target=lambda: first_turn.extend(session.run_turn(_turn_text("LONGSTREAM"))))
    thread.start()
    wait_for(lambda: sum(1 for e in first_turn if e["type"] == "delta") >= 3)
    t0 = time.monotonic()
    session.cancel()
    thread.join(5)
    assert first_turn[-1].get("code") == "cancelled"
    assert time.monotonic() - t0 < 0.4  # the cancel wakes the turn at once
    time.sleep(0.2)  # the old process is still streaming (it ignores SIGTERM)
    events = run(session, "again")
    assert "stale" not in "".join(e["text"] for e in events if e["type"] == "delta")
    assert events[-1] == {**events[-1], "type": "done", "text": "The answer is 42."}
    session.close()


def _turn_text(text: str) -> str:
    from meetstream_agent_kit.prompts import format_turn

    return format_turn([{"speaker": "Nav", "text": text}])


def test_a_custom_command_runs_once_per_turn(tmp_path: Path) -> None:
    config = BridgeConfig(agent=custom_agent(str(STUB)), binary=str(STUB), workdir=tmp_path, quiet=True)
    session = MeetingSession("bot1", config)
    assert session.warm is False
    events = run(session, "q")
    assert events[-1]["type"] == "done"
    assert events[-1]["warm"] is False


# ── ACP (Hermes, Copilot, Muse, OpenClaw) and Codex app-server ────────────────


@pytest.mark.parametrize("agent", ["hermes", "codex"])
def test_warm_drivers_stream_and_keep_one_process(tmp_path: Path, agent: str) -> None:
    session = make_session(tmp_path, agent, instructions="Be brief.")
    assert isinstance(session.driver, AcpDriver | CodexDriver)
    session.start()
    proc = session.proc
    for _ in range(2):
        events = run(session, "what is the answer?")
        assert events[-1] == {**events[-1], "type": "done", "text": "The answer is 42."}
        assert any(e["type"] == "delta" for e in events)
    assert session.proc is proc
    session.close()


@pytest.mark.parametrize("agent", ["hermes", "codex"])
def test_warm_drivers_cancel_and_keep_the_session(tmp_path: Path, agent: str) -> None:
    session = make_session(tmp_path, agent)
    session.start()
    proc = session.proc
    out: list[dict[str, Any]] = []
    thread = threading.Thread(target=lambda: out.extend(run(session, "SLOW please")))
    thread.start()
    time.sleep(0.5)
    t0 = time.monotonic()
    session.cancel()
    thread.join(5)
    assert time.monotonic() - t0 < 3
    assert out[-1]["code"] == "cancelled"
    assert run(session, "again")[-1]["type"] == "done"
    assert session.proc is proc
    session.close()


def test_codex_sign_in_error_is_classified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUB_AUTH_FAIL", "1")
    session = make_session(tmp_path, "codex")
    session.start()
    events = run(session, "hi")
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "auth_required"
    session.close()


def test_codex_gets_the_meeting_secret_by_environment_not_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    real_popen = subprocess.Popen

    def spy(argv: list[str], **kwargs: Any) -> Any:
        seen["argv"], seen["env"] = argv, kwargs.get("env") or {}
        return real_popen(argv, **kwargs)

    monkeypatch.setattr("meetstream_agent_kit.session.subprocess.Popen", spy)
    session = make_session(tmp_path, "codex", tools=TOOLS, port=8787)
    session.start()
    driver = session.driver
    assert isinstance(driver, CodexDriver)
    assert seen["argv"][1] == "app-server"
    assert driver.thread_id == "thr_1"
    assert 'mcp_servers.meeting.url="http://127.0.0.1:8787/mcp/bot1"' in seen["argv"]
    assert 'mcp_servers.meeting.default_tools_approval_mode="approve"' in seen["argv"]
    assert session.mcp_secret not in json.dumps(seen["argv"])
    assert seen["env"]["MEETSTREAM_AGENT_KIT_MCP_SECRET"] == session.mcp_secret
    session.close()


def test_acp_permissions_follow_the_access_level(tmp_path: Path) -> None:
    session = make_session(tmp_path, "hermes")
    session.start()
    assert run(session, "PERMISSION")[-1]["text"] == "permission no"  # readonly denies
    session.close()
    asked: list[str] = []
    session = make_session(
        tmp_path, "hermes", access="confirm", confirm=lambda action: asked.append(action) or True
    )
    session.start()
    assert run(session, "PERMISSION")[-1]["text"] == "permission yes"
    assert "execute" in asked[0]
    session.close()
    session = make_session(tmp_path, "hermes", access="full")
    session.start()
    assert run(session, "PERMISSION")[-1]["text"] == "permission yes"  # full: no prompt
    session.close()


def test_codex_approvals_follow_the_access_level(tmp_path: Path) -> None:
    sent: list[dict[str, Any]] = []
    request = {"id": 7, "method": "item/commandExecution/requestApproval", "params": {"command": "ls"}}
    for access, decision in (("full", "accept"), ("readonly", "decline")):
        driver = make_session(tmp_path, "codex", access=access).driver
        assert isinstance(driver, CodexDriver)
        driver.send = sent.append  # type: ignore[method-assign]
        driver._answer(request)
        assert sent[-1]["result"]["decision"] == decision


def test_acp_meeting_tools_without_the_secret_in_argv(tmp_path: Path) -> None:
    driver = make_session(tmp_path, "hermes", tools=TOOLS, port=8787).driver
    assert isinstance(driver, AcpDriver)
    stdio = driver.mcp_server({})
    assert stdio["command"] == sys.executable
    assert stdio["args"] == [
        "-m",
        "meetstream_agent_kit",
        "mcp-stdio",
        "--url",
        "http://127.0.0.1:8787/mcp/bot1",
    ]
    assert driver.session.mcp_secret not in json.dumps(stdio["args"])
    assert stdio["env"] == [{"name": "MEETSTREAM_AGENT_KIT_MCP_SECRET", "value": driver.session.mcp_secret}]
    http = driver.mcp_server({"mcpCapabilities": {"http": True}})
    assert http["type"] == "http"
    assert http["headers"][0]["name"] == "Authorization"


def test_mcp_stdio_shim_reaches_the_bridge(bridge: Bridge) -> None:
    bridge.post("/v1/session/start", {"session_id": "bot1", "tools": TOOLS})
    secret = bridge.sessions["bot1"].mcp_secret
    proc = subprocess.run(
        [sys.executable, "-m", "meetstream_agent_kit", "mcp-stdio", "--url", bridge.url("/mcp/bot1")],
        input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n",
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "MEETSTREAM_AGENT_KIT_MCP_SECRET": secret},
        check=True,
    )
    reply = json.loads(proc.stdout.strip().splitlines()[-1])
    assert [t["name"] for t in reply["result"]["tools"]] == ["send_chat_message"]


def test_mcp_stdio_shim_refuses_other_hosts() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "meetstream_agent_kit", "mcp-stdio", "--url", "https://example.com/mcp"],
        input="",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 2
