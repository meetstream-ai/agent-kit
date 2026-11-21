"""Setup wizard: ngrok detection, the agent test turn's diagnosis and retry, and plain output."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from meetstream_agent_kit import checks, ui
from meetstream_agent_kit.agents import AGENTS
from meetstream_agent_kit.cli import _check_agent
from meetstream_agent_kit.config import BridgeConfig
from meetstream_agent_kit.oneshot import RunResult, build_oneshot_argv, run_once
from meetstream_agent_kit.parsers import parse_agy
from meetstream_agent_kit.setup import Prompter, SetupAbortedError, ask_setup
from meetstream_agent_kit.tunnel import (
    NgrokSetup,
    ngrok_address_is_random,
    probe_ngrok_address,
    read_ngrok_setup,
)


def _replies(*answers: str) -> Prompter:
    feed: Iterator[str] = iter(answers)

    def read(_prompt: str) -> str:
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    return Prompter(read)


# ── Antigravity output ────────────────────────────────────────────────────────


def test_agy_documented_stream_json_is_read() -> None:
    """agy names each line's kind in "event"; reading only "type" lost every answer."""
    delta = {
        "event": "step_update",
        "step_update": {"state": "ACTIVE", "step_type": "agent_response", "text_delta": "rea"},
    }
    assert parse_agy(json.dumps(delta)).delta == "rea"
    done = {"event": "result", "result": {"status": "SUCCESS", "response": "ready"}}
    assert parse_agy(json.dumps(done)).final == "ready"
    cancelled = {"event": "result", "result": {"status": "CANCELED", "error": "stopped"}}
    assert parse_agy(json.dumps(cancelled)).error == "stopped"
    assert parse_agy(json.dumps({"event": "init", "init": {}})).delta is None


# The sequence a signed-in `agy -p "say hi" --output-format stream-json` printed on Nav's Mac
# (2026-11-18): init, the user's step, the streamed answer, the answer's DONE, then the result.
AGY_REPLAY = [
    {"event": "init", "conversation_id": "c1", "init": {"cwd": "/w", "permission_mode": "request-review"}},
    {
        "event": "step_update",
        "step_update": {"conversation_id": "c1", "step_index": 0, "state": "DONE", "step_type": "user_input"},
    },
    {
        "event": "step_update",
        "step_update": {
            "conversation_id": "c1",
            "step_index": 1,
            "state": "ACTIVE",
            "step_type": "agent_response",
            "text_delta": "Hi! How can I help you today?",
        },
    },
    {
        "event": "step_update",
        "step_update": {
            "conversation_id": "c1",
            "step_index": 1,
            "state": "DONE",
            "step_type": "agent_response",
        },
    },
    {
        "event": "result",
        "result": {
            "conversation_id": "c1",
            "status": "SUCCESS",
            "response": "Hi! How can I help you today?",
            "duration_seconds": 3.1,
            "num_turns": 1,
            "usage": {"input_tokens": 9464, "output_tokens": 66},
        },
    },
]


def test_agy_replay_through_the_real_test_turn(tmp_path: Path) -> None:
    """Regression: the setup test turn said "empty reply" for a signed-in agy (MS-2268)."""
    fake = tmp_path / "agy"
    lines = "\n".join(json.dumps(event) for event in AGY_REPLAY)
    fake.write_text(f"#!/bin/sh\ncat <<'EOF'\n{lines}\nEOF\n", encoding="utf-8")
    fake.chmod(0o755)
    agent = AGENTS["agy"]
    argv, stdin_text = build_oneshot_argv(agent, "say hi", "readonly", str(fake))
    assert argv[1:3] == ["-p", "say hi"]
    result = run_once(agent, argv, stdin_text, str(tmp_path), 30)
    assert result.error is None
    assert result.answer == "Hi! How can I help you today?"
    assert result.deltas == ["Hi! How can I help you today?"]
    assert checks.diagnose_turn(agent, result.answer, result.error, result.stderr_tail) is None


# ── Why the test turn failed ──────────────────────────────────────────────────


def test_diagnosis_names_the_real_reason() -> None:
    agy = AGENTS["agy"]
    assert checks.diagnose_turn(agy, "ready", None) is None
    missing = checks.diagnose_turn(agy, "", "agent CLI not found: agy")
    assert missing is not None
    assert missing.kind == "not_installed"
    signed_out = checks.diagnose_turn(agy, "", "agent exited with 1", "Error: authentication required")
    assert signed_out is not None
    assert signed_out.kind == "signed_out"
    assert "agy acp --login" in signed_out.fix
    assert "authentication required" in signed_out.detail
    slow = checks.diagnose_turn(agy, "", "agent timed out after 120 s", "", 120)
    assert slow is not None
    assert slow.kind == "timeout"
    assert "120 s" in slow.message
    empty = checks.diagnose_turn(agy, "", None, "notice: write_file soft-denied")
    assert empty is not None
    assert empty.kind == "empty"
    assert empty.detail == "notice: write_file soft-denied"


def test_sign_in_commands_that_can_run_as_is() -> None:
    assert checks.login_argv(AGENTS["agy"]) == ["agy", "acp", "--login"]
    assert checks.login_argv(AGENTS["muse"]) == ["muse"]
    assert checks.login_argv(dataclasses.replace(AGENTS["agy"], relogin="tool login <token>")) is None


def _config(tmp_path: Path, agent: str = "agy") -> BridgeConfig:
    return BridgeConfig(agent=AGENTS[agent], binary="/bin/true", workdir=tmp_path, max_access="readonly")


def test_signed_out_agent_signs_in_and_is_tested_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = iter(
        [
            RunResult(error="agent exited with 1", stderr_tail="authentication required"),
            RunResult(answer="ready", total_ms=900),
        ]
    )
    ran: list[list[str]] = []
    outcome = _check_agent(
        _replies(""),  # Enter: the default, "Sign in now"
        _config(tmp_path),
        turn=lambda _c: next(results),
        run=lambda argv: ran.append(argv) or 0,
    )
    out = capsys.readouterr().out
    assert outcome == "ok"
    assert ran == [["agy", "acp", "--login"]]
    assert "Antigravity CLI is signed out" in out
    assert "Antigravity CLI answered in 0.9 s: ready" in out


def test_failed_test_turn_offers_start_anyway(tmp_path: Path) -> None:
    outcome = _check_agent(
        _replies("3"),  # Start anyway
        _config(tmp_path),
        turn=lambda _c: RunResult(),
        run=lambda _argv: 0,
    )
    assert outcome == "anyway"


def test_ended_input_never_loops_on_the_default(tmp_path: Path) -> None:
    calls: list[int] = []

    def turn(_c: BridgeConfig) -> RunResult:
        calls.append(1)
        return RunResult()

    assert _check_agent(_replies(), _config(tmp_path), turn=turn, run=lambda _a: 0) == "quit"
    assert calls == [1]


def test_ended_input_stops_setup_instead_of_looping(tmp_path: Path) -> None:
    with pytest.raises(SetupAbortedError):
        ask_setup(_replies("1"), None, which=lambda _p: None, cwd=str(tmp_path))


# ── ngrok ─────────────────────────────────────────────────────────────────────


def _ngrok_runner(config: Path, reserved: list[str] | None = None) -> Any:
    def run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:3] == ["config", "check"]:
            return subprocess.CompletedProcess(argv, 0, f"Valid configuration file at {config}\n", "")
        if argv[1:3] == ["api", "reserved-domains"]:
            body = {"reserved_domains": [{"domain": d} for d in reserved or []]}
            return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")
        raise AssertionError(argv)

    return run


def test_ngrok_config_tells_connected_and_domain(tmp_path: Path) -> None:
    config = tmp_path / "ngrok.yml"
    config.write_text(
        'version: "3"\nagent:\n  authtoken: secret-token\nendpoints:\n'
        "  - name: bridge\n    url: https://quiet-river.ngrok-free.app\n    upstream:\n      url: 8787\n",
        encoding="utf-8",
    )
    found = read_ngrok_setup(which=lambda _p: "/usr/bin/ngrok", run=_ngrok_runner(config), env={})
    assert found == NgrokSetup("/usr/bin/ngrok", str(config), True, ("quiet-river.ngrok-free.app",))
    assert "secret-token" not in repr(found)


def test_ngrok_without_authtoken_is_not_connected(tmp_path: Path) -> None:
    config = tmp_path / "ngrok.yml"
    config.write_text('version: "3"\n', encoding="utf-8")
    found = read_ngrok_setup(which=lambda _p: "/usr/bin/ngrok", run=_ngrok_runner(config), env={})
    assert not found.connected
    assert read_ngrok_setup(
        which=lambda _p: "/x", run=_ngrok_runner(config), env={"NGROK_AUTHTOKEN": "t"}
    ).connected
    assert read_ngrok_setup(which=lambda _p: None) == NgrokSetup(None)


def test_ngrok_api_key_lists_reserved_domains(tmp_path: Path) -> None:
    config = tmp_path / "ngrok.yml"
    config.write_text("agent:\n  authtoken: t\n  api_key: k\n", encoding="utf-8")
    found = read_ngrok_setup(
        which=lambda _p: "/usr/bin/ngrok", run=_ngrok_runner(config, ["me.ngrok.app"]), env={}
    )
    assert found.domains == ("me.ngrok.app",)


def test_random_ngrok_addresses_are_recognised() -> None:
    assert ngrok_address_is_random("https://67ed-136-152-209-75.ngrok-free.app")
    assert not ngrok_address_is_random("https://quiet-river.ngrok-free.app")


def test_probe_reads_the_address_ngrok_prints(tmp_path: Path) -> None:
    fake = tmp_path / "ngrok"
    fake.write_text(
        f"#!{sys.executable}\nimport json, time\n"
        "line = {'msg': 'started tunnel', 'url': 'https://quiet-river.ngrok-free.app'}\n"
        "print(json.dumps(line), flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    assert probe_ngrok_address(str(fake), timeout_s=10) == ("https://quiet-river.ngrok-free.app", "")


def _tunnel_answers(
    tmp_path: Path, replies: Prompter, detect: NgrokSetup, probe: Any = None
) -> tuple[str, str]:
    answers = ask_setup(
        replies,
        None,
        which=lambda p: "/usr/bin/ngrok" if p == "ngrok" else None,
        cwd=str(tmp_path),
        detect_ngrok=lambda: detect,
        probe_ngrok=probe or (lambda _b: ("", "not called")),
    )
    return answers.tunnel, answers.tunnel_url


def _to_tunnel(*rest: str) -> Prompter:
    # another command, its command line, folder (default), access (default), then the tunnel answers
    return _replies("8", "my-agent", "", "", *rest)


def test_ngrok_domain_from_config_needs_no_question(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    detect = NgrokSetup("/usr/bin/ngrok", "/cfg/ngrok.yml", True, ("quiet-river.ngrok-free.app",))
    assert _tunnel_answers(tmp_path, _to_tunnel("2", "Nav"), detect) == (
        "ngrok",
        "quiet-river.ngrok-free.app",
    )
    out = capsys.readouterr().out
    assert "Have you connected" not in out
    assert "ngrok is installed and connected" in out


def test_ngrok_static_address_is_found_by_starting_it(tmp_path: Path) -> None:
    detect = NgrokSetup("/usr/bin/ngrok", "", True)
    probes: list[str] = []

    def probe(binary: str) -> tuple[str, str]:
        probes.append(binary)
        return "https://quiet-river.ngrok-free.app", ""

    got = _tunnel_answers(tmp_path, _to_tunnel("2", "Nav"), detect, probe)
    assert got == ("ngrok", "quiet-river.ngrok-free.app")
    assert probes == ["/usr/bin/ngrok", "/usr/bin/ngrok"]  # started twice: same address = static


def test_ngrok_new_address_each_start_asks_with_a_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    detect = NgrokSetup("/usr/bin/ngrok", "", True)
    probe = iter([("https://67ed-136-152-209-75.ngrok-free.app", "")])
    got = _tunnel_answers(
        tmp_path, _to_tunnel("2", "https://me.ngrok-free.app/", "Nav"), detect, lambda _b: next(probe)
    )
    out = capsys.readouterr().out
    assert got == ("ngrok", "me.ngrok-free.app")
    assert "dashboard.ngrok.com/domains" in out
    assert "update the Bridge URL" in out


def test_ngrok_not_connected_shows_the_one_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    detect = NgrokSetup("/usr/bin/ngrok", "", False)
    got = _tunnel_answers(tmp_path, _to_tunnel("2", "2", "4", "Nav"), detect)  # pick another way, then none
    out = capsys.readouterr().out
    assert got == ("none", "")
    assert "ngrok config add-authtoken <your authtoken>" in out
    assert "dashboard.ngrok.com/get-started/your-authtoken" in out


# ── Plain output when not on a terminal ──────────────────────────────────────


def test_plain_output_has_no_escape_codes(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    ui.ok("done")
    ui.box("Ready", [("agent", "Claude Code")])
    ui.step(1, 6, "Agent")
    with ui.spinner("Working"):
        pass
    out = capsys.readouterr().out
    assert "\x1b[" not in out
    assert "✓ done" in out
    assert "Working..." in out
    monkeypatch.setenv("FORCE_COLOR", "1")
    ui.ok("done")
    assert "\x1b[" in capsys.readouterr().out


# ── Arrow-key menu on a real terminal ────────────────────────────────────────


def _menu_in_a_terminal(
    keys: list[bytes], gap_s: float = 0.15, *, detail: bool = False, toolkit: bool = False
) -> str:
    """Run Prompter.choose in a pseudo-terminal, press ``keys``, return what the child printed.

    ``toolkit`` False tests the built-in menu (the dependency-free .pyz fallback); True the
    prompt_toolkit one that installed kits use."""
    import os
    import pty
    import select
    import time

    code = (
        "import os\n"
        "from meetstream_agent_kit import tui\n"
        f"tui.AVAILABLE = {toolkit!r}\n"
        "os.environ['PROMPT_TOOLKIT_NO_CPR'] = '1'\n"
        "from meetstream_agent_kit.setup import Prompter\n"
        "p = Prompter()\n"
        "assert p.interactive\n"
        f"detail = (lambda i: f'details: {{i}}') if {detail!r} else None\n"
        "i = p.choose('Pick one', ['alpha', 'beta', 'gamma'], 0, ['first', '', 'third'], detail=detail)\n"
        "print('PICKED', i, flush=True)\n"
    )
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - the child
        os.execv(sys.executable, [sys.executable, "-c", code])  # noqa: S606 - our own interpreter
    import fcntl
    import struct
    import termios

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    out = b""

    def read_for(seconds: float) -> None:
        nonlocal out
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if select.select([fd], [], [], 0.05)[0]:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    return
                if not chunk:
                    return
                out += chunk

    read_for(1.5)
    for key in keys:
        os.write(fd, key)
        read_for(gap_s)
    read_for(1.5)
    os.waitpid(pid, 0)
    os.close(fd)
    return out.decode("utf-8", "replace")


@pytest.mark.skipif(sys.platform == "win32", reason="pseudo-terminals are Unix only")
def test_arrow_keys_pick_an_option() -> None:
    out = _menu_in_a_terminal([b"\x1b[B", b"\x1b[B", b"\x1b[A", b"\r"])
    assert "PICKED 1" in out
    assert "↑/↓ to move" in out
    assert "beta" in out


@pytest.mark.skipif(sys.platform == "win32", reason="pseudo-terminals are Unix only")
def test_an_arrow_key_that_arrives_in_pieces_still_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Over a slow link the escape byte and the rest of an arrow key come in separate reads.

    No race with the clock: the rest is written only after the reader has taken the escape
    byte, and the wait is generous here (CI runners can pause a thread for a while).
    """
    import os
    import threading
    import time

    from meetstream_agent_kit import ui

    monkeypatch.setattr(ui, "ESC_SEQUENCE_WAIT_S", 10.0)
    read_fd, write_fd = os.pipe()
    got: list[str] = []
    try:
        os.write(write_fd, b"\x1b")
        reader = threading.Thread(target=lambda: got.append(ui._read_key(read_fd)))
        reader.start()
        deadline = time.monotonic() + 10
        while select_ready(read_fd) and time.monotonic() < deadline:  # wait until the escape byte is taken
            time.sleep(0.01)
        os.write(write_fd, b"[B")
        reader.join(15)
        assert got == ["\x1b[B"]
    finally:
        os.close(read_fd)
        os.close(write_fd)


def select_ready(fd: int) -> bool:
    import select

    return bool(select.select([fd], [], [], 0)[0])


# ── Installing a missing agent from setup ────────────────────────────────────

_OFFICIAL_SOURCES = (
    "npm install -g @anthropic-ai/claude-code",
    "npm install -g @openai/codex",
    "npm install -g @github/copilot",
    "curl -fsSL https://antigravity.google/cli/install.sh | bash",
    "curl -fsSL https://dev.meta.ai/install.sh | bash",
    "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash",
    "curl -fsSL https://openclaw.ai/install.sh | bash",
)


def test_every_built_in_agent_has_its_vendors_install_command() -> None:
    assert sorted(a.install for a in AGENTS.values()) == sorted(_OFFICIAL_SOURCES)
    for agent in AGENTS.values():
        assert agent.install in agent.install_hint  # doctor and notifications show the same command
        assert agent.install_needs


class _Machine:
    """A pretend computer: which programs exist, and what installers do."""

    def __init__(self, *present: str, installs: bool = True, exit_code: int = 0) -> None:
        self.present = set(present)
        self.installs = installs
        self.exit_code = exit_code
        self.ran: list[list[str]] = []

    def which(self, program: str) -> str | None:
        return f"/usr/bin/{program}" if program in self.present else None

    def run(self, argv: list[str]) -> int:
        self.ran.append(argv)
        if argv[:2] == ["/bin/sh", "-c"] and self.installs:
            binary = next(a.binary for a in AGENTS.values() if a.install == argv[2])
            self.present.add(binary)
        return self.exit_code


def _setup(tmp_path: Path, machine: _Machine, *replies: str, offer: bool = True) -> Any:
    rest = ("", "", "4", "Nav")  # folder, access, no tunnel, name
    return ask_setup(
        _replies(*replies, *rest),
        None,
        which=machine.which,
        run=machine.run,
        cwd=str(tmp_path),
        offer_install=offer,
    )


def test_missing_agent_is_installed_then_signed_in(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    machine = _Machine("curl", "bash")
    answers = _setup(tmp_path, machine, "4", "", "")  # Antigravity, install: yes, sign in: yes
    out = capsys.readouterr().out
    assert answers.agent == "agy"
    assert machine.ran == [
        ["/bin/sh", "-c", "curl -fsSL https://antigravity.google/cli/install.sh | bash"],
        ["agy", "acp", "--login"],  # signing in is the agent's own prompt, run in the foreground
    ]
    assert "Official install command for Antigravity CLI" in out
    assert "Antigravity CLI installed: /usr/bin/agy" in out


def test_declining_the_install_runs_nothing(tmp_path: Path) -> None:
    machine = _Machine("curl", "bash")
    answers = _setup(tmp_path, machine, "4", "n", "8", "my-agent")  # no, then another command
    assert machine.ran == []
    assert answers.command == "my-agent"


def test_a_missing_prerequisite_is_named_with_where_to_get_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    machine = _Machine()  # no npm
    _setup(tmp_path, machine, "1", "8", "my-agent")
    out = capsys.readouterr().out
    assert "npm install -g @anthropic-ai/claude-code" in out
    assert "needs npm" in out
    assert "nodejs.org" in out
    assert machine.ran == []


def test_a_failed_installer_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    machine = _Machine("npm", installs=False, exit_code=1)
    _setup(tmp_path, machine, "2", "", "8", "my-agent")
    assert "exit code 1" in capsys.readouterr().out
    assert machine.ran == [["/bin/sh", "-c", "npm install -g @openai/codex"]]


def test_no_terminal_only_shows_the_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    machine = _Machine("curl", "bash")
    _setup(tmp_path, machine, "6", "8", "my-agent", offer=False)
    out = capsys.readouterr().out
    assert "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash" in out
    assert "now?" not in out
    assert machine.ran == []


def test_an_agent_installed_off_path_is_found_and_explained(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from meetstream_agent_kit import setup as setup_mod

    bin_dir = tmp_path / "local-bin"
    bin_dir.mkdir()
    monkeypatch.setattr(setup_mod, "_INSTALL_DIRS", (bin_dir,))
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))  # restored after the test

    class OffPath(_Machine):
        def run(self, argv: list[str]) -> int:
            self.ran.append(argv)
            if argv[:2] == ["/bin/sh", "-c"]:
                (bin_dir / "muse").write_text("#!/bin/sh\n", encoding="utf-8")
                (bin_dir / "muse").chmod(0o755)
                # Meta's installer also downloads the app beside its launcher
                (bin_dir / ".muse-version").write_text("1.4.25-R9361.1\n", encoding="utf-8")
                (bin_dir / "muse-bin-1.4.25-R9361.1").write_text("app", encoding="utf-8")
                (bin_dir / "muse-bin-1.4.25-R9361.1").chmod(0o755)
            return 0

    machine = OffPath("curl", "bash")
    answers = _setup(tmp_path, machine, "5", "", "n")  # Muse, install, do not sign in now
    out = capsys.readouterr().out
    assert answers.agent == "muse"
    assert f'export PATH="{bin_dir}:$PATH"' in out
    assert os.environ["PATH"].startswith(str(bin_dir))
    assert machine.ran == [["/bin/sh", "-c", "curl -fsSL https://dev.meta.ai/install.sh | bash"]]


def test_openclaw_offers_its_own_first_run_setup(tmp_path: Path) -> None:
    machine = _Machine("curl", "bash")
    _setup(tmp_path, machine, "7", "", "")
    assert machine.ran[-1] == ["openclaw", "onboard", "--install-daemon"]


@pytest.mark.skipif(sys.platform == "win32", reason="pseudo-terminals are Unix only")
def test_prompt_toolkit_menu_moves_shows_detail_and_key_hints() -> None:
    out = _menu_in_a_terminal([b"\x1b[B", b"\r"], detail=True, toolkit=True)
    assert "PICKED 1" in out
    assert "details: 0" in out
    # prompt_toolkit redraws only the characters that change, so "details: 1" is one digit here;
    # tests/test_tui.py checks that the detail line follows the arrow.
    assert "↑↓ move" in out
    assert "Enter pick" in out
    assert "beta" in out
