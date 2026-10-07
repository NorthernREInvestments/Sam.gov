#!/bin/sh
# Background BidNet worker process — separate from the web/API uvicorn service.
# Prefer deploying this as a second Railway service with the same volume (/data).
set -e

export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/app/.playwright-browsers}"
export M3_DATA_ROOT="${M3_DATA_ROOT:-/data}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"
export NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-1}"
export VECLIB_MAXIMUM_THREADS="${VECLIB_MAXIMUM_THREADS:-1}"
export BIDNET_LOGICAL_WORKERS="${BIDNET_LOGICAL_WORKERS:-5}"
export BIDNET_BROWSER_WORKERS="${BIDNET_BROWSER_WORKERS:-2}"
export BIDNET_MAX_BROWSER_PROCESSES="${BIDNET_MAX_BROWSER_PROCESSES:-2}"
export BIDNET_CHECKPOINT_EVERY="${BIDNET_CHECKPOINT_EVERY:-10}"

echo "worker_start thread_caps OPENBLAS=$OPENBLAS_NUM_THREADS browser=$BIDNET_BROWSER_WORKERS logical=$BIDNET_LOGICAL_WORKERS"

MODE="${BIDNET_WORKER_MODE:-recovery}"
if [ "$MODE" = "recovery" ]; then
  exec python -c "
from bidnet_engine.thread_limits import apply_thread_limits, verify_thread_limits
apply_thread_limits(n=1)
print('thread_limits', verify_thread_limits(), flush=True)
from bidnet_engine.recovery import run_bidnet_engine_recovery, format_recovery_report
report = run_bidnet_engine_recovery(stability_sample=6, resume_batch=40)
print(format_recovery_report(report), flush=True)
raise SystemExit(0 if report.get('BIDNET_ENGINE_RECOVERY_PASS') == 'YES' else 1)
"
fi

exec python -c "
from bidnet_engine.thread_limits import apply_thread_limits
apply_thread_limits(n=1)
from bidnet_engine.run import run_bidnet_engine
run_bidnet_engine()
"
