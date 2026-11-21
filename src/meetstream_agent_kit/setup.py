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

import dataclasses
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

from meetstream_agent_kit import checks, context, estimates, tui, ui
from meetstream_agent_kit.access import ACCESS_LEVELS
from meetstream_agent_kit.agents import AGENTS, AgentSpec, custom_agent
from meetstream_agent_kit.compare import Measured, chart_lines, compare, measure
from meetstream_agent_kit.config import BridgeConfig
from meetstream_agent_kit.console import say
from meetstream_agent_kit.login import LoginChecker
from meetstream_agent_kit.models import ModelCatalog, ModelChoice, catalog_for, features, unique
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
from meetstream_agent_kit.usage import project_hour

SETUP_STEPS: Final = 7
"""Agent, model, folder, access, tunnel, name, then the test turn (run by the CLI)."""

MODEL_MENU_SIZE: Final = 12
"""Models shown in the menu; the rest are reached with "Type another model id"."""
COMPARE_MAX: Final = 4

PROJECT_RULE_FILES: Final = (
    "AGENTS.md",
    "CLAUDE.md",
    "CLAUDE.local.md",
    "GEMINI.md",
    ".cursorrules",
    ".github/copilot-instructions.md",
)
"""Files agent CLIs read from the work folder on every turn."""


meeting_workspace = context.meeting_workspace


def project_rules(folder: Path) -> list[tuple[str, int]]:
    """(file, bytes) for each project rules file in ``folder``."""
    found = []
    for name in PROJECT_RULE_FILES:
        try:
            size = (folder / name).stat().st_size
        except OSError:
            continue
        found.append((name, size))
    return found


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
    model: str = ""
    """Empty: the CLI's own default model."""
    meeting_md: str = ""
    """"no": leave MEETING.md out of meetings."""
    ignore_rules: str = ""
    """"yes": skip the work folder's project rules in meetings, where the agent CLI can."""
    effort: str = ""
    """Empty: the CLI's own default effort."""

    def agent_label(self) -> str:
        return self.command or (AGENTS[self.agent].label if self.agent in AGENTS else self.agent)

    def tunnel_label(self) -> str:
        return {
            "cloudflare": "Cloudflare quick tunnel",
            "ngrok": f"ngrok{' ' + self.tunnel_url if self.tunnel_url else ' (new address each start)'}",
            "url": self.tunnel_url,
            "none": "no tunnel",
        }.get(self.tunnel, self.tunnel)

    def context_label(self) -> str:
        agent = AGENTS.get(self.agent) or custom_agent(self.command or "custom")
        return context.summary(
            agent,
            Path(self.workdir),
            ignore_rules=self.ignore_rules == "yes",
            use_meeting_md=self.meeting_md != "no",
        )

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
            ("Model", self.model or "the CLI's default"),
            ("Effort", self.effort or "the CLI's default"),
            ("Context", self.context_label()),
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


class GoBack(Exception):  # noqa: N818 - a signal, not an error
    """The user asked for the previous step (← Back, Esc, ← or "<")."""


class Prompter:
    """Questions on the terminal. End of input takes the default.

    ``interactive`` (a person at a terminal) gives arrow-key menus and answered
    lines that fold up; otherwise questions are numbered and read line by line.
    """

    def __init__(self, read: Callable[[str], str] | None = None, *, interactive: bool | None = None) -> None:
        self._read = read or input
        self.interactive = (read is None and ui.is_interactive()) if interactive is None else interactive
        self.eof = False
        self.back_allowed = False
        """Set by the step loop: menus get a "← Back" row and "<" goes to the previous step."""
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
            typed = self._line(f"{question}{hint}: ")
            if typed == "<" and self.back_allowed:
                raise GoBack
            return typed or default
        if tui.AVAILABLE:
            got = tui.text(question, default, back=self.back_allowed)
            if got is tui.BACK:
                ui.answered(question, ui.BACK_LABEL)
                raise GoBack
            answer = str(got).strip() or default
            ui.answered(question, answer or "(blank)")
            return answer
        typed = self._line(ui.question_prompt(question, default, back=self.back_allowed))
        if typed == "<" and self.back_allowed:
            self._fold(len(question) + len(default) + 9)
            ui.answered(question, ui.BACK_LABEL)
            raise GoBack
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
        if self.interactive and tui.AVAILABLE:
            got = tui.confirm(question, default, back=self.back_allowed)
            if got is tui.BACK:
                ui.answered(question, ui.BACK_LABEL)
                raise GoBack
            ui.answered(question, "Yes" if got else "No")
            return bool(got)
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

    def choose_many(
        self, question: str, options: Sequence[str], chosen: Sequence[int] = (), hints: Sequence[str] = ()
    ) -> list[int]:
        """Several options at once; without a terminal, numbers separated by commas."""
        if self.interactive and tui.AVAILABLE:
            got = tui.select_many(question.strip(), options, chosen, hints, back=self.back_allowed)
            if got is tui.BACK:
                ui.answered(question, ui.BACK_LABEL)
                raise GoBack
            picked: list[int] = [int(i) for i in got]  # type: ignore[attr-defined]
            ui.answered(question, ", ".join(options[i] for i in picked) or "(none)")
            return picked
        if self.interactive:
            return ui.multi_select(question.strip(), options, chosen, hints)
        say(question)
        for index, option in enumerate(options, 1):
            hint = hints[index - 1] if index - 1 < len(hints) else ""
            say(f"  {index}) {option}" + (f"  {hint}" if hint else ""))
        default = ",".join(str(i + 1) for i in chosen)
        while True:
            answer = self.ask("Choose (numbers, comma-separated)", default)
            parts = [p.strip() for p in answer.split(",") if p.strip()]
            if all(p.isdigit() and 1 <= int(p) <= len(options) for p in parts):
                return sorted({int(p) - 1 for p in parts})
            say(f"  Type numbers from 1 to {len(options)}, for example 1,3.")
            if self.eof:
                return list(chosen)

    def choose(
        self,
        question: str,
        options: Sequence[str],
        default: int = 0,
        hints: Sequence[str] = (),
        detail: Callable[[int], str] | None = None,
    ) -> int:
        if self.back_allowed:  # a "← Back" row at the end; Esc or ← work too in a terminal
            hints = [*hints, *[""] * (len(options) - len(hints)), ""]
            real = len(options)
            options = [*options, ui.BACK_LABEL]
            if detail is not None:  # the Back row has its own detail line (it had none: IndexError)
                inner = detail
                detail = lambda i: inner(i) if i < real else "Go back to the previous step."  # noqa: E731
        if self.interactive and tui.AVAILABLE:
            got = tui.select(question.strip(), options, default, hints, detail=detail, back=self.back_allowed)
            index = -1 if got is tui.BACK else int(got)  # type: ignore[call-overload]
            shown = options[index] if index >= 0 else ui.BACK_LABEL
            ui.answered(question.strip(), ui.plain(shown).split("  ")[0].strip() if detail else shown)
        elif self.interactive:
            index = ui.arrow_select(
                question.strip(), options, default, hints, detail=detail, back=self.back_allowed
            )
        else:
            index = self._choose_plain(question, options, default, hints, detail)
        if self.back_allowed and index in (-1, len(options) - 1):
            raise GoBack
        return index

    def _choose_plain(
        self,
        question: str,
        options: Sequence[str],
        default: int,
        hints: Sequence[str],
        detail: Callable[[int], str] | None,
    ) -> int:
        say(question)
        pad = max(len(option) for option in options) + 2
        for index, option in enumerate(options, 1):
            if detail is not None:
                say(ui.clip(f"  {index}) {ui.plain(option)}", ui.width() - 1))
                continue
            hint = hints[index - 1] if index - 1 < len(hints) else ""
            say(f"  {index}) {option.ljust(pad)}{hint}".rstrip())
        while True:
            answer = self.ask("Choose", str(default + 1))
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer) - 1
            say(f"  Type a number from 1 to {len(options)}.")


# ── Step 1: the agent ─────────────────────────────────────────────────────────


_PREREQUISITE_HELP: Final[dict[str, str]] = {
    "npm": "npm comes with Node.js: https://nodejs.org/en/download"
    + (" (or: brew install node)" if sys.platform == "darwin" else ""),
    "curl": "Install curl with your system's package manager (for example: sudo apt install curl).",
    "bash": "Install bash with your system's package manager.",
}

_INSTALL_DIRS: Final = (Path.home() / ".local" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"))
"""Where installers commonly put a CLI, for when the new command is not on PATH yet."""


def _installed_off_path(binary: str) -> Path | None:
    for folder in _INSTALL_DIRS:
        candidate = folder / binary
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _offer_install(
    prompt: Prompter,
    agent: AgentSpec,
    which: Callable[[str], str | None],
    run: Callable[[list[str]], int],
    offer: bool,
) -> bool:
    """Show the vendor's install command and, at a terminal, offer to run it. True once installed."""
    ui.fail(f"{agent.label} is not on this computer.")
    if not agent.install:
        ui.note(agent.install_hint)
        ui.note("Install it, or pick another agent.")
        return False
    ui.note(f"Official install command for {agent.label}:")
    ui.command(agent.install)
    if not offer:
        ui.note("Run it, then run setup again, or pick another agent.")
        return False
    missing = next((program for program in agent.install_needs if which(program) is None), None)
    if missing:
        ui.fail(f"The install command needs {missing}, which is not on this computer.")
        ui.note(_PREREQUISITE_HELP.get(missing, f"Install {missing} first."))
        return False
    if not prompt.confirm(f"Install {agent.label} now?"):
        return False
    ui.note("Running the installer here; its output follows.")
    code = run(["/bin/sh", "-c", agent.install])  # a fixed command from the agent registry, never user input
    if code != 0:
        ui.fail(f"The installer stopped with exit code {code}. Fix what it says, or pick another agent.")
        return False
    found = which(agent.binary)
    if found is None:
        off_path = _installed_off_path(agent.binary)
        if off_path is None:
            ui.fail(f"Installed, but `{agent.binary}` is not on your PATH yet.")
            ui.note("Open a new terminal window and run setup again.")
            return False
        folder = str(off_path.parent)
        os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
        ui.warn(f"`{agent.binary}` is in {folder}, which is not on your PATH. Add it to your shell profile:")
        ui.command(f'export PATH="{folder}:$PATH"')
        found = str(off_path)
    ui.ok(f"{agent.label} installed: {found}")
    first_run = agent.after_install.split() or (
        None if agent.login_check else checks.login_argv(agent)
    )  # agents with a login check are asked in the sign-in step that follows
    if first_run and prompt.confirm(f"Sign in and set it up now ({' '.join(first_run)})?"):
        ui.note("Signing in is yours to do: follow the agent's own prompts.")
        run(first_run)
    return True


def _pick_agent(
    prompt: Prompter,
    which: Callable[[str], str | None],
    last: str,
    run: Callable[[list[str]], int] = lambda argv: subprocess.call(argv),
    offer_install: bool | None = None,
) -> tuple[AgentSpec, str]:
    offer = prompt.interactive if offer_install is None else offer_install
    agents = list(AGENTS.values())
    installed = [i for i, a in enumerate(agents) if which(a.binary) is not None]
    default = next((i for i, a in enumerate(agents) if a.name == last), installed[0] if installed else 0)
    labels = [a.label for a in agents] + ["Another command"]
    hints = [
        "installed"
        if i in installed
        else ("not installed; setup can install it" if a.install else "not found")
        for i, a in enumerate(agents)
    ]
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
        if which(agent.binary) is None and not _offer_install(prompt, agent, which, run, offer):
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


# ── Step 2: model and effort ──────────────────────────────────────────────────


def _menu_models(catalog: ModelCatalog, keep: str) -> list[ModelChoice]:
    """At most MODEL_MENU_SIZE models, with the default and ``keep`` always in."""
    models = unique(list(catalog.models))  # never two rows for one model
    must = [m for m in models if m.id in (catalog.default_model, keep)]
    rest = [m for m in models if m not in must]
    return (must + rest)[:MODEL_MENU_SIZE] if len(models) > MODEL_MENU_SIZE else models


def _pick_effort(prompt: Prompter, catalog: ModelCatalog, model: str, last: str) -> str:
    efforts = catalog.efforts_for(model)
    if not efforts:
        return ""
    labels = ["The CLI's default", *efforts]
    hints = [f"now: {catalog.default_effort}" if catalog.default_effort else ""] + [
        "your CLI's setting" if e == catalog.default_effort else "" for e in efforts
    ]
    wanted = last if last in efforts else ""
    index = prompt.choose("Effort (how hard it thinks)", labels, labels.index(wanted) if wanted else 0, hints)
    return "" if index == 0 else efforts[index - 1]


def _pick_model(
    prompt: Prompter,
    agent: AgentSpec,
    binary: str,
    last: SetupAnswers | None,
    *,
    catalog: Callable[[AgentSpec, str], ModelCatalog],
    compare_config: Callable[[], BridgeConfig],
    measure_model: Callable[[BridgeConfig, str, str], Measured],
) -> tuple[str, str, bool]:
    """(model, effort, asked): '' means the CLI's own default; asked is False when there was no choice.

    MIA's agent config can still choose another model."""
    with ui.spinner(f"Asking {agent.label} which models it offers"):
        listed = catalog(agent, binary)
    if listed.models:
        ui.ok(f"{len(listed.models)} model{'s' if len(listed.models) != 1 else ''} from {listed.source}")
    else:
        ui.warn(f"{agent.label} did not list its models" + (f" ({listed.error})" if listed.error else ""))
    if listed.note:
        ui.note(listed.note)
    last_model = last.model if last and last.agent == agent.name else ""
    last_effort = last.effort if last and last.agent == agent.name else ""
    only = listed.models[0] if len(listed.models) == 1 else None
    if only is not None and only.id in (listed.default_model, "auto"):
        # Nothing to choose (Copilot offers only Auto): say so instead of two near-identical rows.
        name = "Auto" if only.id == "auto" else only.id
        ui.ok(f"{agent.label} picks the model itself ({name})")
        efforts = listed.efforts_for(only.id)
        return only.id, _pick_effort(prompt, listed, only.id, last_effort), bool(efforts)
    while True:
        shown = _menu_models(listed, last_model)
        fallback = next((m for m in listed.models if m.id == listed.default_model), None)
        default_choice = fallback or ModelChoice(
            listed.default_model, vision=listed.vision, vision_source=listed.vision_source
        )
        vision = next(f for f in features(agent, default_choice) if f.name == "vision")
        cli_default = ModelChoice(
            "",
            vision=vision.available,
            vision_source=vision.source,
        )
        measurable = [*shown, cli_default]
        cached = estimates.load_with_shared(agent.name)
        largest = max(
            (project_hour(cached[m.id].usage).total for m in measurable if m.id in cached), default=0
        )
        labels = [estimates.row_text(agent, m, cached.get(m.id), largest) for m in shown]
        labels += ["Type another model id", estimates.row_text(agent, cli_default, cached.get(""), largest)]
        details = [estimates.detail_text(agent, m, cached.get(m.id)) for m in shown]
        details += [
            "Enter a model id from your CLI.",
            estimates.detail_text(agent, cli_default, cached.get("")),
        ]
        if agent.reports_usage:  # measuring needs the CLI's own token counts
            labels.append("Measure live (one short real turn per model)")
            details.append(
                "Choose models to measure. Each runs one short real turn and uses your plan or API credits."
            )
        wanted = last_model or listed.default_model
        default = next((i for i, m in enumerate(shown) if m.id == wanted), len(shown) + 1)
        index = prompt.choose("Which model?", labels, default, detail=details.__getitem__)
        if agent.reports_usage and index == len(shown) + 2:
            ticked = sorted(range(len(measurable)), key=lambda i: measurable[i].id in cached)[:COMPARE_MAX]
            picked = prompt.choose_many(
                "Models to measure (one short real turn each)",
                [m.id or "The CLI's default" for m in measurable],
                ticked,
                [m.label for m in measurable],
            )
            results = compare(
                compare_config(),
                [measurable[i].id for i in picked],
                last_effort,
                run=lambda c, m, e: measure_model(c, m, e if e in listed.efforts_for(m) else ""),
            )
            say("")
            for line in chart_lines(results):
                say(line)
            say("")
            continue
        if index == len(shown):
            model = prompt.ask("Model id (as the CLI names it)", last_model).strip()
        else:
            model = shown[index].id if index < len(shown) else ""
        try:
            return model, _pick_effort(prompt, listed, model, last_effort), True
        except GoBack:  # back from the effort question: the model menu again
            last_model = model
            continue


def _review_project_rules(
    prompt: Prompter, agent: AgentSpec, folder: Path, workspace: Path
) -> tuple[str, bool, bool]:
    """Meeting context for a folder with project rules: (folder, ignore rules, use MEETING.md).

    a) Meeting only: rules ignored where the CLI can, else the meeting workspace.
    b) Include this project's rules.  c) Custom: pick what goes in.
    """
    found = project_rules(folder)
    if not found:
        return str(folder), False, True
    total = sum(size for _, size in found)
    ui.warn(
        f"This folder has project rules your agent reads on every meeting turn (≈ {total // 4:,} tokens):"
    )
    for name, size in found:
        ui.note(f"{name}  {size / 1024:.1f} KB ≈ {size // 4:,} tokens")
    can_skip = bool(agent.ignore_rules_args or agent.ignore_rules_env)
    if not can_skip:
        ui.note(f"{agent.label} {agent.ignore_rules_note or 'has no switch to skip them'}.")
    meeting_hint = agent.ignore_rules_note if can_skip else f"use {ui.short_path(str(workspace))} instead"
    options = [
        ("meeting", "Meeting only", meeting_hint),
        ("project", "Include this project's rules", f"≈ {total // 4:,} tokens on every turn"),
        ("custom", "Custom", "choose what goes in"),
    ]
    pick = options[prompt.choose("Meeting context", [o[1] for o in options], 0, [o[2] for o in options])][0]
    if pick == "meeting" and not can_skip:
        workspace.mkdir(parents=True, exist_ok=True)
        return str(workspace.resolve()), False, True
    if pick != "custom":
        return str(folder), pick == "meeting", True
    parts = (["This project's rules"] if can_skip else []) + ["My MEETING.md instructions"]
    keep = prompt.choose_many("What goes into meetings?", parts, list(range(len(parts))))
    rules_kept = (0 in keep) if can_skip else True
    return str(folder), can_skip and not rules_kept, (len(parts) - 1) in keep


def _show_context(agent: AgentSpec, workdir: str, ignore_rules: bool, use_meeting_md: bool) -> None:
    folder = Path(workdir)
    items = context.effective_context(agent, folder, ignore_rules=ignore_rules, use_meeting_md=use_meeting_md)
    label = context.summary(agent, folder, ignore_rules=ignore_rules, use_meeting_md=use_meeting_md)
    ui.ok(f"Meeting context: {label}")
    for row in context.table(items):
        ui.note(row)
    where = ui.short_path(str(context.meeting_file()))
    ui.note(f"Your meeting instructions: {where} (edit with: meetstream-agent-kit context edit)")


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


def _blank_answers() -> SetupAnswers:
    return SetupAnswers("", "", "", "readonly", "none", "", "")


def ask_setup(
    prompt: Prompter,
    last: SetupAnswers | None,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[[list[str]], int] = lambda argv: subprocess.call(argv),
    cwd: str | None = None,
    detect_ngrok: Callable[[], NgrokSetup] = read_ngrok_setup,
    probe_ngrok: Callable[[str], tuple[str, str]] = probe_ngrok_address,
    offer_install: bool | None = None,
    catalog: Callable[[AgentSpec, str], ModelCatalog] = catalog_for,
    measure_model: Callable[[BridgeConfig, str, str], Measured] = measure,
) -> SetupAnswers:
    """All the questions, in order. Returns the answers; nothing starts here.

    ``offer_install`` (default: only at a terminal) lets setup run a missing agent's
    official install command after a yes; otherwise the command is only shown.
    """
    a: dict[str, Any] = {}  # answers so far; kept when going back
    default_dir = last.workdir if last else (cwd or str(meeting_workspace()))
    workspace = meeting_workspace()

    def step_agent() -> bool:
        ui.step(1, SETUP_STEPS, "Agent")
        previous = a.get("agent")
        agent, command = _pick_agent(
            prompt, which, previous.name if previous else (last.agent if last else ""), run, offer_install
        )
        binary = which(agent.binary)  # the same lookup _pick_agent used (tests pass a pretend PATH)
        if binary and not Path(binary).is_file():
            binary = None
        if binary and not command:
            _check_login(prompt, agent, binary, run)
        if previous is not None and previous.name != agent.name:
            a.pop("model", None)  # another agent: its own models
            a.pop("effort", None)
        a.update(agent=agent, command=command, binary=binary)
        return True

    def step_model() -> bool:
        ui.step(2, SETUP_STEPS, "Model")
        agent, command, binary = a["agent"], a["command"], a["binary"]
        if command:
            ui.note("A custom command picks its own model.")
            a.update(model="", effort="")
            return False
        if binary is None:
            ui.note(f"{agent.label} is not installed yet, so it uses its own default model.")
            a.update(model="", effort="")
            return False
        before = (
            dataclasses.replace(
                last or _blank_answers(), agent=agent.name, model=a["model"], effort=a["effort"]
            )
            if "model" in a
            else last
        )
        model, effort, asked = _pick_model(
            prompt,
            agent,
            binary,
            before,
            catalog=catalog,
            compare_config=lambda: BridgeConfig(
                agent=agent,
                binary=binary,
                workdir=Path(default_dir) if Path(default_dir).is_dir() else Path.home(),
                owner=os.environ.get("USER", "") or "the user",
            ),
            measure_model=measure_model,
        )
        a.update(model=model, effort=effort)
        return asked

    def step_context() -> bool:
        ui.step(3, SETUP_STEPS, "Meeting context")
        ui.note(
            f"{ui.short_path(str(workspace))} is a quiet meeting workspace: no project rules on every turn."
        )
        ui.note("Type a project folder instead to let your agent see that code.")
        while True:
            folder = a.get("workdir", default_dir)
            workdir = str(Path(prompt.ask("Folder your agent works in", folder)).expanduser().resolve())
            if Path(workdir) == workspace.resolve() and not workspace.exists():
                workspace.mkdir(parents=True, exist_ok=True)
                ui.ok(f"Created {ui.short_path(str(workspace))}")
            check = checks.check_workdir(Path(workdir))
            if check.ok:
                break
            ui.fail(check.message)
        workdir, ignore_rules, use_meeting_md = _review_project_rules(
            prompt, a["agent"], Path(workdir), workspace
        )
        context.ensure_meeting_file()
        _show_context(a["agent"], workdir, ignore_rules, use_meeting_md)
        a.update(workdir=workdir, ignore_rules=ignore_rules, use_meeting_md=use_meeting_md)
        return True

    def step_access() -> bool:
        ui.step(4, SETUP_STEPS, "Access")
        ui.note("The most this computer allows. Your MIA agent's settings can only lower it.")
        names = [name for name, _, _ in ACCESS_CHOICES]
        current = a.get("max_access", last.max_access if last else names[0])
        index = prompt.choose(
            "Most access this computer allows",
            [label for _, label, _ in ACCESS_CHOICES],
            names.index(current) if current in names else 0,
            [hint for _, _, hint in ACCESS_CHOICES],
        )
        a["max_access"] = names[index]
        return True

    def step_tunnel() -> bool:
        ui.step(5, SETUP_STEPS, "Tunnel")
        earlier = (
            dataclasses.replace(last or _blank_answers(), tunnel=a["tunnel"], tunnel_url=a["tunnel_url"])
            if "tunnel" in a
            else last
        )
        a["tunnel"], a["tunnel_url"] = _pick_tunnel(prompt, which, earlier, detect_ngrok, probe_ngrok)
        return True

    def step_name() -> bool:
        ui.step(6, SETUP_STEPS, "Your name")
        a["owner"] = prompt.ask(
            "Your name as it shows in meetings",
            a.get("owner", last.owner if last else os.environ.get("USER", "")),
        )
        return True

    steps: list[Callable[[], bool]] = [
        step_agent,
        step_model,
        step_context,
        step_access,
        step_tunnel,
        step_name,
    ]
    asked_at: list[bool] = [False] * len(steps)
    index = 0
    while index < len(steps):
        prompt.back_allowed = index > 0
        try:
            asked_at[index] = steps[index]()
            index += 1
        except GoBack:
            index -= 1
            while index > 0 and not asked_at[index]:  # skip steps that asked nothing (nothing to change)
                index -= 1
    prompt.back_allowed = False
    agent = a["agent"]
    return SetupAnswers(
        agent=agent.name,
        command=a["command"],
        workdir=a["workdir"],
        max_access=a["max_access"],
        tunnel=a["tunnel"],
        tunnel_url=a["tunnel_url"],
        owner=a["owner"],
        model=a["model"],
        effort=a["effort"],
        ignore_rules="yes" if a["ignore_rules"] else "",
        meeting_md="" if a["use_meeting_md"] else "no",
    )
