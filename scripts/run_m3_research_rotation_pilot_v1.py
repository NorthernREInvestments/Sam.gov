"""M3 research novelty + 50-opp rotation pilot (free/cached first, $0 OpenAI default).

Persists selected IDs before research. Does not replace failed candidates mid-run.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    t0 = time.time()
    from buyer_intelligence.commercial_budget_control_v1 import apply_hard_policy_caps, dashboard_payload
    from buyer_intelligence.commercial_execution_queue_v1 import execute_opportunity, build_deals_from_results
    from buyer_intelligence.commercial_intelligence_upgrade_v1 import _save, _utc
    from buyer_intelligence.research_novelty_engine_v1 import (
        novelty_dashboard,
        select_rotation_cohort,
    )
    from buyer_intelligence.supplier_outreach_queue_v1 import sync_from_rfqs

    apply_hard_policy_caps()
    selection = select_rotation_cohort(limit=50, min_fresh=30)
    ids = selection["opportunity_ids"]
    opps = selection["opportunities"]
    print(json.dumps({"selection_counts": selection["counts"], "ids_persisted": len(ids)}, indent=2))

    task_log: list = []
    results = []
    rfq_packets = []
    finance_packets = []
    suppressed = 0
    fresh_run = 0
    prior_run = 0
    new_prices = 0
    reused_prices = 0

    for i, opp in enumerate(opps, 1):
        oid = opp["opportunity_id"]
        if opp.get("previously_researched"):
            prior_run += 1
        else:
            fresh_run += 1
        try:
            r = execute_opportunity(opp, task_log=task_log)
        except Exception as exc:
            r = {"opportunity_id": oid, "status": "ERROR", "error": type(exc).__name__}
        results.append(r)
        if (r.get("novelty_gate") or {}).get("allowed") is False:
            suppressed += 1
        prices = (r.get("prices") or {}).get("prices") or []
        if prices:
            # Treat first-seen in this pilot as new; historical reuse noted separately
            if r.get("previously_researched"):
                reused_prices += len(prices)
            else:
                new_prices += len(prices)
        pkt = (r.get("rfq") or {}).get("packet")
        if pkt:
            rfq_packets.append(pkt)
        fin = r.get("financing") or {}
        if fin.get("packet"):
            finance_packets.append(fin["packet"])
        print(
            f"[{i}/{len(opps)}] {oid} fresh={not opp.get('previously_researched')} "
            f"prices={len(prices)} novelty={(r.get('novelty_gate') or {}).get('reason')}"
        )

    deals = build_deals_from_results(results)
    complete_rfqs = [
        p
        for p in rfq_packets
        if p.get("PRIMARY_SUPPLIER") not in {None, "UNKNOWN", "Category distributor TBD"}
        and any((x.get("mpn") for x in (p.get("PRODUCTS") or [])))
    ]
    rfq_out = {
        "kind": "cex_rfqs_rotation",
        "build": "20261012-m3-rotation-pilot",
        "AUTO_SEND": False,
        "COUNT": len(complete_rfqs),
        "PACKETS": complete_rfqs[:15] or rfq_packets[:8],
    }
    _save("_og_commercial_research_pipeline_out/m3_cex_v1_rfqs.json", rfq_out)
    outreach = sync_from_rfqs()
    budget = dashboard_payload()
    elapsed = round(time.time() - t0, 2)
    report = {
        "kind": "m3_research_rotation_pilot_report_v1",
        "built_at": _utc(),
        "elapsed_s": elapsed,
        "selection": selection["counts"],
        "fresh_executed": fresh_run,
        "previously_researched_executed": prior_run,
        "novelty_suppressed_tasks": suppressed,
        "new_prices_on_fresh": new_prices,
        "prices_on_prior": reused_prices,
        "complete_rfqs": len(complete_rfqs),
        "finance_packets": len(finance_packets),
        "openai_spend_usd": budget.get("openai_spend_today_usd"),
        "sam_calls": budget.get("sam_calls_today"),
        "budget": budget,
        "novelty": novelty_dashboard(),
        "outreach_synced": outreach.get("count") if isinstance(outreach, dict) else len(outreach.get("items") or {}),
        "top_deals": (deals.get("deals") or [])[:12],
        "finance_packets_sample": finance_packets[:5],
        "NOTE": "No OpenAI/SAM spent by default; free/cached research only",
    }
    # outreach sync returns queue dict with items
    if isinstance(outreach, dict) and "items" in outreach:
        report["outreach_synced"] = len(outreach["items"])
    _save("_og_commercial_research_pipeline_out/m3_research_rotation_pilot_report_v1.json", report)
    _save(
        "_og_commercial_research_pipeline_out/m3_research_rotation_pilot_results_v1.json",
        {"build": report["kind"], "results": results, "task_log": task_log},
    )
    _save(
        "_og_commercial_research_pipeline_out/m3_financing_qualification_packets_v1.json",
        {"build": "20261012", "AUTO_SUBMIT": False, "CONFIRMED": 0, "packets": finance_packets},
    )
    print(json.dumps({k: report[k] for k in report if k not in {"top_deals", "finance_packets_sample", "budget", "novelty"}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
