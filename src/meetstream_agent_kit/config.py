"""Settings for one running bridge."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from meetstream_agent_kit.access import AccessLevel
from meetstream_agent_kit.agents import AgentSpec
from meetstream_agent_kit.console import ask_confirm
from meetstream_agent_kit.constants import DEFAULT_TURN_TIMEOUT_S


@dataclass(frozen=True)
class BridgeConfig:
    agent: AgentSpec
    binary: str
    """Resolved path of the agent CLI."""
    workdir: Path
    """Folder the agent works in."""
    max_access: AccessLevel = "readonly"
    """The highest access this machine allows; MIA can only lower it."""
    owner: str = "the user"
    """The owner's name as MIA hears it."""
    timeout_s: float = DEFAULT_TURN_TIMEOUT_S
    model: str = ""
    effort: str = ""
    prime: bool = True
    """Run one tiny turn at meeting start so the agent's prompt cache is warm."""
    quiet: bool = False
    confirm: Callable[[str], bool] = ask_confirm
    """Asks the owner to allow one action (``confirm`` access)."""
    runtime_dir: Path | None = None
    """Where per-meeting files (Claude's MCP config) are written; defaults to the pairing home."""
