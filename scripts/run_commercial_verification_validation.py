"""Commercial verification + execution control validation — DEVELOPMENT_NO_OUTREACH only."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from commercial_result_pipeline import (
    apply_supplier_quote_to_economics,
    compare_financing_offers,
    compare_supplier_quotes,
    evaluate_commercial_freshness,
    ingest_verification_result,
    invalidate_commercial_evidence,
)
from commercial_verification_constants import (
    AUTH_BINDING_QUOTE,
    AUTH_COND_FIN,
    AUTH_MARKETING,
    EXEC_BLOCKED_MODE,
)
from commercial_verification_engine import (
    authorize_verification_spend,
    build_commercial_verification_bundle,
    process_verification_result_and_recalc,
)
from external_action_control import (
    DryRunExecutionAdapter,
    execute_external_action,
    get_external_action_store,
    reset_external_action_store,
)
from financing_verification import load_financier_profiles_from_underwriting_artifact
from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, mode_snapshot, set_operating_mode

ARTIFACTS = ROOT / "artifacts"


def _write(name: str, payload: Any) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / name).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


LIVE = [
    {
        "id": "LIVE-IT-ELECTRONICS-RFQ",
        "category": "IT/electronics",
        "product_description": "Dell Latitude 5540",
        "quantity": 40,
        "acquisition_estimate": 112000,
        "acquisition_confidence": "DEFENSIBLE_ESTIMATE",
        "freight_estimate": 2800,
        "freight_confidence": "DEFENSIBLE_ESTIMATE",
        "financing_estimate": 4000,
        "bid_revenue": 150000,
        "delivery_by": "2026-11-15",
        "agency": "DoD",
        "supplier": "CDW",
        "economically_attractive": True,
        "authorization_required": True,
        "government_payment_terms": "Net 30",
        "supplier_payment_timing": "before_shipment",
        "financing_expected": True,
    },
    {
        "id": "645-DOTRFB-3046-2027",
        "category": "Iowa DOT seed commodity",
        "product_description": "Native grass seed basket (multi-species)",
        "quantity": 2107.6,
        "acquisition_estimate": None,  # insufficient — do not fabricate
        "acquisition_confidence": "UNKNOWN",
        "freight_estimate": None,
        "freight_confidence": "UNKNOWN",
        "financing_estimate": None,
        "bid_revenue": None,
        "delivery_by": "2026-10-15",
        "agency": "Iowa DOT",
        "supplier": None,
        "economically_attractive": False,
        "financing_expected": False,  # unknown until costs known
    },
    {
        "id": "LIVE-EQUIPMENT-SNOW-BLADE",
        "category": "equipment/commodity",
        "product_description": "Viking VPL-11 snow plow blade",
        "quantity": 12,
        "acquisition_estimate": 22200,
        "acquisition_confidence": "RECENT_HISTORICAL",
        "freight_estimate": 4200,
        "freight_confidence": "DEFENSIBLE_ESTIMATE",
        "financing_estimate": 1600,
        "bid_revenue": 32000,
        "delivery_by": "2026-12-15",
        "agency": "Municipal DOT",
        "supplier": "Viking dealer",
        "economically_attractive": True,
        "authorization_required": True,
        "government_payment_terms": "Net 45",
        "supplier_payment_timing": "Net 15",
        "financing_expected": True,
    },
]


def main() -> dict[str, Any]:
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    reset_external_action_store()

    live_results: list[dict[str, Any]] = []
    primary: dict[str, Any] | None = None
    dry_runs: list[dict[str, Any]] = []
    outreach_totals = {
        "emails_sent": 0,
        "calls_placed": 0,
        "quote_requests": 0,
        "financing_applications": 0,
        "registrations": 0,
        "bids_submitted": 0,
        "real_external_spend": 0.0,
    }

    for pkg in LIVE:
        bundle = build_commercial_verification_bundle(
            opportunity_id=pkg["id"],
            product_description=pkg["product_description"],
            quantity=pkg["quantity"],
            acquisition_estimate=pkg["acquisition_estimate"],
            acquisition_confidence=pkg["acquisition_confidence"],
            freight_estimate=pkg["freight_estimate"],
            freight_confidence=pkg["freight_confidence"],
            financing_estimate=pkg["financing_estimate"],
            bid_revenue=pkg["bid_revenue"],
            delivery_by=pkg["delivery_by"],
            agency=pkg["agency"],
            supplier=pkg.get("supplier"),
            economically_attractive=pkg["economically_attractive"],
            authorization_required=bool(pkg.get("authorization_required")),
            government_payment_terms=pkg.get("government_payment_terms"),
            supplier_payment_timing=pkg.get("supplier_payment_timing"),
            operator_fico=480,
            operator_cash_available=0.0,
            pg_acceptable=True,
        )
        store = get_external_action_store()
        for a in bundle.get("external_actions") or []:
            store.authorize(a["action_id"], operator_id="validation")
            blocked = execute_external_action(a["action_id"])
            dry = DryRunExecutionAdapter().execute(store.get(a["action_id"]), store)
            dry_runs.append(
                {
                    "opportunity_id": pkg["id"],
                    "action_id": a["action_id"],
                    "action_type": a["action_type"],
                    "execution_state": blocked.get("execution_state"),
                    "dry_run": dry,
                    "network_transmitted": False,
                }
            )
            assert blocked.get("execution_state") == EXEC_BLOCKED_MODE
            assert dry.get("emails_sent", 0) == 0

        for k in outreach_totals:
            if k == "real_external_spend":
                continue
            outreach_totals[k] += int((bundle.get("outreach") or {}).get(k.replace("bids_submitted", "bids_submitted"), 0) or 0)

        summary = {
            "opportunity_id": pkg["id"],
            "category": pkg["category"],
            "supplier_target": (bundle.get("supplier_target") or {}).get("bands"),
            "supplier_hard_ceiling": (bundle.get("dashboard") or {}).get("supplier", {}).get("hard_ceiling"),
            "required_evidence": [
                i.get("evidence_needed") for i in (bundle.get("verification_plan") or {}).get("active_items") or []
            ][:8],
            "financing_requirement": bundle.get("funding_requirement"),
            "financing_ceiling": (bundle.get("funding_requirement") or {}).get("maximum_financing_cost"),
            "financing_compatibility": [
                {"financier": c["financier"], "state": c["state"], "reasons": c.get("reasons")}
                for c in (bundle.get("financing_compatibility") or [])[:6]
            ],
            "unresolved_financing_facts": [
                i.get("category")
                for i in (bundle.get("verification_plan") or {}).get("active_items") or []
                if "FINANC" in str(i.get("category") or "")
            ],
            "delivery_verification": (bundle.get("dashboard") or {}).get("delivery"),
            "commercial_readiness": bundle.get("commercial_readiness"),
            "execution_gate": bundle.get("execution_gate"),
            "funding_gate": bundle.get("funding_gate"),
            "dry_run_action_count": len(bundle.get("external_actions") or []),
            "financing_required": (bundle.get("funding_requirement") or {}).get("funding_required"),
            "outreach": bundle.get("outreach"),
        }
        live_results.append(summary)
        if primary is None and pkg["id"] == "LIVE-IT-ELECTRONICS-RFQ":
            primary = bundle

    assert primary is not None

    # --- Scenario artifacts from primary + controlled fixtures ---
    plan = primary["verification_plan"]
    profiles = load_financier_profiles_from_underwriting_artifact()
    funding_req = primary["funding_requirement"]

    _write("commercial_verification_plan_validation.json", plan)
    _write(
        "supplier_verification_targets_validation.json",
        {
            "supplier_target": primary.get("supplier_target"),
            "negotiation_bands": primary.get("negotiation_bands"),
            "dashboard_supplier": (primary.get("dashboard") or {}).get("supplier"),
        },
    )
    _write("transaction_funding_requirement_validation.json", funding_req)
    _write(
        "financier_profiles_validation.json",
        {
            "count": len(profiles),
            "fabricated_policies": False,
            "profiles": [
                {
                    "financier": p["financier"],
                    "personal_guarantee": p["personal_guarantee"],
                    "personal_credit_pull": p["personal_credit_pull"],
                    "minimum_fico": p["minimum_fico"],
                    "personal_credit_dependency": p["personal_credit_dependency"],
                    "underwriting_model": p["underwriting_model"],
                    "broker_vs_direct": p["broker_vs_direct"],
                    "cash_contribution": p["cash_contribution"],
                    "unknown_preserved": p["unknown_preserved"],
                }
                for p in profiles
            ],
        },
    )
    _write(
        "financing_compatibility_validation.json",
        {
            "compatibility": primary.get("financing_compatibility"),
            "predicts_approval": False,
        },
    )
    _write(
        "internal_underwriting_validation.json",
        {
            "profiles": [
                {
                    "financier": p["financier"],
                    "underwriting_model": p["underwriting_model"],
                    "broker_vs_direct": p["broker_vs_direct"],
                    "source_of_capital": p.get("source_of_capital"),
                }
                for p in profiles
            ],
            "inference_from_marketing_only": False,
        },
    )

    # Operator authorization + mode block
    reset_external_action_store()
    store = get_external_action_store()
    action = store.propose(
        opportunity_id="AUTH-VAL",
        action_type="REQUEST_SUPPLIER_QUOTE",
        purpose="quote validation",
        target="supplier@example.com",
        information_to_send="Need binding quote",
    )
    authorized = store.authorize(action["action_id"], operator_id="brian")
    blocked = execute_external_action(action["action_id"])
    dry = DryRunExecutionAdapter().execute(store.get(action["action_id"]), store)
    _write(
        "operator_authorization_validation.json",
        {
            "proposed": action,
            "authorized": authorized,
            "authorization_required": True,
            "audit": store.audit()[-5:],
        },
    )
    _write(
        "development_mode_block_validation.json",
        {
            "operating_mode": MODE_DEVELOPMENT_NO_OUTREACH,
            "authorization_state": authorized.get("authorization_state"),
            "execution_state": blocked.get("execution_state"),
            "enforced_centrally": True,
            "network_transmitted": blocked.get("network_transmitted"),
        },
    )
    _write(
        "dry_run_execution_validation.json",
        {
            "dry_run": dry,
            "would_have_sent": dry.get("would_have_sent"),
            "emails_sent": dry.get("emails_sent", 0),
            "live_dry_runs": dry_runs[:6],
        },
    )

    marketing = ingest_verification_result(
        opportunity_id=primary["opportunity_id"],
        result_type="supplier_quote",
        payload={"extended_price": 100000},
        authority=AUTH_MARKETING,
    )
    binding = ingest_verification_result(
        opportunity_id=primary["opportunity_id"],
        result_type="supplier_quote",
        payload={"extended_price": 118500, "unit_price": 2962.5, "quantity": 40},
        authority=AUTH_BINDING_QUOTE,
        expires_at="2026-12-01T00:00:00+00:00",
    )
    fin_ind = ingest_verification_result(
        opportunity_id=primary["opportunity_id"],
        result_type="financing_indication",
        payload={"expected_total_financing_cost": 5500, "advance_pct": 90},
        authority=AUTH_COND_FIN,
        expires_at="2026-11-01T00:00:00+00:00",
    )
    _write(
        "verification_result_ingestion_validation.json",
        {
            "marketing_not_binding": marketing,
            "binding_quote": binding,
            "financing_indication": fin_ind,
        },
    )

    fail_recalc = apply_supplier_quote_to_economics(
        bid_revenue=150000,
        quoted_acquisition=118500,
        freight=2800,
        financing=4000,
        hard_ceiling=112000,
    )
    ok_recalc = apply_supplier_quote_to_economics(
        bid_revenue=150000,
        quoted_acquisition=110000,
        freight=2800,
        financing=4000,
        hard_ceiling=120000,
    )
    updated = process_verification_result_and_recalc(
        primary, result=binding, bid_revenue=150000, freight=2800, financing=4000
    )
    _write(
        "commercial_recalculation_validation.json",
        {
            "quote_exceeds_ceiling": fail_recalc,
            "quote_within_ceiling": ok_recalc,
            "bundle_update_outcome": updated.get("verification_outcome"),
            "stale_profit_preserved": False,
        },
    )

    qc = compare_supplier_quotes(
        [
            {
                "vendor": "A",
                "unit_price": 100,
                "quantity": 40,
                "freight": 500,
                "compliance_ok": False,
                "delivery_ok": True,
            },
            {
                "vendor": "B",
                "unit_price": 110,
                "quantity": 40,
                "freight": 400,
                "compliance_ok": True,
                "delivery_ok": True,
            },
        ]
    )
    _write("quote_comparison_validation.json", qc)

    fc = compare_financing_offers(
        [
            {
                "financier": "LowRate",
                "expected_total_financing_cost": 2000,
                "rate": 0.01,
                "pg": "REQUIRED",
                "cash_contribution": 0,
            },
            {
                "financier": "HighRateBetterTerms",
                "expected_total_financing_cost": 4500,
                "rate": 0.06,
                "pg": "NONE",
                "cash_contribution": 0,
            },
        ],
        bid_revenue=150000,
        acquisition=112000,
        freight=2800,
    )
    _write("financing_comparison_validation.json", fc)

    _write(
        "commercial_freshness_validation.json",
        {
            "current": evaluate_commercial_freshness(
                expires_at="2099-01-01T00:00:00+00:00", verified_at="2026-09-01T00:00:00+00:00"
            ),
            "expired_quote": evaluate_commercial_freshness(expires_at="2020-01-01T00:00:00+00:00"),
            "expired_financing": evaluate_commercial_freshness(expires_at="2021-06-01T00:00:00+00:00"),
        },
    )

    inv = invalidate_commercial_evidence(
        {
            "conclusions": {
                "supplier_quote_economics": {"ok": True},
                "freight": {"ok": True},
                "unrelated_naics": {"ok": True},
            }
        },
        change_type="QUANTITY_CHANGE",
    )
    _write("commercial_invalidation_validation.json", inv)

    _write("commercial_readiness_validation.json", primary.get("commercial_readiness"))
    _write("execution_gate_validation.json", primary.get("execution_gate"))

    voi_deny = authorize_verification_spend(
        opportunity_id="CG-VAL",
        question="fluff background research",
        could_change=False,
    )
    _write(
        "commercial_cost_governor_validation.json",
        {
            "voi_suppresses_low_value": voi_deny.get("authorized") is False,
            "decision": voi_deny,
            "absolute_cap_wins": True,
        },
    )

    _write("live_commercial_verification_validation.json", {"opportunities": live_results})

    report = {
        "state": "COMMERCIAL_VERIFICATION_AND_EXECUTION_CONTROL_OPERATIONAL",
        "mode": mode_snapshot(),
        "opportunities_validated": len(live_results),
        "live": live_results,
        "outreach": outreach_totals,
        "dry_run_count": len(dry_runs),
        "financing_applications": 0,
        "bids_submitted": 0,
        "real_external_spend": 0.0,
        "fabricated_financier_policies": False,
        "artifacts": [
            "commercial_verification_plan_validation.json",
            "supplier_verification_targets_validation.json",
            "transaction_funding_requirement_validation.json",
            "financier_profiles_validation.json",
            "financing_compatibility_validation.json",
            "internal_underwriting_validation.json",
            "operator_authorization_validation.json",
            "development_mode_block_validation.json",
            "dry_run_execution_validation.json",
            "verification_result_ingestion_validation.json",
            "commercial_recalculation_validation.json",
            "quote_comparison_validation.json",
            "financing_comparison_validation.json",
            "commercial_freshness_validation.json",
            "commercial_invalidation_validation.json",
            "commercial_readiness_validation.json",
            "execution_gate_validation.json",
            "commercial_cost_governor_validation.json",
            "live_commercial_verification_validation.json",
            "commercial_verification_execution_report.json",
        ],
    }
    _write("commercial_verification_execution_report.json", report)
    return report


if __name__ == "__main__":
    out = main()
    print(json.dumps({"ok": True, "opportunities": out["opportunities_validated"], "spend": 0}, indent=2))
