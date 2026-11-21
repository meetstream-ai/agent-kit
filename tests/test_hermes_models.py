"""Hermes' model step: the models configured in ~/.hermes/config.yaml, not only its default."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from meetstream_agent_kit.agents import AGENTS
from meetstream_agent_kit.models import ModelCatalog, ModelChoice, catalog_for, hermes_models_from
from meetstream_agent_kit.setup import Prompter, _pick_model

# Shaped like a real Hermes config (comments, quotes, aliases, list items, unrelated blocks).
CONFIG = """\
_config_version: 49
model:
  # Default model to use (can be overridden with --model flag)
  default: "upstage/solar-mini4:free"
  provider: "nous"   # Nous Portal

  aliases:
    go-planner:
      provider: opencode-go
      model: gpt-6-luna
    codex-review:
      provider: openai-codex
      model: gpt-6.1-sol
  base_url: https://inference-api.nousresearch.com/v1
stt:
  provider: local
  local:
    model: "base"              # tiny | base | small
  openai:
    provider: openai
    model: "whisper-1"
delegation:
  max_iterations: 80
  # comments do not end a block
  provider: opencode-go
  model: deepseek-v4.1-flash
  fallback_providers:
    - provider: opencode-go
      model: glm-5.3
fallback_providers:
  - provider: opencode-go
    model: gpt-6-luna
moa:
  reference_models:
    - provider: opencode-go
      model: glm-5.3
      enabled: true
  aggregator:
    provider: openai-codex
    model: gpt-6.1-sol
agent:
  reasoning_effort: high
"""


def _home(tmp_path: Path, config: str = CONFIG, cache: dict[str, Any] | None = None) -> Path:
    (tmp_path / ".hermes").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".hermes" / "config.yaml").write_text(config, encoding="utf-8")
    if cache is not None:
        (tmp_path / ".hermes" / "provider_models_cache.json").write_text(json.dumps(cache), encoding="utf-8")
    return tmp_path


def test_config_pairs_become_provider_model_ids() -> None:
    found = [(m.id, m.label) for m in hermes_models_from(CONFIG)]
    assert found == [
        ("opencode-go:gpt-6-luna", "alias go-planner"),
        ("openai-codex:gpt-6.1-sol", "alias codex-review"),
        ("opencode-go:deepseek-v4.1-flash", "delegation"),
        ("opencode-go:glm-5.3", "delegation"),
    ]  # each pair once; speech-to-text models (stt) are not chat models


def test_hermes_lists_its_default_and_configured_models_without_starting_acp(tmp_path: Path) -> None:
    home = _home(
        tmp_path,
        cache={
            "nous": {"fp": "x", "at": 1, "models": ["anthropic/claude-opus-4.6", "upstage/solar-mini4:free"]},
            "copilot-acp": {"fp": "x", "at": 1, "models": ["auto"]},
            "broken": "not a dict",
        },
    )

    def no_acp(*_args: Any) -> ModelCatalog:
        raise AssertionError("Hermes' ACP listing is slow; setup reads its config instead")

    catalog = catalog_for(AGENTS["hermes"], "hermes", home=home, acp=no_acp)
    assert catalog.source == "~/.hermes/config.yaml"
    assert catalog.default_model == "upstage/solar-mini4:free"
    assert [m.id for m in catalog.models] == [
        "upstage/solar-mini4:free",
        "opencode-go:gpt-6-luna",
        "openai-codex:gpt-6.1-sol",
        "opencode-go:deepseek-v4.1-flash",
        "opencode-go:glm-5.3",
    ]
    assert catalog.default_effort == "high"
    assert "2 more models" in catalog.note  # the cached ones not listed, never "auto"
    assert "nous:anthropic/claude-opus-4.6" in catalog.note  # the main provider's first


def test_without_a_config_the_source_is_still_named(tmp_path: Path) -> None:
    catalog = catalog_for(AGENTS["hermes"], "hermes", home=tmp_path)
    assert catalog.models == []
    assert catalog.error
    only_default = _home(tmp_path, 'model:\n  default: "upstage/solar-mini4:free"\n')
    catalog = catalog_for(AGENTS["hermes"], "hermes", home=only_default)
    assert [m.id for m in catalog.models] == ["upstage/solar-mini4:free"]
    assert catalog.source  # was blank: "✓ 1 model from "


def test_a_failed_acp_list_with_a_settings_default_names_its_source(tmp_path: Path) -> None:
    (tmp_path / ".gemini" / "antigravity-cli").mkdir(parents=True)
    (tmp_path / ".gemini" / "antigravity-cli" / "settings.json").write_text(
        '{"model": "m1"}', encoding="utf-8"
    )

    def broken(*_args: Any) -> ModelCatalog:
        raise TimeoutError("session/new did not answer within 30 s")

    def no_models(*_args: Any, **_kw: Any) -> Any:
        raise OSError("agy models failed")

    catalog = catalog_for(AGENTS["agy"], "agy", home=tmp_path, acp=broken, run=no_models)
    assert [m.id for m in catalog.models] == ["m1"]
    assert catalog.source == "your CLI's settings"


def test_hermes_with_only_its_default_still_gets_a_menu(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only Copilot's Auto skips the menu: Hermes takes other ids even when it lists one."""
    replies = iter(["2", "openai-codex:gpt-6.1-sol", ""])  # "Type another model id", the id, effort

    def read(_q: str) -> str:
        return next(replies)

    model, _effort, asked = _pick_model(
        Prompter(read),
        AGENTS["hermes"],
        "hermes",
        None,
        catalog=lambda _a, _b: ModelCatalog(
            [ModelChoice("upstage/solar-mini4:free")], "upstage/solar-mini4:free", source="test"
        ),
        compare_config=lambda: None,  # type: ignore[arg-type,return-value]
        measure_model=lambda *_a: None,  # type: ignore[arg-type,return-value]
    )
    out = capsys.readouterr().out
    assert "picks the model itself" not in out
    assert "Type another model id" in out
    assert (model, asked) == ("openai-codex:gpt-6.1-sol", True)
