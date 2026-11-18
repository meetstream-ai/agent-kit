"""Which models and effort levels an agent CLI offers, and which it uses by default.

Each list comes from the CLI itself where it can tell us:

* ACP agents (Antigravity, Hermes, Copilot, Muse, OpenClaw): ``session/new``
  returns ``models`` and, for some, a ``thought_level`` option. That is the very
  list meetings use. Antigravity falls back to ``agy models``.
* Codex: ``codex app-server`` answers ``model/list`` with each model's efforts.
* Claude Code has no listing command; its ``--help`` names the model aliases and
  effort levels, so the list says "from claude --help".

The preselected default is the CLI's own setting (Antigravity's settings.json,
Codex's config.toml, Claude Code's settings.json, Hermes's config.yaml), then
what the CLI itself reports as current.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from meetstream_agent_kit.agents import AGENTS, AgentSpec

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

LIST_TIMEOUT_S: Final = 30.0


@dataclass(frozen=True)
class ModelChoice:
    id: str
    label: str = ""
    efforts: tuple[str, ...] = ()
    """Effort levels this model takes, when the CLI lists them per model."""
    vision: bool | None = None
    vision_source: str = ""


@dataclass
class ModelCatalog:
    models: list[ModelChoice] = field(default_factory=list)
    default_model: str = ""
    efforts: list[str] = field(default_factory=list)
    default_effort: str = ""
    source: str = ""
    """Where the list came from, shown next to it (``agy acp``, ``codex app-server``, ``claude --help``)."""
    error: str = ""
    vision: bool | None = None
    vision_source: str = ""

    def efforts_for(self, model: str) -> list[str]:
        chosen = next((m for m in self.models if m.id == model), None)
        return list(chosen.efforts) if chosen and chosen.efforts else list(self.efforts)


@dataclass(frozen=True)
class Feature:
    name: str
    available: bool | None
    source: str = ""


def _documented_vision(agent: AgentSpec, model: str) -> bool | None:
    """Curated fallback from docs; CLI capabilities take precedence."""
    if "gpt-oss" in model.lower():
        return False
    if agent.name == "claude" or "claude" in model.lower():
        return True
    return None


def features(agent: AgentSpec, model: ModelChoice) -> list[Feature]:
    """Meeting capabilities, with the source of the vision claim."""
    # Per-model facts first: a model-specific listing (Codex inputModalities), then the documented
    # per-model rules (e.g. gpt-oss is text-only), and only then an agent-wide ACP capability.
    vision, source = model.vision, model.vision_source
    documented = _documented_vision(agent, model.id)
    agent_wide = source.endswith(" acp")  # ACP promptCapabilities describe the agent, not this model
    if documented is not None and (vision is None or agent_wide):
        vision, source = documented, "from docs"
    tools = agent.warm in ("claude_stream_json", "codex_app_server") or (
        agent.warm == "acp" and agent.acp_mcp
    )
    return [
        Feature("talk", True),
        Feature("tools", tools),
        Feature("vision", vision, source),
        Feature("local", True if agent.name in AGENTS else None, "per access level"),
    ]


# ── The CLI's own defaults ────────────────────────────────────────────────────


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _toml_value(text: str, key: str) -> str:
    """A top-level ``key = "value"`` from a TOML file (before the first table)."""
    top = text.split("\n[", 1)[0]
    match = re.search(rf'^\s*{re.escape(key)}\s*=\s*"([^"]*)"', top, re.MULTILINE)
    return match.group(1) if match else ""


def _yaml_block_value(text: str, block: str, key: str) -> str:
    """``key: value`` inside a top-level ``block:`` of a simple YAML file."""
    match = re.search(rf"^{re.escape(block)}:\s*\n((?:[ \t]+.*\n?|\s*\n)*)", text, re.MULTILINE)
    if not match:
        return ""
    found = re.search(rf"^[ \t]+{re.escape(key)}:\s*[\"']?([^\"'\n#]+)", match.group(1), re.MULTILINE)
    return found.group(1).strip() if found else ""


def cli_defaults(agent: AgentSpec, home: Path | None = None) -> tuple[str, str]:
    """(model, effort) the CLI uses when none is given, from its own settings file."""
    home = home or Path.home()
    if agent.name == "agy":
        data = _read_json(home / ".gemini" / "antigravity-cli" / "settings.json")
        return str(data.get("model") or ""), str(data.get("effort") or "")
    if agent.name == "claude":
        data = _read_json(home / ".claude" / "settings.json")
        return str(data.get("model") or ""), str(data.get("effortLevel") or "")
    if agent.name == "codex":
        try:
            text = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
        except OSError:
            return "", ""
        return _toml_value(text, "model"), _toml_value(text, "model_reasoning_effort")
    if agent.name == "hermes":
        try:
            text = (home / ".hermes" / "config.yaml").read_text(encoding="utf-8")
        except OSError:
            return "", ""
        return _yaml_block_value(text, "model", "default"), _yaml_block_value(
            text, "agent", "reasoning_effort"
        )
    return "", ""


# ── Listings ──────────────────────────────────────────────────────────────────


def _rpc_session(
    argv: list[str], requests: list[tuple[str, dict[str, Any]]], timeout_s: float, *, jsonrpc: bool = True
) -> list[Any]:
    """Start a JSON-RPC stdio process, send requests in order, return each result (None on error)."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=tempfile.gettempdir(),
    )
    replies: dict[int, dict[str, Any]] = {}

    def read() -> None:
        assert proc.stdout is not None  # noqa: S101 - piped above
        for line in proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and "id" in message and "method" not in message:
                replies[message["id"]] = message

    threading.Thread(target=read, daemon=True).start()
    results: list[Any] = []
    deadline = time.monotonic() + timeout_s
    try:
        assert proc.stdin is not None  # noqa: S101 - piped above
        for index, (method, params) in enumerate(requests, 1):
            message: dict[str, Any] = {"id": index, "method": method, "params": params}
            if method == "initialized":
                del message["id"]
            if jsonrpc:  # ACP sends it; Codex app-server omits it
                message["jsonrpc"] = "2.0"
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
            if method == "initialized":  # a notification: no reply
                results.append(None)
                continue
            while index not in replies and time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.05)
            reply = replies.get(index) or {}
            results.append(reply.get("result") if "result" in reply else None)
            if reply.get("error"):
                raise RuntimeError(str((reply["error"] or {}).get("message") or reply["error"]))
            if index not in replies:
                raise TimeoutError(f"{method} did not answer within {int(timeout_s)} s")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    return results


def _acp_catalog(agent: AgentSpec, binary: str, timeout_s: float) -> ModelCatalog:
    init = {
        "protocolVersion": 1,
        "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
        "clientInfo": {"name": "meetstream-agent-kit", "version": "models"},
    }
    initialized, created = _rpc_session(
        [binary, *agent.acp_args],
        [("initialize", init), ("session/new", {"cwd": tempfile.gettempdir(), "mcpServers": []})],
        timeout_s,
    )
    return acp_catalog_from(created or {}, f"{agent.binary} {' '.join(agent.acp_args)}", initialized)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def acp_catalog_from(
    created: dict[str, Any], source: str, initialized: dict[str, Any] | None = None
) -> ModelCatalog:
    """The models and thinking levels an ACP ``session/new`` result advertises."""
    raw = _as_dict(created.get("models"))
    capabilities = _as_dict(_as_dict(initialized).get("agentCapabilities"))
    image = _as_dict(capabilities.get("promptCapabilities")).get("image")
    vision = image if isinstance(image, bool) else None
    vision_source = source if vision is not None else ""
    models = [
        ModelChoice(str(m["modelId"]), str(m.get("name") or ""), vision=vision, vision_source=vision_source)
        for m in raw.get("availableModels") or []
        if isinstance(m, dict) and m.get("modelId")
    ]
    efforts: list[str] = []
    current_effort = ""
    for option in created.get("configOptions") or []:
        if isinstance(option, dict) and option.get("category") == "thought_level":
            efforts = [
                str(o["value"]) for o in option.get("options") or [] if isinstance(o, dict) and o.get("value")
            ]
            current_effort = str(option.get("currentValue") or "")
    return ModelCatalog(
        models,
        str(raw.get("currentModelId") or ""),
        efforts,
        current_effort,
        source,
        vision=vision,
        vision_source=vision_source,
    )


def _codex_catalog(binary: str, timeout_s: float) -> ModelCatalog:
    init = {
        "clientInfo": {"name": "meetstream-agent-kit", "title": "MeetStream Agent Kit", "version": "models"}
    }
    _, _, listed = _rpc_session(
        [binary, "app-server"],
        [("initialize", init), ("initialized", {}), ("model/list", {})],
        timeout_s,
        jsonrpc=False,
    )
    return codex_catalog_from(listed or {})


def codex_catalog_from(listed: dict[str, Any]) -> ModelCatalog:
    models: list[ModelChoice] = []
    default = ""
    default_effort = ""
    for item in listed.get("data") or []:
        if not isinstance(item, dict) or item.get("hidden") or not item.get("id"):
            continue
        efforts = tuple(
            str(e["reasoningEffort"])
            for e in item.get("supportedReasoningEfforts") or []
            if isinstance(e, dict) and e.get("reasoningEffort")
        )
        modalities = item.get("inputModalities")
        vision = "image" in modalities if isinstance(modalities, list) else None
        models.append(
            ModelChoice(
                str(item["id"]),
                str(item.get("displayName") or ""),
                efforts,
                vision,
                "codex app-server" if vision is not None else "",
            )
        )
        if item.get("isDefault"):
            default, default_effort = str(item["id"]), str(item.get("defaultReasoningEffort") or "")
    efforts_all = list(dict.fromkeys(e for m in models for e in m.efforts))
    return ModelCatalog(models, default, efforts_all, default_effort, "codex app-server")


def agy_models_from(text: str) -> list[ModelChoice]:
    """``agy models`` prints ``slug<TAB>label`` per line after a status line."""
    found = []
    for line in text.splitlines():
        slug, _, label = line.strip().partition("\t")
        if re.fullmatch(r"[a-z0-9][a-z0-9.\-_/:]*", slug):
            found.append(ModelChoice(slug, label.strip()))
    return found


def _help_list(text: str, flag: str) -> list[str]:
    """The values a ``--help`` text lists for ``flag`` in parentheses or after a colon."""
    match = re.search(rf"{re.escape(flag)}[^\n]*\n?(?:[^\n]*\n){{0,3}}", text)
    if not match:
        return []
    block = match.group(0)
    inner = re.search(r"\(([a-z, ]+)\)", block) or re.search(r":\s*([a-z]+(?:,\s*(?:or\s+)?[a-z]+)+)", block)
    if not inner:
        return []
    return [part.strip().removeprefix("or ").strip() for part in inner.group(1).split(",") if part.strip()]


def claude_catalog_from(help_text: str) -> ModelCatalog:
    aliases = re.findall(r"'([a-z]+)'", help_text.split("--model", 1)[-1][:400])
    models = [ModelChoice(alias, f"latest {alias.capitalize()}") for alias in dict.fromkeys(aliases)]
    return ModelCatalog(models, "", _help_list(help_text, "--effort"), "", "from claude --help")


def catalog_for(
    agent: AgentSpec,
    binary: str,
    *,
    run: Runner = subprocess.run,
    timeout_s: float = LIST_TIMEOUT_S,
    home: Path | None = None,
    acp: Callable[[AgentSpec, str, float], ModelCatalog] = _acp_catalog,
    codex: Callable[[str, float], ModelCatalog] = _codex_catalog,
) -> ModelCatalog:
    """Ask the CLI. Never raises: a CLI that cannot list leaves an empty catalog with ``error``."""
    catalog = ModelCatalog()
    try:
        if agent.warm == "acp":
            catalog = acp(agent, binary, timeout_s)
        elif agent.warm == "codex_app_server":
            catalog = codex(binary, timeout_s)
        elif agent.name == "claude":
            proc = run([binary, "--help"], capture_output=True, text=True, timeout=timeout_s, check=False)
            catalog = claude_catalog_from(f"{proc.stdout}\n{proc.stderr}")
    except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError) as exc:
        catalog.error = str(exc)[:200]
    if agent.name == "agy" and not catalog.models:
        try:
            proc = run([binary, "models"], capture_output=True, text=True, timeout=timeout_s, check=False)
            catalog.models, catalog.source = agy_models_from(proc.stdout), "agy models"
        except (OSError, subprocess.SubprocessError) as exc:
            catalog.error = catalog.error or str(exc)[:200]
    if agent.name == "agy" and not catalog.efforts:
        catalog.efforts = ["low", "medium", "high"]
    if agent.name == "hermes" and not catalog.efforts:
        catalog.efforts = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
    model, effort = cli_defaults(agent, home)
    if model:
        catalog.default_model = model
        if not any(m.id == model for m in catalog.models):
            catalog.models.insert(
                0,
                ModelChoice(
                    model, "your CLI's default", vision=catalog.vision, vision_source=catalog.vision_source
                ),
            )
    if effort and (not catalog.efforts or effort in catalog.efforts):
        catalog.default_effort = effort
    return catalog
