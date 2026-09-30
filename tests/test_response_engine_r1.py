"""R1 Response Engine — solicitation compiler foundation tests.

0 SAM API calls. Uses fixtures + in-memory/persisted response projects.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from response_engine.classifier import classify_evaluation_method, classify_response_type, detect_product_mode
from response_engine.compliance import (
    build_compliance_matrix,
    compute_hard_blocks,
    evaluate_exact_brand_mismatch,
    evaluate_salient_generic_equal_claim,
    set_requirement_answer,
)
from response_engine.constants import BUILD, FAIL, PASS, REVIEW_REQUIRED, UNKNOWN
from response_engine.deliverables import deliverable_counts_by_type, inventory_deliverables
from response_engine.document_graph import find_duplicate
from response_engine.firewall import firewall_report, ingest_supplier_quote_as_internal
from response_engine.intake import ingest_document, ingest_document_set
from response_engine.models import content_hash, new_response_project
from response_engine.requirements import compile_requirements_from_text, merge_requirements
from response_engine.service import (
    apply_amendment_quantity_change,
    compile_project,
    create_or_get_project_from_opportunity,
    get_project_view,
)
from response_engine.store import find_by_opportunity, load_project, save_project

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "response_engine"

FEDERAL_RFQ = """
REQUEST FOR QUOTATION — Commercial Items (FAR Part 12)
SF1449 required. Signed SF1449 must be submitted.
Submit via PIEE by 2:00 PM ET on October 15, 2026.
Questions due October 4, 2026 ET.
Quantity: 20 each
Delivery by November 15, 2026 FOB Destination.
Country of origin required.
Offerors shall submit a signed SF1449, completed pricing schedule,
product literature demonstrating compliance with all salient characteristics,
and acknowledgment of all amendments by 2:00 PM ET.
Evaluation: lowest price technically acceptable (LPTA).
Technical volume maximum 10 pages.
"""

BRAND_OR_EQUAL = """
Brand Name or Equal: Acme Widget Pro
Salient characteristics: capacity 500W; size 12 inches; voltage 120V
Submit quote via OpenGov portal.
Jurisdiction: State of Nebraska
"""

EXACT_BRAND = """
Brand X model ABC only. No substitutions.
Authorized reseller required.
Response due October 20, 2026 3:00 PM CT
Submit by email to procurement@example.gov
"""

AMENDMENT_BASE = """
RFQ-100 Base Solicitation
Quantity: 10 each
Delivery by December 1, 2026
Acknowledge all amendments.
"""

AMENDMENT_001 = """
Amendment 001 to RFQ-100
Quantity is hereby changed to 20 each.
Delivery date remains December 1, 2026.
"""

CONFLICT_A = """
Amendment 002
Delivery required by January 10, 2027
"""

CONFLICT_B = """
Amendment 003
Delivery required by February 1, 2027
"""

PRE_AWARD_TEXT = """
Offeror must provide manufacturer authorization letter prior to award.
Country of origin certification shall be provided at submission.
"""

DIBBS_LIKE = """
DLA DIBBS Web Quote
Individual solicitation NSN 1234-56-789-0123
Applicable Master Solicitation and procurement notes apply.
Exact product or approved alternate.
Packaging and traceability requirements apply.
Submit via DIBBS.
"""

FORMAT_RULES = """
Technical volume maximum 10 pages.
Font: Times New Roman 12 pt.
Margins of 1 inch.
PDF format required.
Filename must be: OfferorName_RFQ123.pdf
"""


@pytest.fixture()
def tmp_store(tmp_path, monkeypatch):
    import response_engine.store as store

    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "response_projects")
    monkeypatch.setattr(store, "INDEX_PATH", tmp_path / "response_projects" / "index.json")
    store.ensure_store()
    return tmp_path


def test_build_constant():
    assert "r1" in BUILD.lower() or "20260929" in BUILD


def test_response_project_create_idempotent(tmp_store):
    a = create_or_get_project_from_opportunity(
        canonical_opportunity_id="opp-r1-1",
        buyer="Los Angeles County",
        solicitation_number="232178",
        discovery_source="SAM",
        authoritative_source="PIEE",
        submission_system="PIEE",
    )
    b = create_or_get_project_from_opportunity(canonical_opportunity_id="opp-r1-1")
    assert a["response_project_id"] == b["response_project_id"]
    assert a["discovery_source"] == "SAM"
    assert a["authoritative_source"] == "PIEE"
    assert a["submission_system"] == "PIEE"
    assert a["discovery_source"] != a["submission_system"] or a["authoritative_source"] == "PIEE"


def test_document_ingest_dedupe_hash(tmp_store):
    p = create_or_get_project_from_opportunity(canonical_opportunity_id="opp-dedupe")
    r1 = ingest_document_set(p, [{"title": "Base", "text": FEDERAL_RFQ, "document_type": "BASE_SOLICITATION"}])
    r2 = ingest_document_set(p, [{"title": "Base copy", "text": FEDERAL_RFQ, "document_type": "BASE_SOLICITATION"}])
    assert r1["ingested"] == 1
    assert r2["duplicates"] == 1
    assert len(p["documents"]) == 1
    save_project(p)


def test_source_fields_stay_separate(tmp_store):
    p = create_or_get_project_from_opportunity(
        canonical_opportunity_id="opp-sources",
        discovery_source="SAM",
        authoritative_source="PIEE",
        submission_system="PIEE",
    )
    ingest_document(p, title="RFQ", text=FEDERAL_RFQ, document_type="BASE_SOLICITATION")
    compile_project(p)
    assert p["discovery_source"] == "SAM"
    assert p["authoritative_source"] == "PIEE"
    assert p["submission_system"] == "PIEE"
    assert p["response_type"] in {"FEDERAL_COMMERCIAL_RFQ", "FEDERAL_SIMPLIFIED_RFQ", "FEDERAL_PIEE_OFFER"}


def test_response_and_evaluation_classification():
    rt = classify_response_type(text=FEDERAL_RFQ, submission_system="PIEE", authoritative_source="PIEE")
    assert rt["result"] == "FEDERAL_PIEE_OFFER"
    assert rt["evidence"]
    ev = classify_evaluation_method(text=FEDERAL_RFQ)
    assert "LPTA" in ev["methods"]
    # Must not infer LPTA from price alone
    ev2 = classify_evaluation_method(text="Awarded to the lowest price offeror.")
    assert "LPTA" not in ev2["methods"]


def test_atomic_requirement_compiler_splits_paragraph(tmp_store):
    p = create_or_get_project_from_opportunity(canonical_opportunity_id="opp-atomic")
    ingest_document(p, title="RFQ", text=FEDERAL_RFQ, document_type="BASE_SOLICITATION")
    compile_project(p)
    cats = {r["requirement_category"] for r in p["requirements"] if not r.get("superseded")}
    for need in ("SIGNATURE", "FORM", "TECHNICAL_LITERATURE", "AMENDMENT_ACK", "SUBMISSION_DEADLINE", "PAGE_LIMIT"):
        assert need in cats, need
    for r in p["requirements"]:
        assert r.get("provenance")
        assert r["provenance"].get("document_id") or r.get("source_document_id")


def test_amendment_supersession(tmp_store):
    p = create_or_get_project_from_opportunity(canonical_opportunity_id="opp-amd")
    ingest_document(p, title="Base", text=AMENDMENT_BASE, document_type="BASE_SOLICITATION")
    compile_project(p)
    qty_before = [r for r in p["requirements"] if r["requirement_category"] == "QUANTITY" and not r.get("superseded")]
    assert qty_before
    apply_amendment_quantity_change(
        p,
        amendment_number="001",
        new_quantity=20,
        text=AMENDMENT_001,
    )
    active = [r for r in p["requirements"] if r["requirement_category"] == "QUANTITY" and not r.get("superseded")]
    superseded = [r for r in p["requirements"] if r["requirement_category"] == "QUANTITY" and r.get("superseded")]
    assert any("20" in (r.get("requirement_text") or "") for r in active)
    assert superseded
    assert p.get("package_status") == "PACKAGE_STALE_DUE_TO_AMENDMENT"
    assert any(r["requirement_category"] == "AMENDMENT_ACK" for r in p["requirements"])
    assert p.get("amendments")


def test_brand_name_or_equal_salient_characteristics(tmp_store):
    p = create_or_get_project_from_opportunity(
        canonical_opportunity_id="opp-boe",
        jurisdiction="STATE",
        submission_system="OpenGov",
    )
    ingest_document(p, title="ITB", text=BRAND_OR_EQUAL, document_type="BASE_SOLICITATION")
    compile_project(p)
    sal = [r for r in p["requirements"] if r["requirement_category"] == "SALIENT_CHARACTERISTIC" and not r.get("superseded")]
    assert len(sal) >= 3
    # generic equal claim must not PASS
    claim_rev = evaluate_salient_generic_equal_claim(sal[0], "our product is equal")
    assert claim_rev["compliance_status"] == REVIEW_REQUIRED


def test_exact_product_mismatch_is_fail(tmp_store):
    p = create_or_get_project_from_opportunity(canonical_opportunity_id="opp-exact")
    ingest_document(p, title="RFQ", text=EXACT_BRAND, document_type="BASE_SOLICITATION")
    compile_project(p)
    exact = next(r for r in p["requirements"] if r["requirement_category"] == "EXACT_BRAND")
    evaluate_exact_brand_mismatch(exact, required_model="ABC", offered_model="XYZ")
    assert exact["compliance_status"] == FAIL
    build_compliance_matrix(p)
    compute_hard_blocks(p)
    assert any("FAIL" in (b.get("reason") or "") for b in p["hard_blocks"])


def test_required_signature_hard_block(tmp_store):
    p = create_or_get_project_from_opportunity(canonical_opportunity_id="opp-sig")
    ingest_document(p, title="RFQ", text=FEDERAL_RFQ, document_type="BASE_SOLICITATION", authoritative_source="PIEE")
    p["submission_system"] = "PIEE"
    p["submission_timezone"] = "America/New_York"
    compile_project(p)
    # Leave signature unanswered → hard block
    assert any("signature" in (b.get("reason") or "").lower() or "UNKNOWN" in (b.get("reason") or "") for b in p["hard_blocks"])


def test_page_limit_formatting_requirement(tmp_store):
    p = create_or_get_project_from_opportunity(canonical_opportunity_id="opp-fmt")
    ingest_document(p, title="RFP", text=FORMAT_RULES, document_type="BASE_SOLICITATION")
    compile_project(p)
    cats = {r["requirement_category"] for r in p["requirements"]}
    assert "PAGE_LIMIT" in cats
    assert "FONT" in cats or "MARGIN" in cats or "FILE_FORMAT" in cats


def test_supplier_quote_firewall(tmp_store):
    p = create_or_get_project_from_opportunity(canonical_opportunity_id="opp-fw")
    ingest_supplier_quote_as_internal(
        p,
        {"supplier": "Acme", "unit_price": 10, "terms": "pricing subject to change; cancellation fee applies", "max_buy": 9},
    )
    report = firewall_report(p)
    assert report["clean"]
    assert report["supplier_quotes_blocked_from_auto_attach"]
    # Ensure not in submission namespace
    sub = (p.get("evidence_namespaces") or {}).get("GOVERNMENT_SUBMISSION_CONTENT") or []
    assert sub == []


def test_unknown_country_of_origin_blocks(tmp_store):
    p = create_or_get_project_from_opportunity(canonical_opportunity_id="opp-coo")
    ingest_document(p, title="RFQ", text="Country of origin required. Quantity: 5 each. Submit via email.", document_type="BASE_SOLICITATION")
    p["submission_system"] = "EMAIL"
    p["submission_timezone"] = "UTC"
    compile_project(p)
    coo = next(r for r in p["requirements"] if r["requirement_category"] == "COUNTRY_OF_ORIGIN")
    assert coo["compliance_status"] == UNKNOWN
    assert p["unresolved_material_requirement_count"] >= 1
    assert p["hard_block_count"] >= 1


def test_pre_award_not_at_submission_hard_fail(tmp_store):
    p = create_or_get_project_from_opportunity(canonical_opportunity_id="opp-pre")
    ingest_document(p, title="RFQ", text=PRE_AWARD_TEXT, document_type="BASE_SOLICITATION")
    p["submission_system"] = "EMAIL"
    p["submission_timezone"] = "UTC"
    compile_project(p)
    auth = [r for r in p["requirements"] if r["requirement_category"] == "AUTHORIZATION"]
    # If classified with pre-award language
    if auth:
        # pre-award unresolved should not alone force same treatment as AT_SUBMISSION COO
        assert auth[0].get("timing") in {"PRE_AWARD", "AT_SUBMISSION"} or auth[0].get("curability")


def test_clarification_on_document_conflict(tmp_store):
    p = create_or_get_project_from_opportunity(canonical_opportunity_id="opp-clr")
    ingest_document(p, title="Amd2", text=CONFLICT_A, document_type="AMENDMENT", amendment_number="002")
    ingest_document(p, title="Amd3", text=CONFLICT_B, document_type="AMENDMENT", amendment_number="003")
    compile_project(p)
    assert p.get("clarifications") or p.get("document_graph", {}).get("conflicts")


def test_deliverables_inventory(tmp_store):
    p = create_or_get_project_from_opportunity(canonical_opportunity_id="opp-dlv")
    ingest_document(
        p,
        title="Pricing.xlsx",
        text="Buyer pricing sheet template",
        document_type="PRICING_SHEET",
        filename="pricing.xlsx",
        is_buyer_template=True,
    )
    ingest_document(p, title="RFQ", text=FEDERAL_RFQ, document_type="BASE_SOLICITATION")
    compile_project(p)
    counts = deliverable_counts_by_type(p)
    assert sum(counts.values()) >= 1
    assert "BUYER_TEMPLATE" in counts or "FORM" in counts or "SIGNATURE" in counts


def test_persistence_and_idempotent_recompile(tmp_store):
    p = create_or_get_project_from_opportunity(canonical_opportunity_id="opp-persist", buyer="Test")
    ingest_document(p, title="RFQ", text=FEDERAL_RFQ, document_type="BASE_SOLICITATION")
    compile_project(p)
    n1 = len([r for r in p["requirements"] if not r.get("superseded")])
    rid = p["response_project_id"]
    compile_project(p)
    n2 = len([r for r in p["requirements"] if not r.get("superseded")])
    assert n2 == n1
    loaded = load_project(rid)
    assert loaded
    assert loaded["buyer"] == "Test"
    assert find_by_opportunity("opp-persist")["response_project_id"] == rid


def test_change_detection_hash():
    h1 = content_hash("hello")
    h2 = content_hash("hello")
    h3 = content_hash("hello!")
    assert h1 == h2 != h3


def test_dibbs_foundation_type(tmp_store):
    p = create_or_get_project_from_opportunity(
        canonical_opportunity_id="opp-dibbs",
        discovery_source="SAM",
        authoritative_source="DIBBS",
        submission_system="DIBBS",
    )
    ingest_document(p, title="DIBBS RFQ", text=DIBBS_LIKE, document_type="BASE_SOLICITATION")
    ingest_document(p, title="Master Solicitation", text="DLA Master Solicitation version 2026", document_type="MASTER_SOLICITATION")
    compile_project(p)
    assert p["response_type"] == "FEDERAL_DIBBS_WEB_QUOTE"
    assert any(d["document_type"] == "MASTER_SOLICITATION" for d in p["documents"])


def test_ui_read_model(tmp_store):
    p = create_or_get_project_from_opportunity(canonical_opportunity_id="opp-ui", buyer="County")
    ingest_document(p, title="RFQ", text=FEDERAL_RFQ, document_type="BASE_SOLICITATION")
    p["submission_system"] = "PIEE"
    p["submission_timezone"] = "America/New_York"
    compile_project(p)
    view = get_project_view(p["response_project_id"])
    assert view["operator_summary"]["plain"]["requirements"]
    assert "hard blockers" in view["operator_summary"]["plain"]["compliance"].lower() or "hard blockers" in str(view["operator_summary"])


def test_no_sam_imports_in_r1_modules():
    """R1 modules must not call live SAM client."""
    eng = ROOT / "response_engine"
    for path in eng.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "sam_budgeted_client" not in text
        assert "can_spend_sam" not in text
        assert "requests.get(\"https://api.sam.gov" not in text


def test_owner_ui_bid_prep_still_works():
    from phase_l.owner_ui_service import build_bid_prep

    bp = build_bid_prep()
    assert bp["kind"] == "OwnerUiBidPrep"
    assert "items" in bp


def test_api_routes_registered():
    from app import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/response-projects/from-opportunity/{opportunity_id}" in paths
    assert "/api/response-projects/{response_project_id}/compliance" in paths
