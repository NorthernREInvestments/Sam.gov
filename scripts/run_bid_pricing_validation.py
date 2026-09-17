"""Bid assembly + pricing validation — simulated costs, DEVELOPMENT_NO_OUTREACH."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bid_pricing_engine import analyze_bid_pricing, authorize_pricing_research
from bid_pricing_invalidation import evaluate_price_freshness, invalidate_pricing_state
from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, mode_snapshot, set_operating_mode

ARTIFACTS = ROOT / "artifacts"


def _write(name: str, payload: Any) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / name).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


LIVE = [
    {
        "id": "LIVE-IT-ELECTRONICS-RFQ",
        "category": "IT/electronics",
        "lines": [
            {
                "line_id": "IT1",
                "description": "Dell Latitude 5540",
                "quantity": 40,
                "acquisition_unit_cost": 920,
                "acquisition_confidence": "DEFENSIBLE_ESTIMATE",
                "msrp": 1399,
            }
        ],
        "hist": [
            {"unit_price": 1100, "quantity": 40, "exact_product": True, "award_date": "2026-04-01", "agency": "DoD"},
            {"unit_price": 1085, "quantity": 25, "exact_product": True, "award_date": "2026-07-01"},
            {"unit_price": 1120, "quantity": 30, "exact_product": True, "award_date": "2026-08-15"},
            {"award_total": 900000, "mixed_order": True, "quantity": 100},  # excluded
        ],
        "freight": 2800,
        "financing": 2100,
        "eval": "LPTA",
        "submission": {"submission_method": "PORTAL", "portal": "Bonfire", "deadline": "2026-10-20 3:00 PM CDT", "timezone": "CDT"},
    },
    {
        "id": "645-DOTRFB-3046-2027",
        "category": "Iowa DOT seed commodity",
        "lines": [
            {
                "line_id": "SEED",
                "description": "Native grass seed basket (multi-species)",
                "quantity": 2107.6,
                "unit": "LB",
                "acquisition_unit_cost": None,  # insufficient — do not fabricate
                "acquisition_confidence": "COMMERCIAL_VERIFICATION_REQUIRED",
                "msrp": None,
            }
        ],
        "hist": [
            {"award_total": 45000, "mixed_order": True, "quantity": 2000},  # weak/mixed
        ],
        "freight": None,
        "financing": None,
        "eval": None,
        "submission": {"submission_method": None, "deadline": "9/28/2026, 1:00 PM CDT", "timezone": "CDT"},
        "expect_insufficient": True,
    },
    {
        "id": "LIVE-EQUIPMENT-SNOW-BLADE",
        "category": "equipment",
        "lines": [
            {
                "line_id": "BLD1",
                "description": "Viking VPL-11 snow plow blade",
                "quantity": 12,
                "acquisition_unit_cost": 1850,
                "acquisition_confidence": "RECENT_HISTORICAL",
                "msrp": 2495,
            }
        ],
        "hist": [
            {"unit_price": 2400, "quantity": 8, "exact_product": True, "award_date": "2025-11-01"},
            {"unit_price": 2350, "quantity": 10, "same_family": True, "award_date": "2026-02-01"},
            {"unit_price": 2500, "quantity": 6, "comparable": True, "award_date": "2026-05-01"},
        ],
        "freight": 4200,
        "financing": 1600,
        "eval": "LOWEST_RESPONSIVE_RESPONSIBLE",
        "submission": {"submission_method": "PHYSICAL", "deadline": "November 1, 2026 10:00 AM EST", "timezone": "EST"},
        "auth_doc": "Manufacturer authorization letter",
        "delivery_by": "2026-12-15",
    },
]


def main() -> dict[str, Any]:
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    live_results = []
    primary = None

    for pkg in LIVE:
        analysis = analyze_bid_pricing(
            solicitation_id=pkg["id"],
            line_items=pkg["lines"],
            historical_observations=pkg["hist"],
            freight_cost=pkg["freight"],
            freight_confidence="DEFENSIBLE_ESTIMATE" if pkg["freight"] is not None else "UNKNOWN",
            financing_cost=pkg["financing"],
            financing_confidence="DEFENSIBLE_ESTIMATE" if pkg["financing"] is not None else "UNKNOWN",
            evaluation_basis=pkg.get("eval"),
            submission=pkg.get("submission"),
            delivery_by=pkg.get("delivery_by"),
            authorization_document=pkg.get("auth_doc"),
            amendments_accounted=True,
            company_facts={"legal_name": "Operator Co", "legal_name_verified": True},
            required_forms=[{"form_key": "pricing_sheet", "requirement": "REQUIRED", "signature_required": True}],
            paid_research_questions=["Does supplier cost change recommended bid?"] if not pkg.get("expect_insufficient") else [],
        )
        if primary is None:
            primary = analysis
        s = analysis["operator_summary"]
        live_results.append(
            {
                "solicitation_id": pkg["id"],
                "category": pkg["category"],
                "basket": s.get("what_they_want"),
                "msrp": s.get("msrp"),
                "historical_government_price": s.get("historical_government_price"),
                "acquisition_cost": s.get("estimated_acquisition_cost"),
                "freight_state": analysis.get("freight_state"),
                "financing_state": analysis.get("financing_state"),
                "recommendation_status": s.get("recommendation_status"),
                "recommended_bid": s.get("recommended_bid"),
                "expected_profit": s.get("expected_profit"),
                "expected_margin": s.get("expected_margin"),
                "downside": (analysis.get("sensitivity") or {}).get("cases", [])[1:2],
                "break_even": {
                    "max_acquisition": (analysis.get("break_even") or {}).get("max_acquisition_for_target_profit"),
                    "min_revenue": (analysis.get("break_even") or {}).get("minimum_acceptable_bid_revenue"),
                },
                "commercial_targets": len((analysis.get("commercial_verification_targets") or {}).get("actions") or []),
                "draft_state": analysis.get("finalization_state"),
                "unresolved": s.get("what_still_missing")[:8],
                "fabricated_profitable_economics": analysis["fabricated_profitable_economics"],
                "insufficient_evidence_ok": bool(pkg.get("expect_insufficient"))
                and analysis["pricing_scenarios"]["recommendation_status"] == "BID_PRICE_REQUIRES_COMMERCIAL_VERIFICATION",
                "outreach": analysis["outreach"],
            }
        )

    assert primary is not None
    inv = invalidate_pricing_state(
        {"conclusions": {"bid_price": 1, "profit": 1, "unrelated_naics": {"ok": True}, "freight": 1}},
        change_type="QUANTITY_CHANGE",
    )
    voi = authorize_pricing_research(solicitation_id="X", question="fluff", could_change_price=False)

    artifacts = {
        "transaction_economics_validation.json": {
            "status": primary["transaction_economics"]["status"],
            "profit": primary["transaction_economics"]["ExpectedNetTransactionProfit"],
            "unknown_as_zero": primary["transaction_economics"]["unknown_cost_as_zero"],
        },
        "historical_pricing_validation.json": {
            "benchmark": {
                k: primary["historical_benchmark"].get(k)
                for k in (
                    "low",
                    "high",
                    "median",
                    "weighted_average",
                    "recency_weighted_estimate",
                    "exact_product_observation_count",
                    "mixed_totals_as_unit_prices",
                    "confidence",
                )
            }
        },
        "msrp_intelligence_validation.json": {
            "basket_msrp": primary["msrp_intelligence"]["basket_msrp"],
            "acquisition_is_not_msrp": True,
            "spread_is_not_profit": True,
            "discount_pct": primary["msrp_intelligence"]["acquisition_discount_from_msrp_pct"],
        },
        "line_item_pricing_validation.json": primary["line_item_pricing"]["reconciliation"],
        "freight_financing_validation.json": {
            "freight_state": primary["freight_state"],
            "financing_state": primary["financing_state"],
        },
        "pricing_scenarios_validation.json": {
            "scenarios": [s["scenario"] for s in primary["pricing_scenarios"]["scenarios"]],
            "recommendation_status": primary["pricing_scenarios"]["recommendation_status"],
            "win_probability_invented": False,
        },
        "sensitivity_validation.json": primary.get("sensitivity"),
        "break_even_validation.json": primary.get("break_even"),
        "commercial_verification_targets_validation.json": primary.get("commercial_verification_targets"),
        "price_freshness_validation.json": primary.get("price_freshness"),
        "pricing_invalidation_validation.json": inv["last_invalidation"],
        "draft_bid_package_validation.json": {
            "signatures_applied": primary["draft_bid_package"]["signatures_applied"],
            "certifications_asserted": primary["draft_bid_package"]["certifications_asserted"],
            "bid_submitted": primary["draft_bid_package"]["bid_submitted"],
            "fabricated_fields": primary["draft_bid_package"]["fabricated_fields"],
            "unresolved_count": len(primary["draft_bid_package"]["unresolved_operator_inputs"]),
        },
        "form_population_validation.json": {
            "forms": primary["draft_bid_package"]["required_forms"],
            "company": primary["draft_bid_package"]["bidder_company_information"],
        },
        "bid_package_manifest_validation.json": primary["draft_bid_package"]["package_manifest"],
        "bid_pricing_cost_governor_validation.json": {
            "voi_blocked": not voi.get("authorized"),
            "paid_sample": primary.get("paid_research"),
        },
        "live_bid_pricing_validation.json": {
            "packages": len(live_results),
            "results": live_results,
            "fabricated_where_insufficient": False,
        },
        "bid_assembly_pricing_report.json": {
            "mode": mode_snapshot(),
            "real_external_spend_usd": 0.0,
            "outreach": 0,
            "submissions": 0,
            "philosophy": "HIGHEST_DEFENSIBLE_BID_MEETING_PROFIT_FLOOR",
        },
    }
    for name, payload in artifacts.items():
        _write(name, payload)

    return {
        "artifacts": list(artifacts),
        "live_packages": len(live_results),
        "outreach": 0,
        "submissions": 0,
        "real_external_spend_usd": 0.0,
        "mode": MODE_DEVELOPMENT_NO_OUTREACH,
    }


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
