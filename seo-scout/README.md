# seo-scout

An Ahrefs-lite site auditor + keyword rank tracker built for SynthLab. Produces
a client-ready audit report that doubles as a pitch asset.

## Commands

```bash
# Full audit → reports/<domain>-<YYYY-MM-DD>.md
python seoscout.py audit https://example.com

# Side-by-side with a competitor (crawled at half the page budget)
python seoscout.py audit https://example.com --vs https://rival.com

# Offline / canned grading (no LLM call — for testing & demos)
python seoscout.py audit https://example.com --mock-llm

# Keyword rank tracking (reads keywords.yaml)
python seoscout.py rank            # runs web searches, stores history, emails moves
python seoscout.py rank --dry-run  # prints the plan, touches nothing
```

## `audit`

1. **Crawls** up to `max_pages` same-registered-domain pages (BFS from the URL),
   honouring `robots.txt`, with a 1s delay, 10s timeout and a realistic
   User-Agent. Per page: status, title, meta description, H1s, word count,
   images missing alt, canonical, schema.org JSON-LD types, phone/address
   strings (NAP), and internal/external link counts.
2. **Lighthouse** on the homepage (`npx --yes lighthouse …`). If Chrome or
   Lighthouse can't run, the audit continues and the report notes
   "performance data unavailable" — it never aborts for this.
3. **Grades** the structured crawl with a Fable-tier LLM: overall score,
   subscores (titles/meta, content, local SEO, technical, mobile speed),
   prioritized P0/P1/P2 fixes in plain-owner language, quick wins, and an
   owner-facing summary.
4. **Renders** a markdown report — score and summary up top (jargon-free), a
   scorecard, priority fixes, then a technical per-page appendix.

## `rank`

Reads `keywords.yaml` (`target_domain` + `keywords`). For each keyword it runs a
headless Claude web search for the top-10 organic domains, records the target's
position in the `rank_history` SQLite table, compares to the previous check, and
drafts an email digest of notable moves (≥3 positions, new entries, drop-outs).
Keywords with no ranking record cleanly as `not_in_top` (never error).

## Config

- `config.yaml`: `max_pages`, `crawl_delay_s`, `timeout_s`, `user_agent`, `lighthouse`.
- `keywords.yaml`: `target_domain` + `keywords` list.

## Tests (offline)

```bash
/opt/anaconda3/bin/python3 tests/test_offline.py
```

Serves a fixture site via `python -m http.server`, crawls it (checking robots
exclusion, link scoping and page analysis), renders a `--mock-llm` audit and a
`--vs` comparison, and verifies `rank --dry-run`. No LLM, no Lighthouse, no
third-party network.
