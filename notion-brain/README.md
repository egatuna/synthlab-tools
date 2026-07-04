# notion-brain

A Notion AI replacement over your Google Drive docs. Ask questions and get
answers grounded strictly in your own documents, with doc-name citations.

## How it works

1. **Drive-sync flow** (`flows/drive-sync.md`) — a scheduled markdown prompt run
   by the parallel automation-runner dispatcher. Nightly it mirrors the Google
   Drive folders in `config.yaml` (`drive_folders`) into `cache/<folder>/<title>.md`
   with a small frontmatter header, re-downloading only docs whose Drive
   `modified_time` changed and pruning docs that were deleted. **This tool
   depends on that flow to populate `cache/`** — with an empty cache, `index`
   and `ask` still run and simply report nothing to index / no answer.
2. **`index`** — walks `cache/`, chunks each doc (~3000 chars on paragraph
   boundaries, 200-char overlap) and stores the chunks in a SQLite **FTS5**
   virtual table in the shared `tools.db`. Reindex is incremental: a per-doc
   hash in `brain_docs` means unchanged files are skipped.
3. **`ask`** — BM25 top-k retrieval, then an LLM answers using only those
   excerpts, citing `[doc: <name>]`. If nothing matches, it short-circuits with
   "I don't have that in the indexed docs" and never calls the LLM.

### Why FTS5 / BM25 instead of embeddings

A deliberate design choice: **zero extra dependencies and no embedding API or
model to run**. FTS5 ships with SQLite, indexes ~200 docs in seconds, and BM25
keyword retrieval is more than enough for a personal/studio knowledge base. No
vector store, no embedding costs, no model downloads.

## Usage

```bash
PY=/opt/anaconda3/bin/python3

# Build / refresh the index (run after each Drive sync; incremental)
$PY brain.py index

# Ask a question (LLM grounded in your docs)
$PY brain.py ask "What's our Q4 pricing plan?"

# Offline: retrieve + print sources without calling the LLM
$PY brain.py ask "pricing plan" --mock-llm

# Weekly cron: draft an email summarizing what changed in the doc base
$PY brain.py digest
```

## Config (`config.yaml`)

- `drive_folders` — Drive folder names the sync flow mirrors (e.g. `["SynthLab"]`)
- `cache_dir` — local mirror location (default `cache/`)
- `top_k` — BM25 chunks fed to the LLM per question (default 6)
- `chunk_chars` / `chunk_overlap` — chunking parameters

## Notes

- User queries are sanitized before hitting FTS5 `MATCH` (terms extracted and
  quoted), so punctuation-heavy questions like `what's the Q4 plan?!` never
  crash the query parser.
- The index lives in the shared `tools.db` (`brain_chunks`, `brain_docs`).
