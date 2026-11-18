"""A turn that starts right after a cancel never gets the cancelled turn's tail (bot 5b8ac5e7, 11-18)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from .conftest import make_session, run


def test_next_turn_waits_for_the_restart_and_gets_its_own_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_session(tmp_path, "claude")
    session.start()
    original_restart = session.restart

    def slow_restart(resume: bool = True) -> None:
        time.sleep(0.6)  # the cancel's restart thread runs late, as it can under load
        original_restart(resume)

    monkeypatch.setattr(session, "restart", slow_restart)
    first: list[dict[str, Any]] = []
    thread = threading.Thread(target=lambda: first.extend(run(session, "LONGSTREAM please")))
    thread.start()
    time.sleep(0.3)  # the old turn is streaming "stale0 stale1 ..."
    second = run(session, "what is the answer?")  # pre-empts the first turn
    thread.join(10)
    assert first[-1]["code"] == "cancelled"
    assert second[-1]["type"] == "done"
    assert "stale" not in second[-1]["text"]  # nothing from the cancelled turn
    assert second[-1]["text"] == "The answer is 42."
    session.close()
