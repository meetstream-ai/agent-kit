"""The prompt_toolkit prompts, and walking every setup step with Back available."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from meetstream_agent_kit import tui
from meetstream_agent_kit.models import ModelCatalog, ModelChoice
from meetstream_agent_kit.setup import NgrokSetup, Prompter, ask_setup

pytestmark = pytest.mark.skipif(not tui.AVAILABLE, reason="prompt_toolkit is not installed")


@contextmanager
def _keys(text: str) -> Iterator[dict[str, Any]]:
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        pipe.send_text(text)
        yield {"input": pipe, "output": DummyOutput()}


def test_select_moves_and_picks() -> None:
    with _keys("\x1b[B\x1b[B\r") as io:
        assert tui.select("Pick", ["a", "b", "c"], **io) == 2


def test_select_wraps_and_jumps_by_number() -> None:
    with _keys("\x1b[A\r") as io:
        assert tui.select("Pick", ["a", "b", "c"], **io) == 2
    with _keys("2\r") as io:
        assert tui.select("Pick", ["a", "b", "c"], **io) == 1


@pytest.mark.parametrize("key", ["\x1b", "\x1b[D"])  # Esc or ←
def test_select_goes_back(key: str) -> None:
    with _keys(key) as io:
        assert tui.select("Pick", ["a", "b"], back=True, **io) is tui.BACK


def test_select_ignores_esc_when_there_is_no_step_to_go_back_to() -> None:
    with _keys("\x1b\x1b[B\r") as io:
        assert tui.select("Pick", ["a", "b"], back=False, **io) == 1


def test_the_detail_line_follows_the_arrow() -> None:
    seen: list[int] = []

    def detail(index: int) -> str:
        seen.append(index)
        return f"about {index}"

    with _keys("\x1b[B") as io:
        later = threading.Timer(0.5, io["input"].send_text, ["\r"])  # time to redraw first
        later.start()
        assert tui.select("Pick", ["a", "b", "c"], detail=detail, **io) == 1
        later.join()
    assert {0, 1} <= set(seen)


def test_text_accepts_the_default_or_an_edit() -> None:
    with _keys("\r") as io:
        assert tui.text("Name", "Nav", **io) == "Nav"
    with _keys("\x7f\x7f\x7fSam\r") as io:
        assert tui.text("Name", "Nav", **io) == "Sam"


def test_esc_in_a_text_prompt_goes_back_instead_of_typing_escape_codes() -> None:
    with _keys("ab\x1b") as io:
        assert tui.text("Name", "", back=True, **io) is tui.BACK


def test_select_many_ticks_with_space() -> None:
    with _keys(" \x1b[B\x1b[B \r") as io:
        assert tui.select_many("Pick", ["a", "b", "c"], **io) == [0, 2]
    with _keys("\x1b") as io:
        assert tui.select_many("Pick", ["a", "b"], back=True, **io) is tui.BACK


def test_confirm() -> None:
    with _keys("\x1b[B\r") as io:
        assert tui.confirm("Sure?", True, **io) is False
    with _keys("\r") as io:
        assert tui.confirm("Sure?", True, **io) is True


class _PressBackOnce:
    """Stands in for the prompt_toolkit prompts: Back once per question where Back is offered.

    Every menu's detail line is rendered for every row, the Back row included (that row had
    no detail and crashed step 2 with an IndexError).
    """

    def __init__(self) -> None:
        self.backed: set[str] = set()
        self.calls = 0
        self.details_rendered = 0

    def _back(self, question: str, back: bool) -> bool:
        self.calls += 1
        assert self.calls < 200, "setup keeps asking"
        if back and question not in self.backed:
            self.backed.add(question)
            return True
        return False

    def select(
        self,
        question: str,
        options: Sequence[str],
        default: int = 0,
        hints: Sequence[str] = (),
        *,
        detail: Callable[[int], str] | None = None,
        back: bool = False,
    ) -> object:
        if detail is not None:
            for index in range(len(options)):
                assert isinstance(detail(index), str)
                self.details_rendered += 1
        return tui.BACK if self._back(question, back) else default

    def select_many(
        self,
        question: str,
        options: Sequence[str],
        chosen: Sequence[int] = (),
        hints: Sequence[str] = (),
        *,
        back: bool = False,
    ) -> object:
        return tui.BACK if self._back(question, back) else list(chosen)

    def text(self, question: str, default: str = "", *, back: bool = False) -> object:
        return tui.BACK if self._back(question, back) else (default or "Nav")

    def confirm(self, question: str, default: bool = True, *, back: bool = False) -> object:
        return tui.BACK if self._back(question, back) else default


def test_every_step_goes_back_without_crashing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USER", "Nav")
    fake = _PressBackOnce()
    for name in ("select", "select_many", "text", "confirm"):
        monkeypatch.setattr(tui, name, getattr(fake, name))
    agy = tmp_path / "agy"
    agy.write_text("#!/bin/sh\n", encoding="utf-8")
    agy.chmod(0o755)
    catalog = ModelCatalog([ModelChoice("m1"), ModelChoice("m2")], "m1", ["low", "high"], source="test")

    answers = ask_setup(
        Prompter(interactive=True),
        None,
        which=lambda program: str(agy) if program in ("agy", "cloudflared") else None,
        run=lambda _argv: 0,
        cwd=str(tmp_path),
        detect_ngrok=lambda: NgrokSetup(None),
        offer_install=False,
        catalog=lambda _agent, _binary: catalog,
    )

    assert fake.details_rendered > 0  # the model menu (with its Back row) was drawn
    assert len(fake.backed) >= 5  # Back was pressed in every step after the first
    assert answers.owner == "Nav"
    assert answers.workdir == str(tmp_path.resolve())


def test_coloured_rows_are_drawn_as_colours_not_escape_codes() -> None:
    fragments = tui._styled("\x1b[36m████\x1b[0m 445k/h", "class:hint")
    assert "".join(text for _, text in fragments) == "████ 445k/h"
    assert tui._visible("\x1b[36m████\x1b[0m 445k/h") == len("████ 445k/h")
    with _keys("\r") as io:
        assert tui.select("Pick", ["\x1b[1ma\x1b[0m", "b"], 0, ["\x1b[32mtalk\x1b[0m"], **io) == 0
