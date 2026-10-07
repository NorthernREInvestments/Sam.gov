#!/bin/sh
set -e

export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/app/.playwright-browsers}"
export M3_DATA_ROOT="${M3_DATA_ROOT:-/data}"

# Cap BLAS/OpenMP threads before any Python math libs import (prevents pthread explosion).
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"
export NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-1}"
export VECLIB_MAXIMUM_THREADS="${VECLIB_MAXIMUM_THREADS:-1}"
export BIDNET_LOGICAL_WORKERS="${BIDNET_LOGICAL_WORKERS:-5}"
export BIDNET_BROWSER_WORKERS="${BIDNET_BROWSER_WORKERS:-2}"
export BIDNET_MAX_BROWSER_PROCESSES="${BIDNET_MAX_BROWSER_PROCESSES:-2}"
export BIDNET_CHECKPOINT_EVERY="${BIDNET_CHECKPOINT_EVERY:-10}"
echo "thread_caps OPENBLAS=$OPENBLAS_NUM_THREADS OMP=$OMP_NUM_THREADS browser_workers=$BIDNET_BROWSER_WORKERS logical=$BIDNET_LOGICAL_WORKERS"

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
