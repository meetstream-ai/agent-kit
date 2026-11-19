"""Model estimates measured by MeetStream, shared with every kit.

The model picker shows a tokens-per-hour estimate for each model. Your own
measurements (``model-usage.json``) come first and never leave your computer.
For models you have not measured, the picker uses MeetStream's file:

1. ``SHARED_URL``, fetched at most once a day and cached in the kit's folder;
2. when offline or missing, the copy bundled in this package.

MeetStream makes the file with ``meetstream-agent-kit bench --compare --export
model-estimates.json`` (one short real turn per model) and publishes it.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Final

from meetstream_agent_kit import __version__
from meetstream_agent_kit.pairing import default_home
from meetstream_agent_kit.usage import Assumptions, Usage, project_hour

SHARED_URL: Final = "https://meetstream-public-images.s3.amazonaws.com/agent-kit/model-estimates.json"
CACHE_FILE: Final = "model-estimates.json"
CACHE_TTL_S: Final = 24 * 3600
FETCH_TIMEOUT_S: Final = 3.0
SCHEMA: Final = 1

Fetch = Callable[[str, float], bytes]


def _fetch(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - fixed https URL
        return bytes(response.read())


def entry(
    agent: str, model: str, turn: Usage, first_output_ms: float | None, measured_at: str
) -> dict[str, Any]:
    """One model's line in the shared file: the measured turn and its one-hour projection."""
    hour = project_hour(turn)
    return {
        "agent": agent,
        "model": model,
        "measured_at": measured_at,
        "turn": {
            "input": turn.input,
            "cached": turn.cached,
            "output": turn.output,
            "thinking": turn.thinking,
            "cost_usd": turn.cost_usd,
        },
        "per_hour": {
            "input": hour.input,
            "output": hour.output,
            "total": hour.total,
            "cost_usd": hour.cost_usd,
        },
        "first_output_ms": None if first_output_ms is None else round(first_output_ms),
        "first_output_mode": "cold",
    }


def document(entries: Sequence[dict[str, Any]]) -> dict[str, Any]:
    a = Assumptions()
    return {
        "schema": SCHEMA,
        "kit_version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "assumptions": {
            "turns_per_hour": a.turns_per_hour,
            "meeting_tokens_per_turn": a.meeting_tokens_per_turn,
            "session": "warm",
            "sample_turn": "one short meeting turn, read-only, through the CLI's one-run mode",
        },
        "estimates": list(entries),
    }


def export(path: Path, entries: Sequence[dict[str, Any]]) -> int:
    """Write or merge ``path``: new measurements replace the same agent + model. Returns the count."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for item in valid_entries(_read_json(path)):
        merged[(item["agent"], item["model"])] = item
    for item in entries:
        merged[(item["agent"], item["model"])] = item
    path.write_text(
        json.dumps(document(sorted(merged.values(), key=lambda e: (e["agent"], e["model"]))), indent=2)
        + "\n",
        encoding="utf-8",
    )
    return len(merged)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value >= 0


def valid_entries(data: Any) -> list[dict[str, Any]]:
    """The well-formed entries of a shared file; anything odd is skipped, never trusted."""
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return []
    found = []
    for item in data.get("estimates") or []:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("agent"), str)
            or not isinstance(item.get("model"), str)
        ):
            continue
        turn = item.get("turn")
        if not isinstance(turn, dict) or not all(
            _number(turn.get(k)) for k in ("input", "cached", "output", "thinking")
        ):
            continue
        if turn.get("cost_usd") is not None and not _number(turn["cost_usd"]):
            continue
        if item.get("first_output_ms") is not None and not _number(item["first_output_ms"]):
            continue
        if not isinstance(item.get("measured_at"), str):
            continue
        found.append(item)
    return found


def bundled() -> list[dict[str, Any]]:
    try:
        text = (
            resources.files("meetstream_agent_kit")
            .joinpath("data")
            .joinpath(CACHE_FILE)
            .read_text(encoding="utf-8")
        )
        return valid_entries(json.loads(text))
    except (OSError, ValueError, ModuleNotFoundError):
        return []


def load(
    *, fetch: Fetch | None = None, now: Callable[[], float] = time.time, home: Path | None = None
) -> list[dict[str, Any]]:
    """MeetStream's estimates: the cached copy (under a day old), else the URL, else the bundled copy."""
    cache = (home or default_home()) / CACHE_FILE
    try:
        fresh = now() - cache.stat().st_mtime < CACHE_TTL_S
    except OSError:
        fresh = False
    if fresh:
        cached = valid_entries(_read_json(cache))
        if cached:
            return cached
    try:
        raw = (fetch or _fetch)(SHARED_URL, FETCH_TIMEOUT_S)
        fetched = valid_entries(json.loads(raw.decode("utf-8")))
    except (OSError, ValueError, urllib.error.URLError):
        fetched = []
    if fetched:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(raw)
        except OSError:
            pass
        return fetched
    stale = valid_entries(_read_json(cache))
    return stale or bundled()


def for_agent(agent: str, entries: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["model"]: item for item in entries if item["agent"] == agent}
