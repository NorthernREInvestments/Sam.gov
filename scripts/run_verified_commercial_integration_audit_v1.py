"""Audit real stored opportunities through authoritative commercial decision.

Does not invent prices, send RFQs, or apply for financing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    from owner_commercial_queue_v1 import scan_owner_commercial_queues

    report = scan_owner_commercial_queues(limit=150, product_only_titles=True, prefer_non_bidnet=True)
    out = ROOT / "_tmp_verified_commercial_integration_audit.json"
    out.write_text(json.dumps(report, indent=2, default=str)[:400000], encoding="utf-8")
    print(
        json.dumps(
            {
                "examined": report.get("examined"),
                "counts": report.get("counts"),
                "verify_blocker_counts": report.get("verify_blocker_counts"),
                "proven_ids": [o.get("opportunity_id") for o in (report.get("proven_opportunities") or [])[:20]],
                "persisted_path": report.get("persisted_path"),
                "wrote": str(out),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
