#!/usr/bin/env python3
"""notion-brain — a Notion AI replacement over your Google Drive docs.

A parallel Drive-sync flow (flows/drive-sync.md) mirrors Google Docs into
cache/ as markdown. This tool indexes that cache into a SQLite FTS5 table and
answers questions with an LLM grounded strictly in the indexed excerpts, with
doc-name citations.

Retrieval is BM25 over FTS5 — a deliberate choice over embeddings: zero extra
dependencies, no embedding API/model, and it indexes ~200 docs in seconds.

Subcommands:
  index                       (re)build the FTS index from cache/ (incremental)
  ask "<question>" [--mock-llm]
  digest                      weekly cron: draft an email of what changed
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

# --- shared-infra bootstrap ------------------------------------------------
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import yaml  # noqa: E402

from shared import db, llm, notify  # noqa: E402

TOOL = "notion-brain"
HERE = Path(__file__).resolve().parent

NO_ANSWER = "I don't have that in the indexed docs"


# --- config ----------------------------------------------------------------
def load_config(path: Optional[Path] = None) -> dict:
    path = path or (HERE / "config.yaml")
    with open(path) as f:
        return yaml.safe_load(f) or {}


def cache_dir(cfg: dict) -> Path:
    raw = cfg.get("cache_dir", "cache")
    p = Path(raw)
    return p if p.is_absolute() else (HERE / p)


# --- schema ----------------------------------------------------------------
def ensure_schema() -> None:
    with db.conn() as c:
        c.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS brain_chunks USING fts5("
            "doc_name, doc_path UNINDEXED, chunk_no UNINDEXED, content)"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS brain_docs ("
            "path TEXT PRIMARY KEY, sha TEXT, mtime REAL, indexed_at TEXT)"
        )


# --- chunking --------------------------------------------------------------
def _strip_frontmatter(text: str) -> tuple[str, str]:
    """Return (doc_display_name_hint, body_without_frontmatter)."""
    name = ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm = text[3:end]
            m = re.search(r"^source_name:\s*(.+)$", fm, re.MULTILINE)
            if m:
                name = m.group(1).strip().strip("'\"")
            text = text[end + 4:].lstrip("\n")
    return name, text


def chunk_text(text: str, size: int = 3000, overlap: int = 200) -> list[str]:
    """Split on paragraph boundaries into ~`size`-char chunks with `overlap`
    carried between adjacent chunks. Oversized single paragraphs are hard-split."""
    paras: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        while len(para) > size:
            paras.append(para[:size])
            para = para[size:]
        paras.append(para)

    chunks: list[str] = []
    cur = ""
    for para in paras:
        if cur and len(cur) + len(para) + 2 > size:
            chunks.append(cur.strip())
            tail = cur[-overlap:] if overlap else ""
            cur = (tail + "\n\n" + para) if tail else para
        else:
            cur = (cur + "\n\n" + para) if cur else para
    if cur.strip():
        chunks.append(cur.strip())
    return chunks


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- index -----------------------------------------------------------------
def do_index(cfg: dict) -> dict:
    """Incrementally (re)build the FTS index. Returns a counts summary."""
    ensure_schema()
    cdir = cache_dir(cfg)
    size = int(cfg.get("chunk_chars", 3000))
    overlap = int(cfg.get("chunk_overlap", 200))

    files = sorted(cdir.rglob("*.md")) if cdir.exists() else []
    seen_paths: set[str] = set()
    added = changed = unchanged = chunks_written = 0

    with db.conn() as c:
        known = {row[0]: (row[1], row[2])
                 for row in c.execute("SELECT path, sha, mtime FROM brain_docs")}

        for f in files:
            path = str(f.resolve())
            seen_paths.add(path)
            raw = f.read_text(errors="replace")
            sha = _sha(raw)
            mtime = f.stat().st_mtime
            prior = known.get(path)
            if prior and prior[0] == sha:
                unchanged += 1
                continue

            hint, body = _strip_frontmatter(raw)
            doc_name = hint or f.stem
            c.execute("DELETE FROM brain_chunks WHERE doc_path=?", (path,))
            for i, chunk in enumerate(chunk_text(body, size, overlap)):
                c.execute(
                    "INSERT INTO brain_chunks (doc_name, doc_path, chunk_no, content) "
                    "VALUES (?,?,?,?)", (doc_name, path, i, chunk))
                chunks_written += 1
            c.execute(
                "INSERT INTO brain_docs (path, sha, mtime, indexed_at) VALUES (?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET sha=excluded.sha, mtime=excluded.mtime, "
                "indexed_at=excluded.indexed_at",
                (path, sha, mtime, time.strftime("%Y-%m-%dT%H:%M:%S")))
            if prior:
                changed += 1
            else:
                added += 1

        removed = 0
        for path in list(known):
            if path not in seen_paths:
                c.execute("DELETE FROM brain_chunks WHERE doc_path=?", (path,))
                c.execute("DELETE FROM brain_docs WHERE path=?", (path,))
                removed += 1

    summary = {"docs": len(files), "added": added, "changed": changed,
               "unchanged": unchanged, "removed": removed, "chunks": chunks_written}
    notify.log(TOOL, "index: " + ", ".join(f"{k}={v}" for k, v in summary.items()))
    return summary


# --- retrieval -------------------------------------------------------------
def build_match_query(question: str) -> Optional[str]:
    """Turn arbitrary user text into a safe FTS5 MATCH expression.

    Raw user input into MATCH crashes on punctuation/operators, so we extract
    bare alphanumeric terms and quote each as a string literal, OR-joined."""
    terms = re.findall(r"[A-Za-z0-9]+", question.lower())
    terms = [t for t in terms if len(t) > 1 or t.isdigit()]
    if not terms:
        return None
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(terms))


def search(cfg: dict, question: str) -> list[tuple[str, str]]:
    """Return top-k (doc_name, content) chunks by BM25 (best first)."""
    ensure_schema()
    match = build_match_query(question)
    if not match:
        return []
    top_k = int(cfg.get("top_k", 6))
    with db.conn() as c:
        rows = c.execute(
            "SELECT doc_name, content FROM brain_chunks WHERE brain_chunks MATCH ? "
            "ORDER BY bm25(brain_chunks) LIMIT ?", (match, top_k)).fetchall()
    return [(r[0], r[1]) for r in rows]


# --- ask -------------------------------------------------------------------
_ASK_SYSTEM = (
    "You answer questions using ONLY the provided document excerpts. Cite the "
    "source of each claim inline as [doc: <name>] using the exact doc names "
    "shown. If the excerpts do not contain the answer, reply exactly: "
    f'"{NO_ANSWER}" — never invent facts and never fabricate a citation.'
)


def _ask_prompt(question: str, hits: list[tuple[str, str]]) -> str:
    parts = ["Excerpts:\n"]
    for name, content in hits:
        parts.append(f"[doc: {name}]\n{content}\n")
    parts.append(f"\nQuestion: {question}\n\nAnswer using only the excerpts above.")
    return "\n".join(parts)


def do_ask(cfg: dict, question: str, mock: bool = False) -> int:
    hits = search(cfg, question)
    if not hits:
        # short-circuit: no LLM call when retrieval is empty
        print(NO_ANSWER)
        print("\nSources: none")
        return 0

    sources = list(dict.fromkeys(name for name, _ in hits))
    if mock:
        answer = (f"[mock-llm] Retrieved {len(hits)} excerpt(s) for: {question}\n"
                  f"(LLM call skipped.) Grounding docs: "
                  + ", ".join(f"[doc: {s}]" for s in sources))
    else:
        answer = llm.complete(_ask_prompt(question, hits), system=_ASK_SYSTEM)

    print(answer.strip())
    if NO_ANSWER in answer:
        # retrieval surfaced chunks, but none contained the answer —
        # listing them as "Sources" would misread as attribution
        print("\nSources: none (retrieved excerpts did not contain the answer)")
    else:
        print("\nSources:")
        for s in sources:
            print(f"  - {s}")
    notify.log(TOOL, f"ask ({len(hits)} hits, mock={mock}): {question[:80]}")
    return 0


# --- digest ----------------------------------------------------------------
def _snapshot() -> dict[str, str]:
    ensure_schema()
    with db.conn() as c:
        return {row[0]: row[1]
                for row in c.execute("SELECT path, sha FROM brain_docs")}


def do_digest(cfg: dict, mock: bool = False) -> int:
    current = _snapshot()
    prev_raw = db.kv_get(TOOL, "last_digest_state")
    prev = json.loads(prev_raw) if prev_raw else {}

    added = [p for p in current if p not in prev]
    changed = [p for p in current if p in prev and current[p] != prev[p]]
    removed = [p for p in prev if p not in current]

    if not (added or changed or removed):
        notify.log(TOOL, "digest: no changes")
        return 0

    def names(paths: list[str]) -> str:
        return ", ".join(Path(p).stem for p in paths) or "none"

    change_text = (f"Added: {names(added)}\nChanged: {names(changed)}\n"
                   f"Removed: {names(removed)}")

    if mock:
        summary = "[mock-llm] weekly knowledge-base digest.\n\n" + change_text
    else:
        summary = llm.complete(
            "Write a one-paragraph weekly summary of these knowledge-base "
            "changes for a solo studio owner. Plain, concise, no fluff.\n\n"
            + change_text,
            system="You summarize document-base changes crisply. No emoji.")

    ok = notify.draft_email("notion-brain weekly digest", summary)
    db.kv_set(TOOL, "last_digest_state", json.dumps(current))
    notify.log(TOOL, f"digest drafted={ok} "
                     f"(+{len(added)} ~{len(changed)} -{len(removed)})")
    return 0


# --- CLI -------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="brain", description=__doc__)
    p.add_argument("--config", type=Path, default=None,
                   help="path to config.yaml (default: alongside this script)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("index", help="(re)build the FTS index from cache/")
    sp.set_defaults(func=lambda a: _run_index(a))

    sp = sub.add_parser("ask", help="answer a question from the indexed docs")
    sp.add_argument("question")
    sp.add_argument("--mock-llm", action="store_true")
    sp.set_defaults(func=lambda a: do_ask(load_config(a.config), a.question, a.mock_llm))

    sp = sub.add_parser("digest", help="weekly change digest (cron)")
    sp.add_argument("--mock-llm", action="store_true")
    sp.set_defaults(func=lambda a: do_digest(load_config(a.config), a.mock_llm))
    return p


def _run_index(a: argparse.Namespace) -> int:
    summary = do_index(load_config(a.config))
    print("index: " + ", ".join(f"{k}={v}" for k, v in summary.items()))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as e:  # never silent
        notify.log(TOOL, f"ERROR {args.cmd}: {e}")
        try:
            db.record_run(TOOL, "fail", f"{args.cmd}: {e}")
        except Exception:
            pass
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
