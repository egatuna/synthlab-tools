"""Logging + failure notification for synthlab-tools.

log(tool, msg)        — dated log line to logs/<tool>/YYYY-MM-DD.log
fail(tool, reason)    — never-silent failure path:
                          1. log it
                          2. Gmail draft "[synthlab-tools] <tool> failed: <reason>"
                             via `claude -p` + the Gmail MCP create_draft tool
                          3. if the draft can't be created, macOS notification
                             + logs/failures.log so nothing is lost
draft_email(subject, body) — generic Gmail-draft helper for digests
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOGS = REPO / "logs"

GMAIL_DRAFT_TOOL = "mcp__claude_ai_Gmail__create_draft"


def log(tool: str, message: str) -> None:
    day_dir = LOGS / tool
    day_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(day_dir / f"{datetime.now():%Y-%m-%d}.log", "a") as f:
        f.write(f"[{stamp}] {message}\n")


def draft_email(subject: str, body: str, timeout: int = 300) -> bool:
    """Create a Gmail draft via claude -p. Returns True on success."""
    prompt = (
        "Create a Gmail draft using the create_draft tool with exactly this "
        f"subject and body, addressed to me (no recipient needed for a draft).\n\n"
        f"SUBJECT: {subject}\n\nBODY:\n{body}\n\n"
        "After creating the draft, reply with exactly: DRAFT_CREATED"
    )
    cmd = ["claude", "-p", prompt, "--model", "sonnet",
           "--allowedTools", GMAIL_DRAFT_TOOL, "--output-format", "text"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0 and "DRAFT_CREATED" in result.stdout
    except (subprocess.TimeoutExpired, OSError):
        return False


def _macos_notify(title: str, message: str) -> None:
    safe_title = title.replace('"', "'")
    safe_msg = message.replace('"', "'")
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe_msg}" with title "{safe_title}"'],
            capture_output=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        pass


def fail(tool: str, reason: str, detail: str = "") -> None:
    subject = f"[synthlab-tools] {tool} failed: {reason}"
    body = f"Tool: {tool}\nReason: {reason}\nTime: {datetime.now().isoformat()}\n\n{detail}"
    log(tool, f"FAIL: {reason} — {detail[:500]}")
    try:
        from shared import db
        db.record_run(tool, "fail", f"{reason} — {detail[:500]}")
    except Exception:
        pass
    if not draft_email(subject, body):
        _macos_notify(subject, reason)
        LOGS.mkdir(exist_ok=True)
        with open(LOGS / "failures.log", "a") as f:
            f.write(f"[{datetime.now().isoformat()}] {subject}\n{detail}\n---\n")
