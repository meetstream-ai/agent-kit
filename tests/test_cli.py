"""The command line: arguments, startup checks and their messages."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from meetstream_agent_kit import __version__, checks
from meetstream_agent_kit.cli import build_parser, main
from meetstream_agent_kit.pairing import PairingStore

from .conftest import STUB


def test_serve_accepts_max_access_and_the_old_mode_flag() -> None:
    parser = build_parser()
    assert parser.parse_args(["serve", "--max-access", "full"]).max_access == "full"
    assert parser.parse_args(["serve", "--mode", "confirm"]).max_access == "confirm"
    assert parser.parse_args(["serve"]).max_access == "readonly"


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--version"])
    assert __version__ in capsys.readouterr().out


def test_pair_list_revoke(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["pair", "--label", "laptop"]) == 0
    out = capsys.readouterr().out
    assert "msak_" in out
    pairing_id = PairingStore().list()[0].id
    assert main(["list"]) == 0
    assert pairing_id in capsys.readouterr().out
    assert main(["revoke", pairing_id]) == 0
    assert main(["revoke", pairing_id]) == 1


def test_pair_reports_a_missing_agent(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    assert main(["pair", "--agent", "codex"]) == 0
    assert "npm install -g @openai/codex" in capsys.readouterr().out


def _serve(*extra: str) -> list[str]:
    return ["serve", "--binary", str(STUB), "--no-prime", *extra]


def test_serve_needs_a_pairing(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(_serve("--workdir", str(tmp_path))) == 1
    err = capsys.readouterr().err
    assert "no pairing yet" in err
    assert "pair --agent claude" in err


def test_serve_checks_the_work_folder(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    PairingStore().create("t")
    assert main(_serve("--workdir", str(tmp_path / "missing"))) == 1
    assert "does not exist" in capsys.readouterr().err


def test_serve_checks_the_agent_is_installed(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    PairingStore().create("t")
    assert main(["serve", "--binary", "/nonexistent/claude", "--workdir", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "Claude Code not found" in err
    assert "npm install -g @anthropic-ai/claude-code" in err


def test_serve_explains_a_busy_port(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    PairingStore().create("t")
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert main(_serve("--workdir", str(tmp_path), "--port", str(port))) == 1
        assert f"port {port} is used by another program" in capsys.readouterr().err


def test_serve_explains_a_bridge_already_running(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, home: Path
) -> None:
    PairingStore().create("t")
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        checks.write_lock(home, port)  # as if this process were the running bridge
        assert main(_serve("--workdir", str(tmp_path), "--port", str(port))) == 1
        err = capsys.readouterr().err
        assert f"already running on port {port} (pid {os.getpid()})" in err


def test_doctor_lists_every_problem_with_its_fix(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code = main(
        ["doctor", "--binary", "/nonexistent/claude", "--workdir", str(tmp_path / "missing"), "--port", "0"]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "✗ workdir" in out
    assert "✗ agent: Claude Code not found" in out
    assert "fix: Install Claude Code" in out
    assert "✗ pairing: 0 pairing(s)" in out


def test_doctor_passes_on_a_ready_machine(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    PairingStore().create("t")
    code = main(["doctor", "--binary", str(STUB), "--workdir", str(tmp_path), "--port", "0"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "All good." in out
