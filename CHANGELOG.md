# Changelog

All notable changes to this package. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.4.0] - 2026-11-23

Last release. The Agent Kit is replaced by the `meetstream` CLI
([meetstream-ai/cli](https://github.com/meetstream-ai/cli)): the same setup,
your agent over the MeetStream relay with no tunnel, plus bots, transcripts and
calendar in one binary. Install it with `brew install meetstream-ai/tap/meetstream`,
`npm i -g @meetstream/cli` or `pipx install meetstream-cli`.

### Changed

- Every command prints that notice on stderr (stdout and JSON are unchanged).
  `MEETSTREAM_AGENT_KIT_NOTICE=off` hides it.
- Includes the 0.3.6 and 0.3.7 changes below, which were not published on their own.

## [0.3.7] - Not published on its own (shipped in 0.4.0)

### Added

- MIA, MeetStream's mascot, in the terminal: beside the title when setup
  starts (she waves), when the bridge is running, and in `doctor` (her problem
  face when something needs fixing). Terminals that show images (iTerm2,
  WezTerm, kitty, Ghostty; not inside tmux) get the real picture; other colour
  terminals get half-block art in true colour, or the 256-colour palette.
  Without a terminal or with `NO_COLOR` she stays away;
  `MEETSTREAM_AGENT_KIT_MASCOT=off|blocks|image` overrides. The art is
  pre-rendered from the official mascot GIFs by `scripts/build_mascot.py`
  (dev only) and adds no dependencies.

## [0.3.6] - Not published on its own (shipped in 0.4.0)

### Fixed

- Copilot's model step showed "auto, auto": `copilot --acp --stdio` lists
  `auto` twice and nothing else on many accounts. Model lists are now
  de-duplicated for every agent (the picker never shows a row twice), and when
  Copilot offers only Auto the step says how to enable other models. Checked
  with Copilot CLI 1.0.114: over ACP such an account accepts only `auto`
  (`--model` is ignored and `session/set_model` rejects other models), so no
  longer list is offered.
- Setup has a way back: every step after the first has a "← Back" row, and in
  a terminal Esc or ← go back too (typed questions take `<`). Earlier answers
  are kept and preselected; steps that asked nothing are skipped on the way
  back; back from the effort question returns to the model menu.
- A CLI that answers without reporting token usage (Copilot) is no longer
  shown as a failure: the measurement shows its answer time and says the CLI
  doesn't report usage. Picker rows say "usage n/a" (or MeetStream's published
  estimate when there is one), and "Measure live" is only offered for CLIs that
  report usage (Claude Code, Codex, Antigravity, Hermes).
- When the only model on offer is the default (Copilot's Auto), setup says
  "GitHub Copilot CLI picks the model itself (Auto)" instead of a menu with two
  near-identical rows.
- Setup crashed with `IndexError` in step 2 when Back was available: the model
  menu's detail line had no text for the "← Back" row. Every menu now has
  one, and a test walks every step, pressing Back once in each.
- Esc and ← in a typed question went into the answer as escape characters.
  They now go back a step.

- Hermes' model step showed only its default ("picks the model itself") and
  "1 model from " with no source. Setup now lists the default plus the
  provider:model pairs in `~/.hermes/config.yaml` (aliases, fallbacks,
  delegation, MoA), read without starting Hermes (its ACP model list can take
  over 30 s), and says how many more models your signed-in providers offer
  ("Type another model id" takes provider:model). Only Copilot's Auto skips
  the menu. A list that falls back to the CLI's settings names that source.
- Muse Code's installer leaves a small launcher that downloads the ~456 MB app
  on first run. When that download died, setup still said "Muse Code found".
  Setup now checks for the app beside the launcher: the agent menu says
  "launcher only; app not downloaded yet", and at a terminal setup offers to
  run the launcher's own download (`MUSE_LAUNCHER_INSTALL=1 muse`) with a
  retry, instead of moving on.
- API-rate prices matched the provider instead of the model for Hermes ids
  such as `openai-codex:gpt-6.2-astra`.

### Added

- Model picker rows show what an hour would cost at the vendor's API rates
  ("≈$0.17", or "—" when no price is known) and a "plan" tag when the CLI is
  signed in on a subscription or free plan ("free" for `:free` models). The
  detail line names the source: the CLI's own estimate, or the vendor's
  published per-token list price with its date. The dated price table
  (Anthropic, Google, OpenAI; input, output, cached) lives in the shared
  estimates file under `prices`, so the weekly refresh can update it. Plan
  detection reads only local sign-in files and never a secret.
- "Measure live" and `bench --compare` measure models in parallel: a work
  queue of 4 workers (`--parallel N`), each model in its own process and empty
  folder, with per-model timeouts. A free worker takes the next model at once.
  Transient failures (timeout, crash, rate limit, network) are retried once
  after a short pause; sign-in and unknown-model errors are not. At a terminal
  a live table shows each model (queued, its stage, retrying (1/1), ✓ or ✗
  with the reason, seconds, tokens/h and cost) and a progress line with the
  time left. Esc stops the rest and keeps the results so far. Picking more
  than 6 models asks first, since each is a real turn on your plan.

### Changed

- Setup's terminal screens run on prompt_toolkit and rich, the package's first
  two dependencies. Menus and text prompts redraw in place (also after a
  resize), show key hints (↑↓ move · Enter pick · Esc back) and fold into one
  answered line. Step headers are rules, and summaries are boxes. Without a
  terminal the numbered questions are unchanged. The single-file `.pyz` still
  has no dependencies and uses the built-in menus.

## [0.3.5] - 2026-11-22

### Added

- Shared model estimates: the model picker shows MeetStream's measurements for
  models you have not measured yourself, labelled "measured by MeetStream
  <date>" (the row says "MS 11-18"). Setup reads
  `https://meetstream-public-images.s3.amazonaws.com/agent-kit/model-estimates.json`
  at most once a day (cached in the kit's folder, 3 s timeout); offline or
  missing, it uses the copy bundled in the package. Your own measurements win
  and never leave your computer. Models in neither say "not measured yet",
  with "Measure live".
- `bench --compare --export model-estimates.json` writes (or merges into) that
  file: per agent and model, the measured turn (input, cached, output,
  thinking, cost where the CLI reports one), the one-hour projection, the cold
  time to first words, `measured_at`, the kit version and the assumptions.
  Nothing is uploaded.
- The bundled copy is seeded with the real runs of 2026-11-18: Antigravity
  (gemini-3.8-flash low/medium/high, claude-sonnet-4-6) and Claude Code
  (haiku, sonnet).

## [0.3.4] - Not published on its own (shipped in 0.3.5, 2026-11-22)

### Added

- `share_local_app(port, path?)` and `stop_local_app(port?)`: meeting tools the
  bridge serves itself, at Full or Ask-first access (Ask first asks you here).
  MIA's `screen_share` runs on the meeting bot's machine, so `localhost` there
  is not your app; `share_local_app` opens a temporary public address for your
  local port in the background (cloudflared quick tunnel; ngrok when
  cloudflared cannot reach its edge, as on networks that block it), returns it
  for `screen_share(url=...)`, and logs `[tunnel] 3000 -> https://...`. At most
  two per meeting, all closed when the meeting ends. The agent's rules say how
  to show local apps and to never run a tunnel program itself (Claude had run
  `cloudflared` in the foreground and hung the turn). Note: free ngrok
  addresses show ngrok's warning page to browsers first.

### Fixed

- A turn right after a cancelled one could end at once with nothing said
  (Claude Code; "Stop sharing your screen" after a cancelled `get_balance`).
  A cancel restarts Claude's process in the background, and the next turn could
  reach the old process before the restart and take the cancelled turn's end
  as its own. The next turn now waits for that restart.

## [0.3.3] - Not published on its own (shipped in 0.3.5, 2026-11-22)

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
- **Meeting context** (setup step 3): the table of everything your agent reads
  on every meeting turn (the folder's project rules, the agent's own memory
  such as `~/.claude/CLAUDE.md` or `~/.hermes/SOUL.md`, and your MEETING.md),
  each with its size and tokens, and the total in the summary box ("context:
  meeting only · 0.4k tokens/turn"). For a folder with project rules: Meeting
  only, Include this project's rules, or Custom (tick what goes in).
- **MEETING.md**: your own meeting instructions, in the meeting workspace,
  created with a short default. The bridge adds it to its rules for every
  agent (`<owner_meeting_notes>`, at most 4,000 characters). Edit it with
  `meetstream-agent-kit context edit`; `context show` prints the effective
  context and its token estimate; `serve --no-meeting-md` leaves it out.
  `doctor` has a `context` line that fails above 4,000 tokens per turn.
- Setup's test turns show live stages with real timings instead of a silent
  wait: "Woke Codex 0.3 s · First words 5.5 s · Answered 6.5 s", with rotating
  verbs (Pondering, Noodling, ...). The warm ACP check shows "Session ready ·
  Tool called · Answered", and `bench --compare` shows the same stages per
  model. Without a terminal it prints one plain line. `bench` cold rows add
  `first_line_ms`.

## [0.3.2] - Not published on its own (shipped in 0.3.5, 2026-11-22)

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

## [0.3.1] - Not published on its own (shipped in 0.3.5, 2026-11-22)

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
