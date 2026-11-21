"""Copilot's model list (live bug 2026-11-21: the picker showed "auto, auto")."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from meetstream_agent_kit.agents import AGENTS
from meetstream_agent_kit.models import ModelCatalog, ModelChoice, acp_catalog_from, catalog_for, unique
from meetstream_agent_kit.setup import Prompter, _menu_models, _pick_model

RECORDED = Path(__file__).parent / "fixtures" / "copilot_acp_session_new.json"
"""`copilot --acp --stdio` session/new, GitHub Copilot CLI 1.0.114: "auto" twice, nothing else."""


def _recorded() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(RECORDED.read_text(encoding="utf-8"))
    return data


def test_recorded_copilot_list_has_auto_twice_and_we_keep_one() -> None:
    raw = [m["modelId"] for m in _recorded()["models"]["availableModels"]]
    assert raw == ["auto", "auto"]
    catalog = acp_catalog_from(_recorded(), "copilot --acp --stdio")
    assert [m.id for m in catalog.models] == ["auto"]
    assert catalog.default_model == "auto"


def test_copilot_with_only_auto_explains_how_to_get_more(tmp_path: Path) -> None:
    catalog = catalog_for(
        AGENTS["copilot"],
        "copilot",
        home=tmp_path,
        acp=lambda _a, _b, _t: acp_catalog_from(_recorded(), "copilot"),
    )
    assert [m.id for m in catalog.models] == ["auto"]
    assert "github.com/settings/copilot" in catalog.note


def test_a_copilot_account_with_more_models_shows_them_all_once(tmp_path: Path) -> None:
    created = _recorded()
    created["models"]["availableModels"] += [
        {"modelId": "claude-sonnet-5.5", "name": "Claude Sonnet 5.5"},
        {"modelId": "gpt-6.1-sol", "name": "GPT-6.1 Sol"},
        {"modelId": "claude-sonnet-5.5", "name": "Claude Sonnet 5.5"},
    ]
    catalog = catalog_for(
        AGENTS["copilot"],
        "copilot",
        home=tmp_path,
        acp=lambda _a, _b, _t: acp_catalog_from(created, "copilot"),
    )
    assert [m.id for m in catalog.models] == ["auto", "claude-sonnet-5.5", "gpt-6.1-sol"]
    assert catalog.note == ""


def test_the_picker_never_shows_a_model_twice() -> None:
    twice = ModelCatalog([ModelChoice("auto"), ModelChoice("auto"), ModelChoice("x")], default_model="auto")
    assert [m.id for m in _menu_models(twice, "")] == ["auto", "x"]
    assert [m.id for m in unique([ModelChoice("a"), ModelChoice("b"), ModelChoice("a")])] == ["a", "b"]


def test_setup_shows_the_note_and_one_auto_row(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    feed = iter(["1", ""])  # auto, then the CLI's effort

    def read(_q: str) -> str:
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    model, _effort = _pick_model(
        Prompter(read),
        AGENTS["copilot"],
        "copilot",
        None,
        catalog=lambda _a, _b: catalog_for(
            AGENTS["copilot"],
            "copilot",
            home=tmp_path,
            acp=lambda _x, _y, _t: acp_catalog_from(_recorded(), "copilot"),
        ),
        compare_config=lambda: None,  # type: ignore[arg-type,return-value]
        measure_model=lambda *_a: None,  # type: ignore[arg-type,return-value]
    )
    out = capsys.readouterr().out
    assert model == "auto"
    assert out.count("1) auto") == 1
    assert "2) auto" not in out
    assert "github.com/settings/copilot" in out
