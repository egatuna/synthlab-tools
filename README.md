# synthlab-tools

A suite of small command-line tools I built to replace paid SaaS subscriptions for
[SynthLab](https://synthlabco.com), my one-person web studio. Each tool covers one job a paid
product used to do (email finding, SEO audits, short-form video clipping, social scheduling,
document Q&A, workflow automation), runs locally, and uses Claude for the steps that need
judgment.

About 4,250 lines of Python, with offline test suites that run without network access or API calls.

## Tools

| Tool | Replaces | What it does |
|---|---|---|
| [`contact-hunter/`](contact-hunter/) | Hunter, Apollo | Reads a markdown lead list, scrapes each business's site and contact pages for emails (including `name (at) domain` obfuscation), falls back to pattern guesses, and **drops any address whose domain has no MX record**. Every result gets a `confirmed` or `plausible` confidence label. |
| [`seo-scout/`](seo-scout/) | Ahrefs-lite | `audit <url>` crawls a site (robots-aware, same domain), runs Lighthouse, has an LLM grade it, and writes a plain-language client report. `rank` tracks keyword positions over time in SQLite. |
| [`clip-forge/`](clip-forge/) | Opus Clip | Turns a long video into vertical clips: faster-whisper transcription with word timestamps, LLM clip selection with hook scores, ffmpeg 9:16 crop with burned-in word-synced captions. |
| [`post-batcher/`](post-batcher/) | Buffer | Generates a week of platform-specific posts (X, LinkedIn, TikTok) into an editable markdown queue, enforces each platform's character limit in code, and serves the day's posts as a digest. |
| [`notion-brain/`](notion-brain/) | Notion AI | Indexes synced Google Docs into SQLite FTS5 and answers questions grounded only in the retrieved excerpts, with citations. BM25 instead of embeddings on purpose: no extra model, no extra API, and ~200 docs index in seconds. |
| [`automation-runner/`](automation-runner/) | Zapier | A "zap" is a markdown file with YAML frontmatter (schedule, enabled, allowed tools). A cron dispatcher runs whatever is due through headless Claude Code with only the MCP servers that flow declares. Idempotent, and one broken flow never aborts the rest. |
| `shared/bg-remove.py`, `shared/img-resize.py` | Canva Pro | Background removal (rembg) and preset image resizing (Pillow). |

## Design choices

- **Testable offline.** Network, subprocess, and LLM calls sit behind module-level functions,
  and most tools take `--mock-llm`, so every pipeline can run end to end in tests for free.
- **Two-tier models.** `shared/llm.py` routes routine calls to a cheaper model and
  judgment calls (clip scoring, SEO grading) to a stronger one, with automatic fallback.
  Both are overridable via `SYNTHLAB_LLM_DEFAULT` / `SYNTHLAB_LLM_JUDGMENT`.
- **Nothing fails silently.** `shared/notify.py` writes dated logs, drafts a failure email,
  and falls back to a desktop notification.
- **One schedule file.** `crontab.txt` is the single source of truth for what runs when.
- **No secrets in the repo.** Keys live in a gitignored `.env` (see `.env.example`); the
  Claude Code CLI uses its own login.

## Status

| Tool | State |
|---|---|
| contact-hunter, seo-scout, clip-forge, post-batcher, notion-brain, image tools | Working; run on demand or from `crontab.txt` |
| automation-runner | Paused: headless MCP connectors regressed upstream in Claude Code; re-enables once fixed |
| `booking-bridge/` | Retired (kept for reference); its form backend was moved to a different service |

## Run it

```bash
python3 -m pip install pyyaml requests beautifulsoup4 dnspython croniter anthropic  # clip-forge also needs ffmpeg, faster-whisper, yt-dlp
cp .env.example .env                                         # optional: ANTHROPIC_API_KEY
python3 -m pytest -q                                         # offline test suite
python3 seo-scout/seoscout.py audit https://example.com --mock-llm
```

Without `ANTHROPIC_API_KEY`, LLM calls go through the local `claude -p` CLI instead of the API.
