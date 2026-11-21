"""Muse Code: a launcher whose ~456 MB app never downloaded is not an installed agent."""

from __future__ import annotations

from pathlib import Path

import pytest

from meetstream_agent_kit.agents import AGENTS
from meetstream_agent_kit.setup import Prompter, SetupAbortedError, _pick_agent, launcher_without_app

VERSION = "1.4.25-R9361.1"
MUSE_NUMBER = str(list(AGENTS).index("muse") + 1)


def _launcher(tmp_path: Path) -> Path:
    launcher = tmp_path / "bin" / "muse"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text('#!/usr/bin/env bash\nexec "$binary" "$@"\n', encoding="utf-8")
    launcher.chmod(0o755)
    return launcher


def _download(launcher: Path) -> None:
    (launcher.parent / ".muse-version").write_text(VERSION + "\n", encoding="utf-8")
    app = launcher.parent / f"muse-bin-{VERSION}"
    app.write_bytes(b"\x7fELF")
    app.chmod(0o755)


def _replies(*answers: str) -> Prompter:
    feed = iter(answers)

    def read(_q: str) -> str:
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    return Prompter(read)


def test_the_app_beside_the_launcher_decides(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path)
    assert launcher_without_app(AGENTS["muse"], str(launcher))
    (launcher.parent / ".muse-version").write_text(VERSION, encoding="utf-8")
    assert launcher_without_app(AGENTS["muse"], str(launcher))  # version written, app missing
    _download(launcher)
    assert not launcher_without_app(AGENTS["muse"], str(launcher))
    (launcher.parent / ".muse-version").write_text("garbage", encoding="utf-8")
    assert launcher_without_app(AGENTS["muse"], str(launcher))
    assert not launcher_without_app(AGENTS["claude"], str(launcher))  # only Muse has a launcher


def test_a_symlinked_launcher_is_followed(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path)
    _download(launcher)
    link = tmp_path / "elsewhere" / "muse"
    link.parent.mkdir()
    link.symlink_to(launcher)
    assert not launcher_without_app(AGENTS["muse"], str(link))


def test_setup_offers_the_download_and_a_retry(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    launcher = _launcher(tmp_path)
    runs: list[list[str]] = []

    def run(argv: list[str]) -> int:
        runs.append(argv)
        if len(runs) == 1:
            return 56  # curl: connection reset, nothing downloaded
        _download(launcher)
        return 0

    agent, command = _pick_agent(
        _replies(MUSE_NUMBER, "y", "y"),
        lambda program: str(launcher) if program == "muse" else None,
        "",
        run,
        offer_install=True,
    )
    out = capsys.readouterr().out
    assert (agent.name, command) == ("muse", "")
    assert "launcher only; app not downloaded yet" in out  # the agent menu says so
    assert "only the launcher is installed" in out
    assert "The download did not finish (exit code 56)" in out
    assert "Muse Code app downloaded" in out
    assert runs == [["/usr/bin/env", "MUSE_LAUNCHER_INSTALL=1", str(launcher)]] * 2


def test_declining_the_download_returns_to_the_agent_menu(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path)
    claude = AGENTS["claude"]
    picks = _replies(MUSE_NUMBER, "n", str(list(AGENTS).index("claude") + 1))
    agent, _ = _pick_agent(
        picks,
        lambda program: str(launcher) if program in ("muse", claude.binary) else None,
        "",
        lambda _argv: pytest.fail("must not download after a no"),
        offer_install=True,
    )
    assert agent.name == "claude"


def test_without_a_terminal_setup_shows_the_command_and_does_not_continue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    launcher = _launcher(tmp_path)
    with pytest.raises(SetupAbortedError, match="has not finished downloading"):
        _pick_agent(
            _replies(MUSE_NUMBER),
            lambda program: str(launcher) if program == "muse" else None,
            "",
            lambda _argv: pytest.fail("never downloads without a terminal"),
            offer_install=False,
        )
    assert f"MUSE_LAUNCHER_INSTALL=1 {launcher}" in capsys.readouterr().out
