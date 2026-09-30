"""Run Phase L.2.5 source expansion (revisit 23 then broaden)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market-max", type=int, default=200)
    parser.add_argument("--usaspending-max", type=int, default=80)
    parser.add_argument("--procurement-max", type=int, default=160)
    parser.add_argument("--procurement-fetch-max", type=int, default=100)
    parser.add_argument("--expand-limit", type=int, default=100)
    parser.add_argument("--no-live", action="store_true")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode
    from phase_l.l25_rescue import run_phase_l25_source_expansion

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    path = ROOT / "artifacts" / "phase_l" / "accessible_latest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = [r for r in (data.get("rows") or []) if str(r.get("our_bid_access") or "") == "YES"]
    print(f"[l25] input access_yes={len(rows)}", flush=True)

    result = run_phase_l25_source_expansion(
        rows,
        authorize_live=not args.no_live,
        usaspending_max=args.usaspending_max,
        market_max=args.market_max,
        procurement_max=args.procurement_max,
        procurement_fetch_max=args.procurement_fetch_max,
        expand_limit=args.expand_limit,
    )
    summary = {
        "verdict": result.get("verdict"),
        "original_23_counts": result.get("original_23_counts"),
        "counts": result.get("counts"),
        "funnel_math": result.get("funnel_math"),
        "queue_counts": result.get("queue_counts"),
        "profitable_bid_accessible_rate": result.get("profitable_bid_accessible_rate"),
        "source_telemetry_nonzero": {
            k: v for k, v in (result.get("source_telemetry") or {}).items() if any(v.values())
        },
        "recurring_buy": {
            "products_tracked": (result.get("recurring_buy") or {}).get("products_tracked"),
            "repeat_buy_candidates": len((result.get("recurring_buy") or {}).get("repeat_buy_candidates") or []),
        },
        "buyer_watchlist_n": len(result.get("buyer_watchlist") or []),
        "preview": [
            {
                "title": (r.get("title") or "")[:50],
                "primary": r.get("primary_23"),
                "hist": r.get("history_unit"),
                "price": r.get("current_unit"),
                "new_h": r.get("new_history_hit"),
                "new_c": r.get("new_current_hit"),
                "state": r.get("convergence_state"),
                "net": r.get("expected_net"),
            }
            for r in (result.get("candidate_table") or [])[:12]
        ],
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
