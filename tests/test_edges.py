"""Edges: the real ``serve`` process, confirm approvals, timeouts, malformed input, bench."""

from __future__ import annotations

import dataclasses
import http.client
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

import pytest

from meetstream_agent_kit.cli import main
from meetstream_agent_kit.constants import CANCEL_GRACE_S, TIMEOUT_REPLY
from meetstream_agent_kit.pairing import PairingStore
from meetstream_agent_kit.session import MeetingSession

from .conftest import STUB, TOOLS, Bridge, make_config, run


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_serve_runs_answers_and_stops_cleanly(tmp_path: Path, home: Path) -> None:
    _, token = PairingStore().create("t")
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "meetstream_agent_kit", "serve", "--binary", str(STUB),
         "--workdir", str(tmp_path), "--port", str(port), "--no-prime"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=dict(os.environ),
    )  # fmt: skip
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/health", headers={"Authorization": f"Bearer {token}"}
        )
        deadline = time.monotonic() + 60  # first start on a cold macOS CI runner is slow
        while True:
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    health = json.loads(response.read())
                break
            except (urllib.error.URLError, ConnectionError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        assert health["agent"] == "claude"
        assert (home / "run" / f"serve-{port}.json").exists()
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            out, _ = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()  # never leave serve running for the next test
            out, _ = proc.communicate()
            raise
    assert proc.returncode == 0, out
    assert re.search(r"address\s+http://127\.0\.0\.1:\d+", out)
    assert re.search(r"login\s+ok", out)
    assert "stopped" in out
    assert not (home / "run" / f"serve-{port}.json").exists()


def test_claude_confirm_approvals_go_through_the_owner(start_bridge: Callable[..., Bridge]) -> None:
    bridge = start_bridge(max_access="confirm")
    bridge.post("/v1/session/start", {"session_id": "bot1", "tools": TOOLS})
    secret = bridge.sessions["bot1"].mcp_secret
    listed = bridge.post("/mcp/bot1", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=secret)[1]
    assert "approve_action" in [t["name"] for t in listed["result"]["tools"]]
    call = {"name": "approve_action", "arguments": {"tool_name": "Bash", "input": {"command": "ls"}}}
    reply = bridge.post(
        "/mcp/bot1", {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": call}, token=secret
    )
    decision = json.loads(reply[1]["result"]["content"][0]["text"])
    assert decision["behavior"] == "deny"  # the test owner never says yes


def test_mcp_protocol_edges(bridge: Bridge) -> None:
    bridge.post("/v1/session/start", {"session_id": "bot1", "tools": TOOLS})
    secret = bridge.sessions["bot1"].mcp_secret

    def mcp(body: dict[str, object]) -> tuple[int, object]:
        return bridge.post("/mcp/bot1", body, token=secret)

    _, body = mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert body["result"]["serverInfo"]["name"] == "meetstream-meeting"  # type: ignore[index]
    assert mcp({"jsonrpc": "2.0", "method": "notifications/initialized"})[0] == 202
    assert mcp({"jsonrpc": "2.0", "id": 2, "method": "nope"})[1]["error"]["code"] == -32601  # type: ignore[index]
    unknown = mcp({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "rm"}})[1]
    assert unknown["result"]["isError"] is True  # type: ignore[index]
    assert bridge.get("/mcp/bot1")[0] == 405


def test_malformed_requests(bridge: Bridge) -> None:
    def raw(body: bytes, length: str | None = None) -> int:
        connection = http.client.HTTPConnection("127.0.0.1", bridge.server.port, timeout=10)
        headers = {"Authorization": f"Bearer {bridge.token}", "Content-Length": length or str(len(body))}
        connection.request("POST", "/v1/session/start", body=body, headers=headers)
        status = connection.getresponse().status
        connection.close()
        return status

    assert raw(b"{not json") == 400
    assert raw(b"[1, 2]") == 400
    assert raw(b"{}", length="0") == 413
    assert raw(b"{}", length=str(10 * 1024 * 1024)) == 413
    assert bridge.post("/v1/turn", {"session_id": "nope"})[0] == 404
    bridge.post("/v1/session/start", {"session_id": "bot1"})
    assert bridge.post("/v1/turn", {"session_id": "bot1", "messages": []})[0] == 400
    assert bridge.post("/v1/tool_result", {"session_id": "bot1", "call_id": "nope"})[0] == 404
    assert bridge.post("/v1/session/end", {"session_id": "bot1"})[1] == {"ok": True}
    assert bridge.post("/v1/session/end", {"session_id": "bot1"})[1] == {"ok": False}


def test_a_turn_that_runs_too_long_is_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUB_FIRST_S", "5")
    config = dataclasses.replace(make_config(tmp_path), timeout_s=1)
    session = MeetingSession("bot1", config)
    session.start()
    t0 = time.monotonic()
    events = run(session, "slow")
    elapsed = time.monotonic() - t0
    # MIA is never left silent: the turn ends with a short sentence to speak.
    assert events[-1] == {**events[-1], "type": "done", "timed_out": True, "text": TIMEOUT_REPLY}
    assert events[-2] == {"type": "delta", "text": TIMEOUT_REPLY}
    assert elapsed < 1 + CANCEL_GRACE_S + 2
    monkeypatch.setenv("STUB_FIRST_S", "0")
    assert run(session, "and now?")[-1]["type"] == "done"  # the next turn still works
    session.close()


def test_bench(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code = main(["bench", "--binary", str(STUB), "--workdir", str(tmp_path), "--repeat", "1"])
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 0
    assert [row["kind"] for row in rows] == ["cold", "warm_start", "warm"]
    assert all(not row.get("error") for row in rows)
    assert 0 <= rows[0]["first_line_ms"] <= rows[0]["first_output_ms"] <= rows[0]["total_ms"]


def test_sigterm_stops_serve_like_ctrl_c(tmp_path: Path, home: Path) -> None:
    PairingStore().create("t")
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "meetstream_agent_kit", "serve", "--binary", str(STUB),
         "--workdir", str(tmp_path), "--port", str(port), "--no-prime"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=dict(os.environ),
    )  # fmt: skip
    lock = home / "run" / f"serve-{port}.json"
    deadline = time.monotonic() + 60
    while not lock.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    proc.send_signal(signal.SIGTERM)
    try:
        out, _ = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    assert proc.returncode == 0, out
    assert "stopped" in out
    assert not lock.exists()
