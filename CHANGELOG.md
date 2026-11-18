# Changelog

All notable changes to this package. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.3.3] - Unreleased

### Added

- Setup has a **Model** step (now 7 steps). It lists the models your agent CLI
  really offers, from the CLI itself: the ACP session for Antigravity, Hermes,
  Copilot, Muse and OpenClaw (the same list meetings use; `agy models` as a
  fallback), `codex app-server`'s `model/list` for Codex, and the aliases in
  `claude --help` for Claude Code. The CLI's own setting is preselected
  (Antigravity's settings.json, Codex's config.toml, Claude Code's
  settings.json, Hermes's config.yaml). Then the effort, where the CLI has
  one. Both are saved, shown in the summary, and passed with `--model` and
  `--effort`; MIA's agent config can still choose another model.
- In ACP meetings the chosen model is set with `session/set_model` when the
  agent lists it, and the effort with `session/set_config_option` when the
  agent offers that thinking level. Before, `agy acp` ran its own current model
  (gemini-3.8-flash-high), not the one in its settings.
- The model picker shows, per model, an estimated tokens-per-hour bar, feature
  chips (talk, meeting tools, vision, local actions) and the measured time to
  first words, and a detail line for the highlighted model that follows the
  arrow. Estimates come from real measurements kept in `model-usage.json` in the
  kit's folder (setup's test turn and every comparison add to it), never
  invented: an unmeasured model says "not measured". Vision comes from the CLI
  where it says (Codex `inputModalities`, ACP `promptCapabilities`), otherwise
  from a small table marked "from docs".
- **Measure live** in the picker (and `bench --compare`): one short real
  meeting turn per model, the token usage the CLI itself reports, projected to
  an hour of meeting and drawn as bars, with that turn's time and, where the
  CLI reports a price (Claude Code, Hermes), the cost per hour. The assumption
  is printed with it (20 answered turns an hour, one warm session, each turn
  adding the meeting text and the answer to the context; `--turns-per-hour`
  changes it). Without a price it says "plan usage, no per-token bill".
- Hermes `provider:model` ids go to `--provider` and `-m`.
- When your agent fails in a meeting (out of credits, rate limited, timed out,
  crashed), the bridge tells you on your desktop (macOS notification, or
  `notify-send` on Linux; always a terminal line too), once per kind of
  failure per meeting, with what to do. MIA gets a clean error with a
  `reason` (`out_of_credits`, `rate_limited`, `timeout`, `crashed`, `failed`)
  and stays quiet. Signed out is still told by the login check.
- The default work folder is a quiet meeting workspace, `~/MeetStream Agent`,
  created by setup; a project folder is opt-in. Agent CLIs read a folder's
  project rules (CLAUDE.md, AGENTS.md, GEMINI.md, .cursorrules...) on every
  turn, about 7k tokens for a 29 KB CLAUDE.md. Setup now lists them with
  their token cost and offers to ignore them in meetings or to use the
  workspace instead. `serve --ignore-rules` does the same. Claude Code skips
  them with `--setting-sources user` (checked: the folder's CLAUDE.md is no
  longer read), Codex with `-c project_doc_max_bytes=0`, Hermes with
  `--ignore-rules` in single runs (in meetings `hermes acp` has no such
  switch: only its auto-loaded skills are skipped, so use the workspace).
  Antigravity has no switch.
- Setup's test turns show live stages with real timings instead of a silent
  wait: "Woke Codex 0.3 s · First words 5.5 s · Answered 6.5 s", with rotating
  verbs (Pondering, Noodling, ...). The warm ACP check shows "Session ready ·
  Tool called · Answered", and `bench --compare` shows the same stages per
  model. Without a terminal it prints one plain line. `bench` cold rows add
  `first_line_ms`.

## [0.3.2] - Unreleased

### Fixed

- Antigravity gets the meeting tools. Its warm session ran on `agy -p`
  stream-json, which has no way to add an MCP server, so MIA's tools never
  reached it (MIA logged `tools=0`). It now runs `agy acp`, and the meeting
  tools go in `session/new` through the same stdio shim the other ACP agents use.
- A turn could hang silently (Antigravity over ACP, live meeting): the bridge
  waited up to 170 s, longer than MIA's own 120 s, so MIA stopped reading, its
  next turns queued behind the stuck one and got `busy`, and the bridge log
  showed nothing, because only finished turns were logged. Now:
  - a warm turn with no output for `--timeout` (default **60 s**, was a 170 s
    total; the clock pauses while a meeting tool waits on MIA) is cancelled
    (ACP `session/cancel`; the agent is restarted only if it does not stop),
    and the turn ends with the spoken reply "That took too long, so I stopped
    it." (`done` with `timed_out: true`), so later turns still work;
  - a new turn stops a turn MIA gave up on instead of queueing behind it (the
    warm-up turn is still waited out);
  - every failed or stopped turn is logged (`[error] …`, `[timeout] …`);
  - every request an ACP agent sends gets an answer, even a malformed one;
    permission options are matched by ACP kind, then by name ("Allow"/"Deny");
  - `serve --debug` (or `MS_AGENT_KIT_DEBUG=1`) logs each ACP message type in
    and out, and permission decisions, never the text.
- A client that hangs up (MIA, ngrok) no longer prints a Python traceback
  (`ConnectionResetError` in `http.server`) in the bridge window; with
  `--debug` it is one line. Other errors keep their traceback.

### Changed

- Setup and `doctor` check the warm path meetings use. For ACP agents
  (Antigravity, Hermes, Copilot, Muse, OpenClaw), meetings run a different
  command than the one-shot test turn. Setup's test step now also starts the ACP
  session with the meeting tools (through the stdio shim) and asks the agent to
  call a harmless test tool, answered as MIA would: "✓ Warm mode (ACP) ready ·
  meeting tools work", or the real error with its fix, then Test again / Start
  anyway / Quit. `doctor` runs the handshake and reports the meeting tools the
  agent loaded, with no model turn (`--no-warm` skips it). `bench` rows name the
  warm driver; Antigravity's warm turns go through ACP.
- Setup offers to install a missing agent. It shows the vendor's official
  install command (`npm install -g …` for Claude Code, Codex and Copilot; the
  vendor's own install script for Antigravity, Muse, Hermes and OpenClaw),
  checks what that command needs (npm, curl, bash) and says where to get a
  missing one, then runs it in the foreground after a yes. When the new command
  is not on PATH yet, it finds it in the usual install folders and prints the
  line to add to your shell profile. Then it offers the agent's own sign-in (or
  OpenClaw's onboarding); signing in stays yours. Without a terminal it only
  prints the command. `doctor` and the not-installed messages show the same
  command.
- Antigravity's command and file-change requests (Allow/Deny over ACP) are
  answered by access level, so **Ask first** works for it (it used to run as
  **Meeting only**). MIA's model choice is sent with `session/set_model`; if
  agy does not take it, the bridge logs that and agy's own model answers.
  Single runs (the setup test, `bench`) still use `agy -p`.

## [0.3.1] - Unreleased

### Fixed

- Antigravity CLI answers were dropped: its `stream-json` lines name their kind
  in `event`, and the bridge read only `type`, so every turn (the setup test and
  meetings) came back empty. Both keys are read now, and the `CANCELED`,
  `INTERRUPTED` and `INVALID` result states count as errors.

### Changed

- The agent's instructions follow the access level the meeting runs at. Before,
  every level was told to act only in the meeting, so **Full** refused local
  work it was allowed to do. Now **Meeting only** (`readonly`) is a meeting
  companion that changes nothing on the computer, **Ask first** (`confirm`) may
  do anything after your yes, and **Full** may do anything without asking
  (checking out loud before anything hard to undo). Setup and the README use
  these names.
- Setup looks like a modern agent CLI: a header, numbered steps, arrow-key
  menus, coloured ✓/✗ checks, a spinner for slow steps, and a summary of what
  is running with the pairing token and Bridge URL each on its own line.
  Piped or redirected runs keep plain numbered questions (`NO_COLOR` and
  `FORCE_COLOR` are honoured). Still no dependencies.
- ngrok in setup: no more "have you connected ngrok?" question. Setup reads
  the ngrok config (`ngrok config check`), knows whether an authtoken is saved,
  and uses a domain named there (or reserved on the account, when an ngrok API
  key is configured). Otherwise it starts ngrok once against an empty local
  port to see which address the account gets, and keeps it when it stays the
  same. Only then does it ask, saying where to find the domain and what blank
  means. Missing ngrok or authtoken: the one command to run, then Check again.
- When the setup test turn fails, setup names the reason (not installed,
  signed out, timed out, failed, or an empty reply, with the CLI's last stderr
  lines), offers to run the sign-in command and tests again, or starts anyway
  or quits. Ended input never loops on a default answer.

## [0.3.0] - 2026-11-18

First public release, as the `meetstream-agent-kit` package.

### Added

- Guided setup: run `meetstream-agent-kit` (or `setup`) and answer a few
  questions (agent, folder, most access, Cloudflare quick tunnel / ngrok / your
  own address). It checks the agent's login, runs one test turn, asks "Start
  now?", then starts the bridge and tunnel and prints the pairing token and the
  Bridge URL to paste. Answers are saved; the next run starts with one Enter.
- `serve --tunnel cloudflare|ngrok|url` opens the tunnel too, waits until the
  public address really reaches the bridge, prints it, and stops it on Ctrl-C.
  ngrok errors (no account, already running, domain not yours) come with the fix.
- `doctor`: checks Python, the config folder, the agent CLI, its login, the
  work folder, pairings and the port, and prints the fix for each problem.
- `serve` checks before it starts and explains each failure with its fix: a
  busy port, a bridge already running on that port, a missing agent CLI, or a
  missing or unreadable work folder.
- A missing agent CLI is reported as `not_installed` with an install hint,
  instead of as an expired login.
- A single-file `.pyz` build for each release, with SHA-256 checksums and
  build provenance.
- Screenshots from MIA's screen tools reach the agent as MCP images, kept
  whole (text results stay capped). Requests can be up to 4 MiB.

### Changed

- Split into one module per job, with strict typing and full tests.
- Claude Code gets the meeting secret from a 0600 file instead of its command
  line.
- An agent that ignores SIGTERM is killed after 2 seconds. Its pipes are
  closed and the process is reaped.
- Settings live in `~/.config/meetstream-agent-kit`. Pairings from the
  pre-release bridge keep working and move there on the next change.
- New tokens start with `msak_`; older tokens still work.

### Fixed

- The activation code belongs to the meeting: when MIA restarts the agent
  with a changed setup, the owner who already pasted the code stays
  activated. The bridge logs what changed.

## [0.2.0] - pre-release

Internal bridge (`mia_agent_bridge.py`) used for MeetStream's own testing.
