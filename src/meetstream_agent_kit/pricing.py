"""What an hour of meeting would cost at the vendors' API rates, and whether the CLI is on a plan.

Most agent CLIs here run on a subscription or a free plan (Antigravity with a
Google account, Claude Code with a Claude plan, Codex with ChatGPT, Copilot).
Those tokens count against the plan; nothing is billed per token. The picker
still shows an API-equivalent price so models can be compared: the vendors'
published per-token list prices (standard tier, USD per million tokens) from a
small dated table.

The table lives in MeetStream's shared estimates file (``prices``), so the
weekly refresh can update it without a release; the bundled copy is the
fallback. Prices are estimates: the hour is projected from one measured turn
(see :mod:`meetstream_agent_kit.usage`), the measured turn's cached share is
assumed for the hour, and cache writes, tiers and discounts are left out.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Final

from meetstream_agent_kit.usage import HourEstimate, Usage

PLAN: Final = "plan"
"""Signed in on a subscription or free plan: no per-token bill."""
FREE: Final = "free"
"""A free model (an OpenRouter ``:free`` id)."""
API: Final = "api"
"""Billed per token with an API key."""


_VARIANTS: Final = frozenset({"lite", "mini", "nano", "tts", "image", "live", "audio", "embedding"})
"""A suffix that names another, cheaper model ("flash-lite"), not an effort level ("flash-high")."""


@dataclass(frozen=True)
class Price:
    match: str
    vendor: str
    input: float
    cached: float
    output: float
    """USD per million tokens."""
    as_of: str
    note: str = ""


def normalize(model: str) -> str:
    """``nous:anthropic/claude-sonnet-4.6`` or ``claude-sonnet-4.6:free`` -> ``claude-sonnet-4-6``."""
    parts = model.strip().lower().rsplit("/", 1)[-1].split(":")
    while len(parts) > 1 and parts[-1] in _TAGS:
        parts.pop()  # ":free", ":thinking"
    name = parts[1] if len(parts) > 1 else parts[0]  # "opencode-go:gpt-6-luna": Hermes' provider:model
    return name.replace(".", "-").replace("_", "-")


_TAGS: Final = frozenset({"free", "thinking", "beta", "nitro", "floor", "online", "extended", "exacto"})


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value >= 0


def table(document: Any) -> list[Price]:
    """The well-formed rows of a document's ``prices`` block; anything odd is skipped."""
    block = document.get("prices") if isinstance(document, dict) else None
    if not isinstance(block, dict) or not isinstance(block.get("as_of"), str):
        return []
    found = []
    for row in block.get("models") or []:
        if not isinstance(row, dict) or not isinstance(row.get("match"), str) or not row["match"].strip():
            continue
        if not all(_number(row.get(k)) for k in ("input", "cached", "output")):
            continue
        found.append(
            Price(
                normalize(row["match"]),
                str(row.get("vendor") or ""),
                float(row["input"]),
                float(row["cached"]),
                float(row["output"]),
                block["as_of"],
                str(row.get("note") or ""),
            )
        )
    return found


@cache
def load() -> tuple[Price, ...]:
    """The cached shared file's prices (the picker fetched it moments ago), else the bundled ones."""
    from meetstream_agent_kit import shared_estimates  # it imports this module for export

    cached = shared_estimates.cached_document()
    found = table(cached) or table(shared_estimates.bundled_document())
    return tuple(found)


def price_for(model: str, prices: Sequence[Price]) -> Price | None:
    """The longest table entry that ``model`` starts with, at a word boundary."""
    name = normalize(model)
    best: Price | None = None
    for price in prices:
        key = price.match
        rest = name[len(key) :] if name.startswith(key) else None
        fits = rest == "" or (
            rest is not None and rest[0] in "-[" and rest.lstrip("-[").split("-")[0] not in _VARIANTS
        )
        if fits and (best is None or len(key) > len(best.match)):
            best = price
    return best


def hour_cost(hour: HourEstimate, turn: Usage, price: Price) -> float:
    """The hour at ``price``; the measured turn's cached share of input applies to the hour."""
    share = min(1.0, turn.cached / turn.input) if turn.input else 0.0
    fresh = hour.input * (1 - share)
    cached = hour.input * share
    return (fresh * price.input + cached * price.cached + hour.output * price.output) / 1_000_000


def plan_kind(
    agent: str, model: str = "", *, env: Mapping[str, str] | None = None, home: Path | None = None
) -> str:
    """``plan``, ``free``, ``api`` or "" (unknown), from local sign-in files only; no secret is read."""
    environ = os.environ if env is None else env
    root = home or Path.home()
    if model.lower().endswith(":free"):
        return FREE
    if agent in ("agy", "copilot"):
        return PLAN  # a Google account / a Copilot subscription; neither bills per token
    if agent == "claude":
        if environ.get("ANTHROPIC_API_KEY"):
            return API
        return PLAN if isinstance(_read(root / ".claude.json").get("oauthAccount"), dict) else ""
    if agent == "codex":
        auth = _read(root / ".codex" / "auth.json")
        if auth.get("OPENAI_API_KEY"):
            return API
        return PLAN if isinstance(auth.get("tokens"), dict) else ""
    return ""


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def money(value: float) -> str:
    return f"${value:.2f}" if value >= 0.01 or value == 0 else "<$0.01"
