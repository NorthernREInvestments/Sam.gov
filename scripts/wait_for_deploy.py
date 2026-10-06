"""Wait until Railway finishes rebuilding (health may briefly drop or stay same version)."""
from __future__ import annotations

import time

import httpx

TARGET = "20261003-m3-product-identity-v1"
BASE = "https://samgov-production.up.railway.app"
# After railway up, allow build+restart time even if version string unchanged
MIN_WAIT_SEC = 90
MAX_POLLS = 60
POLL_SEC = 10


def main() -> int:
    started = time.time()
    ok_streak = 0
    for i in range(MAX_POLLS):
        try:
            d = httpx.get(f"{BASE}/api/health", timeout=25).json()
            build = d.get("build_version")
            print(f"poll {i}: build={build} elapsed={int(time.time()-started)}s", flush=True)
            if build == TARGET and (time.time() - started) >= MIN_WAIT_SEC:
                ok_streak += 1
                if ok_streak >= 2:
                    # Confirm new endpoint exists
                    try:
                        r = httpx.get(f"{BASE}/api/m3/full-funnel-sweep/last-report", timeout=30)
                        print("endpoint", r.status_code, flush=True)
                        if r.status_code < 500:
                            print("deploy_ready", TARGET, flush=True)
                            return 0
                    except Exception as exc:
                        print("endpoint_err", type(exc).__name__, flush=True)
                        ok_streak = 0
            else:
                ok_streak = 0
        except Exception as exc:
            ok_streak = 0
            print(f"poll {i}: error={type(exc).__name__}", flush=True)
        time.sleep(POLL_SEC)
    print("deploy_timeout", TARGET, flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
