"""A turn never hangs silently: agent requests are always answered, slow turns end with a
spoken reply, a new turn stops one MIA gave up on, and every failure shows in the log."""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from meetstream_agent_kit import session as session_mod
from meetstream_agent_kit.constants import TIMEOUT_REPLY
from meetstream_agent_kit.drivers import acp_summary
from meetstream_agent_kit.session import MeetingSession

from .conftest import TOOLS, Bridge, make_config, make_session, run


def _acp(tmp_path: Path, agent: str = "agy", **changes: Any) -> MeetingSession:
    access = changes.pop("access", "readonly")
    config = dataclasses.replace(make_config(tmp_path, agent, max_access=access), **changes)
    return MeetingSession("bot1", config, access=access)


# ── Agent requests are always answered ───────────────────────────────────────


def test_permission_follows_the_access_level_for_agy(tmp_path: Path) -> None:
    for access, expected in (("readonly", "permission no"), ("full", "permission yes")):
        session = _acp(tmp_path, access=access)
        session.start()
        assert run(session, "PERMISSION")[-1]["text"] == expected
        session.close()


def test_options_told_apart_only_by_name_are_matched(tmp_path: Path) -> None:
    for access, expected in (("readonly", "permission d1"), ("full", "permission a1")):
        session = _acp(tmp_path, access=access)
        session.start()
        assert run(session, "NAMED_PERMISSION")[-1]["text"] == expected
        session.close()


def test_a_malformed_permission_request_is_still_answered(tmp_path: Path) -> None:
    session = _acp(tmp_path, access="full")
    session.start()
    t0 = time.monotonic()
    events = run(session, "BAD_PERMISSION")
    assert events[-1]["type"] == "done"
    assert events[-1]["text"] in ("permission cancelled", "permission error")  # answered, not left hanging
    assert time.monotonic() - t0 < 5
    session.close()


# ── Slow turns end with a spoken reply ───────────────────────────────────────


def test_a_silent_acp_turn_is_cancelled_and_mia_hears_why(tmp_path: Path) -> None:
    session = _acp(tmp_path, timeout_s=1)
    session.start()
    proc = session.proc
    t0 = time.monotonic()
    events = run(session, "SLOW")  # the stand-in waits until session/cancel
    assert events[-1] == {**events[-1], "type": "done", "timed_out": True, "text": TIMEOUT_REPLY}
    assert time.monotonic() - t0 < 3
    assert session.proc is proc  # it stopped on request: same process, conversation kept
    assert run(session, "and now?")[-1]["text"] == "The answer is 42."


def test_an_agent_that_ignores_cancel_is_restarted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_mod, "CANCEL_GRACE_S", 0.3)
    session = _acp(tmp_path, timeout_s=1)
    session.start()
    proc = session.proc
    driver = session.driver
    assert driver is not None
    monkeypatch.setattr(driver, "cancel", lambda: None)  # the agent never hears the cancel
    events = run(session, "SLOW")
    assert events[-1]["timed_out"] is True
    assert session.proc is not proc  # restarted
    assert run(session, "again")[-1]["text"] == "The answer is 42."
    session.close()


def test_waiting_on_a_meeting_tool_does_not_count_as_silence(tmp_path: Path) -> None:
    session = MeetingSession(
        "bot1", dataclasses.replace(make_config(tmp_path, "agy"), timeout_s=1), tools=TOOLS, port=0
    )
    session.pending_tool_calls = lambda: ["call_1"]  # type: ignore[method-assign]
    driver = session.driver
    assert driver is not None
    session.start()
    out: list[dict[str, Any]] = []
    thread = threading.Thread(target=lambda: out.extend(run(session, "SLOW")))
    thread.start()
    time.sleep(2)  # past the 1 s limit, but MIA is "running a tool"
    assert not out
    session.cancel()
    thread.join(5)
    assert out[-1]["code"] == "cancelled"
    session.close()


# ── A new turn stops one MIA gave up on ──────────────────────────────────────


def test_a_new_turn_stops_the_hung_one_instead_of_queueing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _acp(tmp_path, quiet=False)
    session.start()
    first: list[dict[str, Any]] = []
    thread = threading.Thread(target=lambda: first.extend(run(session, "SLOW")))
    thread.start()
    time.sleep(0.5)
    t0 = time.monotonic()
    second = run(session, "can you still hear me?")
    thread.join(5)
    assert first[-1]["code"] == "cancelled"
    assert second[-1]["text"] == "The answer is 42."
    assert time.monotonic() - t0 < 3
    assert "stopped the last one" in capsys.readouterr().out
    session.close()


def test_the_warm_up_turn_is_waited_out_not_stopped(tmp_path: Path) -> None:
    """A turn arriving during the warm-up waits for it; the warm-up is never cancelled.

    Driven by cues, not sleeps: the warm-up ends only when the test creates ``release``,
    after it has seen the second turn waiting (slow CI runners stretch real sleeps).
    """
    session = make_session(tmp_path, "hermes")
    session.start()
    release = tmp_path / "release"
    warmup: list[dict[str, Any]] = []
    first = threading.Thread(
        target=lambda: warmup.extend(session.run_turn(f"warm up WAIT_FOR:{release}", warmup=True))
    )
    first.start()
    deadline = time.monotonic() + 30
    while not session.turn_running:  # the warm-up holds the turn
        assert time.monotonic() < deadline, "the warm-up never started"
        time.sleep(0.01)
    second: list[dict[str, Any]] = []
    waiting = threading.Thread(target=lambda: second.extend(run(session, "hello")))
    waiting.start()
    time.sleep(0.5)  # longer than the 0.2 s after which a normal turn would be stopped
    assert not second  # still waiting behind the warm-up
    assert not session.cancelled  # and the warm-up was not stopped
    release.touch()
    first.join(30)
    waiting.join(30)
    assert warmup[-1]["type"] == "done"  # not cancelled
    assert second[-1]["type"] == "done"
    session.close()


# ── Every failure shows in the log; --debug traces message types ─────────────


def test_failed_and_stopped_turns_are_logged(
    start_bridge: Callable[..., Bridge], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_AUTH_FAIL", "1")  # the ACP session cannot start
    bridge = start_bridge(agent="hermes")
    bridge.server.config = dataclasses.replace(bridge.server.config, quiet=False)
    bridge.post("/v1/session/start", {"session_id": "bot1"})
    bridge.post(
        "/v1/turn", {"session_id": "bot1", "messages": [{"speaker": "Nav", "text": "hi"}]}, stream=True
    )
    out = capsys.readouterr().out
    assert "[error] auth_required: " in out
    assert "Authentication required" in out


def test_debug_traces_acp_message_types_without_their_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _acp(tmp_path, debug=True, access="full")
    session.start()
    run(session, "PERMISSION")
    out = capsys.readouterr().out
    assert "acp <- response id=1" in out  # initialize
    assert "acp -> session/prompt" in out
    assert "acp <- request session/request_permission id=9001 execute rm -rf /" in out
    assert "acp -> permission allowed (full): yes" in out
    assert "acp <- update agent_message_chunk" in out
    assert "acp <- response id=3 stopReason=end_turn" in out
    assert "permission yes" not in out  # the agent's words are not logged
    session.close()


def test_acp_summary_lines() -> None:
    assert acp_summary({"id": 4, "error": {"message": "Authentication required"}}) == (
        "response id=4 error Authentication required"
    )
    update = {
        "method": "session/update",
        "params": {"update": {"sessionUpdate": "tool_call", "title": "ls", "status": "pending"}},
    }
    assert acp_summary(update) == "update tool_call ls pending"
    assert acp_summary({"method": "session/cancel"}) == "notification session/cancel"
    assert (
        acp_summary({"id": 5, "method": "fs/read_text_file", "params": None})
        == "request fs/read_text_file id=5"
    )


# ── A client that hangs up is not an error ───────────────────────────────────


def _raise_then_handle(bridge: Bridge, error: BaseException) -> None:
    try:
        raise error
    except BaseException:
        bridge.server.handle_error(None, ("127.0.0.1", 50000))


def test_a_dropped_connection_prints_no_traceback(
    start_bridge: Callable[..., Bridge], capsys: pytest.CaptureFixture[str]
) -> None:
    bridge = start_bridge()
    for error in (
        ConnectionResetError(54, "Connection reset by peer"),
        BrokenPipeError(32, "Broken pipe"),
        TimeoutError("timed out"),
    ):
        _raise_then_handle(bridge, error)
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err + captured.out
    bridge.server.config = dataclasses.replace(bridge.server.config, debug=True)
    _raise_then_handle(bridge, ConnectionResetError(54, "Connection reset by peer"))
    out = capsys.readouterr().out
    assert out.strip() == "[debug] connection closed by the client (ConnectionResetError)"


def test_real_errors_keep_their_traceback(
    start_bridge: Callable[..., Bridge], capsys: pytest.CaptureFixture[str]
) -> None:
    bridge = start_bridge()
    _raise_then_handle(bridge, ValueError("a real bug"))
    err = capsys.readouterr().err
    assert "Traceback" in err
    assert "ValueError: a real bug" in err


def test_a_client_that_hangs_up_mid_request_is_quiet(
    start_bridge: Callable[..., Bridge], capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end: a reset socket (as when ngrok drops a connection) leaves no traceback."""
    import socket
    import struct

    bridge = start_bridge()
    with socket.create_connection(("127.0.0.1", bridge.server.port)) as conn:
        conn.sendall(b"POST /v1/turn HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n{")
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # RST on close
    time.sleep(0.5)
    assert bridge.post("/v1/session/start", {"session_id": "bot1"})[0] == 200  # still serving
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err + captured.out
