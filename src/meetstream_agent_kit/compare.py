"""Compare models by token use: one real, short meeting turn per model, projected to an hour.

Used by setup ("Measure live") and ``meetstream-agent-kit bench --compare``.
Each model answers the same small meeting turn, read-only, through the CLI's
one-run mode. The CLI's own usage numbers become a one-hour estimate (see
:mod:`meetstream_agent_kit.usage` for the assumption) and a bar chart.
"""

from __future__ import annotations

import tempfile
import textwrap
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from meetstream_agent_kit import estimates, ui
from meetstream_agent_kit.config import BridgeConfig
from meetstream_agent_kit.oneshot import RunResult, build_oneshot_argv, run_once
from meetstream_agent_kit.prompts import format_turn, system_prompt
from meetstream_agent_kit.usage import (
    Assumptions,
    HourEstimate,
    Usage,
    bar,
    human_tokens,
    project_hour,
    usage_from_lines,
)

SAMPLE_TURN = [
    {"speaker": "Priya", "text": "Okay, next item is the release."},
    {"speaker": "Sam", "text": "We're mostly done, two bugs left."},
    {"speaker": "{owner}", "text": "Agent, in one sentence: are we on track to ship this week?"},
]
COMPARE_TIMEOUT_S = 120.0

_PLAN_NOTE = "plan usage, no per-token bill"


@dataclass(frozen=True)
class Measured:
    model: str
    usage: Usage | None
    seconds: float
    error: str = ""
    first_output_ms: float | None = None


def measure(config: BridgeConfig, model: str, effort: str) -> Measured:
    """One read-only sample turn with ``model``; the usage the CLI reports for it."""
    owner = config.owner or "the user"
    turn = format_turn([{**m, "speaker": m["speaker"].replace("{owner}", owner)} for m in SAMPLE_TURN])
    argv, stdin_text = build_oneshot_argv(
        config.agent,
        system_prompt(owner, "Be brief.") + "\n\n" + turn,
        "readonly",
        config.binary,
        model,
        effort,
        config.ignore_rules,
    )
    report: Path | None = None
    if config.agent.usage_file_flag:
        report = Path(tempfile.mkstemp(prefix="msak-usage-", suffix=".json")[1])
        argv += [config.agent.usage_file_flag, str(report)]
    try:
        with ui.Stages.cold(model or config.agent.label) as stages:
            result: RunResult = run_once(
                config.agent,
                argv,
                stdin_text,
                str(config.workdir),
                min(config.timeout_s, COMPARE_TIMEOUT_S),
                on_stage=stages.on_stage,
            )
            if result.error or not result.answer:
                stages.fail("failed", result.total_ms)
            else:
                stages.reach(2, result.total_ms)
        usage = result.usage
        if report is not None:
            usage = usage_from_lines([report.read_text(encoding="utf-8").replace("\n", " ")]) or usage
    except OSError as exc:
        return Measured(model, None, 0.0, str(exc))
    finally:
        if report is not None:
            report.unlink(missing_ok=True)
    error = result.error or ("" if result.answer else "the CLI returned no answer")
    # An answer without token counts is not a failure: some CLIs (Copilot) never report them.
    measured = Measured(model, usage, result.total_ms / 1000, error, result.first_output_ms)
    estimates.save(config.agent.name, model, measured)
    return measured


def compare(
    config: BridgeConfig,
    models: Sequence[str],
    effort: str,
    *,
    run: Callable[[BridgeConfig, str, str], Measured] = measure,
) -> list[Measured]:
    """Measure each model in turn, with a line per model as it finishes."""
    results = []
    for model in models:
        measured = run(config, model, effort)
        estimates.save(config.agent.name, model, measured)
        if measured.usage and not measured.error:
            ui.ok(
                f"{model or 'default'}  {human_tokens(measured.usage.input)} in · "
                f"{human_tokens(measured.usage.output)} out  ({measured.seconds:.1f} s)"
            )
        elif not measured.error:
            ui.ok(
                f"{model or 'default'} answered in {measured.seconds:.1f} s · "
                f"{config.agent.label} doesn't report token usage"
            )
        else:
            ui.fail(f"{model or 'default'}: {measured.error[:160]}")
        results.append(measured)
    return results


def chart_lines(
    results: Sequence[Measured],
    assumptions: Assumptions | None = None,
    *,
    selected: str = "",
    width: int = 0,
) -> list[str]:
    """The estimate as horizontal bars: model, tokens per hour, and cost where the CLI gives a price."""
    a = assumptions or Assumptions()
    rows: list[tuple[str, HourEstimate, float]] = [
        (m.model, project_hour(m.usage, a), m.seconds) for m in results if m.usage and not m.error
    ]
    if not rows:
        if any(m.usage is None and not m.error for m in results):
            return ["  This CLI doesn't report token usage, so there is no usage to chart (times are above)."]
        return ["  No model reported token usage, so there is nothing to chart."]
    name_w = min(32, max(len(name or "default") for name, _, _ in rows))
    largest = max(estimate.total for _, estimate, _ in rows)
    costs = [f"≈ ${e.cost_usd:.2f}/h" if e.cost_usd is not None else _PLAN_NOTE for _, e, _ in rows]
    if not width:  # fit the terminal: everything but the bar is fixed width
        fixed = name_w + 27 + max(len(c) for c in costs)  # " ★ name  ▕bar▏  123k tok/h  12 s  cost"
        width = max(8, min(28, ui.width() - fixed - 1))
    lines = [
        ui.paint("Estimated tokens for one hour of meeting", "bold") + ui.paint("  (an estimate)", "dim"),
        "",
    ]
    for (name, estimate, seconds), cost in zip(rows, costs, strict=True):
        label = (name or "default")[:name_w].ljust(name_w)
        mark = ui.paint("★", "orange") if name == selected else " "
        filled = bar(estimate.total, largest, width)
        lines.append(
            f" {mark} {label}  {ui.paint(filled.ljust(width), 'cyan')}  "
            f"{human_tokens(estimate.total).rjust(6)} tok/h  {f'{seconds:.0f} s'.rjust(4)}  "
            f"{ui.paint(cost, 'dim')}"
        )
    notes = [
        f"Assumes {a.describe()}.",
        "Measured from one real short turn per model on this computer. The seconds are that turn, "
        "started cold; warm meeting turns answer faster. Real meetings vary.",
        "Prices are the CLI's own estimate where it reports one (API rates); on a subscription the same "
        "tokens count against your plan instead.",
    ]
    lines.append("")
    for note in notes:
        lines += [
            ui.paint(line, "dim")
            for line in textwrap.wrap(
                note, max(40, ui.width() - 4), initial_indent="  ", subsequent_indent="  "
            )
        ]
    return lines
