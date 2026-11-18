"""The ``meetstream-agent-kit`` command.

``pair`` / ``list`` / ``revoke``   manage pairing tokens
``serve``                          run the bridge for MIA
``doctor``                         check this machine and explain each fix
``bench``                          time cold vs warm turns for your agent
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from meetstream_agent_kit import __version__, checks
from meetstream_agent_kit.access import ACCESS_LEVELS
from meetstream_agent_kit.agents import AGENTS, AgentSpec, custom_agent
from meetstream_agent_kit.config import BridgeConfig
from meetstream_agent_kit.console import say
from meetstream_agent_kit.constants import DEFAULT_PORT, DEFAULT_TURN_TIMEOUT_S
from meetstream_agent_kit.login import LoginChecker
from meetstream_agent_kit.oneshot import build_oneshot_argv, run_once
from meetstream_agent_kit.pairing import PairingError, PairingStore, default_home
from meetstream_agent_kit.prompts import format_turn, system_prompt
from meetstream_agent_kit.server import BridgeServer
from meetstream_agent_kit.session import MeetingSession

PROG: Final = "meetstream-agent-kit"
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


def cmd_serve(args: argparse.Namespace) -> int:
    home = default_home()
    _fail_on(checks.check_python())
    _fail_on(checks.check_home(home))
    config = _build_config(args, max_access=args.max_access)
    if not PairingStore(home).list():
        raise CliError("no pairing yet", f"Run: {PROG} pair --agent {config.agent.name}")
    try:
        server = BridgeServer(("127.0.0.1", args.port), config)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            _fail_on(checks.port_in_use_message(home, args.port))
        raise CliError(f"cannot listen on 127.0.0.1:{args.port}: {exc.strerror}") from exc
    lock = checks.write_lock(home, server.port)
    login = server.login.check(force=True)
    agent = config.agent
    mode = "warm session per meeting" if agent.warm else "one run per turn"
    say(f"MeetStream Agent Kit {__version__}: {agent.label} ({mode}), max access {config.max_access}")
    say(f"  folder:  {config.workdir}")
    say(f"  address: http://127.0.0.1:{server.port}")
    say(f"  login:   {login.state}" + (f" ({login.detail})" if login.detail and login.state != "ok" else ""))
    if login.state == "expired":
        say(f"           sign in again with: {agent.relogin}")
    if config.max_access == "full":
        say("  FULL access: the agent may run shell commands and change files without asking, but only in")
        say(
            "  meetings where only you (by verified id or activation code) can talk to it; otherwise it asks."
        )
    if agent.warm is None:
        say("  note: a custom command runs with your own permissions; the bridge cannot limit it.")
    say(f"Point your tunnel at it, for example:  cloudflared tunnel --url http://127.0.0.1:{server.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("\nstopped")
    finally:
        server.close_all()
        server.server_close()
        lock.unlink(missing_ok=True)
    return EXIT_OK


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
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

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
    args = build_parser().parse_args(argv)
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
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
