"""Read one line of an agent CLI's output.

Each parser turns a stdout line into a :class:`ParsedLine`: a text delta, the
final answer, or an error, whichever the line carries.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Final, NamedTuple

from meetstream_agent_kit.agents import ParserName


class ParsedLine(NamedTuple):
    delta: str | None = None
    final: str | None = None
    error: str | None = None


_NOTHING: Final = ParsedLine()

_AUTH_ERROR_RE: Final = re.compile(
    r"(failed to authenticate|oauth|not logged in|login expired|authentication required"
    r"|log ?in required|please (log|sign) ?in|authentication (failed|required|expired)"
    r"|session expired|unauthori[sz]ed|invalid api key|credentials? (expired|missing|not found))",
    re.IGNORECASE,
)


def looks_like_auth_error(text: str) -> bool:
    return bool(_AUTH_ERROR_RE.search(text or ""))


def classify_error(message: str) -> str:
    """``auth_required`` when the message says the CLI is signed out, else ``agent_error``."""
    return "auth_required" if looks_like_auth_error(message) else "agent_error"


def _json_object(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def parse_claude(line: str) -> ParsedLine:
    """Claude Code ``--output-format stream-json`` (with partial messages)."""
    event = _json_object(line)
    if event is None:
        return _NOTHING
    kind = event.get("type")
    if kind == "stream_event":
        inner = event.get("event") or {}
        delta = inner.get("delta") or {}
        if inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
            return ParsedLine(delta=str(delta.get("text") or ""))
        return _NOTHING
    if kind == "result":
        if event.get("is_error"):
            return ParsedLine(error=str(event.get("result") or event.get("subtype") or "agent error"))
        return ParsedLine(final=str(event.get("result") or ""))
    return _NOTHING


def parse_codex(line: str) -> ParsedLine:
    """``codex exec --json``."""
    event = _json_object(line)
    if event is None:
        return _NOTHING
    kind = event.get("type")
    item = event.get("item") or {}
    if kind == "item.completed" and item.get("type") in ("agent_message", "assistant_message"):
        return ParsedLine(delta=str(item.get("text") or ""))
    if kind in ("turn.failed", "error"):
        err = event.get("error") or {}
        return ParsedLine(error=str(err.get("message") or event.get("message") or "agent error"))
    return _NOTHING


def parse_agy(line: str) -> ParsedLine:
    """Antigravity ``--output-format stream-json``."""
    event = _json_object(line)
    if event is None:
        return _NOTHING
    kind = event.get("type")
    if kind == "step_update":
        step = event.get("step_update") or event
        if step.get("step_type") == "agent_response" and step.get("text_delta"):
            return ParsedLine(delta=str(step["text_delta"]))
        return _NOTHING
    if kind == "result":
        raw = event.get("result")
        result: dict[str, Any] = raw if isinstance(raw, dict) else event
        if str(result.get("status") or "").lower() in ("error", "failed", "failure"):
            return ParsedLine(error=str(result.get("error") or result.get("response") or "agent error"))
        return ParsedLine(final=str(result.get("response") or ""))
    return _NOTHING


def parse_text(line: str) -> ParsedLine:
    """Plain text: every line is part of the answer."""
    return ParsedLine(delta=line)


PARSERS: Final[dict[ParserName, Callable[[str], ParsedLine]]] = {
    "claude_stream_json": parse_claude,
    "codex_jsonl": parse_codex,
    "agy_stream_json": parse_agy,
    "text": parse_text,
}
