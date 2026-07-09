# automation-runner

A self-hosted Zapier replacement for SynthLab Co. Flows are plain markdown files
executed by headless Claude Code against your connected MCP servers (Gmail,
Google Drive, Google Calendar, Stripe, Jotform). **Adding an automation = adding
a markdown file. No code changes.**

## Commands

```bash
PY=/opt/anaconda3/bin/python3

# Run every due flow — put this on cron every 5 minutes:
#   */5 * * * * /opt/anaconda3/bin/python3 /path/to/automation-runner/runner.py tick
$PY runner.py tick

$PY runner.py run <flow-name>     # force-run one flow now, ignoring schedule
$PY runner.py validate [<name>]   # deterministic dry-run (no claude call); exit 1 if any flow is invalid
$PY runner.py list                # every flow: schedule, enabled, last run, next run
```

## Flow file format

A flow is a markdown file in one of the configured `flows_dirs` (see
`config.yaml`) with a YAML frontmatter block followed by the prompt body:

```markdown
---
name: stripe-receipt          # optional; defaults to the filename stem
schedule: "*/30 * * * *"      # required; standard 5-field cron
enabled: true                 # required-ish; bool, defaults true
allowed_tools: [mcp__claude_ai_Stripe, mcp__claude_ai_Google_Drive, mcp__claude_ai_Gmail]
timeout_seconds: 600          # optional; defaults to config default_timeout (600)
---
<the natural-language prompt body — the operational instructions>
```

`allowed_tools` are passed straight to `claude -p --allowedTools`. A bare server
prefix such as `mcp__claude_ai_Gmail` grants every tool on that server.

## Configuration — `config.yaml`

```yaml
flows_dirs:            # dirs scanned for *.md flows (relative to repo root; missing dirs skipped)
  - automation-runner/flows
  - booking-bridge/flows
  - notion-brain/flows
claude_model: sonnet  # model for `claude -p --model`
default_timeout: 600  # per-flow timeout when a flow omits timeout_seconds
```

## Execution model

On `tick`, each enabled flow whose cron schedule fired between its stored
last-run and now is executed. `runner.py` wraps the flow body in a standard
preamble (repo root, state-file contract, idempotency rules, the required
`FLOW_OK` / `FLOW_FAIL: <reason>` sentinel) and runs it via
`claude -p --model <model> --output-format text --allowedTools ...`.

- Success = the reply ends with `FLOW_OK` → logged + recorded.
- Nonzero exit, timeout, or a missing `FLOW_OK` → `notify.fail(...)` (Gmail-draft
  alert with a `logs/failures.log` fallback) + recorded run. One broken flow
  never aborts the tick.
- A flow file that no longer parses is skipped **and** surfaced via `notify.fail`.

## State model

Two independent layers:

1. **Scheduling marker** — `db.kv['automation-runner']['lastrun:<flow>']`, an ISO
   local timestamp the dispatcher uses to decide "is it due?". On a flow's first
   sighting the runner records *now* and does not backfill missed history.
2. **Flow memory** — `automation-runner/state/<flow>.json`, owned by the flow
   body itself. Holds `processed_ids` (for idempotency — e.g. a Stripe payment
   never gets two receipts) plus any other per-flow notes. The dispatcher never
   edits it; Claude reads it first and writes it back after acting. (This dir is
   gitignored.)

## Shipped flows

| Flow             | Schedule      | What it does |
|------------------|---------------|--------------|
| `stripe-receipt` | every 30 min  | New Stripe payments (last 3 days) → receipt doc in `SynthLab/Receipts/` + thank-you Gmail draft. |
| `jotform-intake` | RETIRED | 2026-07-09, Jotform is ENS-only (owner directive) — see the note in the flow file. |
| `morning-brief`  | 07:00 daily   | Unread inbox + today's calendar + last-24h Stripe → a `Morning Brief — <date>` Gmail draft to self. |

## Testing without spending plan credits

Set `SYNTHLAB_RUNNER_FAKE=ok` (or `fail`) to short-circuit the `claude`
subprocess with a synthetic `FLOW_OK` / `FLOW_FAIL` result. This drives the full
dispatch path (scheduling, last-run advancement, logging, failure alerts) without
ever launching Claude Code. `validate` and `list` never call claude at all.
