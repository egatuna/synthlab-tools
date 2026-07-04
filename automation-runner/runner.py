#!/usr/bin/env python3
"""automation-runner — a self-hosted Zapier replacement for SynthLab Co.

Flows are markdown files with YAML frontmatter (see ``flows/`` and README.md).
Each flow is a natural-language operational prompt executed by headless
Claude Code against the user's connected MCP servers (Gmail, Drive, Stripe,
Jotform, Calendar). This module is the dispatcher: it discovers flows, decides
which are due, runs them idempotently, and never lets one broken flow abort the
whole tick.

Subcommands
-----------
    tick                 run every due flow (call from cron every 5 min)
    run <flow-name>      force-run one flow now, ignoring its schedule
    validate [<name>]    deterministic dry-run (no claude call); exit 1 if bad
    list                 show every flow with schedule / enabled / runs

State model
-----------
* ``db.kv['automation-runner']['lastrun:<flow>']`` — ISO local timestamp of the
  last time the flow was considered by ``tick``. Drives the cron "is it due?"
  calculation. First time a flow is seen we record *now* and do not backfill.
* ``automation-runner/state/<flow>.json`` — per-flow memory owned by the flow
  body itself (Claude reads/writes it). Holds ``processed_ids`` for idempotency
  plus any other notes the flow keeps. The dispatcher never edits it.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml
from croniter import croniter

# --- repo bootstrap: put the repo root on sys.path so `shared` imports work ---
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from shared import db, notify  # noqa: E402

TOOL = "automation-runner"
HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.yaml"
STATE_DIR = HERE / "state"

# Deterministic test hook. When SYNTHLAB_RUNNER_FAKE is set the claude
# subprocess is never launched; instead we synthesize its output. This is what
# the test suite and offline mock-data dry-runs use.
#   SYNTHLAB_RUNNER_FAKE=ok    -> fake success (FLOW_OK)
#   SYNTHLAB_RUNNER_FAKE=fail  -> fake failure (FLOW_FAIL: simulated failure)
FAKE_ENV = "SYNTHLAB_RUNNER_FAKE"


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    """Runner configuration loaded from ``config.yaml``."""

    flows_dirs: list[Path]
    claude_model: str = "sonnet"
    default_timeout: int = 600


def load_config() -> Config:
    """Load ``config.yaml``, resolving flow dirs against the repo root.

    Falls back to sane defaults if the file (or any key) is missing so the tool
    still works with a bare checkout.
    """
    raw: dict = {}
    if CONFIG_PATH.exists():
        raw = yaml.safe_load(CONFIG_PATH.read_text()) or {}

    dirs_cfg = raw.get("flows_dirs") or ["automation-runner/flows"]
    flows_dirs = [(REPO / d).resolve() for d in dirs_cfg]

    return Config(
        flows_dirs=flows_dirs,
        claude_model=str(raw.get("claude_model", "sonnet")),
        default_timeout=int(raw.get("default_timeout", 600)),
    )


# --------------------------------------------------------------------------- #
# Flow model + parsing
# --------------------------------------------------------------------------- #
class FlowError(ValueError):
    """Raised when a flow file cannot be parsed or fails validation."""


@dataclass
class Flow:
    """A parsed, validated flow file."""

    path: Path
    name: str
    schedule: str
    enabled: bool
    allowed_tools: list[str]
    body: str
    timeout_seconds: int = 600
    meta: dict = field(default_factory=dict)

    def next_fire(self, base: datetime | None = None) -> datetime:
        """Return the next scheduled fire time strictly after ``base`` (now)."""
        base = base or datetime.now()
        return croniter(self.schedule, base).get_next(datetime)


def _split_frontmatter(text: str, path: Path) -> tuple[dict, str]:
    """Split a ``---``-delimited YAML frontmatter block from the markdown body.

    Returns ``(frontmatter_dict, body_str)``. Raises :class:`FlowError` if the
    frontmatter block is absent or malformed.
    """
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        raise FlowError(f"{path.name}: missing '---' YAML frontmatter block")

    # Split on the frontmatter fences. lstrip removed leading blank lines, so the
    # first fence is at the very start; the body is everything after the second.
    parts = stripped.split("\n---", 1)
    if len(parts) != 2:
        raise FlowError(f"{path.name}: unterminated frontmatter (need a closing '---')")

    fm_text = parts[0][len("---"):]  # drop the opening fence
    body = parts[1].lstrip("\n")

    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - exercised via validate
        raise FlowError(f"{path.name}: invalid YAML frontmatter: {exc}") from exc

    if not isinstance(fm, dict):
        raise FlowError(f"{path.name}: frontmatter must be a mapping")
    return fm, body


def parse_flow(path: Path, default_timeout: int = 600) -> Flow:
    """Parse and validate a flow file. Raises :class:`FlowError` on any problem.

    Validation is deterministic and does not touch the network or claude:
    frontmatter present, ``schedule`` is a valid cron expression, ``enabled`` is
    a bool, ``allowed_tools`` is a list, and the body is non-empty.
    """
    try:
        text = path.read_text()
    except OSError as exc:
        raise FlowError(f"{path}: cannot read file: {exc}") from exc

    fm, body = _split_frontmatter(text, path)

    name = str(fm.get("name") or path.stem).strip()
    if not name:
        raise FlowError(f"{path.name}: empty 'name'")

    schedule = fm.get("schedule")
    if not schedule or not isinstance(schedule, str):
        raise FlowError(f"{path.name}: 'schedule' is required and must be a cron string")
    if not croniter.is_valid(schedule):
        raise FlowError(f"{path.name}: invalid cron expression: {schedule!r}")

    enabled = fm.get("enabled", True)
    if not isinstance(enabled, bool):
        raise FlowError(f"{path.name}: 'enabled' must be a boolean (got {enabled!r})")

    allowed_tools = fm.get("allowed_tools", [])
    if not isinstance(allowed_tools, list):
        raise FlowError(f"{path.name}: 'allowed_tools' must be a list")
    allowed_tools = [str(t) for t in allowed_tools]

    timeout_seconds = fm.get("timeout_seconds", default_timeout)
    try:
        timeout_seconds = int(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise FlowError(f"{path.name}: 'timeout_seconds' must be an integer") from exc
    if timeout_seconds <= 0:
        raise FlowError(f"{path.name}: 'timeout_seconds' must be positive")

    if not body.strip():
        raise FlowError(f"{path.name}: flow body (the prompt) is empty")

    known = {"name", "schedule", "enabled", "allowed_tools", "timeout_seconds"}
    meta = {k: v for k, v in fm.items() if k not in known}

    return Flow(
        path=path,
        name=name,
        schedule=schedule,
        enabled=enabled,
        allowed_tools=allowed_tools,
        body=body,
        timeout_seconds=timeout_seconds,
        meta=meta,
    )


def discover_flow_paths(cfg: Config) -> list[Path]:
    """Return every ``*.md`` flow path across configured dirs (skipping README)."""
    paths: list[Path] = []
    for d in cfg.flows_dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            if p.name.lower() == "readme.md":
                continue
            paths.append(p)
    return paths


def load_flows(cfg: Config) -> tuple[list[Flow], list[tuple[Path, FlowError]]]:
    """Parse all discovered flows. Returns ``(ok_flows, [(path, error), ...])``.

    Bad flows are collected rather than raised so a single malformed file cannot
    stop the whole tick / listing.
    """
    ok: list[Flow] = []
    bad: list[tuple[Path, FlowError]] = []
    for p in discover_flow_paths(cfg):
        try:
            ok.append(parse_flow(p, cfg.default_timeout))
        except FlowError as exc:
            bad.append((p, exc))
    return ok, bad


# --------------------------------------------------------------------------- #
# last-run bookkeeping
# --------------------------------------------------------------------------- #
def _lastrun_key(name: str) -> str:
    return f"lastrun:{name}"


def get_lastrun(name: str) -> datetime | None:
    """Return the stored last-run time for a flow, or None if never seen."""
    raw = db.kv_get(TOOL, _lastrun_key(name))
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def set_lastrun(name: str, when: datetime) -> None:
    """Persist the last-run marker for a flow."""
    db.kv_set(TOOL, _lastrun_key(name), when.isoformat())


def is_due(flow: Flow, now: datetime) -> bool:
    """Decide whether ``flow`` should fire during this tick.

    A flow is due when a scheduled fire time falls in ``(last_run, now]``. On the
    very first sighting of a flow we record ``now`` and report *not due* — no
    backfilling of missed history.
    """
    last = get_lastrun(flow.name)
    if last is None:
        set_lastrun(flow.name, now)  # first sight: anchor, do not backfill
        return False
    next_fire = croniter(flow.schedule, last).get_next(datetime)
    return next_fire <= now


# --------------------------------------------------------------------------- #
# Prompt assembly + execution
# --------------------------------------------------------------------------- #
def build_prompt(flow: Flow) -> str:
    """Compose the full claude prompt: standard preamble + the flow body."""
    state_file = f"automation-runner/state/{flow.name}.json"
    preamble = f"""You are executing an automated SynthLab Co operations flow named "{flow.name}".

RUNTIME CONTRACT — follow exactly:
1. Repo root is: {REPO}
   All relative paths below are relative to this repo root.
2. This flow's state file is: {state_file}
   Read it FIRST. It may not exist yet — if so, treat it as the empty object
   {{"processed_ids": []}}. It stores "processed_ids" (IDs you have already
   acted on) plus any other memory this flow keeps between runs.
3. IDEMPOTENCY IS CRITICAL. Never take action on an ID already present in
   "processed_ids". For example a Stripe payment must NEVER receive a second
   receipt, and a form submission must never get a duplicate follow-up. When in
   doubt, do nothing rather than risk a duplicate.
4. After you finish acting, WRITE the updated state file back to {state_file}
   with every newly handled ID appended to "processed_ids". Create parent dirs
   if needed. Preserve existing entries.
5. Only use the MCP tools you have been granted. If a required MCP server is not
   connected or not authenticated, do nothing destructive.
6. END YOUR REPLY WITH EXACTLY ONE of these on its own final line:
      FLOW_OK
   on success (including the "nothing new to do" case), or
      FLOW_FAIL: <short reason>
   if anything blocked you (e.g. an MCP server unavailable/unauthenticated).
   The dispatcher parses this line literally — no text after it.

--- FLOW INSTRUCTIONS ---
{flow.body}"""
    return preamble


@dataclass
class ExecResult:
    """Outcome of a single flow execution."""

    ok: bool
    reason: str
    output: str


def _fake_exec(mode: str, flow: Flow) -> ExecResult:
    """Synthesize an execution result without launching claude (test hook)."""
    if mode == "ok":
        return ExecResult(True, "", f"[FAKE] flow {flow.name} ran\nFLOW_OK")
    return ExecResult(
        False, "simulated failure",
        f"[FAKE] flow {flow.name} ran\nFLOW_FAIL: simulated failure",
    )


def execute_flow(flow: Flow, cfg: Config) -> ExecResult:
    """Run a flow via headless Claude Code and interpret its final line.

    Honors the ``SYNTHLAB_RUNNER_FAKE`` test hook. Never raises: subprocess
    errors, timeouts, and missing FLOW_OK all become a failed :class:`ExecResult`.
    """
    fake = os.environ.get(FAKE_ENV)
    if fake:
        return _fake_exec(fake.strip().lower(), flow)

    prompt = build_prompt(flow)
    allowed = flow.allowed_tools or []
    cmd = [
        "claude", "-p", prompt,
        "--model", cfg.claude_model,
        "--output-format", "text",
    ]
    if allowed:
        cmd += ["--allowedTools", *allowed]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=flow.timeout_seconds
        )
    except subprocess.TimeoutExpired as exc:
        tail = (exc.output or "")[-1500:] if isinstance(exc.output, str) else ""
        return ExecResult(False, f"timed out after {flow.timeout_seconds}s", tail)
    except OSError as exc:
        return ExecResult(False, f"could not launch claude: {exc}", "")

    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        return ExecResult(False, f"claude exited {result.returncode}", output[-1500:])

    return interpret_output(output)


def interpret_output(output: str) -> ExecResult:
    """Turn raw claude stdout into an :class:`ExecResult` via the sentinel line."""
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    if last == "FLOW_OK" or last.endswith("FLOW_OK"):
        return ExecResult(True, "", output[-1500:])
    for ln in reversed(lines):
        if "FLOW_FAIL" in ln:
            reason = ln.split("FLOW_FAIL:", 1)[-1].strip() if "FLOW_FAIL:" in ln else ln
            return ExecResult(False, reason or "flow reported FLOW_FAIL", output[-1500:])
    return ExecResult(False, "flow did not end with FLOW_OK", output[-1500:])


def run_flow(flow: Flow, cfg: Config, *, forced: bool = False) -> ExecResult:
    """Execute one flow and record the outcome via shared logging/notify."""
    trigger = "forced" if forced else "scheduled"
    notify.log(TOOL, f"running flow '{flow.name}' ({trigger})")
    res = execute_flow(flow, cfg)
    if res.ok:
        notify.log(TOOL, f"flow '{flow.name}' OK")
        db.record_run(TOOL, "ok", f"flow {flow.name} ok ({trigger})")
    else:
        notify.fail(
            TOOL,
            f"flow {flow.name} failed: {res.reason}",
            detail=res.output,
        )
    return res


# --------------------------------------------------------------------------- #
# Small table helper
# --------------------------------------------------------------------------- #
def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    """Print a simple left-aligned fixed-width text table."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def _fmt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "—"


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #
def cmd_tick(cfg: Config) -> int:
    """Run every enabled flow that is due. Called from cron every 5 minutes."""
    now = datetime.now()
    flows, bad = load_flows(cfg)

    # A flow that no longer parses cleanly must be skipped AND surfaced.
    for path, err in bad:
        notify.fail(TOOL, f"flow {path.name} skipped: invalid frontmatter", detail=str(err))

    ran = 0
    for flow in flows:
        if not flow.enabled:
            continue
        try:
            due = is_due(flow, now)
        except Exception as exc:  # never let scheduling math abort the tick
            notify.fail(TOOL, f"flow {flow.name} schedule error", detail=str(exc))
            continue
        if not due:
            continue
        try:
            run_flow(flow, cfg)
        except Exception as exc:  # defensive: a flow crash must not kill the tick
            notify.fail(TOOL, f"flow {flow.name} crashed", detail=repr(exc))
        finally:
            set_lastrun(flow.name, now)  # advance marker even on failure (no retry storm)
            ran += 1

    print(f"tick complete: {ran} flow(s) fired, {len(bad)} invalid flow(s) skipped")
    return 0


def cmd_run(cfg: Config, name: str) -> int:
    """Force-run a single flow now, regardless of its schedule."""
    flows, bad = load_flows(cfg)
    for path, err in bad:
        if path.stem == name:
            print(f"error: flow '{name}' is invalid: {err}", file=sys.stderr)
            return 1
    match = next((f for f in flows if f.name == name or f.path.stem == name), None)
    if match is None:
        print(f"error: no flow named '{name}'", file=sys.stderr)
        return 1
    res = run_flow(match, cfg, forced=True)
    set_lastrun(match.name, datetime.now())
    if res.ok:
        print(f"flow '{name}' OK")
        return 0
    print(f"flow '{name}' FAILED: {res.reason}", file=sys.stderr)
    return 1


def cmd_validate(cfg: Config, name: str | None) -> int:
    """Deterministic dry-run: parse + validate flows, print a schedule table.

    Returns 1 if any targeted flow is invalid.
    """
    paths = discover_flow_paths(cfg)
    if name:
        paths = [p for p in paths if p.stem == name]
        if not paths:
            print(f"error: no flow file for '{name}'", file=sys.stderr)
            return 1

    now = datetime.now()
    rows: list[list[str]] = []
    errors: list[str] = []
    for p in paths:
        try:
            flow = parse_flow(p, cfg.default_timeout)
        except FlowError as exc:
            errors.append(str(exc))
            rows.append([p.stem, "INVALID", "—", "—", "—"])
            continue
        state = "enabled" if flow.enabled else "disabled"
        nxt = _fmt(flow.next_fire(now)) if flow.enabled else "—"
        rows.append([flow.name, state, flow.schedule, f"{flow.timeout_seconds}s", nxt])

    if rows:
        _print_table(["flow", "status", "schedule", "timeout", "next fire"], rows)

    if errors:
        print("\nVALIDATION ERRORS:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"\nOK: {len(rows)} flow(s) valid")
    return 0


def cmd_list(cfg: Config) -> int:
    """List every flow with schedule, enabled state, last run, and next run."""
    now = datetime.now()
    flows, bad = load_flows(cfg)
    rows: list[list[str]] = []
    for flow in flows:
        last = get_lastrun(flow.name)
        nxt = _fmt(flow.next_fire(now)) if flow.enabled else "—"
        rows.append([
            flow.name,
            flow.schedule,
            "yes" if flow.enabled else "no",
            _fmt(last),
            nxt,
        ])
    for path, _ in bad:
        rows.append([path.stem, "INVALID", "?", "—", "—"])

    if not rows:
        print("no flows found")
        return 0
    _print_table(["flow", "schedule", "enabled", "last run", "next run"], rows)
    if bad:
        print(f"\n{len(bad)} invalid flow(s) — run `validate` for details", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse CLI."""
    parser = argparse.ArgumentParser(
        prog="runner.py",
        description="automation-runner — markdown flows executed by Claude Code.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("tick", help="run every due flow (for cron, every 5 min)")
    p_run = sub.add_parser("run", help="force-run one flow now")
    p_run.add_argument("flow", help="flow name (or filename stem)")
    p_val = sub.add_parser("validate", help="deterministic dry-run, no claude call")
    p_val.add_argument("flow", nargs="?", help="optional single flow to validate")
    sub.add_parser("list", help="list flows with schedule and run history")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    cfg = load_config()

    if args.command == "tick":
        return cmd_tick(cfg)
    if args.command == "run":
        return cmd_run(cfg, args.flow)
    if args.command == "validate":
        return cmd_validate(cfg, args.flow)
    if args.command == "list":
        return cmd_list(cfg)
    return 2  # pragma: no cover - argparse enforces a valid subcommand


if __name__ == "__main__":
    raise SystemExit(main())
