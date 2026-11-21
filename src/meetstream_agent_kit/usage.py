"""Token usage the agent CLIs report, and an estimate for one hour of meeting.

Each CLI reports usage in its own words (read from real runs, 2026-11-18):

* Claude Code ``result``: ``input_tokens`` + ``cache_creation_input_tokens`` +
  ``cache_read_input_tokens``, ``output_tokens`` (thinking in
  ``output_tokens_details``), and ``total_cost_usd``.
* Codex ``turn.completed``: ``input_tokens`` (cached included),
  ``cached_input_tokens``, ``output_tokens``, ``reasoning_output_tokens``.
* Antigravity ``result.usage``: ``input_tokens``, ``output_tokens``,
  ``thinking_tokens``, ``cache_read_tokens``.
* Hermes ``--usage-file``: ``input_tokens``, ``output_tokens``,
  ``reasoning_tokens``, ``cache_read_tokens``, ``estimated_cost_usd``.

Thinking tokens are counted inside output by every CLI above.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

_THINKING_KEYS: Final = ("thinking_tokens", "reasoning_output_tokens", "reasoning_tokens")
_CACHED_KEYS: Final = ("cache_read_input_tokens", "cached_input_tokens", "cache_read_tokens")


@dataclass(frozen=True)
class Usage:
    input: int = 0
    """All input tokens of the turn, cached ones included."""
    cached: int = 0
    output: int = 0
    """Output tokens, thinking included."""
    thinking: int = 0
    cost_usd: float | None = None
    """The CLI's own price for the turn, when it reports one."""

    @property
    def total(self) -> int:
        return self.input + self.output


def _int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def usage_from(event: dict[str, Any]) -> Usage | None:
    """Usage in one JSON object: ``usage``, ``result.usage``, or a top-level usage report."""
    block = _as_dict(event.get("usage")) or _as_dict(_as_dict(event.get("result")).get("usage"))
    if not block and "input_tokens" in event and "output_tokens" in event:
        block = event  # Hermes' --usage-file report
    if not block or "input_tokens" not in block:
        return None
    total_input = _int(block.get("input_tokens"))
    if "cache_creation_input_tokens" in block:  # Anthropic-style: cache counts sit beside input_tokens
        total_input += _int(block.get("cache_creation_input_tokens")) + _int(
            block.get("cache_read_input_tokens")
        )
    thinking = next((_int(block[k]) for k in _THINKING_KEYS if k in block), 0)
    thinking = thinking or _int(_as_dict(block.get("output_tokens_details")).get("thinking_tokens"))
    cost = event.get("total_cost_usd", event.get("estimated_cost_usd", block.get("estimated_cost_usd")))
    return Usage(
        input=total_input,
        cached=next((_int(block[k]) for k in _CACHED_KEYS if k in block), 0),
        output=_int(block.get("output_tokens")),
        thinking=thinking,
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
    )


def usage_from_lines(lines: Iterable[str]) -> Usage | None:
    """The last usage a CLI printed (its final ``result`` or ``turn.completed``)."""
    found: Usage | None = None
    for line in lines:
        if "usage" not in line and "input_tokens" not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            found = usage_from(event) or found
    return found


# ── One hour of meeting ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Assumptions:
    turns_per_hour: int = 20
    """Turns your agent answers in an hour: one every three minutes."""
    meeting_tokens_per_turn: int = 120
    """What MIA sends each turn: the new lines people said, with speaker names."""

    def describe(self) -> str:
        return (
            f"{self.turns_per_hour} answered turns an hour, one warm session; each turn adds "
            f"~{self.meeting_tokens_per_turn} tokens of meeting text plus the answer to the context"
        )


@dataclass(frozen=True)
class HourEstimate:
    input: int
    output: int
    cost_usd: float | None

    @property
    def total(self) -> int:
        return self.input + self.output


def project_hour(turn: Usage, assumptions: Assumptions | None = None) -> HourEstimate:
    """One measured turn, grown to an hour: turn k re-reads the first turn's input plus k turns of history."""
    a = assumptions or Assumptions()
    turns = a.turns_per_hour
    growth = a.meeting_tokens_per_turn + turn.output
    total_input = turns * turn.input + growth * turns * (turns - 1) // 2
    total_output = turns * turn.output
    cost = None
    if turn.cost_usd is not None and turn.total:
        cost = turn.cost_usd * (total_input + total_output) / turn.total
    return HourEstimate(total_input, total_output, cost)


def human_tokens(count: float) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}k" if count >= 10_000 else f"{count / 1_000:.1f}k"
    return str(int(count))


_EIGHTHS: Final = " ▏▎▍▌▋▊▉"


def bar(value: float, largest: float, width: int) -> str:
    """A horizontal bar in eighths of a cell."""
    if largest <= 0 or value <= 0:
        return ""
    eighths = max(1, round(value / largest * width * 8))
    full, rest = divmod(eighths, 8)
    return "█" * full + (_EIGHTHS[rest] if rest else "")
