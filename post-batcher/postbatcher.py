#!/usr/bin/env python3
"""post-batcher — a Buffer replacement for SynthLab Co.

Generates a week of platform-tailored social posts (X / LinkedIn / TikTok),
writes them to a human-editable, strictly-parseable markdown queue, and serves
today's unposted posts as copy-ready blocks or an 8am email digest.

Subcommands:
  plan "<brief>" [--week YYYY-WW] [--force] [--mock-llm]
  today
  digest        # 8am cron entry — drafts an email, or exits quietly
  mark-posted <YYYY-MM-DD> [platform]
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

# --- shared-infra bootstrap ------------------------------------------------
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import yaml  # noqa: E402

from shared import db, llm, notify  # noqa: E402

TOOL = "post-batcher"
HERE = Path(__file__).resolve().parent

PLATFORM_ORDER = ["X", "LinkedIn", "TikTok"]
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# --- config ----------------------------------------------------------------
def load_config(path: Optional[Path] = None) -> dict:
    path = path or (HERE / "config.yaml")
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    return cfg


def queue_dir(cfg: dict) -> Path:
    raw = cfg.get("queue_dir", "queue")
    p = Path(raw)
    return p if p.is_absolute() else (HERE / p)


# --- data model ------------------------------------------------------------
@dataclass
class Post:
    checked: bool
    text: str


@dataclass
class Day:
    date: str  # YYYY-MM-DD
    platforms: dict[str, list[Post]] = field(default_factory=dict)

    @property
    def dow(self) -> str:
        return datetime.strptime(self.date, "%Y-%m-%d").strftime("%a")


# --- week helpers ----------------------------------------------------------
_WEEK_RE = re.compile(r"^(\d{4})-?W?(\d{1,2})$", re.IGNORECASE)


def parse_week(week: Optional[str]) -> tuple[str, list[str]]:
    """Return (canonical 'YYYY-Www' label, list of 7 ISO date strings Mon..Sun)."""
    if week:
        m = _WEEK_RE.match(week.strip())
        if not m:
            raise ValueError(f"bad --week {week!r}; expected YYYY-WW")
        year, wk = int(m.group(1)), int(m.group(2))
    else:
        iso = datetime.now().isocalendar()
        year, wk = iso.year, iso.week
    monday = date.fromisocalendar(year, wk, 1)
    dates = [date.fromordinal(monday.toordinal() + i).isoformat() for i in range(7)]
    return f"{year}-W{wk:02d}", dates


# --- markdown render / parse ----------------------------------------------
def render_item(post: Post) -> list[str]:
    box = "x" if post.checked else " "
    lines = post.text.split("\n")
    out = [f"- [{box}] {lines[0]}"]
    for ln in lines[1:]:
        out.append(f"  {ln}" if ln else "  ")
    return out


def render_queue(week_label: str, days: list[Day]) -> str:
    out: list[str] = [f"# Post Queue — {week_label}", ""]
    for day in days:
        out.append(f"## {day.date} ({day.dow})")
        for plat in PLATFORM_ORDER:
            posts = day.platforms.get(plat)
            if not posts:
                continue
            out.append(f"### {plat}")
            for post in posts:
                out.extend(render_item(post))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


_DAY_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})")
_PLAT_RE = re.compile(r"^###\s+(.+?)\s*$")
_ITEM_RE = re.compile(r"^\s*-\s*\[([ xX])\]\s?(.*)$")


def parse_queue(text: str) -> list[Day]:
    """Tolerant parser: survives extra blank lines, reordered days, missing
    platforms, hand-edited text and multi-line (2-space-indented) posts."""
    days: list[Day] = []
    cur_day: Optional[Day] = None
    cur_plat: Optional[str] = None
    cur_post: Optional[Post] = None

    for raw in text.splitlines():
        m_day = _DAY_RE.match(raw)
        if m_day:
            cur_day = Day(date=m_day.group(1))
            days.append(cur_day)
            cur_plat = None
            cur_post = None
            continue
        m_plat = _PLAT_RE.match(raw)
        if m_plat:
            cur_plat = m_plat.group(1).strip()
            if cur_day is not None:
                cur_day.platforms.setdefault(cur_plat, [])
            cur_post = None
            continue
        m_item = _ITEM_RE.match(raw)
        if m_item:
            cur_post = Post(checked=m_item.group(1) in "xX", text=m_item.group(2))
            if cur_day is not None and cur_plat is not None:
                cur_day.platforms[cur_plat].append(cur_post)
            continue
        # continuation of a multi-line post (indented >= 2 spaces)
        if cur_post is not None and raw.startswith("  "):
            cur_post.text += "\n" + raw[2:]
            continue
        # blank line or stray text ends the current post's continuation window
        cur_post = None
    return days


# --- length validation -----------------------------------------------------
_HASHTAG_RE = re.compile(r"#\w+")


def _word_truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    budget = limit - 1  # room for the ellipsis
    cut = text[:budget]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip() + "…"


def validate_post(platform: str, text: str, cfg: dict) -> Optional[str]:
    """Return a human reason string if the post violates limits, else None."""
    limits = cfg["platforms"].get(platform, {})
    if platform == "TikTok":
        tags = _HASHTAG_RE.findall(text)
        caption = _HASHTAG_RE.sub("", text).strip()
        cap_lim = limits.get("caption_limit", 150)
        lo = limits.get("min_hashtags", 3)
        hi = limits.get("max_hashtags", 5)
        if len(caption) > cap_lim:
            return f"caption {len(caption)}>{cap_lim} chars"
        if not (lo <= len(tags) <= hi):
            return f"{len(tags)} hashtags (need {lo}-{hi})"
        return None
    limit = limits.get("limit")
    if limit and len(text) > limit:
        return f"{len(text)}>{limit} chars"
    return None


def validate_days(days: list[Day], cfg: dict) -> list[tuple[str, str, str]]:
    """Return list of (date, platform, reason) violations."""
    out: list[tuple[str, str, str]] = []
    for day in days:
        for plat, posts in day.platforms.items():
            for post in posts:
                reason = validate_post(plat, post.text, cfg)
                if reason:
                    out.append((day.date, plat, reason))
    return out


def truncate_violation(platform: str, text: str, cfg: dict) -> str:
    """Best-effort fix when the model keeps overshooting: trim at a word
    boundary. TikTok trims only the caption, preserving hashtags."""
    limits = cfg["platforms"].get(platform, {})
    if platform == "TikTok":
        tags = _HASHTAG_RE.findall(text)
        caption = _HASHTAG_RE.sub("", text).strip()
        caption = _word_truncate(caption, limits.get("caption_limit", 150))
        return (caption + " " + " ".join(tags)).strip()
    return _word_truncate(text, limits.get("limit", 280))


# --- LLM generation --------------------------------------------------------
def _system(cfg: dict) -> str:
    return (
        "You write social media posts for SynthLab Co.\n"
        + cfg.get("voice", "").strip()
    )


def _plan_prompt(brief: str, dates: list[str], cfg: dict) -> str:
    p = cfg["platforms"]
    return (
        f"Campaign brief: {brief}\n\n"
        f"Write posts for these 7 dates: {', '.join(dates)}.\n"
        "For EACH date produce three posts, one per platform:\n"
        f"  - X: a single post, HARD max {p['X']['limit']} characters.\n"
        f"  - LinkedIn: ~{p['LinkedIn']['limit']} chars max, line-broken with "
        "real newlines, no hashtag spam.\n"
        f"  - TikTok: a caption <= {p['TikTok']['caption_limit']} characters "
        f"PLUS {p['TikTok']['min_hashtags']}-{p['TikTok']['max_hashtags']} "
        "hashtags in the same string.\n\n"
        'Return JSON: {"days":[{"date":"YYYY-MM-DD","X":"...","LinkedIn":"...",'
        '"TikTok":"..."}, ...]} with exactly one object per date.'
    )


def _fix_prompt(brief: str, violations: list[tuple[str, str, str]], cfg: dict) -> str:
    lines = [f"  - {d} / {p}: {r}" for d, p, r in violations]
    p = cfg["platforms"]
    return (
        f"Campaign brief: {brief}\n\n"
        "These posts violated their length rules and must be rewritten to fit:\n"
        + "\n".join(lines)
        + "\n\nLimits: X <= "
        f"{p['X']['limit']} chars; LinkedIn <= {p['LinkedIn']['limit']} chars; "
        f"TikTok caption <= {p['TikTok']['caption_limit']} chars plus "
        f"{p['TikTok']['min_hashtags']}-{p['TikTok']['max_hashtags']} hashtags.\n"
        'Return JSON: {"fixes":[{"date":"YYYY-MM-DD","platform":"X",'
        '"text":"..."}, ...]} covering exactly the posts listed above.'
    )


def _llm_plan(brief: str, dates: list[str], cfg: dict) -> dict:
    return llm.complete_json(_plan_prompt(brief, dates, cfg), system=_system(cfg))


def _llm_fix(brief: str, violations: list[tuple[str, str, str]], cfg: dict) -> dict:
    return llm.complete_json(_fix_prompt(brief, violations, cfg), system=_system(cfg))


def _days_from_json(data: dict, dates: list[str]) -> list[Day]:
    by_date = {d.get("date"): d for d in data.get("days", [])}
    days: list[Day] = []
    for i, dstr in enumerate(dates):
        rec = by_date.get(dstr) or (data.get("days", [])[i]
                                    if i < len(data.get("days", [])) else {})
        day = Day(date=dstr)
        for plat in PLATFORM_ORDER:
            text = (rec.get(plat) or "").strip()
            if text:
                day.platforms[plat] = [Post(checked=False, text=text)]
        days.append(day)
    return days


def _mock_plan(brief: str, dates: list[str], cfg: dict) -> dict:
    """Deterministic offline generator so `plan --mock-llm` runs with no LLM."""
    days = []
    for dstr in dates:
        dow = datetime.strptime(dstr, "%Y-%m-%d").strftime("%A")
        days.append({
            "date": dstr,
            "X": f"{dow}: {brief}. We ship real work, not decks. "
                 "One studio, AI-powered, no fluff.",
            "LinkedIn": (
                f"{dow} note on {brief}.\n\n"
                "Most studios sell you a process. We sell you the finished site.\n\n"
                "Here is how the week actually goes:\n"
                "- You send the brief\n"
                "- We build, you review\n"
                "- It ships\n\n"
                "That is the whole pitch."
            ),
            "TikTok": f"{dow}: {brief[:60]} #websites #aidesign #studio",
        })
    return {"days": days}


def generate_week(
    brief: str,
    dates: list[str],
    cfg: dict,
    *,
    mock: bool = False,
    plan_fn: Optional[Callable] = None,
    fix_fn: Optional[Callable] = None,
) -> list[Day]:
    """Generate + validate + (retry|truncate) a full week of posts.

    plan_fn/fix_fn are injectable for offline testing; default to live LLM.
    Under mock=True the LLM retry is skipped (still truncates as a last resort).
    """
    plan_fn = plan_fn or (_mock_plan if mock else _llm_plan)
    fix_fn = fix_fn or _llm_fix

    data = plan_fn(brief, dates, cfg)
    days = _days_from_json(data, dates)

    violations = validate_days(days, cfg)
    if violations and not mock:
        fixes = fix_fn(brief, violations, cfg).get("fixes", [])
        index = {(d.date): d for d in days}
        for fx in fixes:
            day = index.get(fx.get("date"))
            plat = fx.get("platform")
            if day and plat in day.platforms and day.platforms[plat]:
                day.platforms[plat][0].text = (fx.get("text") or "").strip()
        violations = validate_days(days, cfg)

    if violations:
        by = {d.date: d for d in days}
        for dstr, plat, reason in violations:
            day = by.get(dstr)
            if day and day.platforms.get(plat):
                post = day.platforms[plat][0]
                post.text = truncate_violation(plat, post.text, cfg)
                notify.log(TOOL, f"WARN truncated {dstr}/{plat} ({reason})")
    return days


# --- copy-ready rendering (today / digest) ---------------------------------
def _blocks_for_day(day: Day) -> list[str]:
    blocks: list[str] = []
    for plat in PLATFORM_ORDER:
        for post in day.platforms.get(plat, []):
            if post.checked:
                continue
            count = len(post.text)
            blocks.append(f"--- {plat} ({count} chars) ---\n{post.text}")
    return blocks


def _find_day(days: list[Day], dstr: str) -> Optional[Day]:
    for d in days:
        if d.date == dstr:
            return d
    return None


# --- subcommands -----------------------------------------------------------
def cmd_plan(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    week_label, dates = parse_week(args.week)
    qdir = queue_dir(cfg)
    qdir.mkdir(parents=True, exist_ok=True)
    target = qdir / f"{week_label}.md"
    if target.exists() and not args.force:
        print(f"refusing to overwrite {target} (use --force)", file=sys.stderr)
        return 1
    days = generate_week(args.brief, dates, cfg, mock=args.mock_llm)
    target.write_text(render_queue(week_label, days))
    total = sum(len(p) for d in days for p in d.platforms.values())
    notify.log(TOOL, f"planned {week_label}: {total} posts -> {target}")
    print(f"Wrote {total} posts to {target}")
    return 0


def _current_week_file(cfg: dict) -> Path:
    week_label, _ = parse_week(None)
    return queue_dir(cfg) / f"{week_label}.md"


def cmd_today(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    path = _current_week_file(cfg)
    if not path.exists():
        print(f"no queue file for this week ({path.name})", file=sys.stderr)
        return 1
    days = parse_queue(path.read_text())
    today = date.today().isoformat()
    day = _find_day(days, today)
    blocks = _blocks_for_day(day) if day else []
    if not blocks:
        print(f"Nothing unposted for {today}.")
        return 0
    print(f"Today's posts — {today}\n")
    print("\n\n".join(blocks))
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    path = _current_week_file(cfg)
    today = date.today().isoformat()
    if not path.exists():
        notify.log(TOOL, f"digest: no queue file for this week ({path.name})")
        return 0
    days = parse_queue(path.read_text())
    day = _find_day(days, today)
    blocks = _blocks_for_day(day) if day else []
    if not blocks:
        notify.log(TOOL, "nothing to post")
        return 0
    body = f"Today's posts — {today}\n\n" + "\n\n".join(blocks)
    ok = notify.draft_email(f"Today's posts — {today}", body)
    notify.log(TOOL, f"digest drafted={ok} ({len(blocks)} posts)")
    return 0


def mark_posted(path: Path, target_date: str, platform: Optional[str] = None) -> int:
    """Flip '- [ ]' -> '- [x]' for a date (all platforms or one), rewriting
    the file and preserving every hand edit outside the changed markers."""
    lines = path.read_text().splitlines()
    out: list[str] = []
    cur_day: Optional[str] = None
    cur_plat: Optional[str] = None
    changed = 0
    unchecked = re.compile(r"^(\s*-\s*\[)( )(\].*)$")
    for raw in lines:
        m_day = _DAY_RE.match(raw)
        if m_day:
            cur_day = m_day.group(1)
            cur_plat = None
        else:
            m_plat = _PLAT_RE.match(raw)
            if m_plat:
                cur_plat = m_plat.group(1).strip()
        m = unchecked.match(raw)
        if (m and cur_day == target_date
                and (platform is None
                     or (cur_plat and cur_plat.lower() == platform.lower()))):
            raw = f"{m.group(1)}x{m.group(3)}"
            changed += 1
        out.append(raw)
    path.write_text("\n".join(out) + "\n")
    return changed


def cmd_mark_posted(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    qdir = queue_dir(cfg)
    # find the week file that contains this date
    target = None
    for f in sorted(qdir.glob("*.md")):
        if any(d.date == args.date for d in parse_queue(f.read_text())):
            target = f
            break
    if target is None:
        target = _current_week_file(cfg)
        if not target.exists():
            print(f"no queue file contains {args.date}", file=sys.stderr)
            return 1
    n = mark_posted(target, args.date, args.platform)
    notify.log(TOOL, f"mark-posted {args.date} {args.platform or 'all'}: {n} flipped")
    print(f"Marked {n} post(s) posted for {args.date}"
          + (f" [{args.platform}]" if args.platform else ""))
    return 0


# --- CLI -------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="postbatcher", description=__doc__)
    p.add_argument("--config", type=Path, default=None,
                   help="path to config.yaml (default: alongside this script)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="generate a week of posts")
    sp.add_argument("brief", help="topic / campaign brief")
    sp.add_argument("--week", help="target ISO week YYYY-WW (default: current)")
    sp.add_argument("--force", action="store_true", help="overwrite existing file")
    sp.add_argument("--mock-llm", action="store_true", help="offline deterministic gen")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("today", help="print today's unposted posts")
    sp.set_defaults(func=cmd_today)

    sp = sub.add_parser("digest", help="email today's unposted posts (8am cron)")
    sp.set_defaults(func=cmd_digest)

    sp = sub.add_parser("mark-posted", help="flip posts to posted")
    sp.add_argument("date", help="YYYY-MM-DD")
    sp.add_argument("platform", nargs="?", default=None,
                    help="optional single platform (X/LinkedIn/TikTok)")
    sp.set_defaults(func=cmd_mark_posted)
    return p


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
