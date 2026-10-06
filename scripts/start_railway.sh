#!/bin/sh
set -e

export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/app/.playwright-browsers}"
export M3_DATA_ROOT="${M3_DATA_ROOT:-/data}"

# Verify Chromium can launch; repair browser + deps if runtime libs are missing.
if ! python -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'])
    b.close()
" 2>/tmp/playwright_boot.err; then
  echo "Playwright Chromium launch failed — repairing browsers/deps..."
  cat /tmp/playwright_boot.err || true
  python -m playwright install-deps chromium || true
  python -m playwright install chromium
fi

exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8080}"
