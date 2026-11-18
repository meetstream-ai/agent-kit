"""Supported agent CLIs and how to drive each one.

Every agent is described by an :class:`AgentSpec`: how to run it once, which
long-lived ("warm") protocol it speaks, which flags make it read-only or give it
full access, and how to check its login without side effects.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Final, Literal

WarmDriver = Literal["claude_stream_json", "agy_stream_json", "acp", "codex_app_server"]
"""Long-lived protocols: Claude Code and Antigravity stream-json, ACP, Codex app-server."""

ParserName = Literal["claude_stream_json", "codex_jsonl", "agy_stream_json", "text"]
"""How one-shot output is read (see :mod:`meetstream_agent_kit.parsers`)."""

CONFIRM_DRIVERS: Final[frozenset[str]] = frozenset({"claude_stream_json", "acp", "codex_app_server"})
"""Drivers that can stop and ask before an action, so ``confirm`` access works."""


@dataclass(frozen=True)
class AgentSpec:
    """How the bridge runs one agent CLI."""

    name: str
    label: str
    binary: str
    oneshot_args: tuple[str, ...]
    """Arguments for one non-interactive run; ``{prompt}`` is replaced when ``prompt_via`` is "arg"."""
    prompt_via: Literal["stdin", "arg"]
    parser: ParserName
    relogin: str
    """The command the user runs to sign in again."""
    install_hint: str
    """What to tell the user when the CLI is not installed."""
    warm: WarmDriver | None = None
    acp_args: tuple[str, ...] = ()
    acp_mcp: bool = True
    """False when the agent rejects per-session MCP servers (meeting tools are then not offered)."""
    effort_args: tuple[str, ...] = ()
    readonly_args: tuple[str, ...] = ()
    full_args: tuple[str, ...] = ()
    model_flag: str | None = None
    model_env: str | None = None
    login_check: tuple[str, ...] | None = None
    """Side-effect-free login check (arguments after the binary), or None when the CLI has none."""
    login_ok_codes: tuple[int, ...] = (0,)

    @property
    def can_confirm(self) -> bool:
        """True when the agent can ask before acting, so ``confirm`` access works."""
        return self.warm in CONFIRM_DRIVERS

    def model_args(self, model: str) -> list[str]:
        return [self.model_flag, model] if model and self.model_flag else []

    def effort_flags(self, effort: str) -> list[str]:
        if not effort:
            return []
        return [part.replace("{effort}", effort) for part in self.effort_args]


def _not_on_path(binary: str, label: str) -> str:
    return f"Install {label} and make sure `{binary}` is on your PATH, or pass --binary."


AGENTS: Final[dict[str, AgentSpec]] = {
    spec.name: spec
    for spec in (
        AgentSpec(
            name="claude",
            label="Claude Code",
            binary="claude",
            oneshot_args=(
                "-p",
                "--output-format",
                "stream-json",
                "--include-partial-messages",
                "--verbose",
                "--no-session-persistence",
            ),
            prompt_via="stdin",
            parser="claude_stream_json",
            warm="claude_stream_json",
            effort_args=("--effort", "{effort}"),
            readonly_args=("--tools=Read,Grep,Glob", "--permission-mode", "dontAsk"),
            full_args=("--tools=default", "--permission-mode", "acceptEdits", "--allowedTools=Bash"),
            model_flag="--model",
            login_check=("auth", "status"),
            relogin="claude auth login",
            install_hint="Install Claude Code: npm install -g @anthropic-ai/claude-code",
        ),
        # Codex never streams token deltas one-shot (the answer arrives whole);
        # its warm mode, `codex app-server`, does. Read-only is its default sandbox.
        AgentSpec(
            name="codex",
            label="Codex CLI",
            binary="codex",
            oneshot_args=("exec", "--json", "--ephemeral", "--skip-git-repo-check", "-"),
            prompt_via="stdin",
            parser="codex_jsonl",
            warm="codex_app_server",
            effort_args=("-c", "model_reasoning_effort={effort}"),
            readonly_args=("--sandbox", "read-only"),
            full_args=("--sandbox", "workspace-write"),
            model_flag="-m",
            login_check=("login", "status"),
            relogin="codex login",
            install_hint="Install Codex CLI: npm install -g @openai/codex",
        ),
        # Copilot's JSONL schema is not documented, so -s (answer text only).
        AgentSpec(
            name="copilot",
            label="GitHub Copilot CLI",
            binary="copilot",
            oneshot_args=("-p", "{prompt}", "--no-color", "--no-ask-user", "-s"),
            prompt_via="arg",
            parser="text",
            warm="acp",
            acp_args=("--acp", "--stdio"),
            effort_args=("--effort", "{effort}"),
            readonly_args=("--deny-tool=shell", "--deny-tool=write"),
            full_args=("--allow-tool=shell,write",),
            model_flag="--model",
            relogin="copilot login",
            install_hint="Install GitHub Copilot CLI: npm install -g @github/copilot",
        ),
        # Headless agy soft-denies actions that need review (its default).
        AgentSpec(
            name="agy",
            label="Antigravity CLI",
            binary="agy",
            oneshot_args=("-p", "{prompt}", "--output-format", "stream-json"),
            prompt_via="arg",
            parser="agy_stream_json",
            warm="agy_stream_json",
            effort_args=("--effort", "{effort}"),
            full_args=("--mode=accept-edits",),
            model_flag="--model",
            relogin="agy acp --login",
            install_hint=_not_on_path("agy", "the Antigravity CLI"),
        ),
        AgentSpec(
            name="muse",
            label="Muse Code",
            binary="muse",
            oneshot_args=("exec", "{prompt}"),
            prompt_via="arg",
            parser="text",
            warm="acp",
            acp_args=("acp",),
            full_args=("--disable-approval",),
            relogin="muse   (sign in when prompted)",
            install_hint=_not_on_path("muse", "Muse Code"),
        ),
        # Hermes Agent: ACP for warm sessions, -z for single runs. Under ACP the
        # bridge decides every edit/delete/move/execute permission request.
        AgentSpec(
            name="hermes",
            label="Hermes Agent",
            binary="hermes",
            oneshot_args=("-z", "{prompt}"),
            prompt_via="arg",
            parser="text",
            warm="acp",
            acp_args=("acp",),
            effort_args=("--reasoning", "{effort}"),
            readonly_args=("--safe-mode", "-t", "web,vision,session_search"),
            model_flag="-m",
            model_env="HERMES_INFERENCE_MODEL",
            relogin="hermes setup",
            install_hint=_not_on_path("hermes", "Hermes Agent"),
        ),
        # OpenClaw: turns go through its Gateway. `openclaw acp` rejects
        # per-session MCP servers, so meeting tools are not offered (text only).
        AgentSpec(
            name="openclaw",
            label="OpenClaw",
            binary="openclaw",
            oneshot_args=("agent", "--agent", "main", "-m", "{prompt}"),
            prompt_via="arg",
            parser="text",
            warm="acp",
            acp_args=("acp",),
            acp_mcp=False,
            effort_args=("--thinking", "{effort}"),
            model_flag="--model",
            login_check=("models", "status", "--json", "--check"),
            login_ok_codes=(0, 2),  # 2 = expiring soon, still usable
            relogin="openclaw models auth login --provider <your provider>",
            install_hint=_not_on_path("openclaw", "OpenClaw (and run `openclaw onboard --install-daemon`)"),
        ),
    )
}


def custom_agent(command: str) -> AgentSpec:
    """An agent given as ``--command 'mytool --flag {prompt}'``.

    Output is read as plain text. The prompt goes in place of ``{prompt}`` when
    it appears, otherwise on stdin. It runs once per turn and the bridge cannot
    limit what it does: it has the user's own permissions.
    """
    parts = shlex.split(command)
    if not parts:
        raise ValueError("--command is empty")
    return AgentSpec(
        name="custom",
        label=parts[0],
        binary=parts[0],
        oneshot_args=tuple(parts[1:]),
        prompt_via="arg" if "{prompt}" in parts[1:] else "stdin",
        parser="text",
        relogin="",
        install_hint=_not_on_path(parts[0], parts[0]),
    )
