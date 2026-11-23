"""The interactive prompts, on prompt_toolkit: menus, multi-select, text and yes/no.

Each prompt is a small inline prompt_toolkit application: it redraws in place
without flicker, survives terminal resizes, shows a key-hint footer, and goes
back a step with Esc (and ← in menus) wherever going back is allowed. When the
prompt is answered it folds into one answered line.

Used only at a terminal. Without one, or when prompt_toolkit is not available
(the single-file ``.pyz`` carries no dependencies), the plain and built-in
prompts in :mod:`meetstream_agent_kit.ui` are used instead.
"""

from __future__ import annotations

import os
import re
import textwrap
from collections.abc import Callable, Sequence
from typing import Any

try:  # prompt_toolkit is a dependency of the package, but the .pyz has none
    from prompt_toolkit.application import Application, get_app
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.formatted_text import ANSI, to_formatted_text
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    from prompt_toolkit.styles import Style

    AVAILABLE = True
    # Menus are small and inline; they need no cursor-position report. Terminals that never
    # answer one (some IDE and CI terminals) would otherwise get a WARNING line mid-setup.
    os.environ.setdefault("PROMPT_TOOLKIT_NO_CPR", "1")
except ImportError:  # pragma: no cover - exercised by the .pyz
    AVAILABLE = False

POINTER = "\u276f"
CARET = "\u203a"
BACK = object()
"""Returned when the user goes back (Esc, or ← in a menu)."""

_STYLE = {
    "question": "bold",
    "mark": "#22d3ee bold",
    "pointer": "#22d3ee bold",
    "current": "#22d3ee bold",
    "hint": "#8b949e",
    "detail": "#8b949e italic",
    "footer": "#6e7681",
    "ticked": "#4ade80",
    "unticked": "#6e7681",
}


_SGR = re.compile(r"\x1b\[[0-9;]*m")


def _visible(text: str) -> int:
    return len(_SGR.sub("", text))


def _styled(text: str, style: str = "") -> Any:
    """Text that may carry ANSI colours (the model rows' bars and chips) as prompt_toolkit fragments."""
    return to_formatted_text(ANSI(text), style=style) if "\x1b" in text else [(style, text)]


def _detail_line(text: str) -> Any:
    """The detail under a menu, wrapped at word boundaries to the terminal's current width."""
    if "\x1b" in text:
        return _styled("  " + text, "class:detail")
    columns = get_app().output.get_size().columns
    lines = textwrap.wrap(text, max(20, columns - 1), initial_indent="  ", subsequent_indent="  ")
    return [("class:detail", "\n".join(lines))]


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _visible(text))


def _footer(back: bool, extra: str = "") -> Any:
    keys = ["↑↓ move", extra or "Enter pick"]
    if back:
        keys.append("Esc back")
    return [("class:footer", "  " + " · ".join(keys))]


def _title(question: str) -> Any:
    return [("class:mark", "? "), ("class:question", question)]


def _run(app: Any) -> object:
    result: object = app.run()
    return result


def select(
    question: str,
    options: Sequence[str],
    default: int = 0,
    hints: Sequence[str] = (),
    *,
    detail: Callable[[int], str] | None = None,
    back: bool = False,
    **io: Any,
) -> int | object:
    """One option; returns its index, or BACK."""
    state = {"current": max(0, min(default, len(options) - 1))}
    hint_list = list(hints) + [""] * (len(options) - len(hints))
    width = max((_visible(o) for o in options), default=0)

    def rows() -> Any:
        out: list[Any] = []
        for index, option in enumerate(options):
            here = index == state["current"]
            out.append(("class:pointer", f"  {POINTER} " if here else "    "))
            out += _styled(
                _pad(option, width) if hint_list[index] else option, "class:current" if here else ""
            )
            if hint_list[index]:
                out += _styled("  " + hint_list[index], "class:hint")
            out.append(("", "\n"))
        return out[:-1]

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _up(_event: Any) -> None:
        state["current"] = (state["current"] - 1) % len(options)

    @kb.add("down")
    @kb.add("j")
    def _down(_event: Any) -> None:
        state["current"] = (state["current"] + 1) % len(options)

    for digit in range(1, min(len(options), 9) + 1):

        @kb.add(str(digit))
        def _jump(_event: Any, index: int = digit - 1) -> None:
            state["current"] = index

    @kb.add("enter")
    def _pick(event: Any) -> None:
        event.app.exit(result=state["current"])

    @kb.add("c-c")
    def _interrupt(event: Any) -> None:
        event.app.exit(exception=KeyboardInterrupt)

    if back:

        @kb.add("escape", eager=True)
        @kb.add("left")
        def _back(event: Any) -> None:
            event.app.exit(result=BACK)

    parts: list[Any] = [
        Window(FormattedTextControl(_title(question)), height=1),
        Window(FormattedTextControl(rows, focusable=True), height=len(options), always_hide_cursor=True),
    ]
    if detail is not None:
        parts.append(
            Window(
                FormattedTextControl(lambda: _detail_line(detail(state["current"]))),
                wrap_lines=True,  # a long detail wraps instead of being cut at the edge
            )
        )
    parts.append(Window(FormattedTextControl(_footer(back)), height=1))
    app: Any = Application(
        layout=Layout(HSplit(parts)),
        key_bindings=kb,
        style=Style.from_dict(_STYLE),
        erase_when_done=True,
        **io,  # tests pass a pipe input and a dummy output
        mouse_support=False,
    )
    return _run(app)


def select_many(
    question: str,
    options: Sequence[str],
    chosen: Sequence[int] = (),
    hints: Sequence[str] = (),
    *,
    back: bool = False,
    **io: Any,
) -> list[int] | object:
    """Several options (Space ticks); returns the ticked indexes, or BACK."""
    state: dict[str, Any] = {"current": 0, "ticked": set(chosen)}
    hint_list = list(hints) + [""] * (len(options) - len(hints))
    width = max((_visible(o) for o in options), default=0)

    def rows() -> Any:
        out: list[Any] = []
        for index, option in enumerate(options):
            here = index == state["current"]
            on = index in state["ticked"]
            out.append(("class:pointer", f"  {POINTER} " if here else "    "))
            out.append(("class:ticked" if on else "class:unticked", "◉ " if on else "◯ "))
            out += _styled(_pad(option, width), "class:current" if here else "")
            if hint_list[index]:
                out += _styled("  " + hint_list[index], "class:hint")
            out.append(("", "\n"))
        return out[:-1]

    kb = KeyBindings()

    @kb.add("up")
    def _up(_event: Any) -> None:
        state["current"] = (state["current"] - 1) % len(options)

    @kb.add("down")
    def _down(_event: Any) -> None:
        state["current"] = (state["current"] + 1) % len(options)

    @kb.add("space")
    def _tick(_event: Any) -> None:
        state["ticked"] ^= {state["current"]}

    @kb.add("enter")
    def _done(event: Any) -> None:
        event.app.exit(result=sorted(state["ticked"]))

    @kb.add("c-c")
    def _interrupt(event: Any) -> None:
        event.app.exit(exception=KeyboardInterrupt)

    if back:

        @kb.add("escape", eager=True)
        def _back(event: Any) -> None:
            event.app.exit(result=BACK)

    app: Any = Application(
        layout=Layout(
            HSplit(
                [
                    Window(
                        FormattedTextControl([("class:mark", "? "), ("class:question", question)]), height=1
                    ),
                    Window(
                        FormattedTextControl(rows, focusable=True),
                        height=len(options),
                        always_hide_cursor=True,
                    ),
                    Window(FormattedTextControl(_footer(back, "Space tick · Enter done")), height=1),
                ]
            )
        ),
        key_bindings=kb,
        style=Style.from_dict(_STYLE),
        erase_when_done=True,
        **io,  # tests pass a pipe input and a dummy output
    )
    return _run(app)


def text(question: str, default: str = "", *, back: bool = False, **io: Any) -> str | object:
    """A line of text, prefilled with ``default`` (editable); returns it, or BACK on Esc."""
    buffer = Buffer(document=None, multiline=False)
    buffer.text = default
    buffer.cursor_position = len(default)
    kb = KeyBindings()

    @kb.add("enter")
    def _done(event: Any) -> None:
        event.app.exit(result=buffer.text)

    @kb.add("c-c")
    def _interrupt(event: Any) -> None:
        event.app.exit(exception=KeyboardInterrupt)

    if back:

        @kb.add("escape", eager=True)
        def _back(event: Any) -> None:
            event.app.exit(result=BACK)

    footer: Any = [("class:footer", "  Enter accept" + (" · Esc back" if back else ""))]
    app: Any = Application(
        layout=Layout(
            HSplit(
                [
                    Window(
                        FormattedTextControl([("class:mark", "? "), ("class:question", question)]), height=1
                    ),
                    Window(
                        BufferControl(buffer=buffer),
                        height=1,
                        get_line_prefix=lambda *_: [("class:pointer", f"  {CARET} ")],
                    ),
                    Window(FormattedTextControl(footer), height=1),
                ]
            )
        ),
        key_bindings=kb,
        style=Style.from_dict(_STYLE),
        erase_when_done=True,
        **io,  # tests pass a pipe input and a dummy output
    )
    return _run(app)


def confirm(question: str, default: bool = True, *, back: bool = False, **io: Any) -> bool | object:
    """Yes or no as a two-option menu; returns True/False, or BACK."""
    result = select(
        question,
        ["Yes", "No"],
        0 if default else 1,
        back=back,
        **io,
    )
    if result is BACK:
        return BACK
    return result == 0
