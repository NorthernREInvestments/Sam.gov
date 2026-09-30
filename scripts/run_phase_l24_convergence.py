"""Run Phase L.2.4 price/history convergence on L.2.3-eligible candidates."""

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
    parser.add_argument("--market-max", type=int, default=160)
    parser.add_argument("--usaspending-max", type=int, default=80)
    parser.add_argument("--expand", action="store_true")
    parser.add_argument("--no-live", action="store_true")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode
    from phase_l.l24_rescue import run_phase_l24_convergence

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    path = ROOT / "artifacts" / "phase_l" / "accessible_latest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = [r for r in (data.get("rows") or []) if str(r.get("our_bid_access") or "") == "YES"]
    print(f"[l24] input access_yes={len(rows)}", flush=True)

    result = run_phase_l24_convergence(
        rows,
        authorize_live=not args.no_live,
        usaspending_max=args.usaspending_max,
        market_max=args.market_max,
        expand_after_23=args.expand,
    )
    summary = {
        "counts": result.get("counts"),
        "funnel": result.get("funnel"),
        "convergence_states": result.get("convergence_states"),
        "queue_counts": result.get("queue_counts"),
        "profitable_accessible_rate": result.get("profitable_accessible_rate"),
        "budget_used": result.get("budget_used"),
        "codes": (result.get("code_counts") or [])[:12],
        "table_preview": [
            {
                "title": (r.get("title") or "")[:50],
                "hist": r.get("history_unit"),
                "price": r.get("current_unit"),
                "access": r.get("price_access"),
                "spread": r.get("unit_spread"),
                "net": r.get("expected_net"),
                "state": r.get("convergence_state"),
            }
            for r in (result.get("candidate_table") or [])[:8]
        ],
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
