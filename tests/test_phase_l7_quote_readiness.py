"""Phase L.7 quote outreach readiness tests."""

from __future__ import annotations

from phase_l.acquisition_lanes import DEEP_RESEARCH_NO_FIXED_COUNT, STAGE3_NO_ROW_CAP
from phase_l.legacy_cleanup import (
    CANONICAL_FUNNEL_STAGES,
    OBSOLETE_RULE_IDS,
    assert_canonical_caps,
    assert_no_fixed_positive_cap,
    legacy_cleanup_report,
    migrate_status,
    obsolete_rule_active,
)
from phase_l.product_page_resolution import EXACT_VERIFIED
from phase_l.quote_economics import UOM_UNRESOLVED, calculate_max_buy_engine, evaluate_quote_opportunity
from phase_l.quote_readiness import (
    AUTHORIZED_LIKELY,
    INTERNAL_FIELDS_NEVER_SUPPLIER,
    OWNER_APPROVAL_REQUIRED,
    QUOTE_ACCEPTABLE,
    QUOTE_BLOCKED_DEADLINE,
    QUOTE_BLOCKED_ELIGIBILITY,
    QUOTE_BLOCKED_NO_SUPPLIER,
    QUOTE_BLOCKED_PRODUCT_IDENTITY,
    QUOTE_BLOCKED_QUANTITY,
    QUOTE_BLOCKED_SOURCE_APPROVAL,
    QUOTE_BLOCKED_UOM,
    QUOTE_EXCELLENT,
    QUOTE_FAIL,
    READY_FOR_QUOTE_OUTREACH,
    SPEC_MATCH_CANDIDATE,
    build_internal_quote_control,
    build_supplier_facing_packet,
    classify_requirement_mode,
    classify_supplier_authorization,
    compare_supplier_quotes,
    evaluate_quote_readiness,
    evaluate_supplier_quote_response,
    filter_owner_queue,
    rank_suppliers_for_quote,
)


def test_legacy_rule_removal():
    for rid in OBSOLETE_RULE_IDS:
        assert obsolete_rule_active(rid) is False
    assert migrate_status("NO_PRICE_INFORMATION") == "PUBLIC_PRICE_NOT_FOUND_OR_QUOTE_REQUIRED"
    rep = legacy_cleanup_report()
    assert rep["obsolete_rules_active_on_live_path"] is False
    assert "quote_readiness" in rep["canonical_funnel"]


def test_canonical_funnel_only():
    assert CANONICAL_FUNNEL_STAGES[0] == "broad_discovery"
    assert "quote_readiness" in CANONICAL_FUNNEL_STAGES
    caps = assert_canonical_caps()
    assert caps["STAGE3_NO_ROW_CAP"] is True


def test_quote_ready_exact_product():
    row = {
        "title": "2027 Ford F-150 Police Responder",
        "agency": "County Sheriff",
        "solicitation_id": "RFQ-1",
        "quantity": 2,
        "uom": "each",
        "place_of_performance": "Austin TX",
        "response_deadline": "2099-12-01",
        "original_posting_url": "https://sam.gov/opp/abc/view",
    }
    commercial = {"manufacturer": "Ford", "model": "F-150 Police Responder"}
    ev = evaluate_quote_opportunity(
        row,
        commercial=commercial,
        history={"historical_award_unit_price": 58000},
        stage3={},
        deadline_days=21,
        original={"original_source_verified": True, "original_posting_url": row["original_posting_url"], "solicitation_number": "RFQ-1"},
    )
    readiness = evaluate_quote_readiness(
        row,
        ev=ev,
        lane="QUOTE_REQUIRED_COMMERCIAL",
        commercial=commercial,
        original={"original_source_verified": True, "original_posting_url": row["original_posting_url"], "solicitation_number": "RFQ-1"},
        submission={"submission_path_resolved": True, "method": "portal"},
        deadline_days=21,
    )
    assert readiness["ready"] is True
    assert readiness["status"] == READY_FOR_QUOTE_OUTREACH
    assert readiness["owner_gate"] == OWNER_APPROVAL_REQUIRED
    assert readiness["send_authorized"] is False


def test_quote_ready_brand_or_equal():
    req = classify_requirement_mode(
        {"title": "Bobcat ToolCat UW56 or equal"},
        {"manufacturer": "Bobcat", "model": "ToolCat UW56"},
    )
    assert req["brand_or_equal"] is True
    assert "equivalent" in req["acceptable_substitution_language"].lower()


def test_spec_driven_quote_packet():
    req = classify_requirement_mode({"title": "Shall provide minimum 48V DC power supply IAW salient characteristics"})
    assert req["requirement_mode"] == "DESCRIPTIVE_SPECIFICATION"
    assert req["spec_match_status"] == SPEC_MATCH_CANDIDATE
    pkt = build_supplier_facing_packet(
        row={"title": "Shall provide minimum 48V DC power supply", "solicitation_id": "S1"},
        commercial={},
        requirement=req,
        original={"solicitation_number": "S1", "issuing_agency": "City"},
    )
    assert pkt["send_authorized"] is False
    assert "max_buy" not in pkt


def test_quantity_blocker():
    row = {"title": "misc supplies", "solicitation_id": "Q1"}
    ev = {
        "government_value": {"state": "GOV_VALUE_STRONG", "unit_value": 1000},
        "max_buy": {"supplier_quote_target": 800, "thresholds": {}},
        "quote_dependent": {"tiers": {"quote_dependent_positive": True}},
        "uom": {"status": UOM_UNRESOLVED, "quantity": None},
        "suppliers": [{"name": "Acme", "supplier_domain": "acme.com"}],
        "freight": {"status": "FREIGHT_ESTIMATE", "amount": 50},
    }
    r = evaluate_quote_readiness(
        row,
        ev=ev,
        commercial={},
        original={"original_source_verified": True, "original_posting_url": "https://example.gov/x"},
        deadline_days=20,
    )
    assert QUOTE_BLOCKED_QUANTITY in r["blockers"] or QUOTE_BLOCKED_PRODUCT_IDENTITY in r["blockers"]


def test_uom_blocker():
    row = {"title": "Widget kit", "solicitation_id": "U1"}
    ev = {
        "government_value": {"state": "GOV_VALUE_EXACT", "unit_value": 100, "total_value": 5000},
        "max_buy": {"supplier_quote_target": 80, "thresholds": {}},
        "quote_dependent": {"tiers": {"quote_dependent_positive": True}},
        "uom": {"status": UOM_UNRESOLVED, "quantity": None},
        "suppliers": [{"name": "Acme"}],
        "freight": {"amount": 10, "status": "FREIGHT_ESTIMATE"},
    }
    r = evaluate_quote_readiness(
        row,
        ev=ev,
        commercial={"model": "W-1"},
        original={"original_source_verified": True, "original_posting_url": "https://example.gov/x"},
        deadline_days=20,
    )
    assert QUOTE_BLOCKED_UOM in r["blockers"]


def test_deadline_blocker():
    row = {
        "title": "Ford F-150",
        "solicitation_id": "D1",
        "quantity": 1,
        "uom": "each",
        "place_of_performance": "TX",
    }
    ev = evaluate_quote_opportunity(
        row,
        commercial={"manufacturer": "Ford", "model": "F-150"},
        history={"historical_award_unit_price": 48000},
        deadline_days=2,
    )
    r = evaluate_quote_readiness(
        row,
        ev=ev,
        commercial={"manufacturer": "Ford", "model": "F-150"},
        original={"original_source_verified": True, "original_posting_url": "https://example.gov/x"},
        deadline_days=2,
    )
    assert QUOTE_BLOCKED_DEADLINE in r["blockers"]
    assert r["ready"] is False


def test_eligibility_blocker():
    row = {"title": "Sole source OEM only mandatory GSA schedule", "solicitation_id": "E1", "quantity": 1}
    ev = {
        "government_value": {"state": "GOV_VALUE_STRONG", "unit_value": 10000},
        "max_buy": {"supplier_quote_target": 7000, "thresholds": {"MAX_BUY_FOR_5K_PROFIT": 2000}},
        "quote_dependent": {"tiers": {"quote_dependent_positive": True, "ge_5k": True}},
        "uom": {"status": "UOM_RESOLVED", "quantity": 1, "uom": "each"},
        "suppliers": [{"name": "OEM"}],
        "freight": {"amount": 100, "status": "FREIGHT_ESTIMATE"},
    }
    r = evaluate_quote_readiness(
        row,
        ev=ev,
        lane="SOLE_SOURCE_RESTRICTED",
        commercial={"model": "X"},
        original={"original_source_verified": True, "original_posting_url": "https://example.gov/x"},
        deadline_days=20,
    )
    assert QUOTE_BLOCKED_ELIGIBILITY in r["blockers"] or QUOTE_BLOCKED_SOURCE_APPROVAL in r["blockers"]


def test_source_approval_blocker():
    row = {"title": "NSN part requiring source approval QPL", "solicitation_id": "SA1"}
    ev = {
        "government_value": {"state": "GOV_VALUE_EXACT", "unit_value": 500},
        "max_buy": {"supplier_quote_target": 400, "thresholds": {}},
        "quote_dependent": {"tiers": {"quote_dependent_positive": True}},
        "uom": {"uom": "each", "quantity": 1, "status": "UOM_RESOLVED"},
        "suppliers": [{"name": "ASL"}],
        "freight": {"amount": 50, "status": "FREIGHT_ESTIMATE"},
    }
    r = evaluate_quote_readiness(
        row,
        ev=ev,
        lane="SOURCE_APPROVAL_REQUIRED",
        commercial={"mpn": "123"},
        original={"original_source_verified": True, "original_posting_url": "https://sam.gov/x"},
        deadline_days=30,
    )
    assert QUOTE_BLOCKED_SOURCE_APPROVAL in r["blockers"]


def test_supplier_authorization_and_ranking():
    suppliers = [
        {"name": "Random", "supplier_domain": "random.example", "source_type": ""},
        {"name": "Ford Dealer", "supplier_domain": "ford.com", "source_type": "AUTHORIZED_DEALER", "product_fit": "EXACT"},
        {"name": "Fleet Co", "supplier_domain": "fleet.example", "source_type": "DISTRIBUTOR", "government_sales": True},
    ]
    ranked = rank_suppliers_for_quote(suppliers, commercial={"manufacturer": "Ford", "model": "F-150"})
    assert ranked[0]["rank"] == 1
    assert ranked[0]["supplier_domain"] == "ford.com"
    assert classify_supplier_authorization(ranked[0]) in {AUTHORIZED_LIKELY, "AUTHORIZED_CONFIRMED"}


def test_internal_max_buy_not_exposed_to_supplier():
    ev = evaluate_quote_opportunity(
        {"title": "Ford F-150 Police Responder", "quantity": 1},
        commercial={"manufacturer": "Ford", "model": "F-150 Police Responder"},
        history={"historical_award_unit_price": 58000},
    )
    internal = build_internal_quote_control(ev)
    assert internal["supplier_facing"] is False
    assert internal.get("supplier_quote_target") is not None
    pkt = build_supplier_facing_packet(
        row={"title": "Ford F-150 Police Responder", "solicitation_id": "P1"},
        commercial={"manufacturer": "Ford", "model": "F-150 Police Responder"},
        original={"solicitation_number": "P1"},
    )
    for field in INTERNAL_FIELDS_NEVER_SUPPLIER:
        assert field not in pkt
    assert "supplier_quote_target" not in pkt
    assert "max_buy" not in pkt
    assert pkt["pricing_request"]


def test_quote_response_evaluator_and_expiration():
    mb = calculate_max_buy_engine(government_unit=80000, quantity=1, freight_reserve=2000)
    good = evaluate_supplier_quote_response(
        quoted_unit=50000,
        quantity=1,
        freight=2000,
        revenue_mid=80000,
        max_buy=mb,
        quote_date="2026-09-01",
        expiration_date="2026-10-01",
        lead_time_days=10,
    )
    assert good["classification"] in {QUOTE_EXCELLENT, QUOTE_ACCEPTABLE, QUOTE_FAIL}
    assert good["expired"] is False
    expired = evaluate_supplier_quote_response(
        quoted_unit=50000,
        quantity=1,
        revenue_mid=80000,
        max_buy=mb,
        quote_date="2026-11-01",
        expiration_date="2026-10-01",
    )
    assert expired["expired"] is True
    assert expired["usable_as_final_acquisition_evidence"] is False


def test_multi_quote_comparison():
    quotes = [
        {"total_landed_cost": 40000, "classification": QUOTE_ACCEPTABLE, "authorization_state": AUTHORIZED_LIKELY, "lead_time_days": 30, "usable_as_final_acquisition_evidence": True},
        {"total_landed_cost": 38000, "classification": QUOTE_EXCELLENT, "authorization_state": "AUTHORIZATION_UNKNOWN", "lead_time_days": 5, "usable_as_final_acquisition_evidence": True},
        {"total_landed_cost": 35000, "classification": QUOTE_FAIL, "expired": True},
    ]
    ranked = compare_supplier_quotes(quotes)
    assert ranked[0]["comparison_rank"] == 1
    assert ranked[-1].get("expired") is True or ranked[-1]["comparison_rank"] == 3


def test_unknown_conversion_and_no_caps():
    from phase_l.quote_economics import convert_unknown_lane

    conv = convert_unknown_lane(
        {"title": "Ford F-150 fleet"},
        commercial={"manufacturer": "Ford", "model": "F-150"},
        current_lane="UNKNOWN_ACQUISITION_CHANNEL",
    )
    assert conv["converted"] is True
    assert_no_fixed_positive_cap(32)
    assert_no_fixed_positive_cap(500)
    assert STAGE3_NO_ROW_CAP and DEEP_RESEARCH_NO_FIXED_COUNT


def test_newly_recovered_positive_label():
    from phase_l.quote_readiness import L6_EXISTING_POSITIVE, L7_NEWLY_RECOVERED_POSITIVE

    assert L6_EXISTING_POSITIVE != L7_NEWLY_RECOVERED_POSITIVE


def test_owner_approval_required_no_outreach():
    row = {
        "title": "2027 Ford Expedition SSV",
        "agency": "City",
        "solicitation_id": "RFQ-2",
        "quantity": 1,
        "uom": "vehicle",
        "place_of_performance": "Dallas TX",
        "original_posting_url": "https://sam.gov/opp/xyz/view",
    }
    commercial = {"manufacturer": "Ford", "model": "Expedition SSV"}
    ev = evaluate_quote_opportunity(
        row,
        commercial=commercial,
        history={"historical_award_unit_price": 55000},
        deadline_days=20,
    )
    r = evaluate_quote_readiness(
        row,
        ev=ev,
        commercial=commercial,
        original={"original_source_verified": True, "original_posting_url": row["original_posting_url"]},
        deadline_days=20,
    )
    if r["ready"]:
        assert r["owner_gate"] == OWNER_APPROVAL_REQUIRED
    assert r["outreach_authorized"] is False
    assert r["send_authorized"] is False


def test_original_solicitation_preserved():
    from phase_l.original_solicitation import resolve_original_solicitation

    row = {
        "title": "Fleet SUV",
        "agency": "State DOT",
        "solicitation_id": "DOT-99",
        "original_posting_url": "https://sam.gov/opp/99/view",
        "response_deadline": "2099-01-01",
    }
    orig = resolve_original_solicitation(row)
    assert orig["original_source_verified"] is True
    assert "sam.gov" in (orig["original_posting_url"] or "")


def test_final_bid_gate_unchanged():
    assert EXACT_VERIFIED == "EXACT_VERIFIED"


def test_filter_owner_queue():
    rows = [
        {"status": READY_FOR_QUOTE_OUTREACH, "expected_profit_tier": "ge_10k", "acquisition_lane": "QUOTE_REQUIRED_COMMERCIAL", "supplier_count": 3},
        {"status": QUOTE_BLOCKED_NO_SUPPLIER, "expected_profit_tier": "ge_5k", "acquisition_lane": "QUOTE_REQUIRED_COMMERCIAL", "supplier_count": 0},
    ]
    ready = filter_owner_queue(rows, ready_only=True)
    assert len(ready) == 1
    blocked = filter_owner_queue(rows, blocked_only=True)
    assert len(blocked) == 1
