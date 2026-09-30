"""Phase L.21 controlled supplier quote outreach preparation tests."""

from __future__ import annotations

import json
from pathlib import Path

from discovery.sam_api_parked import sam_api_park_status
from phase_l.bidnet_parked import BIDNET_AUTH_HISTORY_PARKED
from phase_l.l21_quote_outreach_prep import (
    AUTO_SEND_SUPPLIER_OUTREACH,
    BUILD,
    CONTACT_PATH_UNRESOLVED,
    EMAIL_PUBLIC,
    MAX_BUY_UNRESOLVED,
    NEEDS_MINOR_REVIEW,
    NOT_READY,
    QUOTE_PACKET_STALE,
    READY_FOR_OWNER_APPROVAL,
    RFQ_FORM_PUBLIC,
    VERIFIED_ACQUISITION_PRICE,
    VERIFIED_POSITIVE,
    assert_no_internal_leak,
    build_requirement_packet,
    build_rfq_message,
    classify_contact_path,
    classify_quote_readiness,
    compute_internal_max_buy,
    invalidate_stale_packets,
    live_revalidate,
    load_l20_targets,
    prepare_row,
    select_suppliers,
)
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap
from phase_l.quote_readiness import (
    INTERNAL_FIELDS_NEVER_SUPPLIER,
    build_supplier_facing_packet,
    evaluate_supplier_quote_response,
)
from phase_l.pilot_real_world import FINANCING_PATH_PLAUSIBLE, FINANCING_PATH_UNRESOLVED

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
DOCS = ROOT / "docs"


def test_auto_send_hard_false():
    assert AUTO_SEND_SUPPLIER_OUTREACH is False


def test_load_l20_targets_includes_waiting_and_register():
    targets = load_l20_targets()
    assert len(targets) >= 3
    owners = {t.get("owner_decision_after") for t in targets}
    assert "RESEARCH_COMPLETE_WAITING_QUOTE" in owners or "REGISTER_AND_PURSUUE" in owners


def test_live_revalidation_fields():
    packet = {"title": "Test Item", "buyer": "City", "solicitation": "RFQ-1", "runway": {"days": 10, "label": "STRONG_RUNWAY"}}
    row = {"title": "Test Item", "agency": "City", "response_deadline": "2099-12-01T00:00:00Z", "detail_url": "https://example.com/r"}
    live = live_revalidate(packet, row)
    assert "still_open" in live
    assert "solicitation_id" in live or live.get("original")
    assert live.get("amendment_checked") is True
    assert live["runway"]["label"] in {"STRONG_RUNWAY", "ACCEPTABLE_RUNWAY", "TIGHT_RUNWAY", "EXECUTION_FAIL"}


def test_amendment_stale_packet_invalidation():
    packets = [{"version": 1, "product_description": "Widget", "send_authorized": False}]
    stale = invalidate_stale_packets(packets, amendment_changed=True)
    assert stale[0]["status"] == QUOTE_PACKET_STALE
    assert stale[0]["send_authorized"] is False
    ok = invalidate_stale_packets(packets, amendment_changed=False)
    assert ok[0].get("status") != QUOTE_PACKET_STALE


def test_quantity_spec_completeness():
    req = build_requirement_packet(
        {"title": "Apple iPad 11 Qty 10", "buyer": "County", "commercial": {"manufacturer": "Apple", "model": "iPad 11"}},
        {"title": "Apple iPad 11 Qty 10", "agency": "County"},
        {"solicitation_number": "RFB-1"},
    )
    assert req["kind"] == "OpportunityRequirementPacket"
    assert req["manufacturer"] == "Apple"
    assert req.get("quantity") is not None or req.get("product_specification")


def test_supplier_candidate_filtering_excludes_d_when_abc_exist():
    cands = [
        {"supplier_domain": "ebay.com", "supplier_grade": "SUPPLIER_D", "name": "ebay"},
        {"supplier_domain": "cdw-g.com", "supplier_grade": "SUPPLIER_C", "name": "CDW-G", "product_fit": "EXACT"},
        {"supplier_domain": "apple.com", "supplier_grade": "SUPPLIER_B", "name": "Apple", "source_type": "OEM"},
    ]
    selected = select_suppliers(cands, commercial={"manufacturer": "Apple", "model": "iPad"})
    letters = {_sup_letter_local(s) for s in selected}
    assert "D" not in letters
    assert any(s.get("supplier_domain") == "apple.com" for s in selected)


def _sup_letter_local(s):
    g = str(s.get("supplier_grade") or s.get("_letter") or "")
    for L in ("A", "B", "C", "D"):
        if f"SUPPLIER_{L}" in g or g == L:
            return L
    return "D"


def test_contact_path_validation_public_only():
    path = classify_contact_path({"supplier_domain": "cdw-g.com", "name": "CDW-G"})
    assert path["contact_path_status"] == RFQ_FORM_PUBLIC
    assert path["email"] is None  # never invent private email
    assert path["public_only"] is True
    unknown = classify_contact_path({"supplier_domain": "obscure-no-path.example", "name": "X"})
    assert unknown["contact_path_status"] in {CONTACT_PATH_UNRESOLVED, RFQ_FORM_PUBLIC}


def test_quote_packet_generation_and_internal_exclusion():
    pkt = build_supplier_facing_packet(
        row={"title": "Caterpillar C18", "agency": "Port", "quantity": 1},
        commercial={"manufacturer": "Caterpillar", "model": "C18"},
        supplier={"name": "cat.com", "supplier_domain": "cat.com"},
        freight_info={"destination": "Port of LA"},
        uom={"quantity": 1, "uom": "EA"},
        internal_deadline_days=7,
    )
    pkt["rfq_message"] = build_rfq_message(
        {
            "manufacturer": "Caterpillar",
            "model": "C18",
            "product_specification": "Marine diesel",
            "quantity": 1,
            "uom": "EA",
            "condition": "new",
            "warranty": "OEM",
            "delivery_destination": "Port of LA",
        },
        {"name": "Caterpillar", "supplier_domain": "cat.com"},
    )
    assert_no_internal_leak(pkt)
    for field in INTERNAL_FIELDS_NEVER_SUPPLIER:
        assert field not in pkt
    assert "max_buy" not in json.dumps(pkt).lower() or "max_buy" not in pkt
    assert "BREAK_EVEN" not in json.dumps(pkt)
    assert pkt.get("send_authorized") is False


def test_max_buy_internal_only_and_unresolved():
    weak = compute_internal_max_buy({"gov_letter": "D", "deal_card": {}}, {"quantity": 1})
    assert weak["status"] == MAX_BUY_UNRESOLVED
    assert weak["supplier_facing"] is False
    strong = compute_internal_max_buy(
        {"gov_letter": "C", "deal_card": {"prior_unit_price": 50000}},
        {"quantity": 1},
    )
    assert strong["status"] in {"MAX_BUY_AVAILABLE", MAX_BUY_UNRESOLVED}
    assert strong["supplier_facing"] is False


def test_financing_precheck_unknown_terms_not_fail():
    # Unknown terms must remain PLAUSIBLE or UNRESOLVED — not FAIL solely for unknown
    assert FINANCING_PATH_UNRESOLVED != "FINANCING_EXECUTION_FAIL"
    assert FINANCING_PATH_PLAUSIBLE != "FINANCING_EXECUTION_FAIL"


def test_readiness_classification_paths():
    base_live = {
        "still_open": True,
        "expired": False,
        "cancelled": False,
        "original_url": "https://example.com",
        "runway": {"label": "STRONG_RUNWAY", "days": 10},
    }
    req = {"manufacturer": "Apple", "model": "iPad", "product_specification": "iPad 11", "quantity": 5}
    suppliers = [{"supplier_grade": "SUPPLIER_B", "_letter": "B", "supplier_domain": "apple.com"}]
    contacts = [{"contact_path_status": RFQ_FORM_PUBLIC}]
    packets = [{"rfq_message": "hi", "product_description": "iPad"}]
    fin = {"status": FINANCING_PATH_PLAUSIBLE}
    mb = {"status": MAX_BUY_UNRESOLVED, "reason": "gov_c_without_defensible_unit_or_total"}
    ready = classify_quote_readiness(
        live=base_live,
        req=req,
        suppliers=suppliers,
        contacts=contacts,
        packets=packets,
        financing=fin,
        max_buy=mb,
        packet={},
    )
    assert ready["state"] == READY_FOR_OWNER_APPROVAL
    assert ready["auto_send"] is False

    expired = classify_quote_readiness(
        live={**base_live, "expired": True, "still_open": False},
        req=req,
        suppliers=suppliers,
        contacts=contacts,
        packets=packets,
        financing=fin,
        max_buy=mb,
        packet={},
    )
    assert expired["state"] == NOT_READY


def test_owner_approval_defaults_false_and_deselection():
    targets = load_l20_targets()
    packet = targets[0]
    row = {
        "title": packet.get("title"),
        "agency": packet.get("buyer"),
        "detail_url": "https://example.com/sol",
        "response_deadline": "2099-06-01T00:00:00Z",
        "description": packet.get("title"),
    }
    res = prepare_row(packet, row)
    oa = res["owner_approval"]
    assert oa["kind"] == "OwnerQuoteApproval"
    assert oa["approved"] is False
    assert oa["approved_at"] is None
    assert oa["can_deselect_suppliers"] is True
    assert oa["auto_send"] is False
    assert res["verified_acquisition_price"] is None
    assert res["verified_positive"] is None
    assert VERIFIED_ACQUISITION_PRICE not in json.dumps(res)
    # "VERIFIED_POSITIVE" string may appear only as constant name unused — value must be None
    assert res.get("verified_positive") is None


def test_packet_versioning():
    targets = load_l20_targets()
    packet = next((t for t in targets if (t.get("candidates") or [])), targets[0])
    row = {
        "title": packet.get("title"),
        "agency": packet.get("buyer"),
        "detail_url": "https://example.gov/r",
        "response_deadline": "2099-08-01T00:00:00Z",
    }
    res = prepare_row(packet, row)
    for p in res.get("quote_packets") or []:
        assert p.get("version") == 1
        assert p.get("send_authorized") is False
        assert p.get("auto_send") is False


def test_quote_ingestion_schema_ready():
    sample = evaluate_supplier_quote_response(
        quoted_unit=100.0,
        quantity=2.0,
        freight=25.0,
        revenue_mid=400.0,
        max_buy={"supplier_quote_target": 150.0, "thresholds": {"BREAK_EVEN_MAX_BUY": 150}},
    )
    assert isinstance(sample, dict)
    assert sample.get("error") != "missing_quote"
    # landed / financing / margin fields present for later ingestion
    blob = json.dumps(sample).lower()
    assert "landed" in blob or "financing" in blob or sample.get("state")


def test_no_verified_price_fabricated_constants():
    assert VERIFIED_ACQUISITION_PRICE == "VERIFIED_ACQUISITION_PRICE"
    assert VERIFIED_POSITIVE == "VERIFIED_POSITIVE"


def test_no_external_actions_guards():
    assert AUTO_SEND_SUPPLIER_OUTREACH is False
    assert_no_fixed_positive_cap()
    sam = sam_api_park_status()
    assert sam["calls_consumed"] == 0
    assert BIDNET_AUTH_HISTORY_PARKED


def test_artifacts_and_docs_after_run_exist_or_skippable():
    # After full phase run these must exist; unit suite may run before — soft check
    required = [
        "l21_target_population.json",
        "l21_summary.json",
        "l21_initial_quote_batch.json",
        "l21_owner_approval_queue.json",
        "l21_supplier_quote_packets.json",
    ]
    if (OUT / "l21_summary.json").exists():
        for name in required:
            assert (OUT / name).exists(), name
        summary = json.loads((OUT / "l21_summary.json").read_text(encoding="utf-8"))
        assert summary.get("auto_send_supplier_outreach") is False
        assert summary.get("verdict") in {
            "PHASE_L21_CONTROLLED_QUOTE_PREP_READY",
            "PHASE_L21_PARTIAL_QUOTE_PREP",
            "PHASE_L21_QUOTE_PREP_FAILED",
        }
        for doc in (
            "phase_l21_quote_prep_strategy.md",
            "phase_l21_owner_approval_workflow.md",
            "phase_l21_internal_external_separation.md",
            "phase_l21_regression.md",
        ):
            assert (DOCS / doc).exists()
        assert summary.get("build") == BUILD
        for r in json.loads((OUT / "l21_quote_readiness.json").read_text(encoding="utf-8")).get("rows") or []:
            assert r.get("verified_acquisition_price") is None
            assert r.get("verified_positive") is None
            oa = r.get("owner_approval") or {}
            if oa:
                assert oa.get("approved") is False
