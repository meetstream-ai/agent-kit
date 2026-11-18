"""The ``meetstream-agent-kit`` command.

(no command)                       guided setup in a terminal: questions, then start
``setup``                          the same, on purpose
``pair`` / ``list`` / ``revoke``   manage pairing tokens
``serve``                          run the bridge for MIA (``--tunnel`` opens the tunnel too)
``doctor``                         check this machine and explain each fix
``bench``                          time cold vs warm turns for your agent
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from meetstream_agent_kit import __version__, checks, ui
from meetstream_agent_kit.access import ACCESS_LEVELS
from meetstream_agent_kit.agents import AGENTS, AgentSpec, custom_agent
from meetstream_agent_kit.config import BridgeConfig
from meetstream_agent_kit.console import say
from meetstream_agent_kit.constants import DEFAULT_PORT, DEFAULT_TURN_TIMEOUT_S
from meetstream_agent_kit.login import LoginChecker
from meetstream_agent_kit.oneshot import RunResult, build_oneshot_argv, run_once
from meetstream_agent_kit.pairing import PairingError, PairingStore, default_home
from meetstream_agent_kit.prompts import format_turn, system_prompt
from meetstream_agent_kit.server import BridgeServer
from meetstream_agent_kit.session import MeetingSession
from meetstream_agent_kit.setup import (
    SETUP_STEPS,
    Prompter,
    SetupAbortedError,
    SetupAnswers,
    ask_setup,
    load_answers,
    save_answers,
)
from meetstream_agent_kit.tunnel import TUNNEL_KINDS, Tunnel, TunnelError, TunnelSpec, make_spec

PROG: Final = "meetstream-agent-kit"
TAGLINE: Final = "Your own agent, live in your MeetStream meetings."
TEST_TURN_TIMEOUT_S: Final = 120.0
EXIT_OK: Final = 0
EXIT_FAILED: Final = 1


class CliError(Exception):
    """A problem the user can fix; printed without a traceback."""

    def __init__(self, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.fix = fix


def _print_check(check: checks.Check) -> None:
    mark = "✓" if check.ok else "✗"
    say(f"  {mark} {check.name}: {check.message}")
    if not check.ok and check.fix:
        say(f"      fix: {check.fix}")


def _fail_on(check: checks.Check) -> None:
    if not check.ok:
        raise CliError(check.message, check.fix)


# ── Shared argument handling ──────────────────────────────────────────────────


def _resolve_agent(args: argparse.Namespace) -> AgentSpec:
    if args.command:
        try:
            return custom_agent(args.command)
        except ValueError as exc:
            raise CliError(str(exc)) from exc
    agent: AgentSpec | None = AGENTS.get(args.agent)
    if agent is None:
        raise CliError(
            f"unknown agent {args.agent!r}", f"Choose one of: {', '.join(sorted(AGENTS))}, or use --command."
        )
    return agent


def _build_config(args: argparse.Namespace, *, max_access: str = "readonly") -> BridgeConfig:
    agent = _resolve_agent(args)
    workdir = Path(args.workdir).expanduser().resolve()
    _fail_on(checks.check_workdir(workdir))
    binary = checks.resolve_binary(agent, args.binary)
    if binary is None:
        _fail_on(checks.check_binary(agent, args.binary))
    assert binary is not None  # noqa: S101 - _fail_on raised otherwise
    level = max_access if max_access in ACCESS_LEVELS else "readonly"
    return BridgeConfig(
        agent=agent,
        binary=binary,
        workdir=workdir,
        max_access=level,
        owner=args.owner,
        timeout_s=args.timeout,
        model=args.model or "",
        effort=args.effort or "",
        prime=not getattr(args, "no_prime", False),
    )


# ── Commands ──────────────────────────────────────────────────────────────────


def cmd_pair(args: argparse.Namespace) -> int:
    store = PairingStore()
    pairing, token = store.create(args.label)
    say(f"Paired {pairing.id} ({pairing.label}).")
    say("Save this token in the MeetStream dashboard (Integrations > Models > Local agent).")
    say("It is shown only once:\n")
    say(f"  {token}\n")
    say(f"Revoke it any time with: {PROG} revoke {pairing.id}")
    if args.agent:
        agent = AGENTS.get(args.agent)
        if agent is None:
            raise CliError(f"unknown agent {args.agent!r}", f"Choose one of: {', '.join(sorted(AGENTS))}.")
        binary = checks.resolve_binary(agent)
        if binary is None:
            say(f"{agent.label}: not installed. {agent.install_hint}")
        else:
            status = LoginChecker(agent, binary, notify=lambda title, text: None).check()
            detail = f" ({status.detail})" if status.detail and status.state != "ok" else ""
            say(f"{agent.label} login: {status.state}{detail}")
            if status.state == "expired":
                say(f"  Sign in again with: {agent.relogin}")
    return EXIT_OK


def cmd_list(_: argparse.Namespace) -> int:
    pairings = PairingStore().list()
    if not pairings:
        say(f"No pairings. Create one with: {PROG} pair --agent claude")
    for pairing in pairings:
        say(f"{pairing.id}  {pairing.created_at}  {pairing.label}")
    return EXIT_OK


def cmd_revoke(args: argparse.Namespace) -> int:
    if PairingStore().revoke(args.pairing_id):
        say(f"Revoked {args.pairing_id}. Requests with its token now get 401.")
        return EXIT_OK
    say(f"No pairing {args.pairing_id}. See them with: {PROG} list")
    return EXIT_FAILED


def _tunnel_spec(args: argparse.Namespace) -> TunnelSpec:
    kind = getattr(args, "tunnel", "none") or "none"
    url = getattr(args, "tunnel_url", "") or ""
    try:
        return make_spec(kind, url)
    except TunnelError as exc:
        raise CliError(str(exc), "Use --tunnel url --tunnel-url https://your-address") from exc


def _interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def _stop_on_signals() -> None:
    """kill / a closed terminal stop the bridge like Ctrl-C, so the tunnel and lock file go with it."""
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, _interrupt)


def _print_running(
    config: BridgeConfig,
    port: int,
    tunnel: TunnelSpec,
    public_url: str,
    token: str,
    login_state: str,
    login_detail: str,
) -> None:
    """What is running, then the two things to paste, each on its own line so it is easy to copy."""
    agent = config.agent
    mode = "warm session per meeting" if agent.warm else "one run per turn"
    login = login_state + (f" ({login_detail})" if login_detail and login_state != "ok" else "")
    say("")
    ui.box(
        f"MeetStream Agent Kit {__version__}",
        [
            ("agent", f"{agent.label} ({mode})"),
            ("folder", ui.short_path(str(config.workdir))),
            ("access", config.max_access),
            ("address", f"http://127.0.0.1:{port}"),
            ("login", login),
        ],
    )
    if login_state == "expired":
        ui.fail(f"{agent.label} is signed out. Sign in again with:")
        ui.command(agent.relogin)
    if config.max_access == "full":
        ui.warn("FULL access: the agent may run shell commands and change files without asking, but only in")
        ui.note(
            "meetings where only you (by verified id or activation code) can talk to it; otherwise it asks."
        )
    if agent.warm is None:
        ui.warn("A custom command runs with your own permissions; the bridge cannot limit it.")
    if not (token or public_url):
        say("")
        ui.note("Point your tunnel at it, for example:")
        ui.command(f"cloudflared tunnel --url http://127.0.0.1:{port}")
    else:
        say("")
        say(ui.paint("Next: paste these in the MeetStream dashboard", "bold"))
        number = 1
        if token:
            say(
                f"  {number}. Pairing token  "
                + ui.paint("Integrations > Models > Local agent (shown once)", "dim")
            )
            say(f"     {ui.paint(token, 'green', 'bold')}")
            number += 1
        if public_url:
            say(f"  {number}. Bridge URL     {ui.paint('Agents > your agent > Model > Your agent', 'dim')}")
            say(f"     {ui.paint(public_url, 'cyan', 'bold')}")
            if tunnel.kind == "cloudflare" or (tunnel.kind == "ngrok" and not tunnel.url):
                ui.note(
                    "This address changes each time the tunnel starts; update the Bridge URL when it does."
                )
    stops = " and the tunnel." if public_url and tunnel.kind != "url" else "."
    say("")
    say(f"{ui.paint('●', 'green')} Running. Leave this window open; Ctrl-C stops the bridge{stops}")


def _run_bridge(config: BridgeConfig, port: int, tunnel: TunnelSpec, *, token: str = "") -> int:
    """Start the bridge (and its tunnel), print what to paste in the dashboard, serve until Ctrl-C."""
    home = default_home()
    try:
        server = BridgeServer(("127.0.0.1", port), config)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            _fail_on(checks.port_in_use_message(home, port))
        raise CliError(f"cannot listen on 127.0.0.1:{port}: {exc.strerror}") from exc
    lock = checks.write_lock(home, server.port)
    _stop_on_signals()
    runner = threading.Thread(target=server.serve_forever, daemon=True, name="bridge")
    runner.start()
    tun = Tunnel(tunnel, server.port)
    try:
        if tunnel.kind != "none":
            ui.note(f"Opening the tunnel ({tunnel.kind})...")
        public_url = tun.start()
        login = server.login.check(force=True)
        _print_running(config, server.port, tunnel, public_url, token, login.state, login.detail)
        while runner.is_alive():
            runner.join(0.5)
    except KeyboardInterrupt:
        say("\nstopped")
    except TunnelError as exc:
        raise CliError(f"tunnel: {exc}") from exc
    finally:
        tun.stop()
        server.shutdown()
        server.close_all()
        server.server_close()
        lock.unlink(missing_ok=True)
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    home = default_home()
    _fail_on(checks.check_python())
    _fail_on(checks.check_home(home))
    config = _build_config(args, max_access=args.max_access)
    if not PairingStore(home).list():
        raise CliError("no pairing yet", f"Run: {PROG} pair --agent {config.agent.name}  (or just: {PROG})")
    return _run_bridge(config, args.port, _tunnel_spec(args))


def _test_turn(config: BridgeConfig) -> RunResult:
    """One read-only turn, so a broken login or CLI shows up now and not in the meeting."""
    turn = format_turn([{"speaker": config.owner, "text": "Reply with the single word: ready"}])
    argv, stdin_text = build_oneshot_argv(
        config.agent,
        system_prompt(config.owner, "") + "\n\n" + turn,
        "readonly",
        config.binary,
        config.model,
        config.effort,
    )
    timeout = min(config.timeout_s, TEST_TURN_TIMEOUT_S)
    with ui.spinner(f"Asking {config.agent.label} for one short answer"):
        return run_once(config.agent, argv, stdin_text, str(config.workdir), timeout)


def _check_agent(
    prompt: Prompter,
    config: BridgeConfig,
    *,
    turn: Callable[[BridgeConfig], RunResult] = _test_turn,
    run: Callable[[list[str]], int] = lambda argv: subprocess.call(argv),
) -> str:
    """Test turn until it answers or the user decides: "ok", "anyway" or "quit"."""
    agent = config.agent
    timeout = min(config.timeout_s, TEST_TURN_TIMEOUT_S)
    while True:
        result = turn(config)
        problem = checks.diagnose_turn(agent, result.answer, result.error, result.stderr_tail, timeout)
        if problem is None:
            ui.ok(f"{agent.label} answered in {result.total_ms / 1000:.1f} s: {result.answer.strip()[:80]}")
            return "ok"
        ui.fail(problem.message)
        for line in problem.detail.splitlines()[-4:]:
            ui.note(f"│ {line[:200]}")
        ui.note(f"Fix: {problem.fix}")
        login = checks.login_argv(agent) if problem.kind != "not_installed" else None
        actions: list[tuple[str, str]] = []
        if login:
            actions.append(("login", f"Sign in now ({' '.join(login)}), then test again"))
        actions += [
            ("retry", "Test again"),
            ("anyway", "Start anyway (MIA cannot use your agent until it answers)"),
            ("quit", "Quit"),
        ]
        default = 0 if problem.kind in ("signed_out", "empty", "failed") or not login else 1
        pick = actions[prompt.choose("What now?", [label for _, label in actions], default)][0]
        if prompt.eof:
            return "quit"  # input ended: never loop on a default answer
        if pick == "login" and login:
            ui.note(f"Running {' '.join(login)}. Finish signing in there; this test runs again after.")
            run(login)
        elif pick in ("anyway", "quit"):
            return pick


def _setup_namespace(answers: SetupAnswers) -> argparse.Namespace:
    return argparse.Namespace(
        agent=answers.agent,
        command=answers.command or None,
        binary=None,
        workdir=answers.workdir,
        owner=answers.owner or "the user",
        timeout=DEFAULT_TURN_TIMEOUT_S,
        model=None,
        effort=None,
        no_prime=False,
    )


def cmd_setup(args: argparse.Namespace) -> int:
    """Questions first, then one 'Start now?'; on yes the bridge and tunnel run and print what to paste."""
    home = default_home()
    _fail_on(checks.check_python())
    _fail_on(checks.check_home(home))
    prompt = Prompter()
    last = load_answers(home)
    ui.header("MeetStream Agent Kit", __version__, TAGLINE)
    answers: SetupAnswers | None = None
    if last:
        say("")
        ui.box("Last setup", last.rows())
        if prompt.confirm("Start with it?"):
            answers = last
    if answers is None:
        answers = ask_setup(prompt, last)
        save_answers(home, answers)
    config = _build_config(_setup_namespace(answers), max_access=answers.max_access)
    ui.step(SETUP_STEPS, SETUP_STEPS, "Test")
    if getattr(args, "no_test", False):
        ui.note("Skipped (--no-test).")
    elif _check_agent(prompt, config) == "quit":
        say(f"Saved your answers. Run `{PROG}` again when the agent is ready.")
        return EXIT_FAILED
    store = PairingStore(home)
    token = ""
    pairings = store.list()
    keep = bool(pairings) and prompt.confirm(
        f"Keep using your pairing {pairings[-1].id} ({pairings[-1].label}), already saved in the dashboard?"
    )
    if not keep:
        _pairing, token = store.create("custom agent" if answers.command else answers.agent)
    if not prompt.confirm("Start now?"):
        say(
            f"Saved. Run `{PROG}` any time to start."
            + (f"\nYour new pairing token (shown once): {token}" if token else "")
        )
        return EXIT_OK
    return _run_bridge(
        config,
        getattr(args, "port", DEFAULT_PORT),
        make_spec(answers.tunnel, answers.tunnel_url),
        token=token,
    )


def cmd_doctor(args: argparse.Namespace) -> int:
    home = default_home()
    results = [checks.check_python(), checks.check_home(home)]
    try:
        agent = _resolve_agent(args)
    except CliError as exc:
        results.append(checks.Check("agent", False, str(exc), exc.fix))
        agent = None
    results.append(checks.check_workdir(Path(args.workdir).expanduser().resolve()))
    if agent is not None:
        results.append(checks.check_binary(agent, args.binary))
        binary = checks.resolve_binary(agent, args.binary)
        if binary is not None:
            status = LoginChecker(agent, binary, notify=lambda title, text: None).check(force=True)
            ok = status.state in ("ok", "unknown")
            message = f"{agent.label} {status.state}" + (f" ({status.detail})" if status.detail else "")
            results.append(checks.Check("login", ok, message, "" if ok else f"Run: {agent.relogin}"))
    try:
        count = len(PairingStore(home).list())
        results.append(
            checks.Check(
                "pairing", count > 0, f"{count} pairing(s)", f"Run: {PROG} pair --agent {args.agent}"
            )
        )
    except PairingError as exc:
        results.append(checks.Check("pairing", False, str(exc)))
    results.append(checks.check_port(home, args.port))
    say(f"MeetStream Agent Kit {__version__} doctor")
    for check in results:
        _print_check(check)
    failed = [c for c in results if not c.ok]
    say("All good." if not failed else f"{len(failed)} problem(s) to fix.")
    return EXIT_FAILED if failed else EXIT_OK


def cmd_bench(args: argparse.Namespace) -> int:
    """Cold (one process per turn) vs warm (one long-lived session) timings."""
    config = _build_config(args)
    turn = format_turn([{"speaker": args.owner, "text": args.request}])
    rows: list[dict[str, object]] = []
    for _ in range(args.repeat):
        argv, stdin_text = build_oneshot_argv(
            config.agent,
            system_prompt(config.owner, "") + "\n\n" + turn,
            "readonly",
            config.binary,
            config.model,
            config.effort,
        )
        result = run_once(config.agent, argv, stdin_text, str(config.workdir), config.timeout_s)
        rows.append(
            {
                "kind": "cold",
                "first_output_ms": None
                if result.first_output_ms is None
                else round(result.first_output_ms, 1),
                "total_ms": round(result.total_ms, 1),
                "error": result.error,
            }
        )
    if config.agent.warm:
        session = MeetingSession("bench", config, model=config.model)
        t0 = time.monotonic()
        session.start()
        session.ready.wait(2)  # Claude stream-json prints init only after the first message
        rows.append(
            {
                "kind": "warm_start",
                "spawn_ms": round(session.spawn_ms or 0, 1),
                "ready_ms": round((time.monotonic() - t0) * 1000, 1),
            }
        )
        for _ in range(args.repeat):
            last = [e for e in session.run_turn(turn) if e["type"] in ("done", "error")][-1:]
            event = last[0] if last else {}
            rows.append(
                {
                    "kind": "warm",
                    "first_output_ms": event.get("first_output_ms"),
                    "total_ms": event.get("total_ms"),
                    "error": event.get("message"),
                }
            )
        session.close()
    for row in rows:
        say(json.dumps({"agent": config.agent.name, **row}))
    return EXIT_OK if all(not row.get("error") for row in rows) else EXIT_FAILED


def cmd_mcp_stdio(args: argparse.Namespace) -> int:
    from meetstream_agent_kit import mcp_stdio

    return mcp_stdio.run(args.url)


# ── Parser ────────────────────────────────────────────────────────────────────


def _agent_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--agent", default="claude", help=f"agent CLI: {', '.join(sorted(AGENTS))} (default: claude)"
    )
    parser.add_argument(
        "--command", help="custom agent command; {prompt} is replaced, otherwise stdin is used"
    )
    parser.add_argument("--binary", help="path to the agent CLI (default: found on PATH)")
    parser.add_argument(
        "--workdir", default=str(Path.cwd()), help="folder the agent works in (default: here)"
    )
    parser.add_argument(
        "--owner", default=os.environ.get("USER", "the user"), help="your name, as MIA hears it"
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TURN_TIMEOUT_S, help="seconds per turn")
    parser.add_argument(
        "--model", help="model flag for the CLI (e.g. haiku); MIA's agent config can override"
    )
    parser.add_argument(
        "--effort",
        choices=["minimal", "low", "medium", "high"],
        help="reasoning effort, where the CLI supports it",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Your own agent CLI, live in your meetings through MeetStream MIA.",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    setup = sub.add_parser("setup", help="guided setup: a few questions, then start (also: no command)")
    setup.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"local port (default: {DEFAULT_PORT})")
    setup.add_argument("--no-test", action="store_true", help="skip the test turn")
    setup.set_defaults(func=cmd_setup)

    pair = sub.add_parser("pair", help="create a pairing token (shown once)")
    pair.add_argument("--label", default="my computer", help="a name for this pairing")
    pair.add_argument("--agent", help="also check this agent CLI's login")
    pair.set_defaults(func=cmd_pair)

    sub.add_parser("list", help="list pairings").set_defaults(func=cmd_list)

    revoke = sub.add_parser("revoke", help="revoke a pairing; its token stops working at once")
    revoke.add_argument("pairing_id")
    revoke.set_defaults(func=cmd_revoke)

    serve = sub.add_parser("serve", help="run the bridge on 127.0.0.1 for MIA")
    _agent_options(serve)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"local port (default: {DEFAULT_PORT})")
    serve.add_argument(
        "--max-access",
        "--mode",
        dest="max_access",
        choices=list(ACCESS_LEVELS),
        default="readonly",
        help="most access MIA may use on this machine: readonly (default), confirm (y/N here for each "
        "shell or file action) or full (no prompts). MIA's agent config can only lower it",
    )
    serve.add_argument("--no-prime", action="store_true", help="skip the warm-up turn at meeting start")
    serve.add_argument(
        "--tunnel",
        choices=list(TUNNEL_KINDS),
        default="none",
        help="open a tunnel too and print its https address: cloudflare (quick tunnel, no account), "
        "ngrok (needs an ngrok account), url (an address you already have), none (default)",
    )
    serve.add_argument(
        "--tunnel-url",
        default="",
        help="with --tunnel ngrok: your static domain; with --tunnel url: your https address",
    )
    serve.set_defaults(func=cmd_serve)

    doctor = sub.add_parser("doctor", help="check this machine and show how to fix each problem")
    _agent_options(doctor)
    doctor.add_argument("--port", type=int, default=DEFAULT_PORT)
    doctor.set_defaults(func=cmd_doctor)

    bench = sub.add_parser("bench", help="time cold vs warm turns for your agent")
    _agent_options(bench)
    bench.add_argument("request", nargs="?", default="Reply with one short sentence: what is 2 + 2?")
    bench.add_argument("--repeat", type=int, default=3)
    bench.set_defaults(func=cmd_bench)

    shim = sub.add_parser("mcp-stdio", help=argparse.SUPPRESS)
    shim.add_argument("--url", required=True)
    shim.set_defaults(func=cmd_mcp_stdio)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            args = parser.parse_args(["setup"])
        else:
            parser.print_help()
            return EXIT_FAILED
    try:
        return int(args.func(args))
    except CliError as exc:
        say(f"error: {exc}", err=True)
        if exc.fix:
            say(f"  fix: {exc.fix}", err=True)
        return EXIT_FAILED
    except PairingError as exc:
        say(f"error: {exc}", err=True)
        return EXIT_FAILED
    except SetupAbortedError as exc:
        say(f"error: setup stopped: {exc}", err=True)
        return EXIT_FAILED
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
