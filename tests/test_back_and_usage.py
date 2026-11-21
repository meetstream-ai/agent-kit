"""Going back a step in setup, and CLIs that answer without reporting token usage (Copilot)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from meetstream_agent_kit.agents import AGENTS
from meetstream_agent_kit.compare import Measured, chart_lines, compare
from meetstream_agent_kit.config import BridgeConfig
from meetstream_agent_kit.models import ModelCatalog, ModelChoice
from meetstream_agent_kit.setup import Prompter, _pick_model, ask_setup


def _replies(*answers: str) -> Prompter:
    feed = iter(answers)

    def read(_q: str) -> str:
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    return Prompter(read)


def _agy_installed(tmp_path: Path) -> Any:
    fake = tmp_path / "agy"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    return lambda program: str(fake) if program == "agy" else None


def _two_models(_agent: Any, _binary: str) -> ModelCatalog:
    return ModelCatalog([ModelChoice("m1"), ModelChoice("m2")], "m1", source="test")


def test_back_from_the_model_step_to_pick_another_agent(tmp_path: Path) -> None:
    answers = ask_setup(
        _replies(
            "4",  # Antigravity
            "<",  # model step: back
            "8",  # another command
            "my-agent",
            "",  # folder
            "",  # access
            "4",  # no tunnel
            "Nav",
        ),
        None,
        which=_agy_installed(tmp_path),
        cwd=str(tmp_path),
        catalog=_two_models,
    )
    assert (answers.agent, answers.command, answers.model) == ("custom", "my-agent", "")


def test_the_back_row_keeps_earlier_answers(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    answers = ask_setup(
        _replies(
            "8",
            "my-agent",
            "",  # folder: the default
            "4",  # access menu: 1-3 levels, 4 = "← Back" (the model step asked nothing, so: context)
            "",  # folder again, already filled in
            "2",  # ask first
            "4",
            "Nav",
        ),
        None,
        which=lambda _p: None,
        cwd=str(tmp_path),
    )
    out = capsys.readouterr().out
    assert "4) ← Back" in out
    assert answers.max_access == "confirm"
    assert answers.workdir == str(tmp_path.resolve())
    assert out.count("3/7  Meeting context") == 2  # the step came back once


def test_back_from_the_effort_question_returns_to_the_model_menu(tmp_path: Path) -> None:
    listed = ModelCatalog([ModelChoice("m1"), ModelChoice("m2")], "m1", ["low", "high"], source="test")
    prompt = _replies("2", "<", "1", "2")  # m2, back, m1, low
    prompt.back_allowed = True
    model, effort, asked = _pick_model(
        prompt,
        AGENTS["agy"],
        "agy",
        None,
        catalog=lambda _a, _b: listed,
        compare_config=lambda: None,  # type: ignore[arg-type,return-value]
        measure_model=lambda *_a: None,  # type: ignore[arg-type,return-value]
    )
    assert (model, effort, asked) == ("m1", "low", True)


@pytest.mark.skipif(sys.platform == "win32", reason="pseudo-terminals are Unix only")
@pytest.mark.parametrize("key", [b"\x1b[D", b"\x1b"])  # ← or Esc
def test_esc_or_left_arrow_goes_back_in_a_terminal(key: bytes) -> None:
    import os
    import pty
    import select
    import time

    code = (
        "from meetstream_agent_kit.setup import GoBack, Prompter\n"
        "p = Prompter()\n"
        "p.back_allowed = True\n"
        "try:\n"
        "    p.choose('Pick one', ['alpha', 'beta'])\n"
        "    print('PICKED', flush=True)\n"
        "except GoBack:\n"
        "    print('WENT BACK', flush=True)\n"
    )
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - the child
        os.execv(sys.executable, [sys.executable, "-c", code])  # noqa: S606 - our own interpreter
    out = b""
    deadline = time.monotonic() + 20
    sent = False
    while time.monotonic() < deadline:
        if select.select([fd], [], [], 0.1)[0]:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        if not sent and b"Pick one" in out:
            time.sleep(0.3)
            os.write(fd, key)
            sent = True
        if b"WENT BACK" in out or b"PICKED" in out:
            break
    os.waitpid(pid, 0)
    os.close(fd)
    assert b"WENT BACK" in out


def test_an_answer_without_token_usage_is_not_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = BridgeConfig(agent=AGENTS["copilot"], binary="copilot", workdir=tmp_path)
    results = compare(config, ["auto"], "", run=lambda _c, m, _e: Measured(m, None, 14.8))
    out = capsys.readouterr().out
    assert "✓ auto answered in 14.8 s · GitHub Copilot CLI doesn't report token usage" in out
    assert "✗" not in out
    assert "doesn't report token usage" in chart_lines(results)[0]


def test_measure_live_is_not_offered_when_the_cli_cannot_report_usage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _pick_model(
        _replies("1"),
        AGENTS["copilot"],
        "copilot",
        None,
        catalog=_two_models,
        compare_config=lambda: None,  # type: ignore[arg-type,return-value]
        measure_model=lambda *_a: None,  # type: ignore[arg-type,return-value]
    )
    out = capsys.readouterr().out
    assert "Measure live" not in out
    assert "usage n/a" in out
