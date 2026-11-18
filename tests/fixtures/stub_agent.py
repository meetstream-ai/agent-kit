#!/usr/bin/env python3
"""Stand-in agent CLI for the tests: no model, no login, no network beyond 127.0.0.1.

It never calls a model. It speaks Claude Code's ``stream-json`` output:
  * one-shot: reads the prompt from stdin, prints text deltas and a ``result``;
  * warm (``--input-format stream-json``): one ``user`` line per turn on stdin,
    one ``result`` per turn, the process stays up;
  * a turn whose text contains ``TOOL:<name>`` calls that meeting tool through
    the MCP server named in ``--mcp-config`` and answers with its result;
  * ``auth status`` prints a login status.

Env: STUB_STARTUP_S (delay before init), STUB_FIRST_S / STUB_TOTAL_S (per turn),
STUB_AUTH_FAIL=1 (logged out).
"""

import json
import os
import re
import sys
import time
import urllib.request

ANSWER = "The answer is 42."
AUTH_ERROR = "Failed to authenticate: OAuth token has expired"


def emit(event):
    print(json.dumps(event), flush=True)


def arg_value(flag):
    if flag in sys.argv:
        idx = sys.argv.index(flag)
        if idx + 1 < len(sys.argv):
            return sys.argv[idx + 1]
    return None


def load_mcp_config():
    """``--mcp-config`` is a file path (the bridge keeps the secret out of argv) or inline JSON."""
    value = arg_value("--mcp-config") or "{}"
    if os.path.isfile(value):
        with open(value, encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(value)


def call_mcp_tool(name):
    config = load_mcp_config()
    server = next(iter((config.get("mcpServers") or {}).values()), None)
    if not server:
        return "no MCP server configured"

    def rpc(method, params, msg_id):
        req = urllib.request.Request(
            server["url"],
            method="POST",
            data=json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}).encode(),
            headers={"Content-Type": "application/json", **server.get("headers", {})},
        )
        with urllib.request.urlopen(req, timeout=70) as resp:
            return json.loads(resp.read())

    rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}, 1)
    listed = [t["name"] for t in rpc("tools/list", {}, 2)["result"]["tools"]]
    if name not in listed:
        return f"tool {name} not listed"
    result = rpc("tools/call", {"name": name, "arguments": {"text": "hi"}}, 3)["result"]
    return result["content"][0]["text"]


def answer_turn(text):
    if os.environ.get("STUB_AUTH_FAIL") == "1":
        emit({"type": "result", "is_error": True, "result": AUTH_ERROR})
        return
    first = float(os.environ.get("STUB_FIRST_S", "0"))
    total = float(os.environ.get("STUB_TOTAL_S", "0"))
    answer = ANSWER
    tool = re.search(r"TOOL:([A-Za-z0-9_]+)", text)
    if tool:
        answer = "Tool said: " + call_mcp_tool(tool.group(1))
    elif "<meeting_turns>" not in text:
        answer = "no meeting turns"
    elif "LONGSTREAM" in text:
        # Like Claude Code seen live (11-17): a slow answer that keeps streaming
        # for a moment after SIGTERM, so a barge-in lands mid-output.
        import signal

        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        answer = " ".join(f"stale{i}" for i in range(60))
    time.sleep(first)
    for word in answer.split(" "):
        event = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": word + " "}}
        emit({"type": "stream_event", "event": event})
        if "LONGSTREAM" in text:
            time.sleep(0.03)
    time.sleep(max(0.0, total - first))
    emit({"type": "result", "is_error": False, "result": answer})


def _rpc_loop(handle):
    """Line-delimited JSON-RPC on stdio (ACP and Codex app-server stand-ins)."""
    import threading

    pending = {}

    def reader():
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "method" not in msg and msg.get("id") in pending:  # a reply to our request
                pending.pop(msg["id"]).put(msg)
                continue
            threading.Thread(target=handle, args=(msg, pending), daemon=True).start()

    reader()


def _acp(msg, pending):
    """Minimal ACP agent: streams words; 'SLOW' waits for session/cancel;
    'PERMISSION' asks for an execute permission and reports the outcome."""
    import queue

    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        emit({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1, "agentCapabilities": {}}})
    elif method == "session/new":
        _acp.mcp = params.get("mcpServers") or []
        emit({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "sess_1"}})
    elif method == "session/cancel":
        _acp.cancelled = True
    elif method == "session/prompt":
        text = "".join(p.get("text", "") for p in params.get("prompt") or [])
        answer = ANSWER
        if "PERMISSION" in text:
            ask_id = 9001
            box = queue.Queue()
            pending[ask_id] = box
            emit(
                {
                    "jsonrpc": "2.0",
                    "id": ask_id,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": "sess_1",
                        "toolCall": {"toolCallId": "t1", "kind": "execute", "title": "rm -rf /"},
                        "options": [
                            {"optionId": "yes", "name": "Allow", "kind": "allow_once"},
                            {"optionId": "no", "name": "Deny", "kind": "reject_once"},
                        ],
                    },
                }
            )
            outcome = box.get(timeout=10)["result"]["outcome"]
            answer = f"permission {outcome.get('optionId') or outcome.get('outcome')}"
        if "SLOW" in text:
            _acp.cancelled = False
            for _ in range(500):
                if _acp.cancelled:
                    emit({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "cancelled"}})
                    return
                time.sleep(0.01)
        for word in answer.split(" "):
            emit(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": "sess_1",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": word + " "},
                        },
                    },
                }
            )
        emit({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})


_acp.cancelled = False
_acp.mcp = []


def _app_server(msg, pending):
    """Minimal Codex app-server: threads, turns, deltas, interrupt."""
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        emit({"id": mid, "result": {"userAgent": "stub"}})
    elif method == "thread/start":
        _app_server.thread = params
        emit({"id": mid, "result": {"thread": {"id": "thr_1"}, "model": params.get("model") or "stub"}})
    elif method == "turn/interrupt":
        _app_server.interrupted.add(params.get("turnId"))
        emit({"id": mid, "result": {}})
    elif method == "turn/start":
        _app_server.n += 1
        turn_id = f"turn_{_app_server.n}"
        emit({"id": mid, "result": {"turn": {"id": turn_id, "status": "inProgress"}}})
        text = "".join(p.get("text", "") for p in params.get("input") or [])
        if os.environ.get("STUB_AUTH_FAIL") == "1":
            emit(
                {
                    "method": "error",
                    "params": {
                        "error": {"message": "401", "codexErrorInfo": "unauthorized"},
                        "willRetry": False,
                        "threadId": "thr_1",
                        "turnId": turn_id,
                    },
                }
            )
            return
        if "SLOW" in text:
            for _ in range(500):
                if turn_id in _app_server.interrupted:
                    emit(
                        {
                            "method": "turn/completed",
                            "params": {"threadId": "thr_1", "turn": {"id": turn_id, "status": "interrupted"}},
                        }
                    )
                    return
                time.sleep(0.01)
        for word in ANSWER.split(" "):
            emit(
                {
                    "method": "item/agentMessage/delta",
                    "params": {"threadId": "thr_1", "turnId": turn_id, "itemId": "i1", "delta": word + " "},
                }
            )
        emit(
            {
                "method": "turn/completed",
                "params": {"threadId": "thr_1", "turn": {"id": turn_id, "status": "completed"}},
            }
        )


_app_server.n = 0
_app_server.thread = {}
_app_server.interrupted = set()


def main():
    if sys.argv[1:2] == ["acp"]:
        _rpc_loop(_acp)
        return 0
    if sys.argv[1:2] == ["app-server"]:
        _rpc_loop(_app_server)
        return 0
    if sys.argv[1:3] == ["auth", "status"]:
        if os.environ.get("STUB_AUTH_FAIL") == "1":
            print("Not logged in · Please run /login")
            return 1
        print(json.dumps({"loggedIn": True}))
        return 0
    time.sleep(float(os.environ.get("STUB_STARTUP_S", "0")))
    emit({"type": "system", "subtype": "init"})
    if arg_value("--input-format") == "stream-json":
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            answer_turn(str((msg.get("message") or {}).get("content") or ""))
        return 0
    answer_turn(sys.stdin.read())
    return 1 if os.environ.get("STUB_AUTH_FAIL") == "1" else 0


if __name__ == "__main__":
    sys.exit(main())
