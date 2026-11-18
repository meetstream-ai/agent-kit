"""Desktop alerts when the agent fails in a meeting, and keeping project rules out of meetings."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from meetstream_agent_kit.agents import AGENTS, custom_agent
from meetstream_agent_kit.alerts import TITLE, failure_kind, message_for
from meetstream_agent_kit.oneshot import build_oneshot_argv
from meetstream_agent_kit.session import MeetingSession
from meetstream_agent_kit.setup import Prompter, _review_project_rules, ask_setup, project_rules

from .conftest import Bridge, make_config

CODEX_OUT_OF_CREDITS = (  # what codex exec printed on 2026-11-18
    "ERROR: Your workspace is out of credits. Ask your workspace owner to refill in order to continue."
)


# ── Alerts ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("event", "kind"),
    [
        ({"type": "error", "code": "agent_error", "message": CODEX_OUT_OF_CREDITS}, "out_of_credits"),
        ({"type": "error", "code": "agent_error", "message": "429 Too Many Requests"}, "rate_limited"),
        ({"type": "error", "code": "agent_error", "message": "the agent process exited (1)"}, "crashed"),
        ({"type": "error", "code": "agent_error", "message": "boom"}, "failed"),
        ({"type": "done", "text": "That took too long, so I stopped it.", "timed_out": True}, "timeout"),
        (
            {"type": "error", "code": "auth_required", "message": "Not logged in"},
            "",
        ),  # the login checker tells
        ({"type": "error", "code": "cancelled", "message": "barge-in"}, ""),
        ({"type": "error", "code": "busy", "message": "a turn is already running"}, ""),
        ({"type": "done", "text": "ok"}, ""),
    ],
)
def test_failure_kinds(event: dict[str, Any], kind: str) -> None:
    assert failure_kind(event) == kind


def test_alert_text_says_what_to_do() -> None:
    text = message_for("out_of_credits", AGENTS["codex"])
    assert (
        text
        == "Codex CLI ran out of credits. MIA is quiet in your meeting; run `codex login` or switch agents."
    )


def test_one_alert_per_kind_per_meeting(
    start_bridge: Callable[..., Bridge], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_FAIL_TEXT", CODEX_OUT_OF_CREDITS)
    sent: list[tuple[str, str]] = []
    bridge = start_bridge()
    bridge.server.config = dataclasses.replace(bridge.server.config, notify=lambda t, x: sent.append((t, x)))
    bridge.post("/v1/session/start", {"session_id": "bot1"})
    turn = {"session_id": "bot1", "messages": [{"speaker": "Nav", "text": "hi"}]}
    _, first = bridge.post("/v1/turn", turn, stream=True)
    _, second = bridge.post("/v1/turn", turn, stream=True)
    assert first[-1]["type"] == "error"
    assert first[-1]["reason"] == "out_of_credits"  # a clean error for MIA, which stays quiet
    assert second[-1]["reason"] == "out_of_credits"
    assert sent == [(TITLE, message_for("out_of_credits", AGENTS["claude"]))]  # once, not per turn


# ── Project rules ─────────────────────────────────────────────────────────────


def test_each_cli_gets_its_own_skip_switch() -> None:
    codex, _ = build_oneshot_argv(AGENTS["codex"], "hi", "readonly", "codex", ignore_rules=True)
    assert codex[:4] == [
        "codex",
        "exec",
        "-c",
        "project_doc_max_bytes=0",
    ]  # options right after the subcommand
    claude, _ = build_oneshot_argv(AGENTS["claude"], "hi", "readonly", "claude", ignore_rules=True)
    assert claude[-2:] == ["--setting-sources", "user"]  # verified: skips the folder's CLAUDE.md
    hermes, _ = build_oneshot_argv(AGENTS["hermes"], "hi", "readonly", "hermes", ignore_rules=True)
    assert "--ignore-rules" in hermes
    agy_on, _ = build_oneshot_argv(AGENTS["agy"], "hi", "readonly", "agy", ignore_rules=True)
    agy_off, _ = build_oneshot_argv(AGENTS["agy"], "hi", "readonly", "agy")
    assert agy_on == agy_off  # no switch exists
    assert "no switch" in AGENTS["agy"].ignore_rules_note
    plain, _ = build_oneshot_argv(AGENTS["claude"], "hi", "readonly", "claude")
    assert "--setting-sources" not in plain  # off by default: unchanged behaviour


def test_warm_processes_skip_rules_too(tmp_path: Path) -> None:
    claude = MeetingSession("b", dataclasses.replace(make_config(tmp_path, "claude"), ignore_rules=True))
    assert claude.driver is not None
    assert "--setting-sources" in claude.driver.argv(resume=False)
    codex = MeetingSession("b", dataclasses.replace(make_config(tmp_path, "codex"), ignore_rules=True))
    assert codex.driver is not None
    assert codex.driver.argv(resume=False)[2:4] == ["-c", "project_doc_max_bytes=0"]
    hermes = MeetingSession("b", dataclasses.replace(make_config(tmp_path, "hermes"), ignore_rules=True))
    assert hermes.driver is not None
    assert hermes.driver.argv(resume=False)[1:] == ["acp"]  # hermes acp takes no such flag
    assert hermes._spawn_env()["HERMES_IGNORE_RULES"] == "1"  # skips its auto-loaded skills


def _replies(*answers: str) -> Prompter:
    feed = iter(answers)

    def read(_prompt: str) -> str:
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    return Prompter(read)


def test_project_rules_are_found_with_their_token_cost(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "CLAUDE.md").write_text("x" * 29_500, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("y" * 400, encoding="utf-8")
    assert project_rules(tmp_path) == [("AGENTS.md", 400), ("CLAUDE.md", 29_500)]
    choice = _review_project_rules(_replies("1"), AGENTS["claude"], tmp_path, tmp_path / "ws")
    out = capsys.readouterr().out
    assert choice == (str(tmp_path), True, True)  # meeting only: rules ignored, MEETING.md kept
    assert "CLAUDE.md  28.8 KB ≈ 7,375 tokens" in out
    assert "≈ 7,475 tokens" in out


def test_rules_can_be_avoided_with_the_meeting_workspace(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("rules", encoding="utf-8")
    workspace = tmp_path / "MeetStream Agent"
    # agy has no switch, so the menu is: 1 = workspace, 2 = keep
    workdir, ignore, _ = _review_project_rules(_replies("1"), AGENTS["agy"], tmp_path, workspace)
    assert workdir == str(workspace.resolve())
    assert workspace.is_dir()
    assert ignore is False
    kept = _review_project_rules(_replies("2"), custom_agent("tool"), tmp_path, workspace)
    assert kept == (str(tmp_path), False, True)


def test_setup_defaults_to_the_meeting_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    answers = ask_setup(
        _replies("8", "my-agent", "", "", "4", "Nav"), None, which=lambda _p: None
    )  # another command, its command, the default folder, access, no tunnel, name
    assert answers.workdir == str((tmp_path / "MeetStream Agent").resolve())
    assert (tmp_path / "MeetStream Agent").is_dir()
    assert answers.ignore_rules == ""
