#!/usr/bin/env python3
"""Prepare a saved browser profile by signing in once, the way a human would.

The entry tool never authenticates by itself.  Run this helper once (or let the
test harness run it) so the persistent profile carries a live session; the tool
then only fills the business form.

    python3 entry-tool/bizform/signin.py --url http://127.0.0.1:8765 \
        --profile /var/tmp/profile --username operator --password operator
"""
from __future__ import annotations

import argparse
import os
import sys

from playwright.sync_api import sync_playwright


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--username", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--entry", default="/entry")
    args = ap.parse_args()
    os.makedirs(args.profile, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            args.profile, headless=True,
            viewport={"width": 1100, "height": 800},
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(args.url.rstrip("/") + "/login", wait_until="domcontentloaded")
        page.fill("#username", args.username)
        page.fill("#password", args.password)
        page.click("#btn-login")
        page.wait_for_load_state("domcontentloaded")
        ok = page.locator("#btn-save").count() > 0
        print("signed in" if ok else "login did not reach the entry form", file=sys.stderr)
        ctx.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
