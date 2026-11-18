# Changelog

All notable changes to this package. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

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
