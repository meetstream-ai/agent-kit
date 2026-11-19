"""MeetStream's shared model estimates: export, fetch with a daily cache, bundled fallback, picker labels."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from meetstream_agent_kit import estimates, shared_estimates
from meetstream_agent_kit.agents import AGENTS
from meetstream_agent_kit.cli import main
from meetstream_agent_kit.compare import Measured
from meetstream_agent_kit.models import ModelChoice
from meetstream_agent_kit.usage import Usage

from .conftest import STUB


def _entry(model: str, output: int = 20, agent: str = "agy") -> dict[str, Any]:
    return shared_estimates.entry(agent, model, Usage(9800, 0, output, 0), 3800, "2026-11-18T21:28:33+00:00")


def _doc(*models: str) -> bytes:
    return json.dumps(shared_estimates.document([_entry(m) for m in models])).encode()


def test_export_writes_and_merges(tmp_path: Path) -> None:
    path = tmp_path / "model-estimates.json"
    assert shared_estimates.export(path, [_entry("a"), _entry("b")]) == 2
    assert shared_estimates.export(path, [_entry("b", output=999), _entry("c")]) == 3
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == 1
    assert data["assumptions"]["turns_per_hour"] == 20
    assert data["kit_version"]
    by_model = {e["model"]: e for e in data["estimates"]}
    assert by_model["b"]["turn"]["output"] == 999  # the newer measurement replaced the older one
    assert set(by_model["a"]) >= {"turn", "per_hour", "first_output_ms", "measured_at"}


def test_odd_entries_are_skipped() -> None:
    good = _entry("ok")
    bad = [
        {**good, "agent": 3},
        {**good, "turn": {**good["turn"], "input": -1}},
        {**good, "turn": {**good["turn"], "cost_usd": "free"}},
        {**good, "measured_at": None},
    ]
    assert shared_estimates.valid_entries({"schema": 1, "estimates": [good, *bad]}) == [good]
    assert shared_estimates.valid_entries({"schema": 2, "estimates": [good]}) == []


def test_load_prefers_a_fresh_cache_then_the_url_then_a_stale_cache_then_the_bundle(tmp_path: Path) -> None:
    cache = tmp_path / shared_estimates.CACHE_FILE
    calls: list[str] = []

    def fetch_ok(url: str, _timeout: float) -> bytes:
        calls.append(url)
        return _doc("from-url")

    def offline(_url: str, _timeout: float) -> bytes:
        raise OSError("offline")

    got = shared_estimates.load(fetch=fetch_ok, home=tmp_path)
    assert [e["model"] for e in got] == ["from-url"]
    assert calls == [shared_estimates.SHARED_URL]
    assert cache.exists()  # cached for a day
    assert [e["model"] for e in shared_estimates.load(fetch=fetch_ok, home=tmp_path)] == ["from-url"]
    assert len(calls) == 1  # the fresh cache answered
    old = time.time() - shared_estimates.CACHE_TTL_S - 60
    os.utime(cache, (old, old))
    assert [e["model"] for e in shared_estimates.load(fetch=offline, home=tmp_path)] == ["from-url"]  # stale
    cache.unlink()
    bundled = shared_estimates.load(fetch=offline, home=tmp_path)
    assert {(e["agent"], e["model"]) for e in bundled} >= {
        ("agy", "gemini-3.8-flash-low"),
        ("claude", "sonnet"),
    }


def test_picker_labels_shared_estimates_and_your_own_runs_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "100")
    shared = [_entry("gemini-3.8-flash-low"), _entry("gemini-3.8-flash-high", output=1100)]
    estimates.save("agy", "gemini-3.8-flash-high", Measured("gemini-3.8-flash-high", Usage(9000, 0, 5), 4.0))
    found = estimates.load_with_shared("agy", shared)
    assert found["gemini-3.8-flash-high"].source == "measured here"
    assert found["gemini-3.8-flash-high"].usage.output == 5
    low = found["gemini-3.8-flash-low"]
    assert low.source == "measured by MeetStream 2026-11-18"
    detail = estimates.detail_text(AGENTS["agy"], ModelChoice("gemini-3.8-flash-low"), low)
    assert "measured by MeetStream 2026-11-18" in detail
    row = estimates.row_text(AGENTS["agy"], ModelChoice("gemini-3.8-flash-low"), low, 500_000)
    assert row.rstrip().endswith("MS 11-18")
    assert row.startswith("gemini-3.8-flash-low")
    unknown = estimates.detail_text(AGENTS["agy"], ModelChoice("brand-new"), found.get("brand-new"))
    assert unknown.startswith("not measured yet")
    assert "Measure live" in unknown


def test_bench_compare_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUB_USAGE", "1")
    out = tmp_path / "model-estimates.json"
    args = [
        "bench",
        "--compare",
        "--binary",
        str(STUB),
        "--workdir",
        str(tmp_path),
        "--models",
        "haiku,sonnet",
    ]
    assert main([*args, "--export", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert [(e["agent"], e["model"]) for e in data["estimates"]] == [
        ("claude", "haiku"),
        ("claude", "sonnet"),
    ]
    assert data["estimates"][0]["turn"]["cost_usd"] == pytest.approx(0.05)
