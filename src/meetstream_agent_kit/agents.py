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
"""Long-lived protocols: Claude Code stream-json, ACP (Antigravity among others), Codex app-server.
``agy_stream_json`` is kept for a custom spec; the built-in Antigravity uses ACP."""

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
    acp_set_model: bool = False
    """The model is chosen per ACP session (``session/set_model``), not by a command-line flag."""
    effort_args: tuple[str, ...] = ()
    readonly_args: tuple[str, ...] = ()
    full_args: tuple[str, ...] = ()
    model_flag: str | None = None
    model_env: str | None = None
    provider_flag: str | None = None
    """For ``provider:model`` ids (Hermes): the provider goes to this flag, the model to ``model_flag``."""
    usage_file_flag: str | None = None
    """One-run flag that writes a JSON usage report to a file (Hermes ``--usage-file``)."""
    login_check: tuple[str, ...] | None = None
    """Side-effect-free login check (arguments after the binary), or None when the CLI has none."""
    login_ok_codes: tuple[int, ...] = (0,)
    install: str = ""
    """The vendor's official install command (a shell line), shown before setup offers to run it."""
    install_needs: tuple[str, ...] = ()
    """Programs the install command needs (checked before it runs)."""
    after_install: str = ""
    """The vendor's first-run setup command, offered right after installing (when there is one)."""

    @property
    def can_confirm(self) -> bool:
        """True when the agent can ask before acting, so ``confirm`` access works."""
        return self.warm in CONFIRM_DRIVERS

    def model_args(self, model: str) -> list[str]:
        if not model or not self.model_flag:
            return []
        provider, name = split_provider(model) if self.provider_flag else ("", model)
        return ([self.provider_flag, provider] if provider and self.provider_flag else []) + [
            self.model_flag,
            name,
        ]

    def effort_flags(self, effort: str) -> list[str]:
        if not effort:
            return []
        return [part.replace("{effort}", effort) for part in self.effort_args]


def split_provider(model: str) -> tuple[str, str]:
    """``nous:anthropic/claude-x`` -> ("nous", "anthropic/claude-x"); ``vendor/model:free`` has none."""
    head, sep, rest = model.partition(":")
    if sep and head and "/" not in head and rest:
        return head, rest
    return "", model


_CURL: Final = "curl -fsSL "
_BASH: Final = " | bash"


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
            install="npm install -g @anthropic-ai/claude-code",
            install_needs=("npm",),
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
            install="npm install -g @openai/codex",
            install_needs=("npm",),
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
            install="npm install -g @github/copilot",
            install_needs=("npm",),
        ),
        # Antigravity: `agy acp` for warm sessions, so the meeting tools reach it as a
        # per-session MCP server (its stream-json mode has no MCP flag). Under ACP it
        # asks Allow/Deny for commands and file changes, and the bridge answers by
        # access level. Single runs (`-p`) soft-deny actions that need review.
        AgentSpec(
            name="agy",
            label="Antigravity CLI",
            binary="agy",
            oneshot_args=("-p", "{prompt}", "--output-format", "stream-json"),
            prompt_via="arg",
            parser="agy_stream_json",
            warm="acp",
            acp_args=("acp",),
            acp_set_model=True,
            effort_args=("--effort", "{effort}"),
            full_args=("--mode=accept-edits",),
            model_flag="--model",
            relogin="agy acp --login",
            install_hint=f"Install the Antigravity CLI: {_CURL}https://antigravity.google/cli/install.sh{_BASH}",
            install="curl -fsSL https://antigravity.google/cli/install.sh | bash",
            install_needs=("curl", "bash"),
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
            install_hint=f"Install Muse Code: {_CURL}https://dev.meta.ai/install.sh{_BASH}",
            install="curl -fsSL https://dev.meta.ai/install.sh | bash",
            install_needs=("curl", "bash"),
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
            provider_flag="--provider",
            usage_file_flag="--usage-file",
            relogin="hermes setup",
            install_hint=f"Install Hermes Agent: {_CURL}https://hermes-agent.nousresearch.com/install.sh{_BASH}",
            install="curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash",
            install_needs=("curl", "bash"),
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
            install_hint=(
                "Install OpenClaw: curl -fsSL https://openclaw.ai/install.sh | bash, "
                "then run: openclaw onboard --install-daemon"
            ),
            install="curl -fsSL https://openclaw.ai/install.sh | bash",
            install_needs=("curl", "bash"),
            after_install="openclaw onboard --install-daemon",
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
