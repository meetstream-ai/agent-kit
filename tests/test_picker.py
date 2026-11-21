"""Cached model estimates, capabilities and picker interaction."""

from __future__ import annotations

import dataclasses
import importlib
import json
import stat
import sys
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from meetstream_agent_kit import estimates, pricing, ui
from meetstream_agent_kit.agents import AGENTS, custom_agent
from meetstream_agent_kit.cli import _test_turn
from meetstream_agent_kit.compare import Measured, measure
from meetstream_agent_kit.estimates import Estimate, detail_text, row_text
from meetstream_agent_kit.models import (
    ModelCatalog,
    ModelChoice,
    acp_catalog_from,
    catalog_for,
    claude_catalog_from,
    codex_catalog_from,
    features,
)
from meetstream_agent_kit.oneshot import RunResult
from meetstream_agent_kit.setup import (
    Prompter,
    _menu_models,
    _pick_model,
    ask_setup,
    load_answers,
    save_answers,
)
from meetstream_agent_kit.usage import Usage, project_hour

from .conftest import make_config
from .test_models import AGY_SESSION_NEW, CLAUDE_HELP, _agy_on_path
from .test_wizard import _menu_in_a_terminal


def test_cache_round_trip_and_default_key(home: Path) -> None:
    assert estimates.load("claude") == {}
    measured = Measured("", Usage(9800, 500, 20, 5, 0.02), 6.0, first_output_ms=4800)
    estimates.save("claude", "", measured)
    path = home / "model-usage.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text())
    assert data["claude:"]["first_output_ms"] == 4800
    assert data["claude:"]["input"] == 9800
    loaded = estimates.load("claude")[""]
    assert loaded.usage == measured.usage
    assert loaded.seconds == 6
    assert loaded.measured_at
    path.chmod(0o644)
    estimates.save("hermes", "provider:model", measured)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert estimates.load("claude")[""] == loaded
    assert list(estimates.load("hermes")) == ["provider:model"]


@pytest.mark.parametrize("contents", ["broken", "[]", '{"claude:x":null}', '{"claude:x":{"input":"bad"}}'])
def test_corrupt_cache_is_empty(home: Path, contents: str) -> None:
    home.mkdir()
    (home / "model-usage.json").write_text(contents)
    assert estimates.load("claude") == {}
    estimates.save("claude", "x", Measured("x", Usage(20, output=1), 1))
    assert list(estimates.load("claude")) == ["x"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("seconds", -1),
        ("first_output_ms", float("nan")),
        ("cost_usd", "bad"),
        ("input", True),
        ("thinking", -1),
        ("measured_at", "not a date"),
    ],
)
def test_invalid_measurement_is_ignored(home: Path, field: str, value: object) -> None:
    estimates.save("claude", "x", Measured("x", Usage(20, output=1), 1))
    path = home / "model-usage.json"
    data = json.loads(path.read_text())
    data["claude:x"][field] = value
    path.write_text(json.dumps(data))
    assert estimates.load("claude") == {}


def test_cache_write_failure_does_not_interrupt_setup(home: Path) -> None:
    home.write_text("not a directory")
    estimates.save("claude", "x", Measured("x", Usage(20), 1))
    assert estimates.load("claude") == {}


@pytest.mark.parametrize("model", ["", "sonnet"])
def test_measure_saves_usage_and_first_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    result = RunResult(answer="ready", usage=Usage(9000, output=20), total_ms=6400, first_output_ms=4700)
    module = importlib.import_module("meetstream_agent_kit.compare")
    monkeypatch.setattr(module, "run_once", lambda *_a, **_kw: result)
    measured = measure(make_config(tmp_path), model, "")
    assert measured.first_output_ms == 4700
    saved = estimates.load("claude")[model]
    assert (saved.usage, saved.seconds, saved.first_output_ms) == (result.usage, 6.4, 4700)


@pytest.mark.parametrize("error", [None, "failed"])
def test_setup_test_turn_saves_only_successful_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str | None
) -> None:
    result = RunResult(
        answer="ready", error=error, usage=Usage(9000, output=20), total_ms=6400, first_output_ms=4700
    )
    monkeypatch.setattr("meetstream_agent_kit.cli.run_once", lambda *_a, **_kw: result)
    config = dataclasses.replace(make_config(tmp_path), model="sonnet")
    assert _test_turn(config) is result
    if error:
        assert estimates.load("claude") == {}
    else:
        assert estimates.load("claude")["sonnet"].first_output_ms == 4700


def test_failed_measurement_does_not_replace_good_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    estimates.save("claude", "x", Measured("x", Usage(100), 1))
    module = importlib.import_module("meetstream_agent_kit.compare")
    monkeypatch.setattr(module, "run_once", lambda *_a, **_kw: RunResult(error="failed", usage=Usage(9000)))
    assert measure(make_config(tmp_path), "x", "").error == "failed"
    assert estimates.load("claude")["x"].usage == Usage(100)


def test_codex_vision_comes_from_modalities() -> None:
    catalog = codex_catalog_from(
        {
            "data": [
                {"id": "image", "inputModalities": ["text", "image"]},
                {"id": "text", "inputModalities": ["text"]},
                {"id": "unknown"},
            ]
        }
    )
    assert [m.vision for m in catalog.models] == [True, False, None]
    assert catalog.models[0].vision_source == "codex app-server"


@pytest.mark.parametrize("vision", [True, False, None])
def test_acp_agent_vision_is_kept_for_models_and_unlisted_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vision: bool | None
) -> None:
    init = {"agentCapabilities": {"promptCapabilities": {"image": vision}}}
    monkeypatch.setattr(
        "meetstream_agent_kit.models._rpc_session", lambda *_a, **_kw: [init, AGY_SESSION_NEW]
    )
    monkeypatch.setattr("meetstream_agent_kit.models.cli_defaults", lambda *_a: ("unlisted", ""))
    catalog = catalog_for(AGENTS["agy"], "agy", home=tmp_path)
    assert all(m.vision is vision for m in catalog.models)
    assert catalog.vision is vision
    assert catalog.vision_source == ("agy acp" if vision is not None else "")
    assert acp_catalog_from(AGY_SESSION_NEW, "agy acp").vision is None


@pytest.mark.parametrize(
    ("agent", "model", "vision"),
    [
        ("claude", "sonnet", True),
        ("hermes", "anthropic/claude-sonnet", True),
        ("hermes", "gpt-oss-120b", False),
        ("agy", "unknown", None),
    ],
)
def test_curated_vision_rules(agent: str, model: str, vision: bool | None) -> None:
    feature = next(f for f in features(AGENTS[agent], ModelChoice(model)) if f.name == "vision")
    assert feature.available is vision
    assert feature.source == ("from docs" if vision is not None else "")
    aliases = claude_catalog_from(CLAUDE_HELP)
    assert all(features(AGENTS["claude"], m)[2].available for m in aliases.models)


def test_features_match_meeting_tool_support() -> None:
    for name, agent in AGENTS.items():
        found = {f.name: f for f in features(agent, ModelChoice("x"))}
        assert found["tools"].available is (name != "openclaw")
        assert found["talk"].available is True
        assert found["local"].available is True
    custom = {f.name: f for f in features(custom_agent("mytool"), ModelChoice("x"))}
    assert custom["tools"].available is False
    assert custom["local"].available is None
    explicit = ModelChoice("claude-test", vision=False, vision_source="test CLI")
    assert features(AGENTS["claude"], explicit)[2].available is False


@pytest.mark.parametrize("cost", [None, 0.0, 0.02])
def test_rows_and_details_show_real_usage_and_first_words(
    monkeypatch: pytest.MonkeyPatch, cost: float | None
) -> None:
    monkeypatch.setattr(ui, "width", lambda: 100)
    agent, model = AGENTS["claude"], ModelChoice("sonnet")
    estimate = Estimate(Usage(9800, output=20, cost_usd=cost), 20, 4700, "2026-11-18T00:00:00+00:00")
    row = row_text(agent, model, estimate, project_hour(estimate.usage).total)
    detail = detail_text(agent, model, estimate)
    assert "██████████" in row
    assert "223k/h" in row
    assert "~5 s cold" in row
    if cost is None:  # no price from the CLI: the dated table's Anthropic price for "sonnet"
        assert "223k/h ≈$0.45" in row
        assert "≈ $0.45/h at API rates (estimate; Anthropic list prices, 2026-11-21)" in detail
    else:  # the CLI's own price wins
        assert f"≈{pricing.money(project_hour(estimate.usage).cost_usd or 0)}" in row
        assert "Claude Code's own estimate" in detail
    no_cli_price = dataclasses.replace(estimate, usage=Usage(9800, output=20))
    unpriced = row_text(
        agent, ModelChoice("some-new-model"), no_cli_price, project_hour(no_cli_price.usage).total
    )
    assert "223k/h —" in unpriced
    assert "API price unknown" in detail_text(agent, ModelChoice("some-new-model"), no_cli_price)
    assert "first words ~5 s (cold)" in detail
    assert "vision (from docs), local actions per access level" in detail
    empty = row_text(AGENTS["openclaw"], ModelChoice("unknown"), None, 0)
    assert "[          ] usage n/a" in empty  # OpenClaw does not report token usage
    assert "[          ] not measured" in row_text(AGENTS["agy"], ModelChoice("unknown"), None, 0)
    assert "no-tools vision?" in empty
    assert "—" in empty
    assert 'pick "Measure live"' in detail_text(agent, model, None)
    assert "first words —" in detail_text(agent, model, dataclasses.replace(estimate, first_output_ms=None))


@pytest.mark.parametrize("width", [20, 40, 88, 100])
@pytest.mark.parametrize("color", [False, True])
def test_rows_fit_even_with_colour_and_wide_names(
    monkeypatch: pytest.MonkeyPatch, width: int, color: bool
) -> None:
    monkeypatch.setattr(ui, "width", lambda: width)
    monkeypatch.setattr(ui, "use_color", lambda: color)
    row = row_text(AGENTS["claude"], ModelChoice("模" * 100), None, 0)
    plain = ui.plain(row)
    cells = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in plain)
    assert cells <= width - 6
    assert "\x1b" not in plain


def test_plain_setup_measures_before_picking_and_refreshes_cache(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ui, "use_color", lambda: False)
    catalog = ModelCatalog([ModelChoice(f"m{i}") for i in range(5)], "m1", ["low", "medium"])
    for model in ("m0", "m1"):
        estimates.save("agy", model, Measured(model, Usage(9800, output=20), 6, first_output_ms=4700))
    prompts = []
    replies = iter(["4", "8", "", "2", "3", "", "", "4", "Test"])

    def read(question: str) -> str:
        prompts.append(question)
        return next(replies)

    measured = []

    def fake_measure(_config: Any, model: str, effort: str) -> Measured:
        measured.append((model, effort))
        return Measured(model, Usage(10000, output=20), 7, first_output_ms=5100)

    answers = ask_setup(
        Prompter(read),
        None,
        which=_agy_on_path(tmp_path),
        cwd=str(tmp_path),
        catalog=lambda _a, _b: catalog,
        measure_model=fake_measure,
    )
    out = capsys.readouterr().out
    assert "Choose (numbers, comma-separated) [3,4,5,6]: " in prompts
    assert sorted(measured) == sorted([("m2", ""), ("m3", ""), ("m4", ""), ("", "")])  # 4 at once
    assert out.count("Which model?") == 2
    first, second = out.split("Which model?")[1:]
    assert "██████████" in first
    assert "not measured" in first
    assert "not measured" not in second
    assert "Estimated tokens for one hour of meeting" in first
    assert "Compare models" not in out
    assert "Keep " not in out
    assert "\x1b" not in out
    assert set(estimates.load("agy")) == {"m0", "m1", "m2", "m3", "m4", ""}
    save_answers(tmp_path, answers)
    loaded = load_answers(tmp_path)
    assert loaded is not None
    assert (loaded.model, loaded.effort) == ("m1", "medium")


@pytest.mark.parametrize(("reply", "expected"), [("", "m1"), ("3", "typed"), ("4", "")])
def test_picker_preserves_default_manual_id_and_cli_default(
    tmp_path: Path, reply: str, expected: str
) -> None:
    replies = iter([reply, "typed"])
    model, effort, _asked = _pick_model(
        Prompter(lambda _: next(replies)),
        AGENTS["agy"],
        "agy",
        None,
        catalog=lambda _a, _b: ModelCatalog([ModelChoice("m0"), ModelChoice("m1")], "m1"),
        compare_config=lambda: make_config(tmp_path),
        measure_model=lambda *_a: pytest.fail("must not measure when picking"),
    )
    assert (model, effort) == (expected, "")
    catalog = ModelCatalog([ModelChoice(f"m{i}") for i in range(20)], "m19")
    shown = _menu_models(catalog, "m18")
    assert len(shown) == 12
    assert {"m19", "m18"}.issubset({m.id for m in shown})


def test_plain_prompter_does_not_call_detail(capsys: pytest.CaptureFixture[str]) -> None:
    index = Prompter(lambda _: "2").choose(
        "Pick", ["one", "two"], detail=lambda _: pytest.fail("plain menus have no dynamic details")
    )
    assert index == 1
    assert "2) two" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "win32", reason="pseudo-terminals are Unix only")
def test_arrow_menu_redraws_and_clears_detail() -> None:
    out = _menu_in_a_terminal([b"\x1b[B", b"\r"], detail=True)
    assert "details: 0" in out
    assert "details: 1" in out
    assert "first" in out
    assert "third" in out
    assert "PICKED 1" in out
    assert "\x1b[4F" in out  # three options and one detail row on redraw
    assert "\x1b[5F\x1b[J" in out  # title, options and detail are cleared on fold-up


def test_a_documented_text_only_model_beats_the_agents_image_capability() -> None:
    """agy's ACP says the agent takes images, but gpt-oss models are text-only (seen live, agy 1.5.11)."""
    from meetstream_agent_kit.agents import AGENTS
    from meetstream_agent_kit.models import ModelChoice, features

    vision = {
        f.name: (f.available, f.source)
        for f in features(
            AGENTS["agy"], ModelChoice("gpt-oss-120b-medium", vision=True, vision_source="agy acp")
        )
    }["vision"]
    assert vision == (False, "from docs")
    gemini = {
        f.name: f.available
        for f in features(
            AGENTS["agy"], ModelChoice("gemini-3.8-flash-low", vision=True, vision_source="agy acp")
        )
    }
    assert gemini["vision"] is True
