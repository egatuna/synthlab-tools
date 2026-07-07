#!/usr/bin/env python3
"""contact-hunter — a Hunter/Apollo-lite email finder for local-business leads.

Given a markdown lead file (tables and/or per-business sections), for every
business it:
  1. scrapes the homepage + likely contact pages (and Facebook page if present)
     for email addresses, de-obfuscating `name (at) domain (dot) com` forms;
  2. falls back to pattern candidates (info@, contact@, <owner-first>@ ...) when
     nothing is scraped;
  3. verifies the email's domain has an MX record (dnspython) — an email whose
     domain fails MX is NEVER emitted;
  4. writes a COPY of the input file with `Email` + `Confidence` appended.

Confidence:
  confirmed  — scraped from the site  AND its domain has MX
  plausible  — pattern-generated       AND the business domain has MX
  none       — no MX (no email emitted)

The network layer (`fetch_url`, `resolve_mx`) is intentionally module-level so
tests can monkeypatch it and run fully offline.

Usage:
  python hunter.py run leads.md [--out enriched.md] [--config config.yaml]
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
import yaml

# --- shared infra bootstrap -------------------------------------------------
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from shared import db, notify  # noqa: E402

TOOL = "contact-hunter"
HERE = Path(__file__).resolve().parent


# --- config -----------------------------------------------------------------
@dataclass
class Config:
    request_delay_s: float = 2.0
    timeout_s: float = 10.0
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 SynthLabContactHunter/1.0"
    )
    contact_paths: list[str] = field(
        default_factory=lambda: ["/contact", "/contact-us", "/about"]
    )
    email_patterns: list[str] = field(
        default_factory=lambda: ["info", "contact", "hello"]
    )
    junk_domains: list[str] = field(
        default_factory=lambda: ["example.com", "wixpress.com", "sentry.io"]
    )


def load_config(path: Path | None = None) -> Config:
    cfg_path = path or (HERE / "config.yaml")
    if not cfg_path.exists():
        return Config()
    data = yaml.safe_load(cfg_path.read_text()) or {}
    ua = data.get("user_agent")
    if isinstance(ua, str):
        ua = " ".join(ua.split())
    return Config(
        request_delay_s=float(data.get("request_delay_s", 2.0)),
        timeout_s=float(data.get("timeout_s", 10.0)),
        user_agent=ua or Config().user_agent,
        contact_paths=list(data.get("contact_paths") or Config().contact_paths),
        email_patterns=list(data.get("email_patterns") or Config().email_patterns),
        junk_domains=[d.lower() for d in (data.get("junk_domains") or Config().junk_domains)],
    )


# --- network layer (monkeypatchable) ---------------------------------------
_LAST_REQUEST_TS = 0.0


def _respect_rate_limit(delay: float) -> None:
    """Global politeness gate: ensure >= `delay` seconds between all requests."""
    global _LAST_REQUEST_TS
    elapsed = time.monotonic() - _LAST_REQUEST_TS
    if elapsed < delay:
        time.sleep(delay - elapsed)
    _LAST_REQUEST_TS = time.monotonic()


def fetch_url(url: str, *, timeout: float, user_agent: str, delay: float) -> str | None:
    """Fetch a URL politely. Returns text or None on any failure (never raises)."""
    _respect_rate_limit(delay)
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "text/html,*/*"},
            allow_redirects=True,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    ctype = resp.headers.get("Content-Type", "")
    if ctype and "html" not in ctype and "text" not in ctype:
        return None
    return resp.text


def resolve_mx(domain: str) -> bool:
    """True if `domain` has at least one MX record. Never raises."""
    if not domain:
        return False
    try:
        import dns.resolver

        answers = dns.resolver.resolve(domain, "MX", lifetime=5.0)
        return len(answers) > 0
    except Exception:
        return False


# --- email extraction -------------------------------------------------------
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
MAILTO_RE = re.compile(r"mailto:([^\"'?>\s]+)", re.IGNORECASE)
# Spaced/bracketed obfuscation: name (at) domain (dot) com  /  name[at]domain[dot]com
OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*[\(\[\{]?\s*at\s*[\)\]\}]?\s*"
    r"([A-Za-z0-9.\-]+)\s*[\(\[\{]?\s*dot\s*[\)\]\}]?\s*([A-Za-z]{2,})",
    re.IGNORECASE,
)
_IMG_EXT_RE = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico|css|js)$", re.IGNORECASE)


def _deobfuscate(text: str) -> str:
    """Turn bracketed [at]/[dot] tokens into @ and . so the plain regex catches them."""
    t = re.sub(r"\s*[\(\[\{]\s*at\s*[\)\]\}]\s*", "@", text, flags=re.IGNORECASE)
    t = re.sub(r"\s*[\(\[\{]\s*dot\s*[\)\]\}]\s*", ".", t, flags=re.IGNORECASE)
    return t


def extract_emails(html: str, junk_domains: Iterable[str]) -> list[str]:
    """Extract, de-obfuscate, normalise and junk-filter emails from HTML/text."""
    if not html:
        return []
    found: list[str] = []
    found.extend(MAILTO_RE.findall(html))
    found.extend(EMAIL_RE.findall(html))
    found.extend(EMAIL_RE.findall(_deobfuscate(html)))
    for local, dom, tld in OBFUSCATED_RE.findall(html):
        found.append(f"{local}@{dom}.{tld}")

    junk = {d.lower() for d in junk_domains}
    seen: set[str] = set()
    out: list[str] = []
    for raw in found:
        email = raw.strip().strip(".").lower()
        # strip any leftover query/url cruft
        email = email.split("?")[0].split("#")[0]
        if not EMAIL_RE.fullmatch(email):
            continue
        local, _, domain = email.partition("@")
        if _IMG_EXT_RE.search(email) or "@2x" in email or "@3x" in email:
            continue
        if any(j in domain or j in local for j in junk):
            continue
        if email in seen:
            continue
        seen.add(email)
        out.append(email)
    return out


# --- lead parsing -----------------------------------------------------------
@dataclass
class Business:
    name: str
    website: str | None = None
    facebook: str | None = None
    owner: str | None = None
    # enrichment results
    email: str | None = None
    confidence: str = "none"
    notes: str = ""


_URL_RE = re.compile(r"https?://[^\s)\]<>\"']+")
# Bare domains ("stripe.com", "www.acme.net/path") — common in hand-written lead
# lists. Negative lookbehind keeps email domains (john@acme.com) out. TLD list is
# deliberately conservative so filenames (leads.md, hunter.py) never match.
_BARE_DOMAIN_RE = re.compile(
    r"(?<![@\w.-])((?:www\.)?[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
    r"\.(?:com|net|org|io|co|us|biz|info)(?:/[^\s|)\]<>\"']*)?)",
    re.I,
)
_OWNER_RE = re.compile(r"owner[^A-Za-z0-9]{0,3}\s*([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)", re.I)


def _is_facebook(url: str) -> bool:
    return "facebook.com" in url.lower() or "fb.com" in url.lower()


def _classify_urls(text: str) -> tuple[str | None, str | None]:
    """Return (website, facebook) — first non-FB http URL, first FB URL.
    Falls back to bare domains when the text has no http(s) URL at all."""
    website = facebook = None
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;")
        if _is_facebook(url):
            facebook = facebook or url
        else:
            website = website or url
    if website is None and facebook is None:
        for m in _BARE_DOMAIN_RE.finditer(text):
            url = m.group(1).rstrip(".,;")
            if _is_facebook(url):
                facebook = facebook or url
            else:
                website = website or url
    return website, facebook


def _extract_owner(text: str) -> str | None:
    m = _OWNER_RE.search(text)
    return m.group(1).strip() if m else None


def _split_table_row(line: str) -> list[str]:
    parts = line.strip().split("|")
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [p.strip() for p in parts]


def _is_separator_row(line: str) -> bool:
    cells = _split_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c != "")


def detect_format(text: str) -> str:
    """'table' if a genuine markdown table (header + dash separator) exists, else 'blocks'."""
    lines = text.splitlines()
    for i in range(len(lines) - 1):
        if "|" in lines[i] and _is_separator_row(lines[i + 1]) and "|" in lines[i + 1]:
            return "table"
    return "blocks"


def _pick_column(header_cells: list[str], *keywords: str) -> int | None:
    for idx, cell in enumerate(header_cells):
        low = cell.lower()
        if any(k in low for k in keywords):
            return idx
    return None


def _parse_table(text: str) -> list[Business]:
    lines = text.splitlines()
    businesses: list[Business] = []
    i = 0
    while i < len(lines):
        if "|" in lines[i] and i + 1 < len(lines) and _is_separator_row(lines[i + 1]):
            header = _split_table_row(lines[i])
            name_col = _pick_column(header, "business", "name", "company") or 0
            site_col = _pick_column(header, "website", "url", "site", "web")
            fb_col = _pick_column(header, "facebook", "fb", "social")
            owner_col = _pick_column(header, "owner", "contact", "principal")
            j = i + 2
            while j < len(lines) and "|" in lines[j] and not _is_separator_row(lines[j]):
                cells = _split_table_row(lines[j])
                if any(c for c in cells):
                    rowtext = " ".join(cells)
                    name = cells[name_col] if name_col < len(cells) else cells[0] if cells else "Unknown"
                    website = fb = None
                    if site_col is not None and site_col < len(cells):
                        website, fb_in = _classify_urls(cells[site_col])
                        fb = fb_in
                    if fb_col is not None and fb_col < len(cells):
                        _, fb2 = _classify_urls(cells[fb_col])
                        w2, _ = _classify_urls(cells[fb_col])
                        fb = fb or fb2
                        website = website or w2
                    if website is None and fb is None:
                        website, fb = _classify_urls(rowtext)
                    owner = None
                    if owner_col is not None and owner_col < len(cells):
                        cand = cells[owner_col].strip()
                        owner = cand or None
                    owner = owner or _extract_owner(rowtext)
                    businesses.append(
                        Business(name=name.strip("*[] ") or "Unknown",
                                 website=website, facebook=fb, owner=owner)
                    )
                j += 1
            i = j
        else:
            i += 1
    return businesses


_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*\S)\s*$")


def _parse_blocks(text: str) -> list[Business]:
    lines = text.splitlines()
    businesses: list[Business] = []
    # locate heading indices
    headings = [(idx, m.group(1)) for idx, ln in enumerate(lines)
                if (m := _HEADING_RE.match(ln))]
    if not headings:
        return businesses
    bounds = [h[0] for h in headings] + [len(lines)]
    for k, (start, title) in enumerate(headings):
        block = "\n".join(lines[start:bounds[k + 1]])
        website, fb = _classify_urls(block)
        if website is None and fb is None:
            continue  # not a business entry
        businesses.append(
            Business(
                name=title.strip("*[] ") or "Unknown",
                website=website,
                facebook=fb,
                owner=_extract_owner(block),
            )
        )
    return businesses


_BULLET_RE = re.compile(r"^\s{0,3}[-*+]\s+(.*\S)\s*$")
_PERSON_RE = re.compile(r"[A-Z][\w.'-]+(?:\s+[A-Z][\w.'-]+){1,2}")


def _bullet_lead_cells(line: str) -> list[str] | None:
    """Cells of a pipe-delimited bullet lead line ('- Name | acme.com | Jo Doe'),
    or None if the line isn't one. Requires a pipe AND a URL/domain so that
    prose bullets inside block-format files never match."""
    m = _BULLET_RE.match(line)
    if not m or "|" not in m.group(1):
        return None
    if _classify_urls(m.group(1)) == (None, None):
        return None
    return [c.strip() for c in m.group(1).split("|")]


def _parse_bullets(text: str) -> list[Business]:
    businesses: list[Business] = []
    for ln in text.splitlines():
        cells = _bullet_lead_cells(ln)
        if cells is None:
            continue
        content = " ".join(cells)
        website, fb = _classify_urls(content)
        name = next((c.strip("*[] ") for c in cells
                     if c and _classify_urls(c) == (None, None) and not _PERSON_RE.fullmatch(c)),
                    None)
        owner = next((c for c in cells[1:]
                      if _classify_urls(c) == (None, None) and _PERSON_RE.fullmatch(c.strip())),
                     None)
        if name is None:
            name = (owner or _domain_of(website or fb) or "Unknown")
        businesses.append(Business(name=name or "Unknown", website=website,
                                   facebook=fb, owner=owner or _extract_owner(content)))
    return businesses


def parse_leads(text: str) -> tuple[list[Business], str]:
    fmt = detect_format(text)
    if fmt == "table":
        businesses = _parse_table(text)
        if businesses:
            return businesses, fmt
    blocks = _parse_blocks(text)
    bullets = _parse_bullets(text)
    if len(bullets) > len(blocks):
        return bullets, "bullets"
    return blocks, "blocks"


# --- enrichment -------------------------------------------------------------
def _domain_of(url: str | None) -> str | None:
    if not url:
        return None
    netloc = urlparse(url if "://" in url else "http://" + url).netloc
    # removeprefix, not lstrip: lstrip("www.") strips the CHAR SET {w,.} and
    # corrupts domains like wine.com -> ine.com, wixsite.com -> ixsite.com.
    return netloc.split(":")[0].lower().removeprefix("www.") or None


def _domain_of_email(email: str) -> str:
    return email.partition("@")[2].lower()


def enrich_business(biz: Business, cfg: Config) -> Business:
    """Scrape → pattern-fallback → MX-verify. Mutates & returns the business."""
    domain = _domain_of(biz.website)

    # 1. scrape candidate pages
    scraped: list[str] = []
    urls_to_fetch: list[str] = []
    if biz.website:
        base = biz.website if "://" in biz.website else "http://" + biz.website
        urls_to_fetch.append(base)
        for path in cfg.contact_paths:
            urls_to_fetch.append(urljoin(base, path))
    if biz.facebook:
        urls_to_fetch.append(biz.facebook)

    for url in urls_to_fetch:
        html = fetch_url(url, timeout=cfg.timeout_s, user_agent=cfg.user_agent,
                         delay=cfg.request_delay_s)
        if html:
            scraped.extend(extract_emails(html, cfg.junk_domains))

    scraped = list(dict.fromkeys(scraped))  # de-dupe, keep order

    if scraped:
        # prefer an address on the business's own domain
        preferred = next((e for e in scraped if domain and _domain_of_email(e) == domain), scraped[0])
        if resolve_mx(_domain_of_email(preferred)):
            biz.email = preferred
            biz.confidence = "confirmed"
            alts = [e for e in scraped if e != preferred]
            biz.notes = ("also found: " + ", ".join(alts[:4])) if alts else "scraped from site"
            return biz
        # preferred domain has no MX — try any other scraped address with MX
        for e in scraped:
            if e != preferred and resolve_mx(_domain_of_email(e)):
                biz.email = e
                biz.confidence = "confirmed"
                biz.notes = "scraped (primary address failed MX)"
                return biz
        # scraped but nothing passed MX → suppress, fall through to patterns

    # 2. pattern candidates (need a real domain that has MX)
    if domain and resolve_mx(domain):
        prefixes = list(cfg.email_patterns)
        owner_first = biz.owner.split()[0].lower() if biz.owner else None
        if owner_first and owner_first.isalpha():
            prefixes = [owner_first] + prefixes
        candidates = [f"{p}@{domain}" for p in dict.fromkeys(prefixes)]
        biz.email = candidates[0]
        biz.confidence = "plausible"
        alts = candidates[1:]
        biz.notes = ("alternates: " + ", ".join(alts[:4])) if alts else "pattern guess"
        return biz

    # 3. no MX anywhere → no email emitted (hard rule)
    biz.email = None
    biz.confidence = "none"
    biz.notes = biz.notes or ("no MX for domain" if domain else "no website domain")
    return biz


# --- output rendering -------------------------------------------------------
def _rewrite_table(text: str, results: list[Business]) -> str:
    lines = text.splitlines()
    out: list[str] = []
    queue = list(results)
    i = 0
    while i < len(lines):
        if "|" in lines[i] and i + 1 < len(lines) and _is_separator_row(lines[i + 1]):
            header = _split_table_row(lines[i]) + ["Email", "Confidence"]
            out.append("| " + " | ".join(header) + " |")
            sep = _split_table_row(lines[i + 1]) + ["---", "---"]
            out.append("| " + " | ".join(sep) + " |")
            j = i + 2
            while j < len(lines) and "|" in lines[j] and not _is_separator_row(lines[j]):
                cells = _split_table_row(lines[j])
                if any(c for c in cells) and queue:
                    biz = queue.pop(0)
                    email = biz.email or "—"
                    cells = cells + [email, biz.confidence]
                out.append("| " + " | ".join(cells) + " |")
                j += 1
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _rewrite_blocks(text: str, results: list[Business]) -> str:
    lines = text.splitlines()
    headings = [(idx, m.group(1)) for idx, ln in enumerate(lines)
                if (m := _HEADING_RE.match(ln))]
    bounds = [h[0] for h in headings] + [len(lines)]
    # map heading start -> enriched business (only headings that were businesses)
    queue = list(results)
    enriched_at: dict[int, Business] = {}
    for k, (start, title) in enumerate(headings):
        block = "\n".join(lines[start:bounds[k + 1]])
        website, fb = _classify_urls(block)
        if website is None and fb is None:
            continue
        if queue:
            enriched_at[bounds[k + 1]] = queue.pop(0)

    out: list[str] = []
    for idx, ln in enumerate(lines):
        if idx in enriched_at:
            biz = enriched_at[idx]
            email = biz.email or "—"
            note = f"  _{biz.notes}_" if biz.notes else ""
            # ensure a blank line before the appended fact
            if out and out[-1].strip() != "":
                out.append("")
            out.append(f"**Email:** {email} | **Confidence:** {biz.confidence}{note}")
            out.append("")
        out.append(ln)
    # trailing block (last heading extends to EOF)
    if len(lines) in enriched_at:
        biz = enriched_at[len(lines)]
        email = biz.email or "—"
        note = f"  _{biz.notes}_" if biz.notes else ""
        if out and out[-1].strip() != "":
            out.append("")
        out.append(f"**Email:** {email} | **Confidence:** {biz.confidence}{note}")
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _rewrite_bullets(text: str, results: list[Business]) -> str:
    queue = list(results)
    out: list[str] = []
    for ln in text.splitlines():
        if _bullet_lead_cells(ln) is not None and queue:
            biz = queue.pop(0)
            email = biz.email or "—"
            note = f"  _{biz.notes}_" if biz.notes else ""
            out.append(f"{ln.rstrip()} | **Email:** {email} | **Confidence:** {biz.confidence}{note}")
        else:
            out.append(ln)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def render_output(text: str, results: list[Business], fmt: str) -> str:
    if fmt == "table":
        return _rewrite_table(text, results)
    if fmt == "bullets":
        return _rewrite_bullets(text, results)
    return _rewrite_blocks(text, results)


# --- CLI --------------------------------------------------------------------
def run(input_path: Path, out_path: Path | None, cfg: Config) -> int:
    text = input_path.read_text()
    businesses, fmt = parse_leads(text)
    if not businesses:
        notify.fail(TOOL, "no businesses parsed", f"input={input_path}")
        print(f"error: parsed 0 businesses from {input_path}", file=sys.stderr)
        return 1

    notify.log(TOOL, f"parsed {len(businesses)} businesses ({fmt} format) from {input_path.name}")
    for idx, biz in enumerate(businesses, 1):
        try:
            enrich_business(biz, cfg)
        except Exception as exc:  # never let one bad site kill the run
            biz.email, biz.confidence = None, "none"
            biz.notes = f"error: {exc}"
            notify.log(TOOL, f"[{idx}/{len(businesses)}] {biz.name}: ERROR {exc}")
            continue
        notify.log(TOOL, f"[{idx}/{len(businesses)}] {biz.name}: {biz.confidence} "
                          f"{biz.email or '(none)'}")

    out_text = render_output(text, businesses, fmt)
    out = out_path or input_path.with_name(input_path.stem + "-enriched.md")
    out.write_text(out_text)

    total = len(businesses)
    confirmed = sum(1 for b in businesses if b.confidence == "confirmed")
    plausible = sum(1 for b in businesses if b.confidence == "plausible")
    hit_rate = (confirmed + plausible) / total * 100 if total else 0.0
    summary = (f"{total} leads | {confirmed} confirmed "
               f"({confirmed / total * 100:.0f}%) | {plausible} plausible "
               f"({plausible / total * 100:.0f}%) | {hit_rate:.0f}% actionable")
    print(summary)
    print(f"wrote {out}")
    db.record_run(TOOL, "ok", summary)
    notify.log(TOOL, f"done — {summary} -> {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    _hold = Path(__file__).with_name(".ON_HOLD")
    if _hold.exists():
        reason = _hold.read_text().strip() or "parked until fixed"
        print(f"contact-hunter is ON HOLD — {reason}", file=sys.stderr)
        print(f"(lift the hold by removing {_hold})", file=sys.stderr)
        return 0
    parser = argparse.ArgumentParser(prog="hunter", description="Find + verify business emails from a lead file.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="enrich a leads.md file with emails")
    run_p.add_argument("leads", type=Path)
    run_p.add_argument("--out", type=Path, default=None)
    run_p.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.cmd == "run":
        if not args.leads.exists():
            print(f"error: no such file: {args.leads}", file=sys.stderr)
            return 1
        cfg = load_config(args.config)
        try:
            return run(args.leads, args.out, cfg)
        except Exception as exc:
            notify.fail(TOOL, "unexpected error", f"{type(exc).__name__}: {exc}")
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
