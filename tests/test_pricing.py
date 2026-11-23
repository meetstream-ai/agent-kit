"""API-rate prices per model, and whether a CLI is signed in on a plan."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meetstream_agent_kit import pricing, shared_estimates
from meetstream_agent_kit.usage import HourEstimate, Usage


@pytest.mark.parametrize(
    ("model", "match"),
    [
        ("gemini-3.8-flash-high", "gemini-3-8-flash"),  # Antigravity's effort suffix
        ("gemini-3.8-flash-lite", None),  # a different model, not "flash" with a suffix
        ("claude-opus-4-6-thinking", "claude-opus-4-6"),
        ("claude-opus-4-1", "claude-opus-4-1"),  # the longest match, not claude-opus-4
        ("claude-sonnet-4-6[1m]", "claude-sonnet-4-6"),
        ("nous:anthropic/claude-sonnet-4.6", "claude-sonnet-4-6"),  # Hermes provider prefixes
        ("sonnet", "sonnet"),  # Claude Code's alias
        ("gpt-6.2-astra", "gpt-6-2-astra"),
        ("gpt-6-astra", "gpt-6-astra"),
        ("gpt-oss-120b-medium", None),
        ("upstage/solar-mini4:free", None),
        ("openai-codex:gpt-6.2-astra", "gpt-6-2-astra"),  # Hermes' provider:model
        ("opencode-go:claude-sonnet-4.6:thinking", "claude-sonnet-4-6"),
        ("", None),  # the CLI's default
    ],
)
def test_models_match_the_bundled_table(model: str, match: str | None) -> None:
    found = pricing.price_for(model, pricing.load())
    assert (found.match if found else None) == match


def test_the_bundled_table_is_dated_and_covers_three_vendors() -> None:
    prices = pricing.load()
    assert {p.vendor for p in prices} == {"Anthropic", "Google", "OpenAI"}
    assert {p.as_of for p in prices} == {"2026-11-21"}
    flash = pricing.price_for("gemini-3.8-flash", prices)
    assert flash is not None
    assert (flash.input, flash.cached, flash.output) == (0.75, 0.075, 3.75)


def test_an_hour_uses_the_measured_cached_share() -> None:
    price = pricing.Price("m", "V", input=1.0, cached=0.1, output=10.0, as_of="2026-11-21")
    hour = HourEstimate(input=1_000_000, output=100_000, cost_usd=None)
    assert pricing.hour_cost(hour, Usage(1000, 0, 10), price) == pytest.approx(2.0)
    assert pricing.hour_cost(hour, Usage(1000, 500, 10), price) == pytest.approx(0.5 + 0.05 + 1.0)


def test_a_newer_shared_file_overrides_the_bundled_prices(home: Path) -> None:
    document = shared_estimates.bundled_document()
    document["prices"] = {
        "as_of": "2026-12-01",
        "models": [
            {"match": "gemini-3.8-flash", "vendor": "Google", "input": 1.5, "cached": 0.15, "output": 7.5}
        ],
    }
    home.mkdir(parents=True, exist_ok=True)
    (home / shared_estimates.CACHE_FILE).write_text(json.dumps(document), encoding="utf-8")
    pricing.load.cache_clear()
    found = pricing.price_for("gemini-3.8-flash-low", pricing.load())
    assert found is not None
    assert (found.input, found.as_of) == (1.5, "2026-12-01")


def test_odd_price_rows_are_skipped() -> None:
    document = {
        "prices": {
            "as_of": "2026-11-21",
            "models": [
                {"match": "ok", "input": 1, "cached": 0, "output": 2},
                {"match": "negative", "input": -1, "cached": 0, "output": 2},
                {"match": "text", "input": "1", "cached": 0, "output": 2},
                {"match": "", "input": 1, "cached": 0, "output": 2},
                "not a row",
            ],
        }
    }
    assert [p.match for p in pricing.table(document)] == ["ok"]
    assert pricing.table({"prices": {"models": []}}) == []  # no date: not trusted


def test_export_keeps_the_price_table(tmp_path: Path) -> None:
    out = tmp_path / "model-estimates.json"
    shared_estimates.export(out, [])
    assert pricing.table(json.loads(out.read_text(encoding="utf-8")))  # the bundled table, carried over
    edited = json.loads(out.read_text(encoding="utf-8"))
    edited["prices"]["as_of"] = "2026-12-01"
    out.write_text(json.dumps(edited), encoding="utf-8")
    shared_estimates.export(out, [])
    assert (
        json.loads(out.read_text(encoding="utf-8"))["prices"]["as_of"] == "2026-12-01"
    )  # the refresh's edit stays


def test_plan_detection_reads_sign_in_files_only(tmp_path: Path) -> None:
    home = tmp_path / "user"
    (home / ".codex").mkdir(parents=True)
    assert pricing.plan_kind("agy", "gemini-3.8-flash", home=home, env={}) == pricing.PLAN
    assert pricing.plan_kind("copilot", "auto", home=home, env={}) == pricing.PLAN
    assert pricing.plan_kind("hermes", "upstage/solar-mini4:free", home=home, env={}) == pricing.FREE
    assert pricing.plan_kind("hermes", "nous:anthropic/claude-sonnet-4.6", home=home, env={}) == ""
    assert pricing.plan_kind("claude", "sonnet", home=home, env={}) == ""  # not signed in
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"billingType": "x"}}), encoding="utf-8")
    assert pricing.plan_kind("claude", "sonnet", home=home, env={}) == pricing.PLAN
    assert pricing.plan_kind("claude", "sonnet", home=home, env={"ANTHROPIC_API_KEY": "set"}) == pricing.API
    (home / ".codex" / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": None, "tokens": {}}), encoding="utf-8"
    )
    assert pricing.plan_kind("codex", "", home=home, env={}) == pricing.PLAN
    (home / ".codex" / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "set"}), encoding="utf-8")
    assert pricing.plan_kind("codex", "", home=home, env={}) == pricing.API
