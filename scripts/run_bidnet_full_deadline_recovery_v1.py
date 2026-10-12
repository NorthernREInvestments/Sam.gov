"""Run full BidNet deadline recovery against live L23 store."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> int:
    from buyer_intelligence.bidnet_full_deadline_recovery_v1 import run_full_bidnet_deadline_recovery

    # Fresh national ingest with auth cookies + WAF backoff. skip_prepass keeps
    # this run focused on the list harvest after the store-race wipe.
    report = run_full_bidnet_deadline_recovery(
        max_national_pages=1200,
        max_state_pages=50,
        resume=False,
        page_pause_s=0.85,
        skip_prepass=True,
        empty_streak_stop=40,
    )
    print(
        json.dumps(
            {
                "ok": report.get("ok"),
                "error": report.get("error"),
                "elapsed_s": report.get("elapsed_s"),
                "network_requests": report.get("network_requests"),
                "method_counts": report.get("method_counts"),
                "before": report.get("before"),
                "after": report.get("after"),
                "visibility_counts": report.get("visibility_counts"),
                "list_stats": report.get("list_harvest_stats"),
                "cookies_loaded": report.get("cookies_loaded"),
            },
            indent=2,
            default=str,
        )
    )
    return 0 if report.get("ok", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
