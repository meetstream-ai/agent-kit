"""Live progress follows real output, answer and warm session boundaries."""

from __future__ import annotations

import dataclasses
import random
import re
import sys
import threading
from pathlib import Path

import pytest

from meetstream_agent_kit import ui
from meetstream_agent_kit.cli import _check_agent, _check_warm_mode, _test_turn
from meetstream_agent_kit.compare import compare
from meetstream_agent_kit.oneshot import build_oneshot_argv, run_once
from meetstream_agent_kit.setup import Prompter
from meetstream_agent_kit.warm_check import check_warm

from .conftest import make_config

LABELS = [("Waking up Codex", "Woke Codex"), ("Pondering", "First words"), ("Answering", "Answered")]


def test_cold_events_follow_stdout_and_deltas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUB_FIRST_S", "0.1")
    config = make_config(tmp_path)
    argv, stdin = build_oneshot_argv(config.agent, "ready", "readonly", config.binary)
    events: list[tuple[str, float]] = []
    result = run_once(
        config.agent, argv, stdin, str(tmp_path), 5, on_stage=lambda name, ms: events.append((name, ms))
    )
    assert [name for name, _ in events] == ["output", "first_delta", "done"]
    assert result.first_line_ms is not None
    assert result.first_output_ms is not None
    assert 0 <= result.first_line_ms <= result.first_output_ms <= result.total_ms
    assert [ms for _, ms in events] == [result.first_line_ms, result.first_output_ms, result.total_ms]


@pytest.mark.parametrize(
    ("script", "expected"),
    [("pass", ["done"]), ('print(\'{"type":"system","subtype":"init"}\')', ["output", "done"])],
)
def test_cold_missing_boundaries_are_absent(tmp_path: Path, script: str, expected: list[str]) -> None:
    events: list[tuple[str, float]] = []
    result = run_once(
        make_config(tmp_path).agent,
        [sys.executable, "-c", script],
        None,
        str(tmp_path),
        5,
        on_stage=lambda name, ms: events.append((name, ms)),
    )
    assert [name for name, _ in events] == expected
    assert result.first_output_ms is None
    assert (result.first_line_ms is None) == ("output" not in expected)


def test_missing_binary_reports_done(tmp_path: Path) -> None:
    events: list[tuple[str, float]] = []
    result = run_once(
        make_config(tmp_path).agent,
        [str(tmp_path / "missing")],
        None,
        str(tmp_path),
        5,
        on_stage=lambda name, ms: events.append((name, ms)),
    )
    assert result.error
    assert events == [("done", result.total_ms)]
    assert result.first_line_ms is None


@pytest.mark.parametrize("no_color", [False, True])
def test_plain_summary_once(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, no_color: bool
) -> None:
    monkeypatch.setattr(ui, "is_interactive", lambda: False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
    with ui.Stages(LABELS) as stages:
        stages.reach(0, 800)
        stages.reach(1, 3100)
        stages.reach(2, 5700)
        assert capsys.readouterr().out == ""
    assert capsys.readouterr().out == "  Woke Codex 0.8 s · First words 3.1 s · Answered 5.7 s\n"


def test_missing_ticks_and_failure(capsys: pytest.CaptureFixture[str]) -> None:
    with ui.Stages(LABELS) as stages:
        stages.reach(2, 5700)
    assert capsys.readouterr().out == "  Answered 5.7 s\n"
    with ui.Stages(LABELS) as stages:
        stages.reach(0, 800)
        stages.fail("timed out", 5700)
    assert capsys.readouterr().out == ("  Woke Codex 0.8 s · stopped at Pondering after 5.7 s (timed out)\n")


def test_simultaneous_boundaries(capsys: pytest.CaptureFixture[str]) -> None:
    with ui.Stages(LABELS) as stages:
        stages.reach(0, 800)
        stages.reach(1, 800)
        stages.reach(2, 800)
    assert capsys.readouterr().out == "  Woke Codex 0.8 s · First words 0.8 s · Answered 0.8 s\n"


def test_seeded_verbs() -> None:
    first = random.Random(42)  # noqa: S311 - display wording only
    second = random.Random(42)  # noqa: S311 - display wording only
    assert [ui.Stages.cold("Codex", rng=first).labels for _ in range(10)] == [
        ui.Stages.cold("Codex", rng=second).labels for _ in range(10)
    ]


@pytest.mark.parametrize("error", [KeyboardInterrupt, RuntimeError])
def test_terminal_cleanup_on_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: type[BaseException]
) -> None:
    monkeypatch.setattr(ui, "is_interactive", lambda: True)
    monkeypatch.setenv("NO_COLOR", "1")
    with pytest.raises(error), ui.Stages(LABELS) as stages:
        raise error
    assert stages._thread is not None
    assert not stages._thread.is_alive()
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert "✗ Waking up Codex" in out
    assert "✓" not in out


def test_terminal_clips_coloured_lines_and_accepts_reader_thread(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ui, "is_interactive", lambda: True)
    monkeypatch.setattr(ui, "width", lambda: 32)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    with ui.Stages(LABELS) as stages:

        def complete() -> None:
            for index in range(3):
                stages.reach(index, 800)

        reader = threading.Thread(target=complete)
        reader.start()
        reader.join(timeout=1)
        assert not reader.is_alive()
    out = capsys.readouterr().out
    assert "\x1b[32m" in out
    plain = re.sub(r"\x1b\[[0-9;]*[mK]", "", out)
    assert all(len(line.rstrip("\n")) < 32 for line in plain.split("\r"))
    assert out.count("\n") == 1


def test_setup_keeps_answered_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _check_agent(Prompter(), make_config(tmp_path)) == "ok"
    out = capsys.readouterr().out
    assert all(label in out for label in ("Woke", "First words", "Answered", "answered in"))
    assert "\r" not in out


def test_setup_timeout_does_not_tick_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("STUB_FIRST_S", "5")
    result = _test_turn(dataclasses.replace(make_config(tmp_path), timeout_s=0.5))
    assert result.error
    assert "timed out" in result.error
    out = capsys.readouterr().out
    assert "Woke" in out
    assert "stopped at" in out
    assert "First words" not in out
    assert "Answered" not in out


@pytest.mark.parametrize(
    ("agent", "expected"), [("agy", ["ready", "tool_call", "done"]), ("openclaw", ["ready", "done"])]
)
def test_warm_events(tmp_path: Path, agent: str, expected: list[str]) -> None:
    events: list[tuple[str, float]] = []
    result = check_warm(
        make_config(tmp_path, agent), turn=True, on_stage=lambda name, ms: events.append((name, ms))
    )
    assert result.ok
    assert [name for name, _ in events] == expected
    times = [ms for _, ms in events]
    assert times == sorted(times)


def test_setup_warm_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _check_warm_mode(Prompter(), make_config(tmp_path, "agy")) == "ok"
    out = capsys.readouterr().out
    assert all(label in out for label in ("Session ready", "Tool called", "Answered"))
    assert "\r" not in out


def test_compare_stages_each_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("STUB_USAGE", "1")
    results = compare(make_config(tmp_path), ["haiku", "sonnet"], "")
    assert all(result.usage for result in results)
    out = capsys.readouterr().out
    assert "Woke haiku" in out
    assert "Woke sonnet" in out
    assert out.count("First words") == out.count("Answered") == 2
