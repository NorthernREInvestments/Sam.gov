#!/bin/sh
set -e

export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/app/.playwright-browsers}"

# Browsers from the build phase may not survive the Railway runtime image — install if missing.
if ! python -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
    b.close()
" 2>/dev/null; then
  echo "Playwright Chromium missing — installing to ${PLAYWRIGHT_BROWSERS_PATH}..."
  python -m playwright install chromium
fi

exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8080}"
