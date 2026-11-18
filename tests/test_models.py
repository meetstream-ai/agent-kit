"""Model and effort choice, token usage, the one-hour estimate and its chart.

Listings and usage use the shapes the real CLIs printed on 2026-11-18
(agy 1.5.11, codex-cli 0.162, Claude Code 2.1.286, Hermes).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from meetstream_agent_kit.agents import AGENTS, split_provider
from meetstream_agent_kit.cli import main
from meetstream_agent_kit.compare import Measured, chart_lines
from meetstream_agent_kit.models import (
    ModelCatalog,
    ModelChoice,
    acp_catalog_from,
    agy_models_from,
    catalog_for,
    claude_catalog_from,
    cli_defaults,
    codex_catalog_from,
)
from meetstream_agent_kit.oneshot import run_once
from meetstream_agent_kit.setup import Prompter, SetupAnswers, ask_setup, load_answers, save_answers
from meetstream_agent_kit.usage import Assumptions, Usage, bar, project_hour, usage_from, usage_from_lines

from .conftest import STUB, make_session, run

AGY_SESSION_NEW = {
    "sessionId": "s",
    "models": {
        "availableModels": [
            {"modelId": "gemini-3.8-flash-high", "name": "Gemini 3.8 Flash (High)"},
            {"modelId": "gemini-3.8-flash-low", "name": "Gemini 3.8 Flash (Low)"},
            {"modelId": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6 (Thinking)"},
        ],
        "currentModelId": "gemini-3.8-flash-high",
    },
    "configOptions": [
        {
            "id": "thought_level",
            "category": "thought_level",
            "currentValue": "high",
            "options": [{"value": "low"}, {"value": "medium"}, {"value": "high"}],
        }
    ],
}
CODEX_MODEL_LIST = {
    "data": [
        {
            "id": "gpt-6.1-sol",
            "displayName": "GPT-6.1-Sol",
            "isDefault": True,
            "hidden": False,
            "defaultReasoningEffort": "medium",
            "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "medium", "high", "xhigh")],
        },
        {"id": "gpt-6-luna", "displayName": "GPT-6 Luna", "hidden": False, "supportedReasoningEfforts": []},
        {"id": "internal", "hidden": True},
    ]
}
CLAUDE_HELP = """
  --effort <level>                      Effort level for the current session
                                        (low, medium, high, xhigh, max)
  --model <model>                       Model for the current session. Provide
                                        an alias for the latest model (e.g.
                                        'fable', 'opus', or 'sonnet') or a
                                        model's full name.
"""


# ── Listings ──────────────────────────────────────────────────────────────────


def test_acp_session_new_lists_models_and_thinking_levels() -> None:
    catalog = acp_catalog_from(AGY_SESSION_NEW, "agy acp")
    assert [m.id for m in catalog.models] == [
        "gemini-3.8-flash-high",
        "gemini-3.8-flash-low",
        "claude-sonnet-4-6",
    ]
    assert catalog.default_model == "gemini-3.8-flash-high"
    assert catalog.efforts == ["low", "medium", "high"]
    assert catalog.default_effort == "high"


def test_codex_model_list_keeps_per_model_efforts_and_skips_hidden() -> None:
    catalog = codex_catalog_from(CODEX_MODEL_LIST)
    assert [m.id for m in catalog.models] == ["gpt-6.1-sol", "gpt-6-luna"]
    assert catalog.default_model == "gpt-6.1-sol"
    assert catalog.default_effort == "medium"
    assert catalog.efforts_for("gpt-6.1-sol") == ["low", "medium", "high", "xhigh"]


def test_agy_models_and_claude_help_are_read() -> None:
    text = (
        "Fetching available models...\n"
        "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n"
        "claude-sonnet-4-6\tClaude Sonnet 4.6\n"
    )
    assert agy_models_from(text) == [
        ModelChoice("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)"),
        ModelChoice("claude-sonnet-4-6", "Claude Sonnet 4.6"),
    ]
    claude = claude_catalog_from(CLAUDE_HELP)
    assert [m.id for m in claude.models] == ["fable", "opus", "sonnet"]
    assert claude.efforts == ["low", "medium", "high", "xhigh", "max"]
    assert claude.source == "from claude --help"


def test_each_clis_own_default_is_read(tmp_path: Path) -> None:
    (tmp_path / ".gemini" / "antigravity-cli").mkdir(parents=True)
    (tmp_path / ".gemini" / "antigravity-cli" / "settings.json").write_text(
        json.dumps({"model": "gemini-3.8-flash-low", "effort": "low"}), encoding="utf-8"
    )
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        'model = "gpt-6-astra"\nmodel_reasoning_effort = "high"\n\n[profiles.x]\nmodel = "other"\n',
        encoding="utf-8",
    )
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"model": "opus[1m]"}), encoding="utf-8")
    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".hermes" / "config.yaml").write_text(
        'model:\n  provider: "nous"\n  default: "upstage/solar-mini4:free"\n'
        'agent:\n  reasoning_effort: "high"\n',
        encoding="utf-8",
    )
    assert cli_defaults(AGENTS["agy"], tmp_path) == ("gemini-3.8-flash-low", "low")
    assert cli_defaults(AGENTS["codex"], tmp_path) == ("gpt-6-astra", "high")
    assert cli_defaults(AGENTS["claude"], tmp_path) == ("opus[1m]", "")
    assert cli_defaults(AGENTS["hermes"], tmp_path) == ("upstage/solar-mini4:free", "high")
    assert cli_defaults(AGENTS["copilot"], tmp_path) == ("", "")


def test_catalog_preselects_the_clis_own_default(tmp_path: Path) -> None:
    (tmp_path / ".gemini" / "antigravity-cli").mkdir(parents=True)
    (tmp_path / ".gemini" / "antigravity-cli" / "settings.json").write_text(
        json.dumps({"model": "gemini-3.8-flash-low", "effort": "low"}), encoding="utf-8"
    )
    catalog = catalog_for(
        AGENTS["agy"],
        "agy",
        home=tmp_path,
        acp=lambda _a, _b, _t: acp_catalog_from(AGY_SESSION_NEW, "agy acp"),
    )
    assert catalog.default_model == "gemini-3.8-flash-low"  # settings.json, not ACP's "current"
    assert catalog.default_effort == "low"


def test_a_cli_that_cannot_list_falls_back_without_raising(tmp_path: Path) -> None:
    def broken(_a: Any, _b: str, _t: float) -> ModelCatalog:
        raise TimeoutError("session/new did not answer within 30 s")

    def agy_models(argv: list[str], **_kw: Any) -> Any:
        import subprocess

        assert argv[1:] == ["models"]
        return subprocess.CompletedProcess(argv, 0, "gemini-3.8-flash-low\tFlash\n", "")

    catalog = catalog_for(AGENTS["agy"], "agy", home=tmp_path, acp=broken, run=agy_models)
    assert [m.id for m in catalog.models] == ["gemini-3.8-flash-low"]
    assert catalog.source == "agy models"
    assert "did not answer" in catalog.error
    empty = catalog_for(AGENTS["copilot"], "copilot", home=tmp_path, acp=broken)
    assert empty.models == []
    assert empty.error


# ── Usage, the estimate and the chart ─────────────────────────────────────────


def test_usage_in_each_clis_own_words() -> None:
    agy = {
        "event": "result",
        "result": {
            "status": "SUCCESS",
            "usage": {
                "input_tokens": 9568,
                "output_tokens": 27,
                "thinking_tokens": 26,
                "cache_read_tokens": 0,
            },
        },
    }
    assert usage_from(agy) == Usage(9568, 0, 27, 26, None)
    claude = {
        "type": "result",
        "usage": {
            "input_tokens": 10,
            "cache_creation_input_tokens": 36964,
            "cache_read_input_tokens": 100,
            "output_tokens": 33,
            "output_tokens_details": {"thinking_tokens": 26},
        },
        "total_cost_usd": 0.0741,
    }
    assert usage_from(claude) == Usage(37074, 100, 33, 26, 0.0741)
    codex = {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 16555,
            "cached_input_tokens": 13056,
            "output_tokens": 5,
            "reasoning_output_tokens": 0,
        },
    }
    assert usage_from(codex) == Usage(16555, 13056, 5, 0, None)
    hermes = {
        "estimated_cost_usd": 0.0,
        "input_tokens": 14989,
        "output_tokens": 21,
        "reasoning_tokens": 18,
        "cache_read_tokens": 0,
    }
    assert usage_from(hermes) == Usage(14989, 0, 21, 18, 0.0)
    lines = [
        "not json",
        json.dumps({"type": "assistant", "usage": {"input_tokens": 1, "output_tokens": 1}}),
        json.dumps(claude),
    ]
    assert usage_from_lines(lines) == usage_from(claude)  # the last one: the final result


def test_one_hour_projection_states_its_arithmetic() -> None:
    turn = Usage(input=10_000, output=50, cost_usd=0.02)
    hour = project_hour(turn, Assumptions(turns_per_hour=20, meeting_tokens_per_turn=120))
    # 20 turns re-read the 10k prompt; turn k also re-reads k turns of (120 + 50) tokens.
    assert hour.input == 20 * 10_000 + 170 * 20 * 19 // 2
    assert hour.output == 20 * 50
    assert hour.cost_usd == pytest.approx(0.02 * hour.total / turn.total)
    assert project_hour(Usage(100, 0, 10)).cost_usd is None


def test_chart_bars_scale_and_say_what_is_estimated() -> None:
    results = [
        Measured("low", Usage(9800, 0, 19), 4.9),
        Measured("high", Usage(9800, 0, 2500), 24.2),
        Measured("paid", Usage(9000, 0, 50, cost_usd=0.05), 5.0),
        Measured("broken", None, 0.0, "auth"),
    ]
    lines = chart_lines(results, selected="low")
    text = "\n".join(lines)
    assert "an estimate" in text
    assert "★ low" in text
    assert "broken" not in text
    high = next(line for line in lines if " high " in line)
    low = next(line for line in lines if " low " in line)
    assert high.count("█") > low.count("█")
    assert "plan usage, no per-token bill" in low
    assert "$" in next(line for line in lines if " paid " in line)
    assert "Assumes 20 answered turns an hour" in text
    assert bar(1, 8, 1) == "▏"
    assert bar(8, 8, 2) == "██"
    assert bar(0, 8, 4) == ""


# ── Plumbing: flags, ACP session options, one-run usage ──────────────────────


def test_hermes_provider_prefixed_ids_split_into_two_flags() -> None:
    assert split_provider("nous:anthropic/claude-fable-5.2") == ("nous", "anthropic/claude-fable-5.2")
    assert split_provider("upstage/solar-mini4:free") == ("", "upstage/solar-mini4:free")
    assert AGENTS["hermes"].model_args("nous:anthropic/claude-fable-5.2") == [
        "--provider",
        "nous",
        "-m",
        "anthropic/claude-fable-5.2",
    ]
    assert AGENTS["agy"].model_args("gemini-3.8-flash-low") == ["--model", "gemini-3.8-flash-low"]


def test_acp_session_gets_the_chosen_model_and_thinking_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses

    monkeypatch.setenv("STUB_ACP_MODELS", "1")
    session = make_session(tmp_path, "hermes", model="flash-low")  # advertised, so it is set over ACP
    session.config = dataclasses.replace(session.config, effort="low")
    session.start()
    assert run(session, "MODEL?")[-1]["text"] == "model flash-low"
    assert run(session, "EFFORT?")[-1]["text"] == "effort low"
    session.close()


def test_one_run_reads_the_usage_the_cli_prints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUB_USAGE", "1")
    result = run_once(
        AGENTS["claude"],
        [str(STUB), "-p", "--model", "haiku"],
        "<meeting_turns>hi</meeting_turns>",
        str(tmp_path),
        20,
    )
    assert result.usage == Usage(9010, 0, 50, 5, pytest.approx(0.05))


def test_bench_compare_draws_the_chart(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_USAGE", "1")
    code = main(
        [
            "bench",
            "--compare",
            "--binary",
            str(STUB),
            "--workdir",
            str(tmp_path),
            "--models",
            "haiku,sonnet,opus",
            "--turns-per-hour",
            "10",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "Estimated tokens for one hour of meeting" in out
    assert "Assumes 10 answered turns an hour" in out
    for model in ("haiku", "sonnet", "opus"):
        assert f"✓ {model}" in out
    assert "$" in out


# ── The setup step ────────────────────────────────────────────────────────────


def _replies(*answers: str) -> Prompter:
    feed = iter(answers)

    def read(_prompt: str) -> str:
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    return Prompter(read)


def _agy_on_path(tmp_path: Path) -> Any:
    fake = tmp_path / "agy"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    return lambda program: str(fake) if program == "agy" else None


def _catalog(_agent: Any, _binary: str) -> ModelCatalog:
    catalog = acp_catalog_from(AGY_SESSION_NEW, "agy acp")
    catalog.default_model, catalog.default_effort = "gemini-3.8-flash-low", "low"
    return catalog


def test_setup_picks_model_and_effort_and_saves_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    replies = _replies(
        "4",  # Antigravity
        "",  # model: the preselected CLI default (gemini-3.8-flash-low)
        "3",  # effort: medium (1 = the CLI's default, 2 = low, 3 = medium)
        "",
        "",
        "4",
        "Nav",  # folder, access, no tunnel, name
    )
    answers = ask_setup(replies, None, which=_agy_on_path(tmp_path), cwd=str(tmp_path), catalog=_catalog)
    out = capsys.readouterr().out
    assert (answers.model, answers.effort) == ("gemini-3.8-flash-low", "medium")
    assert "3 models from agy acp" in out
    assert "2/7  Model" in out
    save_answers(tmp_path, answers)
    loaded = load_answers(tmp_path)
    assert loaded is not None
    assert (loaded.model, loaded.effort) == ("gemini-3.8-flash-low", "medium")
    assert ("Model", "gemini-3.8-flash-low") in loaded.rows()


def test_setup_compares_models_then_lets_you_pick_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    measured: list[tuple[str, str]] = []

    def fake_measure(_config: Any, model: str, effort: str) -> Measured:
        measured.append((model, effort))
        return Measured(model, Usage(10_000, 0, 2500 if "high" in model else 20), 3.0)

    replies = _replies(
        "4",  # Antigravity
        "6",  # Measure live, before choosing a model or effort
        "1,2",  # the first two in the menu: the CLI's order (high, low, sonnet)
        "3",  # claude-sonnet-4-6
        "1",  # effort: the CLI's default
        "",
        "",
        "4",
        "Nav",
    )
    answers = ask_setup(
        replies,
        None,
        which=_agy_on_path(tmp_path),
        cwd=str(tmp_path),
        catalog=_catalog,
        measure_model=fake_measure,
    )
    out = capsys.readouterr().out
    assert measured == [("gemini-3.8-flash-high", ""), ("gemini-3.8-flash-low", "")]  # CLI effort default
    assert "Estimated tokens for one hour of meeting" in out
    assert answers.model == "claude-sonnet-4-6"
    assert answers.effort == ""


def test_old_saved_setups_without_a_model_still_load(tmp_path: Path) -> None:
    (tmp_path / "setup.json").write_text(
        json.dumps(
            {
                "agent": "claude",
                "command": "",
                "workdir": str(tmp_path),
                "max_access": "readonly",
                "tunnel": "none",
                "tunnel_url": "",
                "owner": "Nav",
            }
        ),
        encoding="utf-8",
    )
    loaded = load_answers(tmp_path)
    assert loaded == SetupAnswers("claude", "", str(tmp_path), "readonly", "none", "", "Nav", "", "")
