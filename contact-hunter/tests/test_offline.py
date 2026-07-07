#!/usr/bin/env python3
"""Offline test suite for contact-hunter — no real network / DNS / LLM.

Stubs hunter.fetch_url and hunter.resolve_mx, then exercises:
  * table + section parsing
  * email de-obfuscation
  * MX-failure suppression (never emit an email whose domain fails MX)
  * output rendering (columns for tables, lines for sections)

Run:  /opt/anaconda3/bin/python3 tests/test_offline.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL_DIR = HERE.parent
sys.path.insert(0, str(TOOL_DIR))

import hunter  # noqa: E402

FIX = HERE / "fixtures"

# --- fake network -----------------------------------------------------------
FAKE_PAGES = {
    "https://summitplumbingfw.com": (
        '<html><body>Call us. <a href="mailto:owner@summitplumbingfw.com">email</a>'
        " Junk: logo@2x.png and noreply@sentry.io</body></html>"
    ),
    "https://riverbendroofing.test": (
        "<html><body>Reach Mike at mike (at) riverbendroofing (dot) test today."
        "</body></html>"
    ),
    "https://downtowndental.test": (
        "<html><body>Book online. No email published here.</body></html>"
    ),
}

MX_TRUE = {
    "summitplumbingfw.com",
    "riverbendroofing.test",
    "maplecafe.test",
    "downtowndental.test",
}


def fake_fetch(url, *, timeout, user_agent, delay):
    # match ignoring trailing contact-path suffixes
    for base, html in FAKE_PAGES.items():
        if url.rstrip("/") == base.rstrip("/"):
            return html
    return None  # contact sub-pages / facebook / unknown → miss


def fake_mx(domain):
    return domain in MX_TRUE


hunter.fetch_url = fake_fetch
hunter.resolve_mx = fake_mx

CFG = hunter.load_config()

_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {msg}")
    if not cond:
        _failures.append(msg)


# --- tests ------------------------------------------------------------------
def test_parse_table():
    print("test_parse_table")
    text = (FIX / "leads_table.md").read_text()
    businesses, fmt = hunter.parse_leads(text)
    check(fmt == "table", f"format detected as table (got {fmt})")
    check(len(businesses) == 3, f"parsed 3 businesses (got {len(businesses)})")
    by_name = {b.name: b for b in businesses}
    check("Summit Plumbing" in by_name, "Summit Plumbing parsed")
    sp = by_name.get("Summit Plumbing")
    if sp:
        check(sp.website == "https://summitplumbingfw.com", "Summit website extracted")
        check(sp.facebook == "https://facebook.com/summitplumbing", "Summit facebook classified separately")
        check(sp.owner == "John Carter", f"Summit owner parsed (got {sp.owner})")


def test_parse_sections():
    print("test_parse_sections")
    text = (FIX / "leads_sections.md").read_text()
    businesses, fmt = hunter.parse_leads(text)
    check(fmt == "blocks", f"format detected as blocks (got {fmt})")
    check(len(businesses) == 2, f"parsed 2 businesses, ghost excluded (got {len(businesses)})")
    names = {b.name for b in businesses}
    check("Ghost Business" not in names, "URL-less Ghost Business excluded")
    rb = next((b for b in businesses if b.name == "Riverbend Roofing"), None)
    check(rb is not None and rb.owner == "Mike Reynolds", "Riverbend owner parsed")


def test_deobfuscation():
    print("test_deobfuscation")
    emails = hunter.extract_emails(
        "contact mike (at) riverbendroofing (dot) test or sue[at]foo[dot]com",
        CFG.junk_domains,
    )
    check("mike@riverbendroofing.test" in emails, "spaced (at)/(dot) de-obfuscated")
    check("sue@foo.com" in emails, "bracketed [at]/[dot] de-obfuscated")


def test_junk_filter():
    print("test_junk_filter")
    emails = hunter.extract_emails(
        'a@2x.png noreply@sentry.io real@business.test info@example.com',
        CFG.junk_domains,
    )
    check("real@business.test" in emails, "real email kept")
    check("noreply@sentry.io" not in emails, "sentry junk removed")
    check("info@example.com" not in emails, "example.com junk removed")
    check(not any("png" in e for e in emails), "image asset removed")


def test_enrichment_and_mx_suppression():
    print("test_enrichment_and_mx_suppression")
    text = (FIX / "leads_table.md").read_text()
    businesses, _ = hunter.parse_leads(text)
    for b in businesses:
        hunter.enrich_business(b, CFG)
    by_name = {b.name: b for b in businesses}

    sp = by_name["Summit Plumbing"]
    check(sp.email == "owner@summitplumbingfw.com" and sp.confidence == "confirmed",
          f"Summit scraped+MX = confirmed (got {sp.email}/{sp.confidence})")

    ace = by_name["Ace Electric"]
    check(ace.confidence == "none" and ace.email is None,
          f"Ace (MX-fail domain) suppressed to none (got {ace.email}/{ace.confidence})")

    maple = by_name["Maple Cafe"]
    check(maple.confidence == "plausible" and maple.email is not None,
          f"Maple (no scrape, MX ok) = plausible (got {maple.email}/{maple.confidence})")
    check(maple.email == "dana@maplecafe.test",
          f"Maple pattern uses owner first name (got {maple.email})")


def test_output_columns():
    print("test_output_columns")
    text = (FIX / "leads_table.md").read_text()
    businesses, fmt = hunter.parse_leads(text)
    for b in businesses:
        hunter.enrich_business(b, CFG)
    out = hunter.render_output(text, businesses, fmt)
    check("| Email | Confidence |" in out, "table gained Email + Confidence header cells")
    check("owner@summitplumbingfw.com" in out, "confirmed email present in table output")
    check("| confirmed |" in out.replace("  ", " "), "confidence value rendered in row")
    check("Notes below the table should be preserved" in out, "non-table prose preserved")


def test_output_blocks():
    print("test_output_blocks")
    text = (FIX / "leads_sections.md").read_text()
    businesses, fmt = hunter.parse_leads(text)
    for b in businesses:
        hunter.enrich_business(b, CFG)
    out = hunter.render_output(text, businesses, fmt)
    check("**Email:**" in out, "block output appended Email line")
    check("**Confidence:**" in out, "block output appended Confidence line")
    check("mike@riverbendroofing.test" in out, "de-obfuscated scraped email in block output")
    check("Ghost Business" in out, "untouched section preserved")


BULLET_LEADS = """# Leads
- Stripe | stripe.com | John Doe
- GitHub | github.com | Jane Smith
- Placeholder | example.com | Test User
"""


def test_parse_bullets():
    # regression: the exact input shape behind the 2026-07-06 "no businesses parsed" failure
    print("test_parse_bullets")
    businesses, fmt = hunter.parse_leads(BULLET_LEADS)
    check(fmt == "bullets", f"format detected as bullets (got {fmt})")
    check(len(businesses) == 3, f"parsed 3 bullet leads (got {len(businesses)})")
    by_name = {b.name: b for b in businesses}
    check("Stripe" in by_name, "bullet name cell parsed")
    st = by_name.get("Stripe")
    if st:
        check(st.website == "stripe.com", f"bare domain extracted (got {st.website})")
        check(st.owner == "John Doe", f"bullet owner parsed (got {st.owner})")
    # prose bullets without pipes must never become leads (protects block-format files)
    prose = "# Riverbend Roofing\n- Website: https://riverbendroofing.test\n- great crew\n"
    businesses2, fmt2 = hunter.parse_leads(prose)
    check(fmt2 == "blocks", f"pipe-less bullets stay block-format (got {fmt2})")
    check(len(businesses2) == 1, f"one block business, prose bullets not split (got {len(businesses2)})")


def test_output_bullets():
    print("test_output_bullets")
    businesses, fmt = hunter.parse_leads(BULLET_LEADS)
    for b in businesses:
        b.confidence = "none"
    out = hunter.render_output(BULLET_LEADS, businesses, fmt)
    check(out.count("**Email:**") == 3, "every bullet line annotated")
    check("- Stripe | stripe.com | John Doe | **Email:**" in out, "annotation appended inline")
    check("# Leads" in out, "heading preserved")


def main() -> int:
    for t in (
        test_parse_table,
        test_parse_sections,
        test_parse_bullets,
        test_deobfuscation,
        test_junk_filter,
        test_enrichment_and_mx_suppression,
        test_output_columns,
        test_output_blocks,
        test_output_bullets,
    ):
        t()
    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
