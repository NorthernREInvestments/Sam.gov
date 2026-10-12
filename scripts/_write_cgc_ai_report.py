"""Write human-readable A-I commercial gap closure report artifact."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from buyer_intelligence.commercial_budget_control_v1 import dashboard_payload
from buyer_intelligence.commercial_intelligence_upgrade_v1 import _load, _save, _utc


def main() -> int:
    rep = _load("_og_commercial_research_pipeline_out/m3_cgc_v1_report.json") or {}
    rfq = _load("_og_commercial_research_pipeline_out/m3_cgc_v1_rfqs.json") or {}
    fin = _load("_og_commercial_research_pipeline_out/m3_cgc_v1_financing.json") or {}
    learn = _load("_og_commercial_research_pipeline_out/m3_rse_v1_learning_store.json") or {}
    hist = _load("_og_commercial_research_pipeline_out/m3_cgc_v1_historical_report.json") or {}
    fails = _load("_og_commercial_research_pipeline_out/m3_cgc_v1_price_failures.json") or {}
    budget = dashboard_payload()

    out = {
        "kind": "m3_cgc_v1_AI_REPORT",
        "built_at": _utc(),
        "git_local_head": "5ee64d7284d2e32095892aa689245c0df597d559",
        "railway_deployment_id": "f61f81e7-afe0-4526-a48e-0a0226c7a928",
        "cgc_code_deployed": False,
        "NOTE": "CGC engine + validated evidence are LOCAL; production still on prior revision until authorized deploy",
        "A_SCORECARD": rep.get("A_SCORECARD"),
        "B_BEST": rep.get("B_best_opportunities"),
        "C_ACQUISITION": {
            "raw_candidates": 66,
            "validated_exact_new": rep.get("validated_prices"),
            "rejected": 62,
            "failure_classes": fails.get("by_class"),
            "working_sources": [
                "mopec.com product pages",
                "tecisoft.com Ekahau renewals",
                "midwesttargets.com Hornady LE product pages",
            ],
            "rejected_examples": [
                "1092125 circuit breaker -> RevZilla motorcycle pegs (MPN collision)",
                "1092048 circuit breaker -> KnifeCountry Smith & Wesson knives",
                "600017 Mopec carrier -> Velvac fuel cap",
                "DuckDuckGo SERP dollar scraping",
            ],
        },
        "D_HISTORICAL": {
            "newly_scanned_hits": hist.get("newly_acquired_unit_prices"),
            "note": "Most usable historical remains reused HCSO paper-goods tabulation (spec-match, not exact brand). One weak cached MPN hit recorded; not used for verified margin.",
            "usable_classes_seen": ["SPECIFICATION_MATCHED_UNIT_PRICE", "EXACT_PRODUCT_OTHER_BUYER"],
        },
        "E_SUPPLIERS": {
            "rfqs_prepared": rfq.get("COUNT"),
            "AUTO_SEND": False,
            "priority_channels": [
                "Hornady LE distributor",
                "MOPEC OEM",
                "TeciSoft / Ekahau authorized reseller",
            ],
            "actual_quotes_received": 0,
        },
        "F_FINANCING": fin,
        "G_AUTOMATED": {
            "cohort": 50,
            "elapsed_s_research": 903.67,
            "novelty_used": True,
            "openai_usd": budget.get("openai_spend_today_usd"),
            "sam_calls": budget.get("sam_calls_today"),
            "learning": learn,
        },
        "H_PRODUCTION": {
            "railway_online": True,
            "deployed_revision_vs_cgc": "CGC not in production yet",
            "backup_script": "scripts/run_m3_railway_predeploy_backup_v1.py",
            "rollback": "Redeploy prior Railway deployment f61f81e7",
        },
        "I_VERDICT": {
            "1_new_acquisition_prices": "YES — 4 validated exact product-page prices (66 raw candidates; 62 rejected as mismatch/SERP)",
            "2_new_gov_selling_prices": "MINIMAL — 1 weak cached scan hit; no new buyer unit-price schedules usable for verified margin",
            "3_profitable_combinations": "NO — selling prices still unknown; contribution UNKNOWN",
            "4_supplier_pricing_advantage": "NONE VERIFIED — public list references only; dealer/OEM quotes required",
            "5_financing_routes": "UNKNOWN eligibility — SouthStar/TradeCap/King market to gov PO finance; PG/min-size/credit unconfirmed; ~$50k floors may exclude small cash-flow deals",
            "6_first_500_2000": "Best path: Orange County Hornady ammo (partial public prices) IF remaining SKUs + quantities + dealer quotes + buyer historical recovered — currently NOT execution-ready",
            "7_5000_10000_plus": "Collin County Mopec lot (600009-HC1 public $9102.52 x3 = ~$27k acq alone) needs full-lot OEM quote + selling prices — financing likely required; NOT verified profitable",
            "8_before_first_bid": [
                "Recover quantities for Hornady lines (currently missing)",
                "Dealer quotes for remaining Hornady SKUs",
                "Buyer historical/tabulation unit prices",
                "Confirm bid lot rules",
                "Financing or supplier terms decision",
                "Owner approval to send RFQs (AUTO_SEND=false)",
            ],
            "9_m3_next_auto": [
                "Rotate research away from HV circuit-breaker MPN collisions toward RFQ prep",
                "Scan buyer award tabs for Hornady/Mopec/Ekahau unit prices (free/cache)",
                "Keep novelty circuit breakers on blocked login sources",
            ],
            "10_owner_next": [
                "Approve sending Hornady LE distributor RFQ + Mopec OEM RFQ",
                "Decide whether Ekahau software resale is in-scope (license keys present)",
                "Authorize production deploy of CGC after backup if desired",
            ],
        },
    }
    _save("_og_commercial_research_pipeline_out/m3_cgc_v1_AI_REPORT.json", out)
    print(json.dumps({"saved": True, "validated": len(rep.get("validated_prices") or []), "rfqs": rfq.get("COUNT")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
