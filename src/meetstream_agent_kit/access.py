"""Access levels: what the agent may do on this machine during a meeting.

The levels mean the same for every agent; each agent maps them to its own
controls (see :mod:`meetstream_agent_kit.agents`):

``readonly`` (default, "Meeting only")
    A meeting companion: the meeting tools, plus reading and searching files in
    the work folder. Nothing on the computer changes.
``confirm`` ("Ask first")
    Anything on the computer, but every shell command or file change is asked
    y/N in the bridge terminal (no answer within 20 s means no). Needs an agent
    that can ask first.
``full``
    Anything on the computer, no prompts. Only when MIA guarantees that only the
    owner, matched by a verified id, can start turns.

The agent's instructions say the same (see :data:`meetstream_agent_kit.prompts.ACCESS_SCOPE`),
for the level the meeting actually runs at.

The bridge's own maximum (``meetstream-agent-kit serve --max-access``) always wins: MIA's
agent config can lower the level for a meeting, never raise it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, TypeGuard

from meetstream_agent_kit.agents import AgentSpec

AccessLevel = Literal["readonly", "confirm", "full"]
ACCESS_LEVELS: Final[tuple[AccessLevel, ...]] = ("readonly", "confirm", "full")


def is_access_level(value: object) -> TypeGuard[AccessLevel]:
    return value in ACCESS_LEVELS


@dataclass(frozen=True)
class AccessDecision:
    level: AccessLevel
    reason: str = ""
    """Why the level is lower than requested, or empty."""


def effective_access(
    agent: AgentSpec, max_access: str, requested: str, owner_verified: bool
) -> AccessDecision:
    """The level one meeting runs at.

    ``max_access`` is the bridge's ceiling, ``requested`` what MIA's agent config
    asks for (empty means the ceiling), ``owner_verified`` whether MIA lets only
    the owner, matched by a verified id, start turns.
    """
    rank = ACCESS_LEVELS.index
    level: AccessLevel = max_access if is_access_level(max_access) else "readonly"
    reason = ""
    if is_access_level(requested):
        if rank(requested) < rank(level):
            level = requested
        elif rank(requested) > rank(level):
            reason = f"this machine allows up to {level} (meetstream-agent-kit serve --max-access)"
    if level == "full" and not owner_verified:
        level, reason = "confirm", "full needs only-me matched by a verified id"
    if level == "confirm" and not agent.can_confirm:
        level, reason = "readonly", f"{agent.label} cannot ask before acting"
    return AccessDecision(level, reason)
