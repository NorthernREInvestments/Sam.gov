"""Ensure Playwright Chromium is available before PIEE downloads."""

from __future__ import annotations

import logging
import os
import subprocess
import sys

logger = logging.getLogger("govtracker.playwright")

_BROWSER_CHECKED = False


def ensure_playwright_chromium() -> None:
    """Launch Chromium once; install browsers if the runtime cache is empty."""
    global _BROWSER_CHECKED
    if _BROWSER_CHECKED:
        return

    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/.playwright-browsers")

    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
            )
            browser.close()
        _BROWSER_CHECKED = True
        return
    except Exception as exc:
        logger.warning("Playwright Chromium unavailable (%s) — installing...", exc)

    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=True,
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        browser.close()
    _BROWSER_CHECKED = True
