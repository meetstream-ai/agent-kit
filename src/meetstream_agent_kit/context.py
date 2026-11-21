"""What your agent reads in meetings, and your own meeting instructions (MEETING.md).

Agent CLIs read files on every turn: the work folder's project rules (CLAUDE.md,
AGENTS.md...) and their own global memory (~/.claude/CLAUDE.md, ~/.hermes/SOUL.md...).
A 29 KB CLAUDE.md is about 7k tokens on every meeting turn: slower and costlier
answers about the wrong things. This module lists those files with their size,
and keeps ``MEETING.md`` in the meeting workspace: short instructions the bridge
adds to MIA's rules, which you edit with ``meetstream-agent-kit context edit``.

Which files each CLI reads is from its docs (and Hermes' source: AGENTS.md,
CLAUDE.md, .cursorrules plus SOUL.md and memories).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meetstream_agent_kit.agents import AgentSpec

MEETING_FILE: Final = "MEETING.md"
MAX_MEETING_CHARS: Final = 4000
"""MEETING.md beyond this is cut: it rides along on every turn."""

DEFAULT_MEETING_MD: Final = """\
# Meeting instructions

These go to your agent in every MeetStream meeting. Keep them short: every
line is read on every turn.

- Answer in one or two spoken sentences unless asked for more.
- If you are not sure, say so instead of guessing.
"""

_PROJECT_FILES: Final[dict[str, tuple[str, ...]]] = {
    "claude": ("CLAUDE.md", "CLAUDE.local.md"),
    "codex": ("AGENTS.md",),
    "agy": ("GEMINI.md", "AGENTS.md"),
    "hermes": ("AGENTS.md", "CLAUDE.md", ".cursorrules"),
    "copilot": ("AGENTS.md", "CLAUDE.md", "GEMINI.md", ".github/copilot-instructions.md"),
}
_ANY_PROJECT_FILES: Final = (
    "AGENTS.md",
    "CLAUDE.md",
    "CLAUDE.local.md",
    "GEMINI.md",
    ".cursorrules",
    ".github/copilot-instructions.md",
)
_GLOBAL_FILES: Final[dict[str, tuple[str, ...]]] = {
    "claude": (".claude/CLAUDE.md",),
    "codex": (".codex/AGENTS.md",),
    "agy": (".gemini/GEMINI.md",),
    "hermes": (".hermes/SOUL.md", ".hermes/memories/MEMORY.md", ".hermes/memories/USER.md"),
}


@dataclass(frozen=True)
class ContextItem:
    name: str
    """As shown: ``CLAUDE.md``, ``~/.hermes/SOUL.md``, ``MEETING.md``."""
    path: Path
    size: int
    loaded: bool
    note: str = ""

    @property
    def tokens(self) -> int:
        return self.size // 4


WORKSPACE_ENV: Final = "MEETSTREAM_AGENT_KIT_WORKSPACE"


def meeting_workspace() -> Path:
    """The default work folder (``~/MeetStream Agent``, or ``$MEETSTREAM_AGENT_KIT_WORKSPACE``):
    no project rules ride along on every meeting turn."""
    override = os.environ.get(WORKSPACE_ENV)
    return Path(override).expanduser() if override else Path.home() / "MeetStream Agent"


def meeting_file() -> Path:
    return meeting_workspace() / MEETING_FILE


def ensure_meeting_file() -> Path:
    """MEETING.md in the workspace, created with a short default the first time."""
    path = meeting_file()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_MEETING_MD, encoding="utf-8")
    return path


def meeting_notes() -> str:
    """MEETING.md's text for the agent, cut to MAX_MEETING_CHARS; '' when there is none."""
    try:
        return meeting_file().read_text(encoding="utf-8").strip()[:MAX_MEETING_CHARS]
    except OSError:
        return ""


def project_files_for(agent: AgentSpec) -> tuple[str, ...]:
    return _PROJECT_FILES.get(agent.name, _ANY_PROJECT_FILES)


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


def effective_context(
    agent: AgentSpec,
    workdir: Path,
    *,
    ignore_rules: bool,
    use_meeting_md: bool = True,
    home: Path | None = None,
) -> list[ContextItem]:
    """Every file the agent gets in a meeting turn, and whether it is loaded."""
    home = home or Path.home()
    can_skip = bool(agent.ignore_rules_args) and agent.ignore_rules_warm
    items: list[ContextItem] = []
    for name in project_files_for(agent):
        size = _size(workdir / name)
        if size is None:
            continue
        skipped = ignore_rules and can_skip
        note = "skipped" if skipped else ("this agent cannot skip it" if ignore_rules else "")
        items.append(ContextItem(name, workdir / name, size, not skipped, note))
    for rel in _GLOBAL_FILES.get(agent.name, ()):
        size = _size(home / rel)
        if size is not None:
            items.append(ContextItem(f"~/{rel}", home / rel, size, True, "your agent's own memory"))
    size = _size(meeting_file())
    if size is not None:
        items.append(
            ContextItem(MEETING_FILE, meeting_file(), min(size, MAX_MEETING_CHARS), use_meeting_md, "yours")
        )
    return items


def tokens_per_turn(items: list[ContextItem]) -> int:
    return sum(item.tokens for item in items if item.loaded)


def mode_label(workdir: Path, ignore_rules: bool) -> str:
    if workdir.resolve() == meeting_workspace().resolve():
        return "meeting only"
    return "project, rules ignored" if ignore_rules else "project rules included"


def summary(agent: AgentSpec, workdir: Path, *, ignore_rules: bool, use_meeting_md: bool = True) -> str:
    """``meeting only · 0.4k tokens/turn``"""
    items = effective_context(agent, workdir, ignore_rules=ignore_rules, use_meeting_md=use_meeting_md)
    return f"{mode_label(workdir, ignore_rules)} · {tokens_per_turn(items) / 1000:.1f}k tokens/turn"


def table(items: list[ContextItem]) -> list[str]:
    """Rows for the terminal: name, size, tokens, loaded or not."""
    if not items:
        return ["(nothing extra: only MIA's rules and the meeting)"]
    width = max(len(item.name) for item in items)
    rows = []
    for item in items:
        mark = "✓" if item.loaded else "·"
        note = f"  {item.note}" if item.note else ""
        rows.append(
            f"{mark} {item.name.ljust(width)}  {item.size / 1024:5.1f} KB  ≈ {item.tokens:>6,} tokens{note}"
        )
    return rows
