"""stdio to HTTP shim for the meeting tools.

Some ACP agents can only start MCP servers as local commands. This shim is
that command: it reads JSON-RPC lines on stdin, posts each to the bridge's
``/mcp/<session>`` endpoint and prints the reply. The meeting secret comes from
the environment, never from the command line.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

from meetstream_agent_kit.constants import MCP_SECRET_ENV, TOOL_RESULT_TIMEOUT_S


def run(url: str) -> int:
    if not url.startswith("http://127.0.0.1:"):
        sys.stderr.write("mcp-stdio only talks to the local bridge (http://127.0.0.1:...)\n")
        return 2
    secret = os.environ.get(MCP_SECRET_ENV, "")
    for line in sys.stdin:
        if not line.strip():
            continue
        request = urllib.request.Request(  # noqa: S310 - fixed to http://127.0.0.1 above
            url,
            data=line.encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {secret}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TOOL_RESULT_TIMEOUT_S + 10) as response:  # noqa: S310
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8")
        except OSError:
            continue
        if body.strip():
            sys.stdout.write(body.strip() + "\n")
            sys.stdout.flush()
    return 0
