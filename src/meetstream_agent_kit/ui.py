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
import random
import select
import shutil
import sys
import threading
import time
import unicodedata
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import TracebackType
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
_rng = random.Random()  # noqa: S311 - display wording only


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


class Stages:
    """One live line, with ticks only at observed boundaries. Times are cumulative milliseconds."""

    def __init__(self, stages: Sequence[tuple[str, str]], *, events: Sequence[str] = ()) -> None:
        if not stages:
            raise ValueError("at least one stage is required")
        self.labels = tuple(stages)
        self._events = tuple(events)
        self._completed: dict[int, float] = {}
        self._current = 0
        self._failure: tuple[str, float] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._interactive = False
        self._started = 0.0

    @classmethod
    def cold(cls, agent: str, *, rng: random.Random | None = None) -> Stages:
        """Pick reproducible verbs for a cold CLI turn."""
        picker = rng if rng is not None else _rng
        waking = picker.choice(("Waking up", "Stretching", "Booting"))
        thinking = picker.choice(("Pondering", "Noodling", "Mulling it over", "Percolating", "Ruminating"))
        answering = picker.choice(("Answering", "Composing", "Spelling it out"))
        return cls(
            [(f"{waking} {agent}", f"Woke {agent}"), (thinking, "First words"), (answering, "Answered")],
            events=("output", "first_delta"),
        )

    def __enter__(self) -> Stages:
        self._started = time.monotonic()
        self._interactive = is_interactive()
        if self._interactive:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def reach(self, index: int, ms: float) -> None:
        """Complete only this stage; missing earlier boundaries stay absent."""
        if not 0 <= index < len(self.labels):
            raise IndexError(index)
        with self._lock:
            if self._failure is None and index not in self._completed:
                self._completed[index] = ms
                self._current = max(self._current, index + 1)

    def on_stage(self, event: str, ms: float) -> None:
        """Map observed events to ticks. The caller checks success before ticking the final stage."""
        if event in self._events:
            self.reach(self._events.index(event), ms)

    def fail(self, reason: str, ms: float | None = None) -> None:
        """Stop at the current stage without inventing a completion time."""
        with self._lock:
            self._failure = (reason, self._elapsed() if ms is None else ms)

    def _elapsed(self) -> float:
        return (time.monotonic() - self._started) * 1000

    def _line(self, frame: str = "") -> str:
        with self._lock:
            parts: list[tuple[str, str]] = []
            for index, ms in sorted(self._completed.items()):
                mark = "✓ " if self._interactive else ""
                parts.append((f"{mark}{self.labels[index][1]} {ms / 1000:.1f} s", "green"))
            if self._failure is not None:
                reason, ms = self._failure
                current = self.labels[min(self._current, len(self.labels) - 1)][0]
                text = (
                    f"✗ {current} {ms / 1000:.1f} s"
                    if self._interactive
                    else f"stopped at {current} after {ms / 1000:.1f} s"
                )
                parts.append((text + (f" ({reason})" if reason else ""), "red"))
            elif frame and self._current < len(self.labels):
                parts.append(
                    (f"{frame} {self.labels[self._current][0]}… {self._elapsed() / 1000:.1f} s", "cyan")
                )
        if not self._interactive:
            return "  " + " · ".join(text for text, _ in parts)
        # Clip before colouring, reserving a column so terminals never wrap.
        room = width() - 3
        line = "  "
        for index, (text, style) in enumerate(parts):
            text = (" · " if index else "") + text
            clipped = ""
            for char in text:
                cells = (
                    0
                    if unicodedata.combining(char)
                    else 2
                    if unicodedata.east_asian_width(char) in "WF"
                    else 1
                )
                if cells > room:
                    break
                clipped += char
                room -= cells
            line += paint(clipped, style)
            if clipped != text:
                break
        return line

    def _spin(self) -> None:
        for frame in itertools.cycle(_SPINNER):
            sys.stdout.write("\r\x1b[2K" + self._line(frame))
            sys.stdout.flush()
            if self._stop.wait(1 / 12):
                break

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if exc_type is not None:
            self.fail("interrupted" if exc_type is KeyboardInterrupt else "failed")
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if self._interactive:
            sys.stdout.write("\r\x1b[2K")
        _write(self._line())


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


def multi_select(
    question: str, options: Sequence[str], chosen: Sequence[int] = (), hints: Sequence[str] = ()
) -> list[int]:
    """Pick several options: ↑/↓ to move, Space to tick, Enter to finish. Needs a terminal."""
    import termios  # Unix only; callers check is_interactive() first
    import tty

    fd = sys.stdin.fileno()
    current = 0
    ticked = set(chosen)
    room = width() - 8
    hint_list = list(hints) + [""] * (len(options) - len(hints))
    pad = max(len(option) for option in options)

    def render(first: bool) -> None:
        if not first:
            sys.stdout.write(f"\x1b[{len(options)}F")
        for index, option in enumerate(options):
            box_mark = paint("◉", "green") if index in ticked else paint("◯", "dim")
            label = _clip(option.ljust(pad), room)
            hint = hint_list[index]
            extra = f"  {paint(hint, 'dim')}" if hint and len(label) + len(hint) + 2 <= room else ""
            pointer = paint(_POINTER, "cyan", "bold") if index == current else " "
            text = paint(label, "cyan", "bold") if index == current else label
            sys.stdout.write(f"\x1b[2K  {pointer} {box_mark} {text}{extra}\n")
        sys.stdout.flush()

    keys = "↑/↓ move, Space tick, Enter done"
    title = _clip(question, width() - 3)
    shown_keys = f"  {paint(keys, 'dim')}" if len(title) + len(keys) + 4 <= width() else ""
    sys.stdout.write(f"{paint('?', 'cyan', 'bold')} {paint(title, 'bold')}{shown_keys}\n")
    old = termios.tcgetattr(fd)
    sys.stdout.write("\x1b[?25l")
    try:
        tty.setcbreak(fd)
        render(True)
        while True:
            key = _read_key(fd)
            if key in ("\r", "\n", "\x04"):
                break
            if key == "\x03":
                raise KeyboardInterrupt
            if key in _UP:
                current = (current - 1) % len(options)
            elif key in _DOWN:
                current = (current + 1) % len(options)
            elif key == " ":
                ticked ^= {current}
            elif key.isdigit() and 1 <= int(key) <= len(options):
                ticked ^= {int(key) - 1}
            render(False)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\x1b[?25h")
        sys.stdout.write(f"\x1b[{len(options) + 1}F\x1b[J")
        sys.stdout.flush()
    picked = sorted(ticked)
    answered(question, ", ".join(options[i] for i in picked) or "(none)")
    return picked
