"""Check the warm path meetings use, before a meeting does.

Setup's test turn and ``bench`` can run an agent once (``agy -p``), but meetings
keep one warm process (``agy acp`` and the other ACP agents). This check runs
that warm path on its own: a bridge on a free local port, the agent's ACP
process, ``initialize`` and ``session/new`` with the meeting tools through the
stdio shim, and (in setup) one turn asking the agent to call a harmless test
tool, answered here as MIA would. ``doctor`` stops after the handshake, so it
costs no model tokens.
"""

from __future__ import annotations

import dataclasses
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Final

from meetstream_agent_kit.config import BridgeConfig
from meetstream_agent_kit.login import LoginChecker
from meetstream_agent_kit.parsers import looks_like_auth_error
from meetstream_agent_kit.prompts import format_turn
from meetstream_agent_kit.server import BridgeServer

CHECK_TOOL: Final = "check_ping"
CHECK_TOOLS: Final[list[dict[str, Any]]] = [
    {
        "name": CHECK_TOOL,
        "description": "Setup check: returns one word. Call it only when asked to.",
        "parameters": {"type": "object", "properties": {}},
    }
]
TOOL_LIST_WAIT_S: Final = 8.0
"""After the handshake, how long to wait for the agent to load the meeting tools."""


@dataclass(frozen=True)
class WarmCheck:
    ok: bool
    message: str
    fix: str = ""
    tools_listed: int | None = None
    """Meeting tools the agent loaded from the bridge (None: it did not load them)."""
    tool_called: bool = False
    total_ms: float = 0.0


def applies_to(config: BridgeConfig) -> bool:
    """Only ACP agents: their meetings run a different command than the one-shot test."""
    return config.agent.warm == "acp"


def _fail(config: BridgeConfig, message: str, t0: float) -> WarmCheck:
    agent = config.agent
    if looks_like_auth_error(message):
        fix = f"Sign in with: {agent.relogin}" if agent.relogin else "Sign in to the agent CLI."
    else:
        fix = f"Run `{agent.binary} {' '.join(agent.acp_args)}` yourself to see its error."
    return WarmCheck(False, message, fix, total_ms=(time.monotonic() - t0) * 1000)


def check_warm(config: BridgeConfig, *, turn: bool, timeout_s: float = 120.0) -> WarmCheck:
    """Start the agent's warm ACP session with the meeting tools; with ``turn``, also call a test tool."""
    t0 = time.monotonic()
    quiet = dataclasses.replace(config, quiet=True, prime=False, timeout_s=min(config.timeout_s, timeout_s))
    server = BridgeServer(
        ("127.0.0.1", 0), quiet, login=LoginChecker(quiet.agent, quiet.binary, notify=lambda _t, _x: None)
    )
    threading.Thread(target=server.serve_forever, daemon=True, name="warm-check").start()
    offers_tools = quiet.agent.acp_mcp
    try:
        session, _ = server.start_session(
            "warm-check",
            instructions="",
            tools=CHECK_TOOLS if offers_tools else [],
            model=quiet.model,
            access="readonly",
            activation_required=False,
        )
        driver = session.driver
        if driver is None:
            return WarmCheck(False, "this agent has no warm mode", total_ms=(time.monotonic() - t0) * 1000)
        not_ready = driver.check_ready()
        if not_ready:
            return _fail(quiet, not_ready, t0)
        if offers_tools and not turn:
            deadline = time.monotonic() + TOOL_LIST_WAIT_S
            while session.mcp_tools_listed is None and time.monotonic() < deadline:
                time.sleep(0.1)
        if not turn:
            return WarmCheck(
                True, _ready_message(offers_tools, session.mcp_tools_listed, called=False), "",
                session.mcp_tools_listed, False, (time.monotonic() - t0) * 1000,
            )  # fmt: skip
        return _tool_turn(quiet, session, offers_tools, t0)
    finally:
        server.close_all()
        server.shutdown()
        server.server_close()


def _tool_turn(config: BridgeConfig, session: Any, offers_tools: bool, t0: float) -> WarmCheck:
    word = "ready-" + secrets.token_hex(2)
    ask = (
        f"This is a setup check. Call the {CHECK_TOOL} tool once, then reply with only the word it returned."
        if offers_tools
        else "This is a setup check. Reply with the single word: ready"
    )
    called = False
    last: dict[str, Any] = {}
    for event in session.run_turn(format_turn([{"speaker": config.owner, "text": ask}])):
        if event["type"] == "tool_call" and event.get("name") == CHECK_TOOL:
            called = True
            session.resolve_tool(str(event["call_id"]), word, False)
        last = event
    if last.get("type") != "done":
        return _fail(config, str(last.get("message") or "the warm turn did not finish"), t0)
    total = (time.monotonic() - t0) * 1000
    listed = session.mcp_tools_listed
    if offers_tools and not (called and word in str(last.get("text") or "")):
        return WarmCheck(
            False,
            "warm mode answered, but did not call the meeting tool"
            + ("" if listed else " (it never loaded the meeting tools)"),
            "Meeting tools will not work in meetings. Check that the agent accepts MCP servers per session.",
            listed, called, total,
        )  # fmt: skip
    return WarmCheck(True, _ready_message(offers_tools, listed, called), "", listed, called, total)


def _ready_message(offers_tools: bool, listed: int | None, called: bool) -> str:
    if not offers_tools:
        return "Warm mode (ACP) ready · text only (this agent takes no meeting tools)"
    if called:
        return "Warm mode (ACP) ready · meeting tools work (test tool called)"
    if listed:
        return f"Warm mode (ACP) ready · {listed} meeting tool{'s' if listed != 1 else ''} loaded"
    return "Warm mode (ACP) ready · meeting tools offered; the agent loads them on first use"
