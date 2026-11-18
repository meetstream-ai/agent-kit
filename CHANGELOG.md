# Changelog

All notable changes to this package. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.3.0] - unreleased

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
