"""MIA in the terminal: bundled art, half-block rendering, inline images, and staying out of the way."""

from __future__ import annotations

import base64
import io
import sys

import pytest

from meetstream_agent_kit import mascot


def test_bundled_art_has_every_state() -> None:
    data = mascot.art()
    assert set(data["states"]) == {"idle", "thinking", "speaking", "problem"}
    assert len(mascot.frames("idle")) > 1  # the wave is animated
    assert base64.b64decode(data["png"]).startswith(b"\x89PNG")
    frame = mascot.frames("idle")[0]
    assert len(frame) == data["width"] * data["height"] * 4


def test_half_blocks_in_true_colour_and_256_colours() -> None:
    frame = mascot.frames("idle")[0]
    rows = mascot.block_rows(frame, full=True)
    assert len(rows) == mascot.art()["height"] // 2
    joined = "".join(rows)
    assert "▀" in joined
    assert "\x1b[38;2;" in joined
    palette = "".join(mascot.block_rows(frame, full=False))
    assert "\x1b[38;5;" in palette
    assert "\x1b[38;2;" not in palette


def test_no_mascot_without_a_terminal_or_when_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    assert mascot.style() == "off"  # pytest is not a terminal
    assert mascot.show(["hi"]) is False
    monkeypatch.setattr(mascot.ui, "is_interactive", lambda: True)
    monkeypatch.setattr(mascot.ui, "use_color", lambda: True)
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    monkeypatch.delenv("TMUX", raising=False)
    assert mascot.style() == "blocks"
    monkeypatch.setenv(mascot.STYLE_ENV, "off")
    assert mascot.style() == "off"
    monkeypatch.setattr(mascot.ui, "use_color", lambda: False)  # NO_COLOR
    monkeypatch.delenv(mascot.STYLE_ENV)
    assert mascot.style() == "off"


@pytest.mark.parametrize(
    ("env", "protocol"),
    [
        ({"TERM_PROGRAM": "iTerm.app"}, "iterm"),
        ({"TERM_PROGRAM": "WezTerm"}, "iterm"),
        ({"TERM": "xterm-kitty"}, "kitty"),
        ({"TERM_PROGRAM": "ghostty"}, "kitty"),
        ({"TERM_PROGRAM": "iTerm.app", "TMUX": "/tmp/tmux-1/default,1,0"}, None),  # tmux would mangle it
        ({"TERM_PROGRAM": "Apple_Terminal"}, None),
    ],
)
def test_inline_image_only_where_the_terminal_shows_it(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], protocol: str | None
) -> None:
    for name in ("TERM_PROGRAM", "TERM", "TMUX", "STY", "KITTY_WINDOW_ID"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert mascot.image_protocol() == protocol


def _capture(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    return out


def test_iterm_gets_the_real_picture_with_text_beside_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mascot, "style", lambda: "image")
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.delenv("TMUX", raising=False)
    out = _capture(monkeypatch)
    assert mascot.show(["MeetStream Agent Kit"]) is True
    text = out.getvalue()
    assert text.startswith("\x1b]1337;File=inline=1;")
    assert mascot.art()["png"][:40] in text
    assert "MeetStream Agent Kit" in text


def test_the_wave_plays_then_rests_on_the_first_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mascot, "style", lambda: "blocks")
    monkeypatch.setattr(mascot.time, "sleep", lambda _s: None)
    ticks = iter([0.0] + [0.1 * i for i in range(1, 40)])
    monkeypatch.setattr(mascot.time, "monotonic", lambda: next(ticks))
    out = _capture(monkeypatch)
    assert mascot.show(["title"], animate=True, seconds=1.0) is True
    text = out.getvalue()
    rows = mascot.art()["height"] // 2
    assert text.count(f"\x1b[{rows}F") >= 5  # redrawn frame after frame
    assert text.endswith("\x1b[?25h")  # cursor shown again
    first = "\n".join(mascot._beside(mascot.block_rows(mascot.frames("idle")[0]), ["title"]))
    assert text.rsplit(f"\x1b[{rows}F", 1)[1].startswith(first)
