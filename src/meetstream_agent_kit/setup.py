"""Guided setup: a few questions, then the bridge and its tunnel start together.

Running ``meetstream-agent-kit`` with no command in a terminal lands here. It asks
which agent answers for MIA, checks that agent is installed and signed in, asks
the folder, the most access this computer allows and how to reach it from the
internet (Cloudflare quick tunnel, ngrok, or an address you already have), runs
one test turn, and saves the answers so the next run starts with one Enter.

On a terminal the questions are arrow-key menus; piped input gets numbered
questions, so the wizard can be scripted and tested.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

from meetstream_agent_kit import checks, ui
from meetstream_agent_kit.access import ACCESS_LEVELS
from meetstream_agent_kit.agents import AGENTS, AgentSpec, custom_agent
from meetstream_agent_kit.console import say
from meetstream_agent_kit.login import LoginChecker
from meetstream_agent_kit.tunnel import (
    NGROK_AUTHTOKEN_URL,
    NGROK_DOMAINS_URL,
    TUNNEL_KINDS,
    NgrokSetup,
    install_hint,
    ngrok_address_is_random,
    probe_ngrok_address,
    program_for,
    read_ngrok_setup,
)

SETUP_STEPS: Final = 6
"""Agent, folder, access, tunnel, name, then the test turn (run by the CLI)."""

ACCESS_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("readonly", "Meeting only", "a meeting companion: reads the folder, changes nothing on this computer"),
    ("confirm", "Ask first", "may do anything on this computer; each command or change waits for your yes"),
    ("full", "Full", "may do anything on this computer without asking; only when you alone can talk to it"),
)

TUNNEL_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("cloudflare", "Cloudflare quick tunnel", "no account; the address changes each time it starts"),
    ("ngrok", "ngrok", "free account; a static domain keeps one address"),
    ("url", "An https address I already have", "for example a named Cloudflare tunnel"),
    ("none", "No tunnel", "I will run my own"),
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

    def agent_label(self) -> str:
        return self.command or (AGENTS[self.agent].label if self.agent in AGENTS else self.agent)

    def tunnel_label(self) -> str:
        return {
            "cloudflare": "Cloudflare quick tunnel",
            "ngrok": f"ngrok{' ' + self.tunnel_url if self.tunnel_url else ' (new address each start)'}",
            "url": self.tunnel_url,
            "none": "no tunnel",
        }.get(self.tunnel, self.tunnel)

    def describe(self) -> str:
        return f"{self.agent_label()}, {self.max_access}, folder {self.workdir}, {self.tunnel_label()}"

    def rows(self) -> list[tuple[str, str]]:
        return [
            ("Agent", self.agent_label()),
            ("Folder", ui.short_path(self.workdir)),
            (
                "Access",
                next(
                    (label for name, label, _ in ACCESS_CHOICES if name == self.max_access), self.max_access
                ),
            ),
            ("Reached by", self.tunnel_label()),
            ("Your name", self.owner or "-"),
        ]


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


def _stdin_is_terminal() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


class SetupAbortedError(Exception):
    """Input ended before setup had the answers it needs."""


class Prompter:
    """Questions on the terminal. End of input takes the default.

    ``interactive`` (a person at a terminal) gives arrow-key menus and answered
    lines that fold up; otherwise questions are numbered and read line by line.
    """

    def __init__(self, read: Callable[[str], str] | None = None, *, interactive: bool | None = None) -> None:
        self._read = read or input
        self.interactive = (read is None and ui.is_interactive()) if interactive is None else interactive
        self.eof = False
        """True once input has ended: loops that would ask again should stop."""

    def _line(self, prompt: str) -> str:
        try:
            answer = self._read(prompt).strip()
        except EOFError:
            self.eof = True
            answer = ""
        if self._read is input and not _stdin_is_terminal():
            say(answer)  # piped answers are not echoed; show them so the log reads like the session
        return answer

    def ask(self, question: str, default: str = "") -> str:
        if not self.interactive:
            hint = f" [{default}]" if default else ""
            return self._line(f"{question}{hint}: ") or default
        typed = self._line(ui.question_prompt(question, default))
        answer = typed or default
        self._fold(len(question) + len(default) + len(typed) + 8)
        ui.answered(question, answer or "(blank)")
        return answer

    def _fold(self, length: int) -> None:
        """Replace the question just answered (which may have wrapped) with its answered line."""
        lines = max(1, -(-length // max(1, ui.width())))
        sys.stdout.write(f"\x1b[{lines}F\x1b[J")
        sys.stdout.flush()

    def confirm(self, question: str, default: bool = True) -> bool:
        while True:
            if self.interactive:
                typed = self._line(ui.question_prompt(question, "Y/n" if default else "y/N")).lower()
                self._fold(len(question) + len(typed) + 12)
            else:
                typed = self.ask(f"{question} ({'Y/n' if default else 'y/N'})").lower()
            if typed in ("", "y", "yes", "n", "no"):
                value = default if not typed else typed.startswith("y")
                if self.interactive:
                    ui.answered(question, "Yes" if value else "No")
                return value
            say("  Please answer y or n.")

    def choose(
        self, question: str, options: Sequence[str], default: int = 0, hints: Sequence[str] = ()
    ) -> int:
        if self.interactive:
            return ui.arrow_select(question.strip(), options, default, hints)
        say(question)
        pad = max(len(option) for option in options) + 2
        for index, option in enumerate(options, 1):
            hint = hints[index - 1] if index - 1 < len(hints) else ""
            say(f"  {index}) {option.ljust(pad)}{hint}".rstrip())
        while True:
            answer = self.ask("Choose", str(default + 1))
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer) - 1
            say(f"  Type a number from 1 to {len(options)}.")


# ── Step 1: the agent ─────────────────────────────────────────────────────────


def _pick_agent(prompt: Prompter, which: Callable[[str], str | None], last: str) -> tuple[AgentSpec, str]:
    agents = list(AGENTS.values())
    installed = [i for i, a in enumerate(agents) if which(a.binary) is not None]
    default = next((i for i, a in enumerate(agents) if a.name == last), installed[0] if installed else 0)
    labels = [a.label for a in agents] + ["Another command"]
    hints = ["installed" if i in installed else "not found" for i in range(len(agents))]
    hints.append("any CLI that reads a prompt")
    while True:
        index = prompt.choose("Which agent should answer for MIA?", labels, default, hints)
        if index == len(agents):
            command = prompt.ask("Command ({prompt} is replaced; otherwise the prompt goes to stdin)")
            try:
                return custom_agent(command), command
            except ValueError as exc:
                ui.fail(str(exc))
                if prompt.eof:
                    raise SetupAbortedError("no agent command given") from exc
                continue
        agent = agents[index]
        if which(agent.binary) is None:
            ui.fail(f"{agent.label} is not on this computer. {agent.install_hint}")
            ui.note("Install it, or pick another agent.")
            if prompt.eof:
                raise SetupAbortedError(f"{agent.label} is not installed")
            continue
        return agent, ""


def _check_login(prompt: Prompter, agent: AgentSpec, binary: str, run: Callable[[list[str]], int]) -> None:
    ui.ok(f"{agent.label} found: {binary}")
    status = LoginChecker(agent, binary, notify=lambda _t, _x: None).check(force=True)
    if status.state == "ok":
        ui.ok(f"{agent.label} is signed in")
        return
    if status.state == "unknown":
        ui.note(f"{agent.label} has no sign-in check; the test turn at the end will tell.")
        return
    ui.fail(f"{agent.label} is signed out on this computer")
    argv = checks.login_argv(agent)
    if argv and prompt.confirm(f"Sign in now ({' '.join(argv)})?"):
        run(argv)


# ── Step 4: how MeetStream reaches this computer ──────────────────────────────


def _install_command(program: str) -> str:
    return install_hint(program).split(": ", 1)[-1]


def _normal_domain(text: str) -> str:
    return text.strip().removeprefix("https://").removeprefix("http://").rstrip("/")


def _ask_ngrok_domain(prompt: Prompter, last_domain: str) -> str:
    ui.note(f"Free ngrok accounts get one static domain: {NGROK_DOMAINS_URL}")
    ui.note("It looks like name.ngrok-free.app (paid plans: your own domain).")
    ui.note("Blank: ngrok gives a new address each start, and you update the Bridge URL")
    ui.note("in your MIA agent (Model > Your agent) every time it changes.")
    while True:
        domain = _normal_domain(prompt.ask("Your ngrok domain (blank for none)", last_domain))
        if not domain or ("." in domain and " " not in domain and "/" not in domain):
            return domain
        ui.fail("That does not look like a domain. Example: quiet-river.ngrok-free.app")
        if prompt.eof:
            return ""


def _setup_ngrok(
    prompt: Prompter,
    last_domain: str,
    detect: Callable[[], NgrokSetup],
    probe: Callable[[str], tuple[str, str]],
) -> str | None:
    """ngrok's domain ('' for a new address each start), or None to pick another way."""
    while True:
        info = detect()
        if info.binary is None:
            ui.fail("ngrok is not installed. Install it, then check again:")
            ui.command(_install_command("ngrok"))
        elif not info.connected:
            ui.fail("ngrok is not connected to an ngrok account on this computer.")
            ui.note(f"1. Sign in (or sign up free) and copy your authtoken: {NGROK_AUTHTOKEN_URL}")
            ui.note("2. Run this in another terminal window:")
            ui.command("ngrok config add-authtoken <your authtoken>")
        else:
            break
        again = prompt.choose("ngrok is not ready", ["Check again", "Pick another way"], 0)
        if again == 1 or prompt.eof:
            return None
    where = f" ({info.config_path})" if info.config_path else ""
    ui.ok(f"ngrok is installed and connected to your account{where}")
    if last_domain and prompt.confirm(f"Keep your ngrok domain {last_domain}?"):
        return last_domain
    if info.domains:
        index = 0
        if len(info.domains) > 1:
            index = prompt.choose("Which ngrok domain?", list(info.domains), 0)
        ui.ok(f"Your ngrok domain: {info.domains[index]}")
        return info.domains[index]
    assert info.binary is not None  # noqa: S101 - checked in the loop above
    with ui.spinner("Asking ngrok which address it gives your account"):
        url, error = probe(info.binary)
        second = ""
        if url and not ngrok_address_is_random(url):
            second, error = probe(info.binary)
    if url and second == url:
        ui.ok(f"Your ngrok address stays the same: {url}")
        return _normal_domain(url)
    if error:
        ui.warn(error)
    elif url:
        ui.warn(f"ngrok gave {url}, but it gives your account a new address each start.")
    return _ask_ngrok_domain(prompt, "")


def _pick_tunnel(
    prompt: Prompter,
    which: Callable[[str], str | None],
    last: SetupAnswers | None,
    detect_ngrok: Callable[[], NgrokSetup] = read_ngrok_setup,
    probe_ngrok: Callable[[str], tuple[str, str]] = probe_ngrok_address,
) -> tuple[str, str]:
    have_cloudflared = which("cloudflared") is not None
    have_ngrok = which("ngrok") is not None
    kinds = [kind for kind, _, _ in TUNNEL_CHOICES]
    labels = [label for _, label, _ in TUNNEL_CHOICES]
    hints = [hint for _, _, hint in TUNNEL_CHOICES]
    if not have_cloudflared:
        hints[0] += "; needs cloudflared"
    if not have_ngrok:
        hints[1] += "; needs ngrok"
    default = kinds.index(last.tunnel) if last else (0 if have_cloudflared or not have_ngrok else 1)
    while True:
        kind = kinds[prompt.choose("How should MeetStream reach this computer?", labels, default, hints)]
        if kind == "ngrok":
            last_domain = last.tunnel_url if last and last.tunnel == "ngrok" else ""
            domain = _setup_ngrok(prompt, last_domain, detect_ngrok, probe_ngrok)
            if domain is None:
                continue
            return kind, domain
        program = program_for(kind)
        if program and which(program) is None:
            ui.fail(f"{program} is not installed. Install it with:")
            ui.command(_install_command(program))
            if not prompt.confirm("Pick another way?"):
                return kind, ""
            continue
        if kind == "url":
            url = prompt.ask(
                "Your https address (for example a named Cloudflare tunnel)",
                last.tunnel_url if last and last.tunnel == "url" else "",
            )
            if not url.startswith("https://"):
                ui.fail("It must start with https://")
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
    detect_ngrok: Callable[[], NgrokSetup] = read_ngrok_setup,
    probe_ngrok: Callable[[str], tuple[str, str]] = probe_ngrok_address,
) -> SetupAnswers:
    """All the questions, in order. Returns the answers; nothing starts here."""
    ui.step(1, SETUP_STEPS, "Agent")
    agent, command = _pick_agent(prompt, which, last.agent if last else "")
    binary = checks.resolve_binary(agent)
    if binary and not command:
        _check_login(prompt, agent, binary, run)

    ui.step(2, SETUP_STEPS, "Folder")
    default_dir = last.workdir if last else (cwd or str(Path.cwd()))
    while True:
        workdir = str(Path(prompt.ask("Folder your agent works in", default_dir)).expanduser().resolve())
        check = checks.check_workdir(Path(workdir))
        if check.ok:
            break
        ui.fail(check.message)

    ui.step(3, SETUP_STEPS, "Access")
    ui.note("The most this computer allows. Your MIA agent's settings can only lower it.")
    access_index = prompt.choose(
        "Most access this computer allows",
        [label for _, label, _ in ACCESS_CHOICES],
        [name for name, _, _ in ACCESS_CHOICES].index(last.max_access) if last else 0,
        [hint for _, _, hint in ACCESS_CHOICES],
    )

    ui.step(4, SETUP_STEPS, "Tunnel")
    tunnel, tunnel_url = _pick_tunnel(prompt, which, last, detect_ngrok, probe_ngrok)

    ui.step(5, SETUP_STEPS, "Your name")
    owner = prompt.ask(
        "Your name as it shows in meetings", last.owner if last else os.environ.get("USER", "")
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
