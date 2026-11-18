"""The warm (ACP) check in setup, doctor and bench: the path meetings really use."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from meetstream_agent_kit import warm_check
from meetstream_agent_kit.cli import _check_warm_mode, main
from meetstream_agent_kit.setup import Prompter
from meetstream_agent_kit.warm_check import WarmCheck, check_warm

from .conftest import STUB, make_config


def test_warm_check_calls_a_meeting_tool_through_acp(tmp_path: Path) -> None:
    result = check_warm(make_config(tmp_path, "agy"), turn=True)
    assert result.ok, result
    assert result.tool_called
    assert result.tools_listed == 1
    assert result.message == "Warm mode (ACP) ready · meeting tools work (test tool called)"


@pytest.fixture(autouse=True)
def _short_tool_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(warm_check, "TOOL_LIST_WAIT_S", 0.5)


def test_doctor_style_check_stops_after_the_handshake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lazy = check_warm(make_config(tmp_path, "hermes"), turn=False)
    assert lazy.ok
    assert not lazy.tool_called  # no model turn, no tokens
    assert lazy.message == "Warm mode (ACP) ready · meeting tools offered; the agent loads them on first use"
    monkeypatch.setenv("STUB_MCP_AT_START", "1")  # like agents that load MCP tools at session start
    eager = check_warm(make_config(tmp_path, "agy"), turn=False)
    assert eager.ok
    assert eager.tools_listed == 1
    assert eager.message == "Warm mode (ACP) ready · 1 meeting tool loaded"


def test_signed_out_acp_agent_is_reported_with_its_sign_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_AUTH_FAIL", "1")
    result = check_warm(make_config(tmp_path, "agy"), turn=False)
    assert not result.ok
    assert "Authentication required" in result.message
    assert result.fix == "Sign in with: agy acp --login"


def test_an_agent_that_ignores_the_meeting_tools_fails_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_NO_TOOLS", "1")
    result = check_warm(make_config(tmp_path, "agy"), turn=True)
    assert not result.ok
    assert "did not call the meeting tool" in result.message
    assert "never loaded the meeting tools" in result.message


def test_text_only_acp_agent_gets_a_plain_turn(tmp_path: Path) -> None:
    result = check_warm(make_config(tmp_path, "openclaw"), turn=True)
    assert result.ok
    assert "text only" in result.message


def test_only_acp_agents_get_the_warm_check(tmp_path: Path) -> None:
    assert warm_check.applies_to(make_config(tmp_path, "agy"))
    assert not warm_check.applies_to(make_config(tmp_path, "claude"))
    assert not warm_check.applies_to(make_config(tmp_path, "codex"))


def _replies(*answers: str) -> Prompter:
    feed = iter(answers)

    def read(_prompt: str) -> str:
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    return Prompter(read)


def test_setup_retries_the_warm_check_then_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    results = iter(
        [
            WarmCheck(False, "Authentication required", "Sign in with: agy acp --login"),
            WarmCheck(True, "ready"),
        ]
    )
    calls: list[dict[str, Any]] = []

    def check(_config: Any, **kwargs: Any) -> WarmCheck:
        calls.append(kwargs)
        return next(results)

    assert _check_warm_mode(_replies("1"), make_config(tmp_path, "agy"), check=check) == "ok"
    out = capsys.readouterr().out
    assert "Warm mode (ACP): Authentication required" in out
    assert "Fix: Sign in with: agy acp --login" in out
    assert calls == [{"turn": True}, {"turn": True}]


def test_setup_warm_check_stops_when_input_ends(tmp_path: Path) -> None:
    calls: list[int] = []

    def check(_config: Any, **_kwargs: Any) -> WarmCheck:
        calls.append(1)
        return WarmCheck(False, "boom")

    assert _check_warm_mode(_replies(), make_config(tmp_path, "agy"), check=check) == "quit"
    assert calls == [1]
    assert _check_warm_mode(_replies(), make_config(tmp_path, "claude"), check=check) == "ok"  # not ACP


def test_doctor_shows_the_warm_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["doctor", "--agent", "agy", "--binary", str(STUB), "--workdir", str(tmp_path), "--port", "0"])
    out = capsys.readouterr().out
    assert "✓ warm: Warm mode (ACP) ready" in out
    main(["doctor", "--agent", "agy", "--binary", str(STUB), "--workdir", str(tmp_path), "--no-warm"])
    assert "warm:" not in capsys.readouterr().out


def test_bench_runs_warm_turns_over_acp(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["bench", "--agent", "agy", "--binary", str(STUB), "--workdir", str(tmp_path), "--repeat", "1"])
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {"kind": "warm_start", "driver": "acp"}.items() <= next(
        r for r in rows if r["kind"] == "warm_start"
    ).items()
    warm = [r for r in rows if r["kind"] == "warm"]
    assert warm
    assert warm[0]["error"] is None
