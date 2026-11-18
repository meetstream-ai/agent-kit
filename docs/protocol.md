# Bridge protocol

How MIA talks to the bridge. Everything is JSON over HTTP on the user's tunnel.

## Authentication

Every `/v1` request carries the pairing token:

```
Authorization: Bearer msak_...
```

A missing, wrong or revoked token gets `401 {"error": "unauthorized"}`. Request
bodies must be a JSON object of 1 byte to 256 KiB.

## `GET /v1/health`

```json
{
  "ok": true, "agent": "claude", "label": "Claude Code", "version": "0.3.0",
  "max_access": "readonly", "mode": "readonly", "warm": true, "sessions": 0,
  "login": "ok", "relogin": "claude auth login"
}
```

`mode` is the old name of `max_access`, kept for compatibility.

`login` is one of:

- `ok`: signed in.
- `unknown`: the CLI has no login check; the first turn tells.
- `expired`: run the `relogin` command.
- `not_installed`: the response also has an `install` hint.

When `login` is not `ok`, a `login_detail` field explains why.

## `POST /v1/session/start`

Called while the bot joins, before anyone speaks. It starts one warm agent
process for the meeting.

```json
{
  "session_id": "bot_123",
  "instructions": "MIA's configured instructions",
  "tools": [{"name": "send_chat_message", "description": "...", "parameters": {"type": "object"}}],
  "model": "",
  "access": "confirm",
  "owner_verified": false,
  "claim_required": true
}
```

- `session_id`: `[A-Za-z0-9_-]{1,80}`.
- `tools`: up to 64 meeting tools.
- `access`: the level MIA's agent config asks for (`readonly`, `confirm` or
  `full`). The bridge uses the lower of this and its own `--max-access`.
- `owner_verified`: MIA lets only the owner, matched by a verified id, start turns.
- `claim_required`: turn on the activation code.

The same `session_id` with the same settings reuses the warm agent; any change
restarts it.

Response:

```json
{
  "agent": "claude", "warm": true, "reused": false, "spawn_ms": 31.2,
  "access": "confirm", "max_access": "full", "access_reason": "",
  "claim": "pending", "login": "ok", "relogin": "claude auth login"
}
```

If `login` is `expired` or `not_installed`, no session is started.

## `POST /v1/turn`

```json
{"session_id": "bot_123", "messages": [{"speaker": "Alex", "text": "what changed in the API?"}]}
```

The bridge sends the last 20 messages to the agent as untrusted meeting text.
The response is `application/x-ndjson`, sent in chunks. Each line is one of:

| `type` | Fields | Meaning |
|---|---|---|
| `delta` | `text` | answer text as it is written; speak it at once |
| `tool_call` | `call_id`, `name`, `arguments` (JSON string) | run this meeting tool, then call `/v1/tool_result` |
| `done` | `text`, `first_output_ms`, `total_ms`, `warm`; `timed_out: true` when stopped for time | the turn is over |
| `error` | `code`, `message` | the turn failed |

The `code` of an `error` line is one of:

- `auth_required`: the line also has `relogin`.
- `agent_error`
- `timeout`: no longer sent for a slow turn (see below); kept for older bridges.
- `cancelled`
- `busy`: only when a running turn could not be stopped within 15 s.

**A turn never goes silent.** A warm turn with no output for `--timeout`
seconds (default 60; the clock pauses while a meeting tool waits on MIA), or a
one-run-per-turn agent that takes that long in all, is stopped: the bridge asks
the agent to cancel (ACP `session/cancel`), keeps the session when it does and
restarts it when it does not. The turn then ends with the delta
`"That took too long, so I stopped it."` and a `done` line with
`timed_out: true`, so MIA has something to say. 60 s stays under MIA's own
120 s read timeout.

**One turn at a time.** MIA runs one turn per meeting at a time, so a turn
that arrives while another is still running means MIA gave up on that one:
the bridge cancels it (that stream ends with `cancelled`) and runs the new
turn, instead of queueing it. The warm-up turn at meeting start is waited out
instead.

With the activation code on and not yet claimed, the turn gets
`403 {"error": "locked", "claim": "pending"}`.

## `POST /v1/tool_result`

```json
{"session_id": "bot_123", "call_id": "call_...", "output": "posted", "is_error": false}
```

## `POST /v1/cancel`

```json
{"session_id": "bot_123"}
```

Barge-in. The bridge stops the running turn and keeps the conversation.

## `POST /v1/claim` and `POST /v1/release`

```json
{"session_id": "bot_123", "code": "123456", "participant_id": "p-42", "name": "Alex"}
```

`/v1/claim` returns `{"ok": true, "reason": "ok", "claim": "claimed"}`. Other
`reason` values:

- `wrong_code`
- `expired`
- `locked`
- `already_claimed`
- `no_participant_id`

The session gets `409` when it has no activation code.

`/v1/release` takes `{"session_id": ..., "participant_ids": [...]}`, the
participants who left. If the owner is among them, the agent locks again and a
new code is shown.

## `POST /v1/session/end`

```json
{"session_id": "bot_123"}
```

Stops the meeting's agent process. Returns `{"ok": true}`, or `{"ok": false}`
for an unknown session.

## `/mcp/<session_id>` (local only)

The meeting tools, served to the local agent process as an MCP server
(Streamable HTTP, JSON responses).

- It needs that meeting's own secret. The pairing token does not work here.
- The agent gets the secret through a 0600 config file (Claude Code) or an
  environment variable (Codex, ACP agents), never on the command line.
- MIA never calls this endpoint.
