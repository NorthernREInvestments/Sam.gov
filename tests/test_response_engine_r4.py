"""R4 response generation tests — fail-closed, no auto-sign, 0 SAM, firewall = 0 leaks."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from response_engine.document_generators import (
    generate_quote_letter,
    generate_technical_matrix,
    generate_technical_narrative_draft,
    scan_text_for_leaks,
)
from response_engine.field_map import build_field_maps, summarize_field_maps
from response_engine.models import new_response_project
from response_engine.r4_constants import (
    OWNER_SIGNATURE_REQUIRED_FIELD,
    READY_FOR_R5_PREFLIGHT,
)
from response_engine.response_plan import build_response_plan
from response_engine.spreadsheet_fill import (
    create_pricing_schedule_xlsx,
    populate_xlsx_from_maps,
    sha256_bytes,
    validate_extended_totals,
)


@pytest.fixture
def rp_store(tmp_path, monkeypatch):
    from response_engine import package_store, store

    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "rp")
    monkeypatch.setattr(store, "INDEX_PATH", store.STORE_DIR / "index.json")
    monkeypatch.setattr(package_store, "GENERATED_ROOT", tmp_path / "rp")
    store.ensure_store()
    return tmp_path


def _fixture_project(**extra):
    p = new_response_project(canonical_opportunity_id="r4-fix-1", title="RFQ Widgets")
    p["solicitation_number"] = "RFQ-001"
    p["buyer"] = "Test Agency"
    p["response_type"] = "RFQ"
    p["documents"] = [
        {
            "document_id": "D1",
            "filename": "solicitation.pdf",
            "text": "Request for Quote. Brand or equal. Submit price and product schedule.",
            "document_type": "SOLICITATION",
        }
    ]
    p["requirements"] = [
        {
            "requirement_id": "R1",
            "requirement_text": "Provide unit prices for each CLIN",
            "requirement_category": "PRICE",
            "mandatory": True,
        }
    ]
    p["line_items"] = [
        {
            "line_item_id": "L1",
            "CLIN": "0001",
            "buyer_line_number": "0001",
            "description": "Widget",
            "quantity": "10",
            "buyer_uom": "EA",
            "normalized_quantity": "10",
            "normalized_uom": "EA",
            "template_map": {
                "kind": "BuyerPricingFieldMap",
                "sheet": "Pricing",
                "unit_price_cell": "D12",
                "template_document_id": "T1",
            },
            "bid_unit_price": "100.00",
        }
    ]
    p["offered_products"] = [
        {
            "offered_product_id": "P1",
            "line_item_id": "L1",
            "selected": True,
            "manufacturer": "Acme",
            "MPN": "W-1",
            "country_of_origin": "US",
        }
    ]
    p["technical_compliance_items"] = [
        {
            "requirement_id": "T1",
            "requirement_text": "Must be brand or equal to Acme W-1",
            "offered_mpn": "W-1",
            "offered_value": "W-1",
            "status": "PASS_VERIFIED",
            "evidence_id": "EV1",
        },
        {
            "requirement_id": "T2",
            "requirement_text": "Warranty 5 years",
            "status": "UNKNOWN",
        },
    ]
    p["pricing_scenarios"] = [
        {
            "scenario_id": "S1",
            "scenario_status": "ACTIVE",
            "approved_for_r4_draft": True,
            "selected_for_draft": True,
            "total_bid_price": "1000.00",
            "line_unit_prices": {"L1": "100.00"},
        }
    ]
    p["selected_bid_price_scenario_id"] = "S1"
    p["deliverables"] = []
    p.update(extra)
    return p


# --- Response plan ---
def test_response_plan_from_r1():
    p = _fixture_project()
    plan = build_response_plan(p)
    assert plan["kind"] == "ResponsePlan"
    types = {d["deliverable_type"] for d in plan["deliverables"]}
    assert "GENERATED_QUOTE_LETTER" in types
    assert "GENERATED_PRODUCT_SCHEDULE" in types


# --- Field mapping / missing-field blocking ---
def test_field_map_blocks_unknown_cage_when_federal():
    p = _fixture_project(jurisdiction="federal", submission_system="DIBBS")
    maps = build_field_maps(p, profile={"legal_name": "Acme", "UEI": "UNKNOWN", "CAGE": "UNKNOWN"})
    cage = [m for m in maps if m.get("source_requirement") == "company.CAGE"][0]
    assert cage["generation_status"] == "BLOCKED"
    assert "CAGE" in (cage.get("source_value") or "CAGE")


def test_field_map_no_invented_na():
    p = _fixture_project()
    maps = build_field_maps(p, profile={"legal_name": "UNKNOWN"})
    name = [m for m in maps if m.get("source_requirement") == "company.legal_name"][0]
    assert name["generation_status"] != "POPULATED"
    assert name.get("source_value") != "N/A"


def test_owner_attestation_gates_cert_field():
    p = _fixture_project(
        owner_attestations=[
            {
                "attestation_id": "A1",
                "topic": "section_889",
                "question": "Covered telecom?",
                "owner_confirmed": False,
            }
        ]
    )
    maps = build_field_maps(p, profile={"legal_name": "Acme LLC", "UEI": "ABC", "CAGE": "1AAA1"})
    att = [m for m in maps if m.get("generation_status") == "OWNER_ATTESTATION_REQUIRED"]
    assert att
    sig = [m for m in maps if m.get("generation_status") == OWNER_SIGNATURE_REQUIRED_FIELD]
    assert sig


# --- Spreadsheet ---
def test_xlsx_population_preserves_formula_and_original(tmp_path):
    import openpyxl

    src = tmp_path / "buyer.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Pricing"
    ws["D12"] = None
    ws["E12"] = "=D12*10"
    ws["A1"] = "Keep"
    wb.save(src)
    original_hash = hashlib.sha256(src.read_bytes()).hexdigest()

    maps = [
        {
            "field_map_id": "F1",
            "generation_status": "READY",
            "target_page_or_sheet": "Pricing",
            "target_field_or_cell": "D12",
            "target_type": "CURRENCY",
            "source_value": "100",
        },
        {
            "field_map_id": "F2",
            "generation_status": "READY",
            "target_page_or_sheet": "Pricing",
            "target_field_or_cell": "E12",
            "target_type": "CURRENCY",
            "source_value": "999",  # must NOT overwrite formula
        },
    ]
    out = tmp_path / "out.xlsx"
    result = populate_xlsx_from_maps(original_path=src, original_bytes=None, field_maps=maps, out_path=out)
    assert result["ok"]
    assert result["original_hash"] == original_hash
    assert hashlib.sha256(src.read_bytes()).hexdigest() == original_hash
    wb2 = openpyxl.load_workbook(out)
    assert wb2["Pricing"]["D12"].value == 100
    assert str(wb2["Pricing"]["E12"].value).startswith("=")
    assert result["integrity_ok"]


def test_xlsm_manual_completion(tmp_path):
    src = tmp_path / "macro.xlsm"
    src.write_bytes(b"fake-xlsm-bytes")
    out = tmp_path / "out.xlsm"
    result = populate_xlsx_from_maps(
        original_path=src,
        original_bytes=None,
        field_maps=[],
        out_path=out,
        is_xlsm=True,
    )
    assert result["status"] == "MACRO_TEMPLATE_MANUAL_COMPLETION_REQUIRED"
    assert out.read_bytes() == b"fake-xlsm-bytes"


def test_extended_total_validation():
    lines = [{"line_item_id": "L1", "quantity": "10", "extended_price": "999"}]
    issues = validate_extended_totals(lines, {"L1": "100"})
    assert issues and issues[0]["status"] == "TOTAL_MISMATCH"


# --- Quote / narrative / matrix ---
def test_quote_letter_no_auto_sign(tmp_path):
    p = _fixture_project()
    maps = build_field_maps(
        p,
        profile={"legal_name": "Acme LLC", "UEI": "UEI1", "CAGE": "CAGE1"},
        selected_scenario=p["pricing_scenarios"][0],
    )
    out = tmp_path / "quote.txt"
    r = generate_quote_letter(p, out_path=out, field_maps=maps, profile={"legal_name": "Acme LLC", "UEI": "UEI1", "CAGE": "CAGE1"})
    text = out.read_text(encoding="utf-8")
    assert "OWNER_SIGNATURE_REQUIRED" in text
    assert "/s/" not in text
    assert "DRAFT — NOT SUBMITTED" in text
    assert r["auto_signed"] if "auto_signed" in r else r["signature_status"] == OWNER_SIGNATURE_REQUIRED_FIELD
    assert not r["leaks"]


def test_technical_matrix_unknown_not_complies(tmp_path):
    p = _fixture_project()
    out = tmp_path / "matrix.csv"
    r = generate_technical_matrix(p, out_path=out)
    text = out.read_text(encoding="utf-8")
    assert "UNKNOWN" in text
    assert "Complies" not in text
    assert r["unresolved"] >= 1


def test_narrative_no_fabricated_claims(tmp_path):
    p = _fixture_project()
    out = tmp_path / "tech.txt"
    r = generate_technical_narrative_draft(p, out_path=out)
    text = out.read_text(encoding="utf-8").lower()
    assert "fully compliant" not in text
    assert r["draft_review_required"] is True
    assert r["traces"]


def test_firewall_scan_detects_max_buy():
    leaks = scan_text_for_leaks("Our max_buy is 50 and target_profit is 10")
    assert "max_buy" in leaks
    assert "target_profit" in leaks


# --- R4 service integration ---
def test_r4_generation_draft_incomplete_without_full_company(rp_store):
    from response_engine.r4_service import run_r4_generation

    p = _fixture_project()
    pkg = run_r4_generation(p, persist=True, force=True)
    assert pkg["kind"] == "GeneratedResponsePackage"
    assert pkg["never_ready_to_submit"] is True
    assert pkg["package_status"] != "READY_TO_SUBMIT"
    assert pkg["label"] == "DRAFT — NOT SUBMITTED"
    assert len(pkg["generated_documents"]) >= 1
    assert p.get("submission_handoff", {}).get("executed") is False


def test_r4_price_change_invalidates(rp_store):
    from response_engine.r4_service import invalidate_r4, run_r4_generation, select_bid_price_scenario

    p = _fixture_project()
    run_r4_generation(p, persist=True)
    p["pricing_scenarios"].append(
        {
            "scenario_id": "S2",
            "scenario_status": "ACTIVE",
            "total_bid_price": "87500",
            "line_unit_prices": {"L1": "87.50"},
        }
    )
    select_bid_price_scenario(p, "S2")
    assert p["r4_package_status"] == "STALE_DUE_TO_PRICE_CHANGE"
    pkg2 = run_r4_generation(p, persist=True, force=True)
    assert pkg2["generation_version"] >= 2


def test_r4_amendment_invalidation(rp_store):
    from response_engine.r4_service import invalidate_r4, run_r4_generation

    p = _fixture_project()
    run_r4_generation(p, persist=True)
    invalidate_r4(p, reason="STALE_DUE_TO_AMENDMENT")
    assert p["generated_package"]["stale"] is True


def test_r4_product_change_invalidation(rp_store):
    from response_engine.r4_service import invalidate_r4, run_r4_generation

    p = _fixture_project()
    run_r4_generation(p, persist=True)
    invalidate_r4(p, reason="STALE_DUE_TO_PRODUCT_CHANGE")
    assert "PRODUCT" in p["r4_package_status"]


def test_r4_company_change_invalidation(rp_store):
    from response_engine.r4_service import invalidate_r4, run_r4_generation

    p = _fixture_project()
    run_r4_generation(p, persist=True)
    invalidate_r4(p, reason="STALE_DUE_TO_COMPANY_PROFILE_CHANGE")
    assert "COMPANY" in p["r4_package_status"]


def test_r4_package_zip_excludes_internal(rp_store):
    from response_engine.r4_service import run_r4_generation

    p = _fixture_project()
    # Inject internal economics that must not leak into buyer docs
    p["pricing_scenarios"][0]["internal_max_buy"] = "50"
    p["pricing_scenarios"][0]["target_margin"] = "0.4"
    pkg = run_r4_generation(p, persist=True)
    fw = p.get("r4_firewall") or {}
    assert fw.get("ok") is True or len(fw.get("leaks") or []) == 0
    # ZIP should exist and not include supplier_quote filenames
    z = pkg.get("zip") or {}
    assert z.get("zip_path")
    assert all("supplier_quote" not in f.lower() for f in (z.get("files") or []))


def test_r4_handoff_not_executed(rp_store):
    from response_engine.r4_service import run_r4_generation

    p = _fixture_project()
    run_r4_generation(p, persist=True)
    ho = p["submission_handoff"]
    assert ho["kind"] == "SubmissionHandoff"
    assert ho["executed"] is False


def test_r4_conflict_detection(rp_store):
    from response_engine.r4_service import run_r4_generation

    p = _fixture_project(
        verified_delivery_days="30",
        manual_response_overrides=[{"field": "delivery_days", "value": "15", "reason": "test"}],
    )
    pkg = run_r4_generation(p, persist=True)
    assert any(c.get("type") == "RESPONSE_DATA_CONFLICT" for c in (pkg.get("conflicts") or []))


def test_r4_no_ready_to_submit(rp_store):
    from response_engine.r4_service import run_r4_generation

    p = _fixture_project()
    pkg = run_r4_generation(p, persist=True)
    assert pkg["package_status"] != "READY_TO_SUBMIT"
    assert READY_FOR_R5_PREFLIGHT != "READY_TO_SUBMIT"


def test_pricing_schedule_leaves_unknown_blank(tmp_path):
    lines = [{"line_item_id": "L1", "CLIN": "0001", "quantity": "5", "buyer_uom": "EA"}]
    out = tmp_path / "price.xlsx"
    create_pricing_schedule_xlsx(lines=lines, field_maps=[], out_path=out)
    import openpyxl

    wb = openpyxl.load_workbook(out)
    # Unit price cell should be empty (not 0, not N/A)
    assert wb.active.cell(2, 5).value is None
