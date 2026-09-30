"""Run Phase L.2 live enrichment on accessible Phase L opportunities."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_accessible() -> list[dict]:
    path = ROOT / "artifacts" / "phase_l" / "accessible_latest.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("rows") or []
    return [r for r in rows if str(r.get("our_bid_access") or "") == "YES"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase L.2 enrichment")
    parser.add_argument("--refresh-hunt", action="store_true", help="Re-run Phase L hunt first")
    parser.add_argument("--max-sources", type=int, default=40)
    parser.add_argument("--usaspending-max", type=int, default=80)
    parser.add_argument("--market-max", type=int, default=60)
    parser.add_argument("--deep-limit", type=int, default=120)
    parser.add_argument("--no-live", action="store_true")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    rows = _load_accessible()
    if args.refresh_hunt or len(rows) < 400:
        print(f"[phase_l2] accessible pool={len(rows)} — refreshing Phase L hunt...", flush=True)
        from phase_l.hunt import run_phase_l_hunt

        hunt = run_phase_l_hunt(
            authorize_live=not args.no_live,
            max_sources=args.max_sources,
        )
        print(f"[phase_l2] hunt access_yes={hunt.get('counts', {}).get('access_yes')}", flush=True)
        rows = _load_accessible()

    print(f"[phase_l2] enriching access_yes={len(rows)}", flush=True)
    from phase_l.enrichment import run_phase_l2_enrichment

    result = run_phase_l2_enrichment(
        rows,
        authorize_live=not args.no_live,
        usaspending_max=args.usaspending_max,
        market_max=args.market_max,
        deep_limit=args.deep_limit,
    )
    summary = {
        "counts": result.get("counts"),
        "budget_used": result.get("budget_used"),
        "funnel_total": result.get("funnel", {}).get("TOTAL"),
        "open_competition_stats": result.get("open_competition_stats"),
        "failure_reasons": result.get("failure_reasons")[:10],
        "top5": [
            {
                "title": (t.get("title") or "")[:70],
                "source_level": t.get("source_level"),
                "expected_net_profit": t.get("expected_net_profit"),
                "profit_tier": t.get("profit_tier"),
                "historical_award_unit_price": t.get("historical_award_unit_price"),
                "public_retail_price": t.get("public_retail_price"),
            }
            for t in (result.get("top20") or [])[:5]
        ],
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
