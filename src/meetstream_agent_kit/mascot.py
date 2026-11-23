"""MIA, MeetStream's mascot, in the terminal.

The art is MeetStream's own mascot (``mia-state-gifs/mascot-v1``), pre-rendered by
``scripts/build_mascot.py`` into ``data/mascot.json``: small pixel frames per state
(idle wave, thinking, speaking, problem) and one PNG.

* Terminals that show images (iTerm2, WezTerm, kitty, Ghostty; not inside tmux)
  get the real picture.
* Other colour terminals get half-block art: two pixels per character, true
  colour where the terminal has it, else the 256-colour palette. The setup
  header plays MIA's wave.
* No terminal, ``NO_COLOR``, or ``MEETSTREAM_AGENT_KIT_MASCOT=off``: no mascot.
  ``=blocks`` or ``=image`` forces a style.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from collections.abc import Sequence
from functools import cache
from importlib import resources
from typing import Any, Final, Literal

from meetstream_agent_kit import ui

Style = Literal["off", "blocks", "image"]
STYLE_ENV: Final = "MEETSTREAM_AGENT_KIT_MASCOT"
_RESET: Final = "\x1b[0m"
_GAP: Final = 3
FPS: Final = 12.0


@cache
def art() -> dict[str, Any]:
    """The bundled frames; empty when missing (the kit then simply shows no mascot)."""
    try:
        text = (
            resources.files("meetstream_agent_kit")
            .joinpath("data")
            .joinpath("mascot.json")
            .read_text("utf-8")
        )
        data = json.loads(text)
    except (OSError, ValueError, ModuleNotFoundError):
        return {}
    return data if isinstance(data, dict) and data.get("states") else {}


def style() -> Style:
    forced = os.environ.get(STYLE_ENV, "").lower()
    if forced in ("off", "none", "0"):
        return "off"
    if not art() or not ui.is_interactive() or not ui.use_color():
        return "off"
    if forced in ("blocks", "image"):
        return forced  # type: ignore[return-value]
    return "image" if image_protocol() else "blocks"


def image_protocol() -> str | None:
    """``iterm`` or ``kitty`` when the terminal shows inline images (never through tmux/screen)."""
    if os.environ.get("TMUX") or os.environ.get("STY"):
        return None
    program = os.environ.get("TERM_PROGRAM", "")
    if program in ("iTerm.app", "WezTerm"):
        return "iterm"
    if (
        os.environ.get("TERM", "") == "xterm-kitty"
        or program == "ghostty"
        or os.environ.get("KITTY_WINDOW_ID")
    ):
        return "kitty"
    return None


def truecolor() -> bool:
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return True
    term = os.environ.get("TERM", "")
    program = os.environ.get("TERM_PROGRAM", "")
    return (
        "direct" in term or term == "xterm-kitty" or program in ("iTerm.app", "WezTerm", "ghostty", "vscode")
    )


def _cube(value: int) -> int:
    return 0 if value < 48 else 1 if value < 115 else (value - 35) // 40


def _ansi(rgb: tuple[int, int, int], *, background: bool, full: bool) -> str:
    layer = 48 if background else 38
    if full:
        return f"\x1b[{layer};2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    index = 16 + 36 * _cube(rgb[0]) + 6 * _cube(rgb[1]) + _cube(rgb[2])
    return f"\x1b[{layer};5;{index}m"


def frames(state: str = "idle") -> list[bytes]:
    data = art()
    states = data.get("states") or {}
    chosen = states.get(state) or states.get("idle") or []
    return [base64.b64decode(frame) for frame in chosen]


def block_rows(frame: bytes, *, full: bool | None = None) -> list[str]:
    """One frame as half-block rows: each character is a top and a bottom pixel."""
    data = art()
    width, height = int(data["width"]), int(data["height"])
    full = truecolor() if full is None else full
    rows = []
    for y in range(0, height, 2):
        line = ""
        for x in range(width):
            top = frame[(y * width + x) * 4 : (y * width + x) * 4 + 4]
            bottom = frame[((y + 1) * width + x) * 4 : ((y + 1) * width + x) * 4 + 4]
            top_on, bottom_on = top[3] > 0, bottom[3] > 0
            if top_on and bottom_on:
                line += _ansi((top[0], top[1], top[2]), background=False, full=full)
                line += _ansi((bottom[0], bottom[1], bottom[2]), background=True, full=full) + "▀" + _RESET
            elif top_on:
                line += _ansi((top[0], top[1], top[2]), background=False, full=full) + "▀" + _RESET
            elif bottom_on:
                line += _ansi((bottom[0], bottom[1], bottom[2]), background=False, full=full) + "▄" + _RESET
            else:
                line += " "
        rows.append(line)
    return rows


def _beside(rows: Sequence[str], text: Sequence[str]) -> list[str]:
    """Art on the left, text vertically centred on the right."""
    top = max(0, (len(rows) - len(text)) // 2)
    width = int(art().get("width", 0))
    out = []
    for index, row in enumerate(rows):
        line = text[index - top] if 0 <= index - top < len(text) else ""
        out.append(row + " " * (width - len(ui.plain(row)) + _GAP) + line)
    return out


def _image(text: Sequence[str]) -> None:
    """The real picture (iTerm2 or kitty protocol) with the text beside it."""
    data = art()
    cells_wide = int(data["width"])
    rows = int(data["height"]) // 2
    payload = str(data.get("png") or "")
    if image_protocol() == "kitty":
        chunks = [payload[i : i + 4096] for i in range(0, len(payload), 4096)] or [""]
        for index, chunk in enumerate(chunks):
            more = 1 if index < len(chunks) - 1 else 0
            head = f"a=T,f=100,c={cells_wide},r={rows},m={more}" if index == 0 else f"m={more}"
            sys.stdout.write(f"\x1b_G{head};{chunk}\x1b\\")
    else:
        sys.stdout.write(
            f"\x1b]1337;File=inline=1;width={cells_wide};height={rows};preserveAspectRatio=1:{payload}\a"
        )
    sys.stdout.write("\n" * rows if image_protocol() == "iterm" else "\n")
    sys.stdout.write(f"\x1b[{rows}A")  # back to the top of the picture, then the text beside it
    top = max(0, (rows - len(text)) // 2)
    for index in range(rows):
        line = text[index - top] if 0 <= index - top < len(text) else ""
        sys.stdout.write(f"\x1b[{cells_wide + _GAP + 1}G{line}\n")
    sys.stdout.flush()


def show(text: Sequence[str], state: str = "idle", *, animate: bool = False, seconds: float = 2.0) -> bool:
    """MIA with ``text`` beside her. False (nothing printed) when there is no mascot here."""
    chosen = style()
    if chosen == "off":
        return False
    if chosen == "image":
        _image(text)
        return True
    all_frames = frames(state)
    if not all_frames:
        return False
    first = _beside(block_rows(all_frames[0]), text)
    sys.stdout.write("\n".join(first) + "\n")
    sys.stdout.flush()
    if animate and len(all_frames) > 1:
        sys.stdout.write("\x1b[?25l")
        try:
            end = time.monotonic() + seconds
            index = 0
            while time.monotonic() < end:
                index = (index + 1) % len(all_frames)
                time.sleep(1 / FPS)
                sys.stdout.write(
                    f"\x1b[{len(first)}F" + "\n".join(_beside(block_rows(all_frames[index]), text)) + "\n"
                )
                sys.stdout.flush()
            sys.stdout.write(f"\x1b[{len(first)}F" + "\n".join(first) + "\n")  # rest on the first frame
        finally:
            sys.stdout.write("\x1b[?25h")
            sys.stdout.flush()
    return True
