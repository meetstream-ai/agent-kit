"""Guided setup and tunnels: address parsing, error messages, reachability, saved answers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from meetstream_agent_kit import tunnel as tunnel_mod
from meetstream_agent_kit.setup import Prompter, SetupAnswers, ask_setup, load_answers, save_answers
from meetstream_agent_kit.tunnel import (
    Tunnel,
    TunnelError,
    TunnelSpec,
    find_cloudflare_url,
    make_spec,
    parse_ngrok_line,
)


def _script(path: Path, body: str) -> str:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_cloudflare_address_is_found_in_its_log_line() -> None:
    line = "2026-11-18T06:00:00Z INF |  https://quiet-river-1234.trycloudflare.com                  |"
    assert find_cloudflare_url(line) == "https://quiet-river-1234.trycloudflare.com"
    assert find_cloudflare_url("INF Starting tunnel") is None


def test_ngrok_lines_give_the_address_or_a_fix() -> None:
    assert parse_ngrok_line('{"msg":"started tunnel","url":"https://abc.ngrok-free.app"}') == (
        "https://abc.ngrok-free.app",
        None,
    )
    _, no_account = parse_ngrok_line('{"lvl":"eror","err":"authentication failed ERR_NGROK_4018"}')
    assert no_account is not None
    assert "add-authtoken" in no_account
    _, busy = parse_ngrok_line('{"err":"limited to 1 simultaneous session ERR_NGROK_108"}')
    assert busy is not None
    assert "already running" in busy
    assert parse_ngrok_line("not json") == (None, None)
    assert parse_ngrok_line('{"err":"<nil>"}') == (None, None)


def test_make_spec_checks_input() -> None:
    assert make_spec("cloudflare") == TunnelSpec("cloudflare", "")
    with pytest.raises(TunnelError, match="https://"):
        make_spec("url", "http://plain.example")
    with pytest.raises(TunnelError, match="unknown tunnel"):
        make_spec("carrier-pigeon")


def test_own_address_is_used_once_it_reaches_the_bridge() -> None:
    seen: list[str] = []

    def check(url: str) -> bool:
        seen.append(url)
        return True

    assert Tunnel(TunnelSpec("url", "https://bridge.example/"), 8787, check=check).start() == (
        "https://bridge.example"
    )
    assert seen == ["https://bridge.example"]


def test_address_that_does_not_reach_the_bridge_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tunnel_mod, "REACH_TIMEOUT_S", 0.01)
    with pytest.raises(TunnelError, match="does not reach this bridge"):
        Tunnel(TunnelSpec("url", "https://elsewhere.example"), 8787, check=lambda _u: False).start()


def test_missing_tunnel_program_says_how_to_install() -> None:
    with pytest.raises(TunnelError, match="cloudflared is not installed"):
        Tunnel(TunnelSpec("cloudflare"), 8787, which=lambda _p: None).start()


@pytest.fixture
def fake_cloudflared(tmp_path: Path) -> str:
    return _script(
        tmp_path / "cloudflared",
        "import sys, time\n"
        "print('INF Requesting new quick Tunnel', file=sys.stderr, flush=True)\n"
        "print('INF |  https://fake-tunnel-42.trycloudflare.com  |', file=sys.stderr, flush=True)\n"
        "print('INF Registered tunnel connection connIndex=0', file=sys.stderr, flush=True)\n"
        "time.sleep(30)\n",
    )


def test_cloudflare_quick_tunnel_address_is_read_and_stopped(fake_cloudflared: str) -> None:
    asked: list[str] = []
    tun = Tunnel(
        TunnelSpec("cloudflare"),
        8787,
        which=lambda _p: fake_cloudflared,
        check=lambda _u: True,
        published=lambda u: asked.append(u) or True,
    )
    assert tun.argv(fake_cloudflared)[-2:] == ["--url", "http://127.0.0.1:8787"]
    try:
        assert tun.start() == "https://fake-tunnel-42.trycloudflare.com"
        assert asked == ["https://fake-tunnel-42.trycloudflare.com"]
    finally:
        tun.stop()


def test_ngrok_without_an_account_explains_the_signup(tmp_path: Path) -> None:
    fake = _script(
        tmp_path / "ngrok",
        "import json\n"
        "print(json.dumps({'lvl': 'eror', 'err': 'authentication failed: ERR_NGROK_4018'}), flush=True)\n",
    )
    tun = Tunnel(TunnelSpec("ngrok", "my-name.ngrok.app"), 8787, which=lambda _p: fake)
    assert tun.argv(fake)[-2:] == ["--url", "my-name.ngrok.app"]
    with pytest.raises(TunnelError, match="add-authtoken"):
        tun.start()


def test_answers_round_trip_and_bad_files_are_ignored(tmp_path: Path) -> None:
    answers = SetupAnswers("hermes", "", str(tmp_path), "confirm", "ngrok", "me.ngrok.app", "Nav")
    save_answers(tmp_path, answers)
    assert load_answers(tmp_path) == answers
    assert (tmp_path / "setup.json").stat().st_mode & 0o777 == 0o600
    (tmp_path / "setup.json").write_text('{"agent": "nope", "tunnel": "cloudflare"}', encoding="utf-8")
    assert load_answers(tmp_path) is None


def test_setup_questions_with_a_custom_command_and_own_address(tmp_path: Path) -> None:
    replies = iter(
        [
            "8",  # another command
            "my-agent --print {prompt}",
            str(tmp_path),  # folder
            "2",  # ask me first
            "3",  # an address I already have
            "https://bridge.example",
            "Nav",
        ]
    )
    answers = ask_setup(Prompter(lambda _q: next(replies)), None, which=lambda _p: None, cwd=str(tmp_path))
    assert answers == SetupAnswers(
        agent="custom",
        command="my-agent --print {prompt}",
        workdir=str(tmp_path.resolve()),
        max_access="confirm",
        tunnel="url",
        tunnel_url="https://bridge.example",
        owner="Nav",
    )


def test_setup_skips_agents_that_are_not_installed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    replies = iter(["1", "8", "my-agent", "", "", "4", ""])
    answers = ask_setup(Prompter(lambda _q: next(replies)), None, which=lambda _p: None, cwd=str(tmp_path))
    assert "Claude Code is not on this computer" in capsys.readouterr().out
    assert answers.command == "my-agent"
    assert answers.max_access == "readonly"
    assert answers.tunnel == "none"


def test_local_lookup_waits_until_public_dns_has_the_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tunnel_mod.time, "sleep", lambda _s: None)
    answers = iter([False, False, True])
    order: list[str] = []

    def published(_url: str) -> bool:
        value = next(answers)
        order.append(f"dns:{value}")
        return value

    def check(_url: str) -> bool:
        order.append("check")
        return True

    tun = Tunnel(TunnelSpec("ngrok"), 8787, check=check, published=published)
    tun.url = "https://x.ngrok-free.app"
    tun._wait_reachable()
    assert order == ["dns:False", "dns:False", "dns:True", "check"]
