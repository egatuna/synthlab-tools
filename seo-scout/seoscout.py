#!/usr/bin/env python3
"""seo-scout — an Ahrefs-lite site auditor + rank tracker for SynthLab.

Commands
--------
audit <url> [--vs <competitor>] [--mock-llm]
    Crawl the site (BFS, same registered domain, robots-aware), run Lighthouse
    on the homepage, grade with a Fable-tier LLM, and render a client-ready
    markdown report to reports/<domain>-<date>.md.

rank [--dry-run]
    Read keywords.yaml, check each keyword's SERP position via a headless
    Claude web search, store history in the rank_history table, and email a
    digest of notable moves.

The network + subprocess layers (`fetch_page`, `run_lighthouse`,
`websearch_serp`) are module-level so tests can run offline.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

# --- shared infra bootstrap -------------------------------------------------
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from shared import db, llm, notify  # noqa: E402

TOOL = "seo-scout"
HERE = Path(__file__).resolve().parent
REPORTS_DIR = HERE / "reports"


# --- config -----------------------------------------------------------------
@dataclass
class Config:
    max_pages: int = 25
    crawl_delay_s: float = 1.0
    timeout_s: float = 10.0
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 SynthLabSeoScout/1.0"
    )
    lighthouse: bool = True


def load_config(path: Path | None = None) -> Config:
    cfg_path = path or (HERE / "config.yaml")
    if not cfg_path.exists():
        return Config()
    data = yaml.safe_load(cfg_path.read_text()) or {}
    ua = data.get("user_agent")
    if isinstance(ua, str):
        ua = " ".join(ua.split())
    return Config(
        max_pages=int(data.get("max_pages", 25)),
        crawl_delay_s=float(data.get("crawl_delay_s", 1.0)),
        timeout_s=float(data.get("timeout_s", 10.0)),
        user_agent=ua or Config().user_agent,
        lighthouse=bool(data.get("lighthouse", True)),
    )


# --- url helpers ------------------------------------------------------------
def registered_domain(host: str) -> str:
    host = host.split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    return ".".join(labels[-2:])


def same_site(url_a: str, url_b: str) -> bool:
    return registered_domain(urlparse(url_a).netloc) == registered_domain(urlparse(url_b).netloc)


def _clean_domain_label(url: str) -> str:
    return registered_domain(urlparse(url).netloc) or "site"


# --- network layer (monkeypatchable) ---------------------------------------
def fetch_page(url: str, *, timeout: float, user_agent: str) -> tuple[int, str | None, str]:
    """Fetch a page. Returns (status_code, html_or_None, final_url). Never raises."""
    try:
        resp = requests.get(
            url, timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "text/html,*/*"},
            allow_redirects=True,
        )
    except requests.RequestException:
        return 0, None, url
    html = None
    ctype = resp.headers.get("Content-Type", "")
    if resp.status_code == 200 and ("html" in ctype or "text" in ctype or not ctype):
        # requests defaults to ISO-8859-1 for text/* without a charset — the modern
        # web is overwhelmingly UTF-8, so use that (avoids em-dash/quote mojibake).
        if "charset" not in ctype.lower():
            resp.encoding = "utf-8"
        html = resp.text
    return resp.status_code, html, str(resp.url)


def _build_robots(start_url: str, cfg: Config) -> robotparser.RobotFileParser:
    rp = robotparser.RobotFileParser()
    parsed = urlparse(start_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        status, text, _ = fetch_page(robots_url, timeout=cfg.timeout_s, user_agent=cfg.user_agent)
        if status == 200 and text:
            rp.parse(text.splitlines())
        else:
            rp.parse([])  # nothing to disallow → allow all
    except Exception:
        rp.parse([])
    return rp


# --- page analysis ----------------------------------------------------------
_PHONE_RE = re.compile(r"(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}")
_ADDRESS_RE = re.compile(
    r"\d{1,6}\s+(?:[A-Za-z0-9.'\-]+\s){1,4}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|Suite|Ste)\.?\b",
    re.IGNORECASE,
)


@dataclass
class PageReport:
    url: str
    status: int
    title: str | None = None
    meta_description: str | None = None
    h1s: list[str] = field(default_factory=list)
    word_count: int = 0
    images_missing_alt: int = 0
    canonical: str | None = None
    schema_types: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)
    internal_links: int = 0
    external_links: int = 0

    def issues(self) -> list[str]:
        out: list[str] = []
        if self.status != 200:
            out.append(f"HTTP {self.status}")
            return out
        if not self.title:
            out.append("missing <title>")
        elif len(self.title) > 60:
            out.append(f"title too long ({len(self.title)} chars)")
        if not self.meta_description:
            out.append("missing meta description")
        elif len(self.meta_description) > 160:
            out.append(f"meta description too long ({len(self.meta_description)} chars)")
        if len(self.h1s) == 0:
            out.append("no H1")
        elif len(self.h1s) > 1:
            out.append(f"{len(self.h1s)} H1s (should be 1)")
        if self.word_count < 150:
            out.append(f"thin content ({self.word_count} words)")
        if self.images_missing_alt:
            out.append(f"{self.images_missing_alt} image(s) missing alt text")
        if not self.canonical:
            out.append("no canonical tag")
        return out


def _schema_types(soup: BeautifulSoup) -> list[str]:
    types: list[str] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if isinstance(obj, dict):
                t = obj.get("@type")
                if isinstance(t, list):
                    types.extend(str(x) for x in t)
                elif t:
                    types.append(str(t))
    return sorted(set(types))


def analyze_page(url: str, status: int, html: str | None) -> PageReport:
    report = PageReport(url=url, status=status)
    if not html:
        return report
    soup = BeautifulSoup(html, "html.parser")

    if soup.title and soup.title.string:
        report.title = soup.title.string.strip()
    desc = soup.find("meta", attrs={"name": "description"})
    if desc and desc.get("content"):
        report.meta_description = desc["content"].strip()
    report.h1s = [h.get_text(strip=True) for h in soup.find_all("h1")]

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    report.word_count = len(text.split())

    report.images_missing_alt = sum(
        1 for img in soup.find_all("img") if not (img.get("alt") or "").strip()
    )
    canon = soup.find("link", attrs={"rel": "canonical"})
    if canon and canon.get("href"):
        report.canonical = canon["href"].strip()

    report.schema_types = _schema_types(BeautifulSoup(html, "html.parser"))
    report.phones = sorted(set(m.group(0).strip() for m in _PHONE_RE.finditer(text)))[:5]
    report.addresses = sorted(set(m.group(0).strip() for m in _ADDRESS_RE.finditer(text)))[:5]

    internal = external = 0
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        target = urljoin(url, href)
        if urlparse(target).scheme not in ("http", "https"):
            continue
        if same_site(url, target):
            internal += 1
        else:
            external += 1
    report.internal_links = internal
    report.external_links = external
    return report


def _collect_links(url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        target = urljoin(url, href).split("#")[0]
        if urlparse(target).scheme in ("http", "https"):
            links.append(target)
    return links


# --- crawler ----------------------------------------------------------------
def crawl(start_url: str, cfg: Config, max_pages: int | None = None) -> list[PageReport]:
    """BFS crawl of same-registered-domain pages, robots-aware, polite."""
    budget = max_pages if max_pages is not None else cfg.max_pages
    rp = _build_robots(start_url, cfg)
    seen: set[str] = set()
    queue: deque[str] = deque([start_url])
    reports: list[PageReport] = []
    first = True

    while queue and len(reports) < budget:
        url = queue.popleft()
        norm = url.rstrip("/") or url
        if norm in seen:
            continue
        seen.add(norm)
        if not first:
            time.sleep(cfg.crawl_delay_s)
        first = False
        try:
            allowed = rp.can_fetch(cfg.user_agent, url)
        except Exception:
            allowed = True
        if not allowed:
            notify.log(TOOL, f"robots disallow: {url}")
            continue

        status, html, final_url = fetch_page(url, timeout=cfg.timeout_s, user_agent=cfg.user_agent)
        reports.append(analyze_page(final_url, status, html))
        if html and len(reports) < budget:
            for link in _collect_links(final_url, html):
                ln = link.rstrip("/") or link
                if ln not in seen and same_site(start_url, link):
                    queue.append(link)
    return reports


# --- lighthouse -------------------------------------------------------------
def run_lighthouse(url: str, cfg: Config) -> dict | None:
    """Run Lighthouse on the homepage. Returns {category: score0-100} or None."""
    if not cfg.lighthouse:
        return None
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = tmp.name
    cmd = [
        "npx", "--yes", "lighthouse", url,
        "--output=json", f"--output-path={tmp_path}", "--quiet",
        "--only-categories=performance,seo,accessibility,best-practices",
        '--chrome-flags=--headless=new',
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            notify.log(TOOL, f"lighthouse failed (rc={proc.returncode}): {proc.stderr[:200]}")
            return None
        data = json.loads(Path(tmp_path).read_text())
        cats = data.get("categories", {})
        return {
            key: round((cats[key]["score"] or 0) * 100)
            for key in ("performance", "seo", "accessibility", "best-practices")
            if key in cats and cats[key].get("score") is not None
        }
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError, KeyError) as exc:
        notify.log(TOOL, f"lighthouse unavailable: {type(exc).__name__}: {exc}")
        return None
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass


# --- grading ----------------------------------------------------------------
def _crawl_summary(reports: list[PageReport]) -> dict:
    pages = [asdict(r) for r in reports]
    schema = sorted({t for r in reports for t in r.schema_types})
    phones = sorted({p for r in reports for p in r.phones})
    addresses = sorted({a for r in reports for a in r.addresses})
    return {
        "pages_crawled": len(reports),
        "pages_missing_title": sum(1 for r in reports if r.status == 200 and not r.title),
        "pages_missing_meta": sum(1 for r in reports if r.status == 200 and not r.meta_description),
        "pages_thin_content": sum(1 for r in reports if r.status == 200 and r.word_count < 150),
        "total_images_missing_alt": sum(r.images_missing_alt for r in reports),
        "pages_no_canonical": sum(1 for r in reports if r.status == 200 and not r.canonical),
        "pages_no_h1": sum(1 for r in reports if r.status == 200 and not r.h1s),
        "schema_types_found": schema,
        "distinct_phones": phones,
        "distinct_addresses": addresses,
        "nap_phone_consistent": len(phones) <= 1,
        "pages": pages,
    }


def _mock_grade(has_competitor: bool = False) -> dict:
    grade = {
        "overall_score": 71,
        "subscores": {
            "titles_meta": 66,
            "content": 74,
            "local_seo": 58,
            "technical": 82,
            "mobile_speed": 70,
        },
        "prioritized_fixes": [
            {
                "priority": "P0",
                "issue": "Several pages are missing a meta description",
                "why_it_matters": "This is the little summary line people see on Google. "
                                  "Blank means Google writes its own — often the wrong sentence — "
                                  "so fewer people click through to you.",
                "how_to_fix": "Add a single clear sentence (under 160 characters) describing each "
                              "page in your site editor's SEO/description field.",
            },
            {
                "priority": "P1",
                "issue": "Your business phone number isn't shown the same way on every page",
                "why_it_matters": "Google trusts a business more when its name, address and phone "
                                  "match everywhere. Inconsistency can hold back your local ranking.",
                "how_to_fix": "Pick one format for your phone number and use it identically in the "
                              "footer of every page.",
            },
            {
                "priority": "P2",
                "issue": "Some images have no alt text",
                "why_it_matters": "Alt text helps Google understand your photos and helps visually "
                                  "impaired visitors. It's a small, safe win.",
                "how_to_fix": "Add a short description to each image in your editor's image settings.",
            },
        ],
        "quick_wins": [
            "Write meta descriptions for the 3 pages that are missing one.",
            "Standardise the phone number in the site footer.",
            "Add LocalBusiness schema so Google shows your hours and address.",
        ],
        "summary_for_owner": (
            "Your website has a solid technical foundation and loads reasonably fast. The biggest "
            "opportunities are small copy fixes — a few missing page summaries and a consistent "
            "phone number — plus adding local-business details Google can read. None of these are "
            "expensive; together they should lift how often you show up for local searches."
        ),
    }
    if has_competitor:
        grade["comparison"] = {
            "verdict": "You lead on site speed and technical health; the competitor edges ahead on "
                       "local-SEO signals and depth of content.",
            "rows": [
                {"area": "Overall", "you": 71, "competitor": 68},
                {"area": "Titles & meta", "you": 66, "competitor": 72},
                {"area": "Content", "you": 74, "competitor": 77},
                {"area": "Local SEO", "you": 58, "competitor": 70},
                {"area": "Technical", "you": 82, "competitor": 65},
                {"area": "Mobile speed", "you": 70, "competitor": 61},
            ],
        }
    return grade


def grade_site(summary: dict, lighthouse: dict | None,
               competitor: dict | None = None,
               competitor_lh: dict | None = None,
               mock: bool = False) -> dict:
    if mock:
        return _mock_grade(has_competitor=competitor is not None)

    system = (
        "You are a senior technical-SEO consultant writing for a non-technical local "
        "trade-business owner. Grade fairly and explain plainly."
    )
    payload: dict = {"site_crawl": summary, "lighthouse_scores": lighthouse}
    if competitor is not None:
        payload["competitor_crawl"] = competitor
        payload["competitor_lighthouse"] = competitor_lh
    schema_note = (
        'Return JSON: {"overall_score":0-100,"subscores":{"titles_meta","content",'
        '"local_seo","technical","mobile_speed"},"prioritized_fixes":[{"priority":'
        '"P0|P1|P2","issue","why_it_matters","how_to_fix"}],"quick_wins":[...],'
        '"summary_for_owner":"2-3 plain sentences"'
        + (',"comparison":{"verdict","rows":[{"area","you","competitor"}]}' if competitor else "")
        + "}. why_it_matters/how_to_fix and summary_for_owner must be jargon-free."
    )
    prompt = f"{schema_note}\n\nDATA:\n{json.dumps(payload, default=str)[:20000]}"
    return llm.complete_json(prompt, system=system, judgment=True)


# --- report rendering -------------------------------------------------------
def _subscore_table(subscores: dict) -> str:
    labels = {
        "titles_meta": "Titles & meta", "content": "Content", "local_seo": "Local SEO",
        "technical": "Technical", "mobile_speed": "Mobile speed",
    }
    rows = ["| Area | Score |", "| --- | --- |"]
    for key, label in labels.items():
        if key in subscores:
            rows.append(f"| {label} | {subscores[key]}/100 |")
    return "\n".join(rows)


def render_report(url: str, grade: dict, reports: list[PageReport],
                  lighthouse: dict | None, competitor_url: str | None = None) -> str:
    domain = _clean_domain_label(url)
    today = date.today().isoformat()
    score = grade.get("overall_score", "—")
    lines: list[str] = []
    lines.append(f"# SEO Scout Report — {domain}")
    lines.append(f"*{today}*  ·  prepared by SynthLab")
    lines.append("")
    lines.append(f"## Overall score: {score}/100")
    lines.append("")
    lines.append(grade.get("summary_for_owner", ""))
    lines.append("")

    lines.append("### Scorecard")
    lines.append(_subscore_table(grade.get("subscores", {})))
    lines.append("")

    if lighthouse:
        lines.append("### Google Lighthouse (homepage)")
        lh_rows = ["| Category | Score |", "| --- | --- |"]
        for k, v in lighthouse.items():
            lh_rows.append(f"| {k.replace('-', ' ').title()} | {v}/100 |")
        lines.append("\n".join(lh_rows))
        lines.append("")
    else:
        lines.append("### Google Lighthouse (homepage)")
        lines.append("_Performance data unavailable (Lighthouse/Chrome could not run in this "
                     "environment). All other findings are unaffected._")
        lines.append("")

    comparison = grade.get("comparison")
    if comparison and competitor_url:
        lines.append(f"### Head-to-head vs {_clean_domain_label(competitor_url)}")
        lines.append(comparison.get("verdict", ""))
        lines.append("")
        cmp_rows = ["| Area | You | Competitor |", "| --- | --- | --- |"]
        for r in comparison.get("rows", []):
            cmp_rows.append(f"| {r.get('area','')} | {r.get('you','')} | {r.get('competitor','')} |")
        lines.append("\n".join(cmp_rows))
        lines.append("")

    lines.append("### Priority fixes")
    for fix in grade.get("prioritized_fixes", []):
        lines.append(f"#### {fix.get('priority','')} — {fix.get('issue','')}")
        lines.append(f"**Why it matters:** {fix.get('why_it_matters','')}")
        lines.append("")
        lines.append(f"**How to fix:** {fix.get('how_to_fix','')}")
        lines.append("")

    quick = grade.get("quick_wins", [])
    if quick:
        lines.append("### Quick wins")
        for q in quick:
            lines.append(f"- {q}")
        lines.append("")

    # --- technical appendix ---
    lines.append("---")
    lines.append("## Appendix — per-page findings")
    lines.append("")
    lines.append(f"Crawled {len(reports)} page(s).")
    lines.append("")
    for r in reports:
        issues = r.issues()
        flag = "OK" if not issues else "; ".join(issues)
        lines.append(f"### {r.url}")
        meta = (f"status {r.status} · {r.word_count} words · "
                f"{len(r.h1s)} H1 · {r.images_missing_alt} img-no-alt · "
                f"schema: {', '.join(r.schema_types) or 'none'}")
        lines.append(f"`{meta}`")
        lines.append("")
        lines.append(f"- **Title:** {r.title or '(none)'}")
        lines.append(f"- **Meta:** {r.meta_description or '(none)'}")
        lines.append(f"- **Canonical:** {r.canonical or '(none)'}")
        lines.append(f"- **Links:** {r.internal_links} internal / {r.external_links} external")
        if r.phones:
            lines.append(f"- **Phones:** {', '.join(r.phones)}")
        if r.addresses:
            lines.append(f"- **Addresses:** {', '.join(r.addresses)}")
        lines.append(f"- **Issues:** {flag}")
        lines.append("")
    return "\n".join(lines) + "\n"


# --- audit command ----------------------------------------------------------
def audit(url: str, cfg: Config, competitor: str | None = None, mock: bool = False) -> Path:
    if "://" not in url:
        url = "https://" + url
    notify.log(TOOL, f"audit start: {url}"
                     + (f" vs {competitor}" if competitor else "")
                     + (" [mock-llm]" if mock else ""))

    reports = crawl(url, cfg)
    notify.log(TOOL, f"crawled {len(reports)} pages of {_clean_domain_label(url)}")
    lighthouse = run_lighthouse(url, cfg)
    summary = _crawl_summary(reports)

    comp_summary = comp_lh = None
    if competitor:
        if "://" not in competitor:
            competitor = "https://" + competitor
        comp_reports = crawl(competitor, cfg, max_pages=max(1, cfg.max_pages // 2))
        notify.log(TOOL, f"crawled {len(comp_reports)} pages of competitor")
        comp_summary = _crawl_summary(comp_reports)
        comp_lh = run_lighthouse(competitor, cfg)

    grade = grade_site(summary, lighthouse, comp_summary, comp_lh, mock=mock)
    report_md = render_report(url, grade, reports, lighthouse, competitor)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"{_clean_domain_label(url)}-{date.today().isoformat()}.md"
    out.write_text(report_md)
    db.record_run(TOOL, "ok", f"audit {url} score={grade.get('overall_score')} -> {out.name}")
    notify.log(TOOL, f"report written: {out}")
    return out


# --- rank command -----------------------------------------------------------
_RANK_SCHEMA = """
CREATE TABLE IF NOT EXISTS rank_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL,
    date TEXT NOT NULL,
    position INTEGER,
    serp_top3_json TEXT
);
"""

NOT_IN_TOP = 51  # sentinel for move math when a keyword isn't in the top 10


def _ensure_rank_table() -> None:
    with db.conn() as c:
        c.executescript(_RANK_SCHEMA)


def websearch_serp(keyword: str, cfg_model: str = "sonnet", timeout: int = 300) -> list[str]:
    """Ask headless Claude for the top-10 organic result domains. Returns [] on failure."""
    prompt = (
        f'Use web search to find the current top 10 organic Google results for the query '
        f'"{keyword}". Ignore ads. Reply with ONLY a JSON array of the result domains in rank '
        f'order, lowercase, no protocol or path, e.g. ["a.com","b.com"]. No prose.'
    )
    cmd = ["claude", "-p", prompt, "--model", cfg_model,
           "--output-format", "text", "--allowedTools", "WebSearch"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0:
        return []
    text = proc.stdout.strip()
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return [str(d).strip().lower().lstrip("www.") for d in arr if isinstance(d, (str,))]


def _position_of(target: str, serp: list[str]) -> int | None:
    target = registered_domain(target)
    for i, dom in enumerate(serp, 1):
        if registered_domain(dom) == target or target in dom:
            return i
    return None


def _prev_position(keyword: str) -> int | None:
    with db.conn() as c:
        row = c.execute(
            "SELECT position FROM rank_history WHERE keyword=? ORDER BY date DESC, id DESC LIMIT 1",
            (keyword,),
        ).fetchone()
    return row[0] if row else None


def rank(cfg: Config, dry_run: bool = False, keywords_path: Path | None = None) -> int:
    kw_path = keywords_path or (HERE / "keywords.yaml")
    if not kw_path.exists():
        notify.fail(TOOL, "keywords.yaml missing", str(kw_path))
        return 1
    data = yaml.safe_load(kw_path.read_text()) or {}
    target = str(data.get("target_domain", "")).strip()
    keywords = [str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()]

    if not target or not keywords:
        notify.fail(TOOL, "keywords.yaml incomplete", "need target_domain + keywords")
        return 1

    if dry_run:
        print(f"[dry-run] target_domain: {target}")
        print(f"[dry-run] {len(keywords)} keyword(s); would run 1 web search each:")
        for kw in keywords:
            print(f"  - {kw!r}  (claude -p ... --allowedTools WebSearch)")
        print("[dry-run] no subprocess launched, no DB writes, no email.")
        return 0

    _ensure_rank_table()
    today = date.today().isoformat()
    moves: list[str] = []
    for kw in keywords:
        serp = websearch_serp(kw)
        pos = _position_of(target, serp) if serp else None
        prev = _prev_position(kw)
        top3 = json.dumps(serp[:3])
        with db.conn() as c:
            c.executescript(_RANK_SCHEMA)
            c.execute(
                "INSERT INTO rank_history (keyword,date,position,serp_top3_json) VALUES (?,?,?,?)",
                (kw, today, pos, top3),
            )
        # move detection
        cur_v = pos if pos is not None else NOT_IN_TOP
        if prev is None:
            if pos is not None:
                moves.append(f"NEW: '{kw}' entered at #{pos}")
        else:
            prev_v = prev if prev is not None else NOT_IN_TOP
            if pos is None and prev is not None:
                moves.append(f"DROPPED OUT: '{kw}' (was #{prev})")
            elif pos is not None and prev is None:
                moves.append(f"NEW: '{kw}' entered at #{pos}")
            elif abs(cur_v - prev_v) >= 3:
                arrow = "up" if cur_v < prev_v else "down"
                a, b = (f"#{prev}" if prev else "unranked"), (f"#{pos}" if pos else "unranked")
                moves.append(f"{arrow.upper()}: '{kw}' {a} -> {b}")
        notify.log(TOOL, f"rank '{kw}': {('#'+str(pos)) if pos else 'not_in_top'}")

    db.record_run(TOOL, "ok", f"rank checked {len(keywords)} keywords, {len(moves)} moves")
    if moves:
        body = (f"Rank movements for {target} ({today}):\n\n" + "\n".join(f"- {m}" for m in moves)
                + "\n\n(Only moves of 3+ positions, new entries and drop-outs are shown.)")
        notify.draft_email(f"[seo-scout] {len(moves)} rank move(s) for {target}", body)
        print(f"{len(moves)} move(s) — digest drafted.")
    else:
        print("no notable rank moves.")
    return 0


# --- CLI --------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="seoscout", description="Ahrefs-lite auditor + rank tracker.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("audit", help="crawl + grade a site")
    a.add_argument("url")
    a.add_argument("--vs", dest="competitor", default=None, help="competitor URL to compare against")
    a.add_argument("--mock-llm", action="store_true", help="use canned grading (offline)")
    a.add_argument("--config", type=Path, default=None)

    r = sub.add_parser("rank", help="check keyword rankings")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--config", type=Path, default=None)

    args = parser.parse_args(argv)
    cfg = load_config(getattr(args, "config", None))

    if args.cmd == "audit":
        try:
            out = audit(args.url, cfg, competitor=args.competitor, mock=args.mock_llm)
            print(f"report: {out}")
            return 0
        except Exception as exc:
            notify.fail(TOOL, "audit failed", f"{type(exc).__name__}: {exc}")
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if args.cmd == "rank":
        try:
            return rank(cfg, dry_run=args.dry_run)
        except Exception as exc:
            notify.fail(TOOL, "rank failed", f"{type(exc).__name__}: {exc}")
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
