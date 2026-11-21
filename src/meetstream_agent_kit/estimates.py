"""Local measurements and the model picker's usage estimates."""

from __future__ import annotations

import contextlib
import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meetstream_agent_kit import shared_estimates, ui
from meetstream_agent_kit.agents import AgentSpec
from meetstream_agent_kit.models import ModelChoice, features
from meetstream_agent_kit.pairing import default_home
from meetstream_agent_kit.usage import Usage, bar, human_tokens, project_hour

if TYPE_CHECKING:
    from meetstream_agent_kit.compare import Measured


@dataclass(frozen=True)
class Estimate:
    usage: Usage
    seconds: float
    first_output_ms: float | None
    measured_at: str
    source: str = "measured here"
    """``measured here`` (your own runs) or ``measured by MeetStream <date>`` (the shared file)."""


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _number(value: object) -> bool:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def load(agent: str) -> dict[str, Estimate]:
    """Read this agent's valid measurements; a broken cache is harmless."""
    estimates: dict[str, Estimate] = {}
    for key, item in _read(default_home() / "model-usage.json").items():
        if not key.startswith(agent + ":") or not isinstance(item, dict):
            continue
        if any(
            type(item.get(k)) is not int or item[k] < 0 for k in ("input", "cached", "output", "thinking")
        ):
            continue
        if not _number(item.get("seconds")):
            continue
        if any(item.get(k) is not None and not _number(item[k]) for k in ("cost_usd", "first_output_ms")):
            continue
        stamp = item.get("measured_at")
        if not isinstance(stamp, str):
            continue
        try:
            datetime.fromisoformat(stamp)
        except ValueError:
            continue
        estimates[key[len(agent) + 1 :]] = Estimate(
            Usage(item["input"], item["cached"], item["output"], item["thinking"], item.get("cost_usd")),
            item["seconds"],
            item.get("first_output_ms"),
            stamp,
        )
    return estimates


def load_with_shared(agent: str, shared: list[dict[str, Any]] | None = None) -> dict[str, Estimate]:
    """Your own measurements, then MeetStream's shared estimates for models you have not measured."""
    found = load(agent)
    entries = shared_estimates.load() if shared is None else shared
    for model, item in shared_estimates.for_agent(agent, entries).items():
        if model in found:
            continue
        turn = item["turn"]
        found[model] = Estimate(
            Usage(
                int(turn["input"]),
                int(turn["cached"]),
                int(turn["output"]),
                int(turn["thinking"]),
                turn.get("cost_usd"),
            ),
            0.0,
            item.get("first_output_ms"),
            item["measured_at"],
            f"measured by MeetStream {item['measured_at'][:10]}",
        )
    return found


def save(agent: str, model: str, measured: Measured) -> None:
    """Atomically keep a successful measurement, with owner-only permissions."""
    if measured.usage is None or measured.error:
        return
    temporary: Path | None = None
    try:
        path = default_home() / "model-usage.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _read(path)
        data[f"{agent}:{model}"] = {
            **asdict(measured.usage),
            "seconds": measured.seconds,
            "first_output_ms": measured.first_output_ms,
            "measured_at": datetime.now(timezone.utc).isoformat(),
        }
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle, allow_nan=False)
        temporary.replace(path)
    except (OSError, ValueError):
        pass
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)


def row_text(
    agent: AgentSpec, model: ModelChoice, estimate: Estimate | None, largest: int, *, name_width: int = 24
) -> str:
    """A clipped usage bar, feature chips and cold first-output time."""
    hour = project_hour(estimate.usage) if estimate else None
    filled = bar(hour.total, largest, 10) if hour else ""
    usage = f"{human_tokens(hour.total)}/h" if hour else "not measured"
    if hour and hour.cost_usd is not None:
        usage += f" ${hour.cost_usd:.2f}/h"
    chips = []
    for feature in features(agent, model):
        label = (
            feature.name
            if feature.available
            else (f"no-{feature.name}" if feature.available is False else f"{feature.name}?")
        )
        style = "green" if feature.available else "dim" if feature.available is False else "yellow"
        chips.append(ui.paint(label, style))
    speed = (
        f"~{estimate.first_output_ms / 1000:.0f} s cold"
        if estimate and estimate.first_output_ms is not None
        else "—"
    )
    if estimate and estimate.source.startswith("measured by MeetStream "):
        speed += "  " + ui.paint("MS " + estimate.measured_at[5:10], "dim")  # the detail line spells it out
    tail = f"[{ui.paint(filled.ljust(10), 'cyan')}] {usage}  {' '.join(chips)}  {speed}"
    room = ui.width() - 6  # space for a two-digit menu number or the arrow
    name_width = max(8, min(name_width, room - len(ui.plain(tail)) - 2))
    name = ui.clip(model.id or "The CLI's default", name_width).ljust(name_width)
    return ui.clip(f"{name}  {tail}", room)


def detail_text(agent: AgentSpec, model: ModelChoice, estimate: Estimate | None) -> str:
    """Explain one row without colour or terminal state."""
    parts = []
    if estimate:
        hour = project_hour(estimate.usage)
        parts += [
            f"≈ {human_tokens(hour.total)} tokens/h",
            f"≈ ${hour.cost_usd:.2f}/h" if hour.cost_usd is not None else "plan usage, no per-token bill",
            f"first words ~{estimate.first_output_ms / 1000:.0f} s (cold)"
            if estimate.first_output_ms is not None
            else "first words —",
            estimate.source,
        ]
    else:
        parts.append("not measured yet")
    names = {"talk": "talk", "tools": "meeting tools", "vision": "vision", "local": "local actions"}
    descriptions = []
    for feature in features(agent, model):
        name = names[feature.name]
        if feature.available is False:
            name = f"no {name}"
        elif feature.available is None:
            name += "?"
        if feature.name == "local" and feature.available:
            name += " per access level"
        elif feature.source and feature.name == "vision":
            name += f" ({feature.source})"
        descriptions.append(name)
    parts.append(", ".join(descriptions))
    if not estimate:
        parts.append('pick "Measure live" to run one short turn per model')
    return " · ".join(parts)
