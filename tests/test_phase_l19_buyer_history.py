"""Phase L.19 buyer-specific history recovery tests."""

from __future__ import annotations

import json
from pathlib import Path

from discovery.buyer_history_profiles import (
    CATEGORY_ONLY,
    COMPARABLE_SPEC,
    EXACT_PRODUCT,
    EXACT_SAME_BUY,
    HISTORY_SOURCE_FOUND,
    NO_MATCH,
    NO_PUBLIC_HISTORY_SOURCE_FOUND,
    PRIOR_GOVERNMENT_VENDOR,
    STRONG_EQUIVALENT,
    classify_match_confidence,
    discover_and_profile_buyer,
    note_prior_government_vendor,
)
from discovery.sam_api_parked import SAM_API_PENDING_REPLACEMENT_KEY, sam_api_park_status
from phase_l.bidnet_parked import BIDNET_AUTH_HISTORY_PARKED
from phase_l.exact_history_recovery import grade_recovered_award
from phase_l.l19_buyer_history_recovery import (
    BUILD,
    L18_BASELINE,
    load_l18_targets,
    match_structured_history,
)
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap
from phase_l.quality_audit import GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C, GOV_VALUE_D

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
DOCS = ROOT / "docs"


def test_load_l18_targets():
    targets = load_l18_targets()
    assert len(targets) >= 30
    assert any(t.get("owner_decision") == "RESEARCH_COMPLETE_WAITING_QUOTE" for t in targets)


def test_buyer_history_source_discovery():
    prof = discover_and_profile_buyer("City of Austin", solicitation="IFB-1", model="F-150")
    assert prof["kind"] == "BuyerHistoryProfile"
    assert prof["buyer_id"]
    assert isinstance(prof.get("history_sources"), list)
    assert "live_sources" in prof  # separate from history_sources


def test_match_confidence_priority_and_category_guard():
    live = {"agency": "City of Austin", "title": "Ford F-150 fleet", "solicitation_number": "IFB-99"}
    exact = classify_match_confidence(
        live_row=live,
        award={"buyer": "City of Austin", "solicitation_id": "IFB-99", "item": "Ford F-150", "unit_price": 40_000},
        commercial={"manufacturer": "Ford", "model": "F-150"},
    )
    assert exact == EXACT_SAME_BUY
    product = classify_match_confidence(
        live_row={"agency": "Dallas", "title": "Ford F-150"},
        award={"buyer": "Houston", "item": "Ford F-150 pickup", "model": "F-150", "unit_price": 39_000},
        commercial={"model": "F-150", "manufacturer": "Ford"},
    )
    assert product in {EXACT_PRODUCT, STRONG_EQUIVALENT}
    cat = classify_match_confidence(
        live_row={"agency": "Austin", "title": "office furniture chairs"},
        award={"buyer": "Dallas", "item": "furniture supplies bulk", "total": 1000},
        commercial={},
    )
    assert cat in {CATEGORY_ONLY, NO_MATCH, COMPARABLE_SPEC}
    # Category-only must not become Gov A via grade_recovered_award without exact compat
    graded = grade_recovered_award(
        {"buyer": "Dallas", "item": "general category equipment", "unit_price": 100, "source": "category"},
        row={"agency": "Austin", "title": "random pumps"},
        commercial={},
    )
    assert graded["grade"] != GOV_VALUE_A or graded.get("compat", {}).get("exact")


def test_po_quantity_uom_guardrail():
    # total without quantity → grade_recovered_award refuses unit invent as A
    graded = grade_recovered_award(
        {"buyer": "City of Austin", "item": "Ford F-150", "total": 96000, "source": "po"},
        row={"agency": "City of Austin", "title": "Ford F-150"},
        commercial={"manufacturer": "Ford", "model": "F-150"},
    )
    assert graded.get("reason") in {"total_without_quantity_not_unit", "lot_price_not_unit"} or graded["grade"] == GOV_VALUE_D
    # Singular capital item with qty=1 from description is allowed
    from phase_l.l19_buyer_history_recovery import _parse_qty_from_text

    assert _parse_qty_from_text("FORD POLICE INTERCEPTOR") == 1.0
    assert _parse_qty_from_text("VEHICLES, MARKED AND UNMARKED POLICE") is None
    graded2 = grade_recovered_award(
        {
            "buyer": "City of Austin",
            "item": "FORD POLICE INTERCEPTOR",
            "total": 33497.98,
            "quantity": 1,
            "uom": "EA",
            "source": "po",
            "model": "Police Pursuit Interceptor",
        },
        row={"agency": "City of Austin", "title": "Vehicles - Ford Police Pursuit Interceptors"},
        commercial={"manufacturer": "Ford", "model": "Police Pursuit Interceptor"},
    )
    assert graded2["grade"] in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C} or graded2.get("gov")


def test_prior_government_vendor_semantics():
    pv = note_prior_government_vendor("ACME Motors", award={"source": "bid_tab", "award_date": "2024-01-01"})
    assert pv["kind"] == PRIOR_GOVERNMENT_VENDOR
    assert pv["not_acquisition_supplier"] is True


def test_structured_match_and_multiline_not_collapsed():
    history = [
        {
            "buyer": "Los Angeles County",
            "product": "ASUS Chromebox desktop",
            "total": 12000,
            "unit_price": 400,
            "quantity": 30,
            "source": "structured_test",
            "source_url": "https://example.gov/awards",
        },
        {
            "buyer": "Los Angeles County",
            "product": "Office furniture lot",
            "total": 50000,
            "source": "structured_test",
        },
    ]
    live = {"agency": "Los Angeles County", "title": "RFB ASUS Chromebox Closing"}
    hits = match_structured_history(
        live, history, commercial={"model": "Chromebox", "manufacturer": "ASUS"}, buyer="Los Angeles County"
    )
    assert hits
    assert hits[0]["confidence"] in {EXACT_SAME_BUY, EXACT_PRODUCT, STRONG_EQUIVALENT, COMPARABLE_SPEC}


def test_gates_unchanged():
    assert_no_fixed_positive_cap()
    assert BIDNET_AUTH_HISTORY_PARKED
    st = sam_api_park_status()
    assert st["status"] == SAM_API_PENDING_REPLACEMENT_KEY
    assert st["calls_consumed"] == 0
    assert st["calls_allowed_this_phase"] == 0
    assert BUILD.startswith("20260928-m3-phase-l19")
    assert L18_BASELINE["gov"]["A"] == 0
    assert HISTORY_SOURCE_FOUND and NO_PUBLIC_HISTORY_SOURCE_FOUND


def test_l19_artifacts_when_present():
    required = [
        "l19_target_population.json",
        "l19_buyer_history_profiles.json",
        "l19_history_source_discovery.json",
        "l19_award_matches.json",
        "l19_bid_tabs.json",
        "l19_purchase_orders.json",
        "l19_contract_registers.json",
        "l19_recurring_buy_signals.json",
        "l19_gov_upgrades.json",
        "l19_economics_recomputed.json",
        "l19_quote_targets.json",
        "l19_summary.json",
    ]
    if not (OUT / "l19_summary.json").exists():
        return
    for name in required:
        assert (OUT / name).exists(), name
    summary = json.loads((OUT / "l19_summary.json").read_text(encoding="utf-8"))
    assert summary["verdict"].startswith("PHASE_L19_")
    assert summary.get("no_sam_api_calls") is True
    assert summary.get("no_auth_bypass") is True
    assert summary.get("evidence_gate_unchanged") is True
    for doc in (
        "phase_l19_buyer_history_strategy.md",
        "phase_l19_history_sources.md",
        "phase_l19_award_matching.md",
        "phase_l19_bid_tab_recovery.md",
        "phase_l19_purchase_order_recovery.md",
        "phase_l19_recurring_buy_intelligence.md",
        "phase_l19_gov_evidence_delta.md",
        "phase_l19_quote_target_delta.md",
        "phase_l19_legacy_cleanup.md",
        "phase_l19_regression.md",
    ):
        assert (DOCS / doc).exists(), doc
