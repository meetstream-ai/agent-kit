"""Guided setup: a few questions, then the bridge and its tunnel start together.

Running ``meetstream-agent-kit`` with no command in a terminal lands here. It asks
which agent answers for MIA, checks that agent is installed and signed in, asks
the folder, the most access this computer allows and how to reach it from the
internet (Cloudflare quick tunnel, ngrok, or an address you already have), runs
one test turn, and saves the answers so the next run starts with one Enter.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from meetstream_agent_kit import checks
from meetstream_agent_kit.access import ACCESS_LEVELS
from meetstream_agent_kit.agents import AGENTS, AgentSpec, custom_agent
from meetstream_agent_kit.console import say
from meetstream_agent_kit.login import LoginChecker
from meetstream_agent_kit.tunnel import TUNNEL_KINDS, install_hint, program_for

ACCESS_CHOICES: tuple[tuple[str, str], ...] = (
    ("readonly", "Read-only: reads and searches the folder, changes nothing"),
    ("confirm", "Ask me first: each command or file change waits for a yes here"),
    ("full", "Full: no prompts (only when you alone can talk to it)"),
)


@dataclass
class SetupAnswers:
    agent: str
    command: str
    workdir: str
    max_access: str
    tunnel: str
    tunnel_url: str
    owner: str

    def describe(self) -> str:
        who = self.command or (AGENTS[self.agent].label if self.agent in AGENTS else self.agent)
        where = {
            "cloudflare": "Cloudflare quick tunnel",
            "ngrok": f"ngrok{' ' + self.tunnel_url if self.tunnel_url else ''}",
            "url": self.tunnel_url,
            "none": "no tunnel",
        }.get(self.tunnel, self.tunnel)
        return f"{who}, {self.max_access}, folder {self.workdir}, {where}"


def setup_path(home: Path) -> Path:
    return home / "setup.json"


def load_answers(home: Path) -> SetupAnswers | None:
    try:
        data: Any = json.loads(setup_path(home).read_text(encoding="utf-8"))
        answers = SetupAnswers(**{k: str(data.get(k, "")) for k in SetupAnswers.__dataclass_fields__})
    except (OSError, ValueError, TypeError):
        return None
    if answers.tunnel not in TUNNEL_KINDS or answers.max_access not in ACCESS_LEVELS:
        return None
    if not answers.command and answers.agent not in AGENTS:
        return None
    return answers


def save_answers(home: Path, answers: SetupAnswers) -> None:
    path = setup_path(home)
    path.write_text(json.dumps(asdict(answers), indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


class Prompter:
    """Questions on the terminal. End of input takes the default."""

    def __init__(self, read: Callable[[str], str] = input) -> None:
        self._read = read

    def ask(self, question: str, default: str = "") -> str:
        hint = f" [{default}]" if default else ""
        try:
            answer = self._read(f"{question}{hint}: ").strip()
        except EOFError:
            answer = ""
        return answer or default

    def confirm(self, question: str, default: bool = True) -> bool:
        while True:
            answer = self.ask(f"{question} ({'Y/n' if default else 'y/N'})").lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            say("  Please answer y or n.")

    def choose(self, question: str, options: Sequence[str], default: int = 0) -> int:
        say(question)
        for index, option in enumerate(options, 1):
            say(f"  {index}) {option}")
        while True:
            answer = self.ask("Choose", str(default + 1))
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer) - 1
            say(f"  Type a number from 1 to {len(options)}.")


def _agent_line(agent: AgentSpec, which: Callable[[str], str | None]) -> str:
    found = which(agent.binary) is not None
    return f"{agent.label:<20} {'installed' if found else 'not found'}"


def _pick_agent(prompt: Prompter, which: Callable[[str], str | None], last: str) -> tuple[AgentSpec, str]:
    agents = list(AGENTS.values())
    installed = [i for i, a in enumerate(agents) if which(a.binary) is not None]
    default = next((i for i, a in enumerate(agents) if a.name == last), installed[0] if installed else 0)
    lines = [_agent_line(a, which) for a in agents] + ["Another command (any CLI that reads a prompt)"]
    while True:
        index = prompt.choose("\nWhich agent should answer for MIA?", lines, default)
        if index == len(agents):
            command = prompt.ask("Command ({prompt} is replaced; otherwise the prompt goes to stdin)")
            try:
                return custom_agent(command), command
            except ValueError as exc:
                say(f"  {exc}")
                continue
        agent = agents[index]
        if which(agent.binary) is None:
            say(f"  {agent.label} is not on this computer. {agent.install_hint}")
            say("  Install it, or pick another agent.")
            continue
        return agent, ""


def _check_login(prompt: Prompter, agent: AgentSpec, binary: str, run: Callable[[list[str]], int]) -> None:
    status = LoginChecker(agent, binary, notify=lambda _t, _x: None).check(force=True)
    if status.state == "ok":
        say(f"  {agent.label} is signed in.")
        return
    if status.state == "unknown":
        say(f"  {agent.label} has no login check; the test turn below will tell.")
        return
    say(f"  {agent.label} is signed out on this computer.")
    if agent.relogin and "<" not in agent.relogin and prompt.confirm(f"  Sign in now ({agent.relogin})?"):
        run(shlex.split(agent.relogin))


def _pick_tunnel(
    prompt: Prompter, which: Callable[[str], str | None], last: SetupAnswers | None
) -> tuple[str, str]:
    have_cloudflared = which("cloudflared") is not None
    have_ngrok = which("ngrok") is not None
    options = [
        "Cloudflare quick tunnel: no account needed; the address changes each time it starts"
        + ("" if have_cloudflared else "  (needs cloudflared)"),
        "ngrok: needs a free ngrok account; a free static domain keeps one address"
        + ("" if have_ngrok else "  (needs ngrok)"),
        "I already have an https address that forwards to this computer",
        "No tunnel: I will run my own",
    ]
    kinds = ["cloudflare", "ngrok", "url", "none"]
    default = kinds.index(last.tunnel) if last else (0 if have_cloudflared or not have_ngrok else 1)
    while True:
        kind = kinds[prompt.choose("\nHow should MeetStream reach this computer?", options, default)]
        program = program_for(kind)
        if program and which(program) is None:
            say(f"  {program} is not installed. {install_hint(program)}")
            if not prompt.confirm("  Pick another option?"):
                return kind, ""
            continue
        if kind == "ngrok":
            if not prompt.confirm(
                "  Have you connected ngrok to your account on this computer (ngrok config add-authtoken)?"
            ):
                say("  1. Sign up free at https://dashboard.ngrok.com/signup")
                say("  2. Copy your authtoken from https://dashboard.ngrok.com/get-started/your-authtoken")
                say("  3. Run: ngrok config add-authtoken <your authtoken>")
                if not prompt.confirm("  Done?"):
                    continue
            domain = prompt.ask(
                "  Your ngrok static domain, if you have one (blank for a new address each time)",
                last.tunnel_url if last and last.tunnel == "ngrok" else "",
            )
            return kind, domain.removeprefix("https://").rstrip("/")
        if kind == "url":
            url = prompt.ask(
                "  Your https address (for example a named Cloudflare tunnel)",
                last.tunnel_url if last and last.tunnel == "url" else "",
            )
            if not url.startswith("https://"):
                say("  It must start with https://")
                continue
            return kind, url.rstrip("/")
        return kind, ""


def ask_setup(
    prompt: Prompter,
    last: SetupAnswers | None,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[[list[str]], int] = lambda argv: subprocess.call(argv),
    cwd: str | None = None,
) -> SetupAnswers:
    """All the questions, in order. Returns the answers; nothing starts here."""
    agent, command = _pick_agent(prompt, which, last.agent if last else "claude")
    binary = checks.resolve_binary(agent)
    if binary and not command:
        _check_login(prompt, agent, binary, run)
    default_dir = last.workdir if last else (cwd or str(Path.cwd()))
    while True:
        workdir = str(Path(prompt.ask("\nFolder your agent works in", default_dir)).expanduser().resolve())
        check = checks.check_workdir(Path(workdir))
        if check.ok:
            break
        say(f"  {check.message}" + (f" {check.fix}" if check.fix else ""))
    access_index = prompt.choose(
        "\nMost access this computer allows (the dashboard can only lower it):",
        [label for _, label in ACCESS_CHOICES],
        [name for name, _ in ACCESS_CHOICES].index(last.max_access) if last else 0,
    )
    tunnel, tunnel_url = _pick_tunnel(prompt, which, last)
    owner = prompt.ask(
        "\nYour name as it shows in meetings", last.owner if last else os.environ.get("USER", "")
    )
    return SetupAnswers(
        agent=agent.name,
        command=command,
        workdir=workdir,
        max_access=ACCESS_CHOICES[access_index][0],
        tunnel=tunnel,
        tunnel_url=tunnel_url,
        owner=owner,
    )
