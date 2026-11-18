"""Limits and defaults shared across the bridge. Changing one changes behaviour."""

from typing import Final

DEFAULT_PORT: Final = 8787
"""``meetstream-agent-kit serve`` listens on 127.0.0.1 at this port unless ``--port`` is given."""

DEFAULT_TURN_TIMEOUT_S: Final = 170.0
"""Longest one meeting turn may run before it is stopped."""

TOOL_RESULT_TIMEOUT_S: Final = 60.0
"""How long the agent waits for MIA to run a meeting tool."""

CONFIRM_WAIT_S: Final = 20.0
"""``confirm`` access: seconds to answer y/N in the terminal; no answer means no."""

STOP_GRACE_S: Final = 2.0
"""An agent that ignores SIGTERM is killed this long after it."""

LOGIN_CACHE_S: Final = 30.0
"""A login check result is reused for this long."""

MAX_BODY_BYTES: Final = 256 * 1024
MAX_FIELD_CHARS: Final = 4000
MAX_INSTRUCTIONS_CHARS: Final = 40_000
MAX_ANSWER_CHARS: Final = 20_000
MAX_TOOLS: Final = 64
MAX_TURN_MESSAGES: Final = 20

MCP_SERVER_NAME: Final = "meeting"
"""Name the meeting tools are served under (the agent sees ``mcp__meeting__*``)."""

APPROVE_TOOL: Final = "approve_action"
"""Internal MCP tool Claude Code calls to ask the owner before acting (``confirm``)."""

MCP_SECRET_ENV: Final = "MEETSTREAM_AGENT_KIT_MCP_SECRET"  # noqa: S105 - a variable name, not a secret
"""Environment variable that carries the per-meeting MCP secret to the agent process."""

HOME_ENV: Final = "MEETSTREAM_AGENT_KIT_HOME"
"""Overrides where pairings and runtime files are kept."""
