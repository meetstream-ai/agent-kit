"""The setup wizard's look: colours, ✓/✗ lines, step headers, boxes, a spinner and arrow-key menus.

Standard library only. Colour and arrow keys are used only when both stdin and
stdout are terminals; piped or redirected runs get plain numbered questions and
plain lines, so scripts and logs stay readable. ``NO_COLOR`` turns colour off,
``FORCE_COLOR`` turns it on.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import select
import shutil
import sys
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final

_CODES: Final[dict[str, str]] = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "orange": "38;5;208",
}
_SPINNER: Final = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_ANSWER_MARK: Final = "\u203a"  # right-pointing quote between a question and its answer
_POINTER: Final = "\u276f"  # heavy pointer before the highlighted menu option


def is_interactive() -> bool:
    """True when a person is at the keyboard: stdin and stdout are both terminals."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def paint(text: str, *styles: str) -> str:
    if not styles or not use_color():
        return text
    codes = ";".join(_CODES[s] for s in styles)
    return f"\x1b[{codes}m{text}\x1b[0m"


def _write(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def short_path(path: str) -> str:
    """``~/code/project`` instead of ``/Users/you/code/project``, to keep lines short."""
    home = str(Path.home())
    if home and home != "/" and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home) :]
    return path


def width() -> int:
    columns = shutil.get_terminal_size((88, 24)).columns or 88  # 0 = a terminal with no size set
    return max(20, min(columns, 100))


# ── Status lines ──────────────────────────────────────────────────────────────


def ok(text: str) -> None:
    _write(f"  {paint('✓', 'green', 'bold')} {text}")


def fail(text: str) -> None:
    _write(f"  {paint('✗', 'red', 'bold')} {text}")


def warn(text: str) -> None:
    _write(f"  {paint('!', 'yellow', 'bold')} {text}")


def note(text: str) -> None:
    """A dim hint under a question or a status line."""
    _write(f"    {paint(text, 'dim')}")


def command(text: str) -> None:
    """A command for the user to run, on its own line so it is easy to copy."""
    _write(f"      {paint(text, 'cyan')}")


def header(title: str, version: str, tagline: str) -> None:
    left = f"◆ {title}"
    inner = max(len(left) + len(version) + 3, len(tagline)) + 4
    top = "╭" + "─" * inner + "╮"
    bottom = "╰" + "─" * inner + "╯"
    line1 = f"  {paint(left, 'orange', 'bold')}  {paint('v' + version, 'dim')}"
    pad1 = inner - (len(left) + len(version) + 5)
    line2 = f"  {tagline}"
    _write("")
    _write(paint(top, "dim"))
    _write(paint("│", "dim") + line1 + " " * pad1 + paint("│", "dim"))
    _write(paint("│", "dim") + line2 + " " * (inner - len(line2)) + paint("│", "dim"))
    _write(paint(bottom, "dim"))


def step(number: int, total: int, title: str) -> None:
    label = f" {number}/{total}  {title} "
    rule = "─" * max(4, min(width(), 72) - len(label) - 2)
    _write("")
    _write(paint("──", "dim") + paint(label, "bold", "cyan") + paint(rule, "dim"))


def box(title: str, rows: Sequence[tuple[str, str]]) -> None:
    """A titled box of ``label  value`` rows (no secrets: values here are not meant to be copied)."""
    key_w = max((len(k) for k, _ in rows), default=0)
    lines = [f"{k.ljust(key_w)}  {v}" for k, v in rows]
    inner = max([len(line) for line in lines] + [len(title) + 2]) + 2
    if inner + 2 > width():  # too wide for a frame: plain rows instead
        _write(paint(title, "bold"))
        for line in lines:
            _write(f"  {line}")
        return
    _write(
        paint("╭─ ", "dim") + paint(title, "bold") + paint(" " + "─" * (inner - len(title) - 3) + "╮", "dim")
    )
    for (key, value), line in zip(rows, lines, strict=True):
        styled = paint(key.ljust(key_w), "dim") + "  " + value
        _write(paint("│", "dim") + " " + styled + " " * (inner - len(line) - 1) + paint("│", "dim"))
    _write(paint("╰" + "─" * inner + "╯", "dim"))


@contextlib.contextmanager
def spinner(text: str) -> Iterator[None]:
    """Animate while a slow step runs; a plain ``text...`` line when not on a terminal."""
    if not is_interactive():
        _write(f"  {text}...")
        yield
        return
    stop = threading.Event()

    def spin() -> None:
        for frame in itertools.cycle(_SPINNER):
            sys.stdout.write(f"\r  {paint(frame, 'cyan')} {text}")
            sys.stdout.flush()
            if stop.wait(0.08):
                break

    thread = threading.Thread(target=spin, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
        sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()


# ── Questions on a terminal ──────────────────────────────────────────────────


def answered(question: str, answer: str) -> None:
    """How a question looks once it is answered."""
    _write(
        f"{paint('✔', 'green')} {paint(question, 'bold')} "
        f"{paint(_ANSWER_MARK, 'dim')} {paint(answer, 'cyan')}"
    )


def question_prompt(question: str, default: str) -> str:
    hint = f" {paint('(' + default + ')', 'dim')}" if default else ""
    return f"{paint('?', 'cyan', 'bold')} {paint(question, 'bold')}{hint} {paint(_ANSWER_MARK, 'dim')} "


_UP: Final = ("\x1b[A", "\x1bOA", "k")
_DOWN: Final = ("\x1b[B", "\x1bOB", "j")


def _read_key(fd: int) -> str:
    """One key press; an escape sequence (arrow keys) is read whole even when it arrives in pieces."""
    data = os.read(fd, 1)
    if data == b"\x1b":
        while len(data) < 8 and select.select([fd], [], [], 0.1)[0]:
            data += os.read(fd, 1)
            if len(data) > 2 and (data[-1:].isalpha() or data[-1:] == b"~"):
                break
    return data.decode("utf-8", "replace")


def _clip(text: str, room: int) -> str:
    return text if len(text) <= room else text[: max(1, room - 1)] + "…"


def arrow_select(question: str, options: Sequence[str], default: int = 0, hints: Sequence[str] = ()) -> int:
    """Pick one option with ↑/↓ (or j/k, or its number) and Enter. Needs a terminal."""
    import termios  # Unix only; callers check is_interactive() first
    import tty

    fd = sys.stdin.fileno()
    current = default
    room = width() - 6
    hint_list = list(hints) + [""] * (len(options) - len(hints))

    def render(first: bool) -> None:
        if not first:
            sys.stdout.write(f"\x1b[{len(options)}F")
        pad = max(len(option) for option in options)
        for index, option in enumerate(options):
            hint = hint_list[index]
            label = _clip(option.ljust(pad) if hint else option, room)
            extra = f"  {paint(hint, 'dim')}" if hint and len(label) + len(hint) + 2 <= room else ""
            if index == current:
                line = f"  {paint(_POINTER, 'cyan', 'bold')} {paint(label, 'cyan', 'bold')}{extra}"
            else:
                line = f"    {label}{extra}"
            sys.stdout.write("\x1b[2K" + line + "\n")
        sys.stdout.flush()

    keys = "↑/↓ to move, Enter to pick"
    title = _clip(question, width() - 3)
    shown_keys = f"  {paint(keys, 'dim')}" if len(title) + len(keys) + 4 <= width() else ""
    sys.stdout.write(f"{paint('?', 'cyan', 'bold')} {paint(title, 'bold')}{shown_keys}\n")
    old = termios.tcgetattr(fd)
    sys.stdout.write("\x1b[?25l")  # hide the cursor while the menu is up
    try:
        tty.setcbreak(fd)
        render(True)
        while True:
            key = _read_key(fd)
            if key in ("\r", "\n"):
                break
            if key == "\x03":
                raise KeyboardInterrupt
            if key == "\x04":  # Ctrl-D keeps the default, like end of input elsewhere
                current = default
                break
            if key in _UP:
                current = (current - 1) % len(options)
            elif key in _DOWN:
                current = (current + 1) % len(options)
            elif key.isdigit() and 1 <= int(key) <= len(options):
                current = int(key) - 1
            render(False)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\x1b[?25h")
        # Fold the menu into one answered line.
        sys.stdout.write(f"\x1b[{len(options) + 1}F\x1b[J")
        sys.stdout.flush()
    answered(question, options[current])
    return current
