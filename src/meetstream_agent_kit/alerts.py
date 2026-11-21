"""Tell the owner on their desktop when their agent fails in a meeting.

MIA stays quiet when the agent fails, so the owner needs to hear it somewhere
else: one desktop notification (macOS, or Linux with notify-send; always also
a terminal line) per kind of failure per meeting, saying what to do.
"""

from __future__ import annotations

import re
from typing import Any, Final

from meetstream_agent_kit.agents import AgentSpec

TITLE: Final = "MeetStream Agent Kit"

_CREDITS: Final = re.compile(
    r"out of credits|insufficient (?:credits|quota|balance|funds)|exceeded your current quota|quota exceeded"
    r"|credit balance is too low|payment required|usage limit (?:reached|exceeded)|refill",
    re.IGNORECASE,
)
_RATE: Final = re.compile(r"rate.?limit|too many requests|\b429\b|overloaded", re.IGNORECASE)


def failure_kind(event: dict[str, Any]) -> str:
    """``out_of_credits``, ``rate_limited``, ``timeout``, ``crashed``, ``failed``, or '' when it is fine.

    Signed out is told by the login checker already, and a cancelled or busy
    turn is not the agent's failure.
    """
    if event.get("type") == "done":
        return "timeout" if event.get("timed_out") else ""
    if event.get("type") != "error" or event.get("code") in ("auth_required", "cancelled", "busy"):
        return ""
    message = str(event.get("message") or "")
    if _CREDITS.search(message):
        return "out_of_credits"
    if _RATE.search(message):
        return "rate_limited"
    if event.get("code") == "timeout":
        return "timeout"
    if "exited" in message:
        return "crashed"
    return "failed"


def message_for(kind: str, agent: AgentSpec, detail: str = "") -> str:
    """One or two short sentences: what happened, that MIA is quiet, what to do."""
    name = agent.label
    fix = f"run `{agent.relogin}` or switch agents" if agent.relogin else "check your agent or switch agents"
    texts = {
        "out_of_credits": f"{name} ran out of credits. MIA is quiet in your meeting; {fix}.",
        "rate_limited": f"{name} is rate limited. MIA is quiet until it recovers; wait or switch models.",
        "timeout": f"{name} took too long, so the turn was stopped. MIA told the meeting.",
        "crashed": f"{name} stopped unexpectedly. The bridge starts it again on the next turn.",
        "failed": f"{name} failed: {detail[:120]}. MIA is quiet in your meeting.",
    }
    return texts.get(kind, texts["failed"])
