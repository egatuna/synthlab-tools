# synthlab-tools

Internal CLI suite replacing ~$140–250/mo of SaaS subscriptions. Everything
runs headless via cron + `claude -p` + the Anthropic API. No web UIs.

**Global by design.** Nothing here is scoped to a project: cron runs
machine-wide, and `bin/` is on PATH (via `~/.zshrc`), so from any directory:
`clip-forge run <video>`, `seo-scout audit <url>`, `contact-hunter run
<leads.md>`, `post-batcher plan "<brief>"`, `notion-brain ask "<q>"`,
`flows list|run|validate`, `bg-remove <dir>`, `img-resize <preset> <dir>`.
"SynthLab" only appears in editable defaults (voice/keywords/email copy in
each tool's `config.yaml`).

| Tool | Replaces | Run it |
|---|---|---|
| `automation-runner/` | Zapier | `python3 automation-runner/runner.py tick\|run\|validate\|list` |
| `clip-forge/` | Opus Clip | `python3 clip-forge/clipforge.py run <video.mp4\|url>` |
| `seo-scout/` | Ahrefs-lite / Ubersuggest | `python3 seo-scout/seoscout.py audit <url>` / `rank` |
| `contact-hunter/` | Hunter + Apollo | `python3 contact-hunter/hunter.py run <leads.md>` |
| `booking-bridge/` | Calendly | live form: https://form.jotform.com/261846157248061 (flows do the rest) |
| `post-batcher/` | Buffer | `python3 post-batcher/postbatcher.py plan "<brief>"` / `today` / `mark-posted` |
| `notion-brain/` | Notion AI | `python3 notion-brain/brain.py ask "<question>"` / `index` / `digest` |
| `shared/bg-remove.py`, `shared/img-resize.py` | Canva Pro | `python3 shared/bg-remove.py <dir>` / `img-resize.py <preset> <dir>` |

Framer/Webflow/v0 need no build here — covered by the existing
website-builder + design skills with GitHub Pages hosting. **Do not buy
Canva Pro** — bg-remove (rembg) and img-resize (Pillow presets) cover the
paid features used.

## Shared infrastructure

- `shared/llm.py` — LLM wrapper. Default model `claude-sonnet-4-6`; judgment
  calls (`judgment=True`: clip scoring, SEO grading) use `claude-fable-5`
  with an automatic server-side fallback to `claude-opus-4-8` on safety
  refusals. Backend: Anthropic SDK when `ANTHROPIC_API_KEY` is in `.env`,
  otherwise `claude -p` on the local Claude Code (Max plan) login.
- `shared/db.py` — one `tools.db` (SQLite): per-tool KV, seen-ID idempotency,
  run history. Tools may add their own tables.
- `shared/notify.py` — dated logs in `logs/<tool>/`; failures create a Gmail
  draft `[synthlab-tools] <tool> failed: <reason>` and fall back to a macOS
  notification + `logs/failures.log`. Nothing fails silently.
- MCPs (Gmail, Calendar, Drive, Jotform, Stripe) are reached through
  headless `claude -p` — flows declare which servers they may touch.

## Flows (the Zapier model)

A "zap" is a markdown file with YAML frontmatter (`schedule`, `enabled`,
`allowed_tools`) in `automation-runner/flows/`, `booking-bridge/flows/`, or
`notion-brain/flows/`. The dispatcher tick (cron, every 5 min) runs whatever
is due. **Adding an automation = adding a file.** Validate with
`runner.py validate` before enabling.

## Schedules

`crontab.txt` is the single source of truth. Install: `crontab crontab.txt`.

## Setup notes

- `.env` (optional): `ANTHROPIC_API_KEY=` to use the API instead of the
  Max-plan CLI backend. Copy from `.env.example`.
- Stripe MCP must be connected in Claude Code for the stripe-receipt flow;
  until then the flow reports `FLOW_FAIL: stripe mcp unavailable` (visible,
  not silent).
- clip-forge deps: ffmpeg (installed via brew), faster-whisper, yt-dlp.
