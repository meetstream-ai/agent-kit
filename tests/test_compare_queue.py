"""Measure live: a work queue of a few models at once, one retry for transient failures, a live table."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from meetstream_agent_kit import compare
from meetstream_agent_kit.agents import AGENTS
from meetstream_agent_kit.compare import Job, Measured, retryable, run_queue, time_left
from meetstream_agent_kit.config import BridgeConfig
from meetstream_agent_kit.usage import Usage


def _config(tmp_path: Path) -> BridgeConfig:
    return BridgeConfig(agent=AGENTS["claude"], binary="claude", workdir=tmp_path)


def _sleeper(seconds: dict[str, float], started: dict[str, float] | None = None, t0: float = 0.0):  # type: ignore[no-untyped-def]
    def run(_config: BridgeConfig, model: str, _effort: str) -> Measured:
        if started is not None:
            started[model] = time.monotonic() - t0
        time.sleep(seconds.get(model, 0.3))
        return Measured(model, Usage(9000, output=20), seconds.get(model, 0.3))

    return run


def test_models_run_at_the_same_time(tmp_path: Path) -> None:
    models = [f"m{i}" for i in range(8)]
    t0 = time.monotonic()
    jobs = run_queue(_config(tmp_path), models, "", run=_sleeper({}), parallel=4)
    took = time.monotonic() - t0
    assert [job.state for job in jobs] == ["done"] * 8
    assert took < 1.2  # 8 x 0.3 s one after another would be 2.4 s; 4 at a time is ~0.6 s


def test_a_free_worker_takes_the_next_model_at_once(tmp_path: Path) -> None:
    started: dict[str, float] = {}
    t0 = time.monotonic()
    run = _sleeper({"slow": 1.0, "a": 0.1, "b": 0.1, "c": 0.1}, started, t0)
    run_queue(_config(tmp_path), ["slow", "a", "b", "c"], "", run=run, parallel=2)
    # "b" and "c" start as soon as "a"'s worker is free, not when the batch (with "slow") ends.
    assert started["b"] < 0.5
    assert started["c"] < 0.5


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("agent timed out after 120 s", True),
        ("agent exited with -9", True),
        ("429 Too Many Requests", True),
        ("rate limit exceeded, try again", True),
        ("connection reset by peer", True),
        ("503 Service Unavailable", True),
        ("crashed: boom", True),
        ("Please log in: run claude auth login", False),
        ("401 Unauthorized", False),
        ("model not found: gpt-9", False),
        ("Unknown model 'x'", False),
        ("agent exited with 1: not signed in", False),
        ("agent CLI not found: agy", False),
        ("the CLI returned no answer", False),
        ("", False),
    ],
)
def test_only_transient_failures_are_retried(error: str, expected: bool) -> None:
    assert retryable(error) is expected


def _flaky(errors: dict[str, list[str]], calls: dict[str, int]):  # type: ignore[no-untyped-def]
    def run(_config: BridgeConfig, model: str, _effort: str) -> Measured:
        calls[model] = calls.get(model, 0) + 1
        queued = errors.get(model, [])
        if queued:
            return Measured(model, None, 0.1, queued.pop(0))
        return Measured(model, Usage(9000, output=20), 0.1)

    return run


def test_one_retry_for_transient_failures_and_none_for_sign_in(tmp_path: Path) -> None:
    calls: dict[str, int] = {}
    errors = {
        "flaky": ["agent timed out after 120 s"],
        "down": ["503 Service Unavailable", "503 Service Unavailable"],
        "signed-out": ["Please log in"],
        "unknown": ["model not found: unknown"],
    }
    seen: list[tuple[str, str]] = []
    jobs = run_queue(
        _config(tmp_path),
        ["flaky", "down", "signed-out", "unknown", "fine"],
        "",
        run=_flaky(errors, calls),
        backoff_s=0,
        on_change=lambda job: seen.append((job.model, job.state)),
    )
    state = {job.model: (job.state, job.retried) for job in jobs}
    assert state == {
        "flaky": ("done", True),
        "down": ("failed", True),
        "signed-out": ("failed", False),
        "unknown": ("failed", False),
        "fine": ("done", False),
    }
    assert calls == {"flaky": 2, "down": 2, "signed-out": 1, "unknown": 1, "fine": 1}
    assert ("flaky", "retrying") in seen  # the table shows the retry
    assert next(job for job in jobs if job.model == "down").reason == "503 Service Unavailable"


def test_stop_keeps_finished_results_and_cancels_the_rest(tmp_path: Path) -> None:
    stop = threading.Event()

    def run(_config: BridgeConfig, model: str, _effort: str) -> Measured:
        if model == "m0":
            return Measured(model, Usage(9000, output=20), 0.1)
        cancelled = compare._hooks.cancel.wait(5)  # a real run_once kills the CLI on this event
        return Measured(model, None, 0.1, "cancelled" if cancelled else "")

    threading.Timer(0.3, stop.set).start()
    t0 = time.monotonic()
    jobs = run_queue(_config(tmp_path), ["m0", "m1", "m2", "m3"], "", run=run, parallel=2, stop=stop)
    assert time.monotonic() - t0 < 2  # the running model was stopped, not waited for
    assert [job.state for job in jobs] == ["done", "cancelled", "cancelled", "cancelled"]
    assert jobs[0].measured is not None
    assert jobs[0].measured.usage is not None
    assert jobs[-1].reason == "not started"
    assert [m.error for m in compare.results(jobs)][1:] == ["stopped", "stopped", "not started"]


def test_a_crash_in_one_model_does_not_stop_the_others(tmp_path: Path) -> None:
    def run(_config: BridgeConfig, model: str, _effort: str) -> Measured:
        if model == "bad":
            raise ValueError("boom")
        return Measured(model, Usage(9000, output=20), 0.1)

    jobs = run_queue(_config(tmp_path), ["bad", "good"], "", run=run, backoff_s=0)
    assert [(job.model, job.state) for job in jobs] == [("bad", "failed"), ("good", "done")]
    assert jobs[0].reason == "crashed: boom"


def test_time_left_from_the_models_done_so_far() -> None:
    now = 100.0
    done = Job("a", state="done", started=80.0, ended=90.0)  # 10 s each
    running = Job("b", state="running", started=96.0)
    queued = [Job(f"q{i}") for i in range(4)]
    assert time_left([Job("x")], 2, now) is None
    assert time_left([done, running, *queued], 2, now) == pytest.approx(6 + 2 * 10)


def test_the_live_table_shows_every_state(tmp_path: Path) -> None:
    from rich.console import Console

    jobs = [
        Job(
            "done-model",
            state="done",
            started=1.0,
            ended=5.0,
            measured=Measured("sonnet", Usage(9000, output=20), 4),
        ),
        Job("failing", state="failed", started=1.0, ended=2.0, reason="401 Unauthorized"),
        Job("flaky", state="retrying", stage="booting", started=1.0, retried=True, reason="timed out"),
        Job("running", state="running", stage="first words", started=1.0),
        Job("waiting"),
    ]
    console = Console(record=True, width=110, color_system=None)
    console.print(compare._table(AGENTS["claude"], jobs, 4, "⠋"))
    text = console.export_text()
    assert "✓  done-model" in text
    assert "≈ $" in text  # tokens/h and the API-rate estimate for the finished model
    assert "✗  failing" in text
    assert "401 Unauthorized" in text
    assert "retrying (1/1) · booting · timed out" in text
    assert "401 Unauthorized" in text
    assert "first words" in text
    assert "queued" in text
    assert "2/5 done · 2 running" in text
    assert "Esc stop" in text
    finished = [Job("a", state="done"), Job("b", state="failed"), Job("c", state="cancelled")]
    console.print(compare._table(AGENTS["claude"], finished, 4, ""))
    assert "1 answered · 1 failed · 1 stopped" in console.export_text()
