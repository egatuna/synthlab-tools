#!/usr/bin/env python3
"""Offline test suite for seo-scout.

Spins up a local `python -m http.server` over a tiny HTML fixture site and:
  * crawls it (verifying robots exclusion, link scoping, page analysis)
  * runs a full audit with --mock-llm and checks the rendered report
  * runs audit --vs (comparison table)
  * verifies rank --dry-run does no network/DB work

No LLM, no Lighthouse, no third-party sites are touched.

Run:  /opt/anaconda3/bin/python3 tests/test_offline.py
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL_DIR = HERE.parent
sys.path.insert(0, str(TOOL_DIR))

import seoscout  # noqa: E402

SITE = HERE / "fixtures" / "site"
KEYWORDS = HERE / "fixtures" / "keywords_test.yaml"

_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        _failures.append(msg)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_up(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


class Server:
    def __init__(self) -> None:
        self.port = _free_port()
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> str:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(self.port)],
            cwd=str(SITE), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if not _wait_up(self.port):
            raise RuntimeError("fixture http.server did not start")
        return f"http://127.0.0.1:{self.port}/index.html"

    def __exit__(self, *exc) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


CFG = seoscout.Config(max_pages=25, crawl_delay_s=0.0, timeout_s=10.0, lighthouse=False)


def test_crawl(base: str):
    print("test_crawl")
    reports = seoscout.crawl(base, CFG)
    urls = [r.url for r in reports]
    check(len(reports) == 3, f"crawled 3 same-site pages (got {len(reports)}: {[u.split('/')[-1] for u in urls]})")
    check(not any("private.html" in u for u in urls), "robots.txt Disallow honoured (private.html skipped)")
    check(not any("example.com" in u for u in urls), "external domain not crawled")

    idx = next((r for r in reports if r.url.endswith("index.html")), None)
    check(idx is not None, "index page analyzed")
    if idx:
        check(idx.title == "Acme Plumbing — Fort Wayne's Trusted Plumbers", "title extracted")
        check(idx.meta_description is not None, "meta description extracted")
        check("LocalBusiness" in idx.schema_types, "JSON-LD schema type detected")
        check(idx.images_missing_alt == 1, f"1 image missing alt (got {idx.images_missing_alt})")
        check(idx.canonical is not None, "canonical extracted")
        check(idx.internal_links >= 2, f"internal links counted (got {idx.internal_links})")
        check(idx.external_links == 1, f"1 external link (got {idx.external_links})")
        check(any("555-0142" in p for p in idx.phones), "phone number captured for NAP")

    contact = next((r for r in reports if r.url.endswith("contact.html")), None)
    if contact:
        check(len(contact.h1s) == 2, f"contact page flagged 2 H1s (got {len(contact.h1s)})")
        check(any("Maple Grove" in a for a in contact.addresses), "street address captured")

    about = next((r for r in reports if r.url.endswith("about.html")), None)
    if about:
        check(about.meta_description is None, "about page correctly missing meta description")


def test_audit_report(base: str):
    print("test_audit_report")
    out = seoscout.audit(base, CFG, mock=True)
    check(out.exists(), f"report file written ({out.name})")
    text = out.read_text()
    check("Overall score:" in text, "report has overall score")
    check("### Scorecard" in text, "report has scorecard table")
    check("### Priority fixes" in text, "report has prioritized fixes")
    check("Appendix — per-page findings" in text, "report has per-page appendix")
    check("Lighthouse" in text and "unavailable" in text, "Lighthouse-unavailable note present (graceful)")
    check("index.html" in text, "appendix lists crawled pages")


def test_audit_vs(base: str):
    print("test_audit_vs")
    out = seoscout.audit(base, CFG, competitor=base, mock=True)
    text = out.read_text()
    check("Head-to-head" in text, "comparison section rendered with --vs")
    check("| Area | You | Competitor |" in text, "comparison table header present")


def test_rank_dry_run():
    print("test_rank_dry_run")
    rc = seoscout.rank(CFG, dry_run=True, keywords_path=KEYWORDS)
    check(rc == 0, "rank --dry-run returned 0")


def test_helpers():
    print("test_helpers")
    check(seoscout.registered_domain("www.synthlabco.com") == "synthlabco.com", "registered_domain strips www")
    check(seoscout.registered_domain("blog.example.co") == "example.co", "registered_domain takes last 2 labels")
    check(seoscout._position_of("synthlabco.com", ["a.com", "synthlabco.com", "b.com"]) == 2,
          "_position_of finds target rank")
    check(seoscout._position_of("synthlabco.com", ["a.com", "b.com"]) is None, "_position_of returns None when absent")


def main() -> int:
    test_helpers()
    test_rank_dry_run()
    with Server() as base:
        test_crawl(base)
        test_audit_report(base)
        test_audit_vs(base)
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
