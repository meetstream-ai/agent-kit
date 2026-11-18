"""Terminal output, desktop notifications and y/N prompts.

The bridge never prints request headers, tokens or meeting secrets.
"""

from __future__ import annotations

import json
import select
import shutil
import subprocess
import sys

from meetstream_agent_kit.constants import CONFIRM_WAIT_S


def say(text: str, *, end: str = "\n", err: bool = False) -> None:
    """Print one line and flush, so it shows at once next to a tunnel's output."""
    stream = sys.stderr if err else sys.stdout
    stream.write(text + end)
    stream.flush()


def notify(title: str, text: str) -> None:
    """Desktop notification (macOS, or Linux with notify-send). Always also printed."""
    say(f"[agent-kit] {title}: {text}")
    try:
        if sys.platform == "darwin" and shutil.which("osascript"):
            script = f"display notification {json.dumps(text)} with title {json.dumps(title)}"
            subprocess.run(["osascript", "-e", script], timeout=5, capture_output=True, check=False)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, text], timeout=5, capture_output=True, check=False)
    except (OSError, subprocess.SubprocessError):
        pass  # a missing notifier must never stop the bridge


def ask_confirm(action: str, wait_s: float = CONFIRM_WAIT_S) -> bool:
    """Ask y/N in this terminal. No terminal, or no answer within ``wait_s``, means no."""
    if not sys.stdin or not sys.stdin.isatty():
        return False
    say(f"\n[confirm] the agent wants to: {action[:300]}")
    say(f"[confirm] allow? [y/N] ({int(wait_s)} s) ", end="")
    ready, _, _ = select.select([sys.stdin], [], [], wait_s)
    if not ready:
        say("\n[confirm] no answer: denied")
        return False
    return sys.stdin.readline().strip().lower() in ("y", "yes")
