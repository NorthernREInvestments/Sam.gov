"""Tests for Specification-to-Supplier + Acquisition Target Engine."""

from __future__ import annotations

from m3_acquisition_target_engine import (
    FIRST_TRANSACTION_CANDIDATE,
    LEVEL_5_NO_PRICE,
    PUBLIC_ECONOMICS_FAILS_WHOLESALE_VERIFICATION_REQUIRED,
    Q_NEEDS_COMMERCIAL_PRODUCT_MATCH,
    Q_NEEDS_PUBLIC_PRICING,
    TRACK_RECORD_BUILDER,
    WHOLESALE_ACCESS_UNVERIFIED,
    analyze_acquisition_targets_top,
    analyze_line_acquisition,
    analyze_opportunity_acquisition,
    apply_va_acquisition_update,
    assign_acquisition_queue,
    build_acquisition_price_gap,
    build_compliance_match_profile,
    build_first_transaction_profile,
    build_government_revenue_profile,
    build_va_acquisition_queues,
    calculate_acquisition_target,
    classify_acquisition_channel_status,
    deal_room_acquisition_section,
)
from m3_deal_economics import STATUS_BELOW, STATUS_MEETS, STATUS_UNKNOWN, STATUS_UNVIABLE
from m3_product_identity_resolution import (
    READY_WITH_SPECIFICATIONS,
    SPECIFICATION_BASED,
    SPECIFICATION_IDENTITY,
    resolve_opportunity_product_identities,
)


class _MemStore:
    def __init__(self, rows: dict):
        self._rows = rows

    def all(self):
        return list(self._rows.values())

    def save(self):
        return None


def _blade_row(**extra):
    row = {
        "canonical_id": "blade-acq-1",
        "title": "Tungsten-Carbide Blades for Snow/Ice Removal",
        "estimated_value": 45000,
        "quantity": 20,
        "line_items": [
            {
                "description": (
                    "Tungsten-carbide blades for snow/ice removal, "
                    "3/4 inch x 6 ft, ASTM compatible mounting"
                ),
                "quantity": 20,
                "unit": "EA",
            }
        ],
        "attachment_text": (
            "Industrial blade shall be tungsten-carbide. Dimensions 0.75 in x 6 ft. "
            "Application: snow/ice removal plow. Must meet ASTM wear standard. "
            "Compatible with existing plow mounting brackets. "
            "Or equal products meeting these specifications are acceptable."
        ),
        "operator_economics": {
            "estimated_freight_usd": 800,
            "estimated_financing_cost_usd": 400,
            "known_fees_usd": 100,
        },
    }
    row.update(extra)
    return row


def test_search_brief_not_compliant():
    profile = {
        "Specifications": {
            "Materials": ["tungsten-carbide"],
            "Dimensions": "0.75 in x 6 ft",
            "Application": "snow/ice plow",
            "Standards": ["ASTM"],
            "Required_features": ["mounting"],
        },
        "Manufacturer": "UNKNOWN",
    }
    brief = {
        "candidate_origin": "specification_search_brief",
        "Manufacturer": "UNKNOWN",
        "Specifications": profile["Specifications"],
    }
    comp = build_compliance_match_profile(profile, brief)
    assert comp["overall"] == "SEARCH_BRIEF_NOT_A_PRODUCT"


def test_compliance_mismatch_not_presented_as_compliant():
    profile = {
        "Specifications": {
            "Materials": ["tungsten-carbide"],
            "Dimensions": "0.75 in x 6 ft",
            "Application": "snow/ice plow",
            "Standards": ["ASTM"],
            "Required_features": ["mounting brackets"],
        },
        "Manufacturer": "UNKNOWN",
    }
    candidate = {
        "Manufacturer": "Acme Steel",
        "Product_name": "Mild steel blade",
        "Specifications": {
            "Materials": ["mild steel"],
            "Dimensions": "0.75 in x 6 ft",
            "Application": "snow/ice plow",
            "Standards": ["ASTM"],
            "Required_features": ["mounting brackets"],
        },
    }
    comp = build_compliance_match_profile(profile, candidate)
    assert any(c["Result"] == "MISMATCH" for c in comp["comparisons"])
    assert comp["overall"] == "NOT_COMPLIANT_EVIDENCED"


def test_acquisition_target_formula():
    revenue = {
        "status": "CURRENT_ESTIMATED_REVENUE",
        "primary_revenue": 45000,
    }
    row = _blade_row()
    target = calculate_acquisition_target(revenue, row, quantity=20)
    assert target["calculable"] is True
    # 45000 - 10000 - 800 - 400 - 100 = 33700
    assert target["TOTAL_TARGET_ACQUISITION_COST"] == 33700.0
    assert target["TARGET_UNIT_ACQUISITION_COST"] == 1685.0


def test_profit_gap_math():
    row = _blade_row()
    target = {
        "TARGET_UNIT_ACQUISITION_COST": 8700.0,
        "TOTAL_TARGET_ACQUISITION_COST": 174000.0,
    }
    pricing = {"best_observed_unit": 9000.0}
    gap = build_acquisition_price_gap(
        target, pricing, quantity=20, revenue=185000.0, row=row
    )
    assert gap["calculable"] is True
    assert gap["Unit_difference"] == 300.0
    assert gap["Total_difference"] == 6000.0
    # 185000 - 180000 - 800 - 400 - 100 = 3700
    assert gap["Expected_profit_at_observed_price"] == 3700.0
    assert gap["Profit_target_status"] == STATUS_BELOW


def test_public_fail_preserves_wholesale_path():
    pricing = {
        "best_observed_unit": 9000.0,
        "counts": {"LEVEL_2": 1},
        "levels": {"LEVEL_2": [{"Price": 9000}]},
    }
    channels = [{"Channel_type": "industrial_mro", "status": "PATH_IDENTIFIED_UNVERIFIED"}]
    status = classify_acquisition_channel_status(
        pricing, channels, public_fails_target=True
    )
    assert status["status"] == PUBLIC_ECONOMICS_FAILS_WHOLESALE_VERIFICATION_REQUIRED
    assert status["wholesale_unverified"] is True
    assert status["status"] != "PRICE_ACCESS_BLOCKED"


def test_first_transaction_not_auto_recommend():
    gap = {
        "calculable": True,
        "Expected_profit_at_observed_price": 3500.0,
        "Profit_target_status": STATUS_BELOW,
    }
    channel = {"status": WHOLESALE_ACCESS_UNVERIFIED, "wholesale_unverified": True}
    financing = {"status": "ECONOMICALLY_ATTRACTIVE_FUNDING_VERIFICATION_REQUIRED"}
    ftx = build_first_transaction_profile(_blade_row(), gap, channel, financing)
    assert ftx["status"] in {
        FIRST_TRANSACTION_CANDIDATE,
        TRACK_RECORD_BUILDER,
        "NORMAL_PROFIT_TARGET",
        "NOT_ATTRACTIVE",
    }
    assert ftx.get("auto_recommend_pursue") is False or "auto_recommend_pursue" not in ftx or ftx.get(
        "auto_recommend_pursue"
    ) is False


def test_blade_line_produces_search_brief_and_channels():
    row = _blade_row()
    identity = resolve_opportunity_product_identities(row, update_learning=False)
    assert identity["Specification_identities"] >= 1
    primary = identity["primary_profile"]
    assert primary["Identity_type"] == SPECIFICATION_IDENTITY
    assert (primary.get("SUPPLIER_RESEARCH_READINESS_SCORE") or {}).get("status") in {
        READY_WITH_SPECIFICATIONS,
        "NEEDS_IDENTITY_RESEARCH",
    }
    line = analyze_line_acquisition(row, primary, allow_paid_web=False)
    assert line["kind"] == "M3LineAcquisitionIntelligence"
    assert (line.get("candidates_found") or 0) + (line.get("search_briefs") or 0) >= 1
    assert len(line["SUPPLIER_CHANNELS"]) >= 1
    for c in line.get("COMMERCIAL_PRODUCT_CANDIDATES") or []:
        if c.get("candidate_origin") == "specification_search_brief":
            assert (c.get("COMPLIANCE_MATCH_PROFILE") or {}).get("overall") == "SEARCH_BRIEF_NOT_A_PRODUCT"
            assert c.get("presented_as_compliant") is not True
    assert line["GOVERNMENT_REVENUE_PROFILE"]["status"] in {
        "CURRENT_VERIFIED_REVENUE",
        "CURRENT_ESTIMATED_REVENUE",
        "INSUFFICIENT_REVENUE_EVIDENCE",
        "HISTORICAL_EXACT_BENCHMARK",
        "HISTORICAL_COMPARABLE_BENCHMARK",
    }
    assert line["ACQUISITION_TARGET"]["kind"] == "ACQUISITION_TARGET"
    assert line["PRICE_SCENARIOS"]["kind"] == "PRICE_SCENARIOS" or "scenarios" in line["PRICE_SCENARIOS"]
    q = assign_acquisition_queue(line)
    assert q in {
        Q_NEEDS_COMMERCIAL_PRODUCT_MATCH,
        Q_NEEDS_PUBLIC_PRICING,
        "NEEDS_WHOLESALE_VERIFICATION",
        "NEEDS_REVENUE_EVIDENCE",
        "NEEDS_FINANCING_VERIFICATION",
        "ECONOMICS_READY",
        "OWNER_REVIEW",
    }


def test_batch_and_deal_room():
    store = _MemStore(
        {
            "blade-acq-1": _blade_row(),
            "seed-1": {
                "canonical_id": "seed-1",
                "title": "Wildflower / Native Grass Seed Mix",
                "estimated_value": 12000,
                "line_items": [
                    {
                        "description": "Native grass and wildflower seed mix, PLS certified, Iowa origin",
                        "quantity": 500,
                        "unit": "LB",
                    }
                ],
                "attachment_text": (
                    "Seed shall be native grass and wildflower mix. "
                    "Application roadside restoration. Certification PLS required. "
                    "Origin Iowa preferred. Equivalents meeting PLS purity acceptable."
                ),
            },
        }
    )
    run = analyze_acquisition_targets_top(store, limit=5, persist=False)
    assert run["NEXT_STATE"] == "SPECIFICATION_TO_ACQUISITION_ECONOMICS_OPERATIONAL"
    assert run["analyzed"] >= 1
    assert run["SPECIFICATION_TO_PRODUCT"]["Products_analyzed"] >= 1
    assert run["SAFETY"]["Outreach_actions"] == 0

    panel = deal_room_acquisition_section(store._rows["blade-acq-1"])
    assert panel["kind"] == "M3DealRoomAcquisitionIntelligence"
    assert panel["Government_requirement"]
    assert "Missing_information" in panel
    assert "CONTACT_SUPPLIER" in panel["VA_forbidden_actions"]


def test_va_forbidden_and_queues():
    store = _MemStore({"blade-acq-1": _blade_row()})
    bad = apply_va_acquisition_update(store, "blade-acq-1", action="CONTACT_SUPPLIER")
    assert bad["ok"] is False
    ok = apply_va_acquisition_update(
        store,
        "blade-acq-1",
        action="ATTACH_PRICING_EVIDENCE",
        note="public listing",
        evidence={"unit_price": 1750, "level": "public"},
    )
    assert ok["ok"] is True
    assert store._rows["blade-acq-1"]["commercial_pricing"]["public_unit_price"] == 1750.0
    queues = build_va_acquisition_queues()
    assert "NEEDS_PUBLIC_PRICING" in queues["queues"]


def test_unknown_not_rejected_on_insufficient_revenue():
    row = {
        "canonical_id": "no-rev",
        "title": "Law Enforcement Badges",
        "line_items": [
            {
                "description": "Gold plated law enforcement badges with agency seal, clutch back",
                "quantity": 50,
            }
        ],
        "attachment_text": (
            "Badges shall be gold plated. Application law enforcement uniform. "
            "Clutch back required. Agency seal engraved. Dimensions 2.5 inch."
        ),
    }
    result = analyze_opportunity_acquisition(row, allow_paid_web=False)
    assert result["lines_analyzed"] >= 0
    for li in result.get("LINE_ACQUISITION") or []:
        gap = li.get("ACQUISITION_PRICE_GAP_PROFILE") or {}
        # Missing evidence → UNKNOWN, never auto-rejected as UNVIABLE without cost+price
        if not gap.get("calculable"):
            assert gap.get("Profit_target_status") == STATUS_UNKNOWN
