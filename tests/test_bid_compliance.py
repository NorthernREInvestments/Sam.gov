"""Focused tests — bid requirements + compliance intelligence."""

from __future__ import annotations

from fastapi.testclient import TestClient

from bid_compliance_engine import analyze_bid_compliance, authorize_compliance_research
from bid_compliance_invalidation import classify_material_change, process_amendment_or_qa
from bid_readiness_engine import (
    commercial_verification_gate,
    evaluate_bid_readiness_ladder,
    evaluate_package_completeness,
)
from bid_requirement_extraction import (
    distinguish_mandatory_vs_informational,
    extract_requirements_from_text,
)
from bid_submission_intelligence import (
    delivery_invalidates_economics,
    extract_evaluation_basis,
    extract_registration_requirements,
    extract_required_forms,
    extract_submission_instructions,
)
from compliance_matrix import build_compliance_matrix, map_company_to_requirement
from governing_documents import (
    apply_amendment_supersession,
    build_package_map,
    governing_document,
    resolve_requirement_authority,
)
from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode
from product_bid_compliance import (
    classify_authorization_requirement,
    classify_origin_compliance,
    evaluate_product_compliance,
    extract_brand_or_equal_details,
)


RFQ_TEXT = """
REQUEST FOR QUOTE RFQ-IT-100
Quantity: 50 each Dell Latitude 5540 or equal.
Brand name or equal is permitted with documentation of salient characteristics.
Bidders SHALL acknowledge all amendments.
Submit electronically via Bonfire portal in PDF format.
Bid deadline: October 15, 2026, 2:00 PM CDT
Evaluation: lowest price technically acceptable (LPTA).
SAM.gov registration required.
Authorized signature required on pricing sheet.
Buy American Act applies.
Delivery within 30 days FOB Destination Warehouse B.
"""

BRAND_ONLY_TEXT = """
Exact OEM model Cisco C9300-48P required. No substitutions.
Only authorized resellers may submit. Manufacturer authorization letter required.
"""


def test_governing_document_hierarchy_and_reference():
    docs = [
        governing_document(solicitation_id="S1", filename="rfq.pdf", document_type="RFQ", governing=True, extracted_text="base"),
        governing_document(solicitation_id="S1", filename="hist.pdf", document_type="HISTORICAL_REFERENCE", extracted_text="old"),
    ]
    pm = build_package_map(solicitation_id="S1", documents=docs)
    assert pm["governing_count"] == 1
    assert len(pm["reference_only"]) == 1
    assert pm["can_distinguish_governing_vs_reference"]


def test_amendment_supersession_quantity():
    base = governing_document(solicitation_id="S1", filename="rfq.pdf", document_type="RFQ", document_id="base1", governing=True)
    pm = build_package_map(solicitation_id="S1", documents=[base])
    amd = governing_document(
        solicitation_id="S1",
        filename="amd1.pdf",
        document_type="AMENDMENT",
        amendment_number="1",
        supersedes="base1",
        extracted_text="Quantity: 125",
    )
    pm2 = apply_amendment_supersession(pm, amendment_doc=amd, supersedes_document_id="base1")
    superseded = [d for d in pm2["documents"] if d["document_id"] == "base1"][0]
    assert superseded.get("superseded_by")
    assert not superseded.get("governing")
    res = resolve_requirement_authority(
        field="quantity",
        candidates=[
            {"value": 100, "document_id": "base1", "amendment_number": 0, "authority": "GOVERNING"},
            {"value": 125, "document_id": amd["document_id"], "amendment_number": 1, "authority": "GOVERNING"},
        ],
    )
    assert res["value"] == 125
    assert res["state"] == "ACTIVE_REQUIREMENT"
    assert res["superseded"]


def test_conflicting_requirements():
    res = resolve_requirement_authority(
        field="qty",
        candidates=[
            {"value": 10, "document_id": "a", "amendment_number": 1, "authority": "GOVERNING"},
            {"value": 20, "document_id": "b", "amendment_number": 1, "authority": "GOVERNING"},
        ],
    )
    assert res["state"] == "CONFLICTING_REQUIREMENT"
    assert res["conflict"] == "REQUIREMENT_CONFLICT_REQUIRES_RESOLUTION"


def test_mandatory_vs_informational_extraction():
    reqs = extract_requirements_from_text(RFQ_TEXT, solicitation_id="S1", source_document="rfq.pdf")
    assert any(r["category"] == "BRAND_OR_EQUAL" for r in reqs)
    assert any(r["mandatory"] for r in reqs)
    split = distinguish_mandatory_vs_informational(reqs)
    assert split["mandatory_count"] >= 1
    # preferred language alone should not be hard blocker without SHALL
    pref = extract_requirements_from_text(
        "Preferred delivery within 14 days.",
        solicitation_id="S1",
    )
    for r in pref:
        assert r["severity"] != "HARD_BLOCKER_IF_UNSATISFIED" or "shall" in (r.get("source_snippet") or "").lower()


def test_compliance_matrix_and_unknown_company():
    reqs = extract_requirements_from_text(RFQ_TEXT, solicitation_id="S1")
    matrix = build_compliance_matrix(reqs, company_facts={})  # no facts → unknown
    assert matrix["kind"] == "ComplianceMatrix"
    sam = next(r for r in matrix["rows"] if r["category"] == "SAM_REGISTRATION")
    assert sam["company_state"] == "UNKNOWN"
    # Known unsatisfied bonding
    bond_reqs = extract_requirements_from_text("Performance bond required.", solicitation_id="S1")
    mapped = map_company_to_requirement(bond_reqs[0], None)
    assert mapped["company_state"] in {"KNOWN_UNSATISFIED", "UNKNOWN"}


def test_hard_blocker_eligibility():
    reqs = extract_requirements_from_text(
        "Bidder shall have minimum 10 years experience. Bidder must be licensed.",
        solicitation_id="S1",
    )
    assert any(r["severity"] == "HARD_BLOCKER_IF_UNSATISFIED" for r in reqs)


def test_product_exact_brand_or_equal_and_substitution():
    exact = evaluate_product_compliance(
        required_manufacturer="Dell",
        required_model="Latitude 5540",
        offered_manufacturer="Dell",
        offered_model="Latitude 5540",
        package_text=RFQ_TEXT,
    )
    assert exact["state"] == "EXACT_MATCH"
    brand = extract_brand_or_equal_details(RFQ_TEXT)
    assert brand["brand_or_equal_permitted"] is True
    rejected = evaluate_product_compliance(
        required_manufacturer="Cisco",
        required_model="C9300-48P",
        offered_manufacturer="Generic",
        offered_model="Switch-X",
        package_text=BRAND_ONLY_TEXT,
    )
    assert rejected["unsupported_substitution_rejected"] is True
    assert rejected["state"] == "NONCOMPLIANT"


def test_authorization_vs_authenticity():
    authz = classify_authorization_requirement(BRAND_ONLY_TEXT)
    assert authz["bidder_authorization_required"] is True
    authz2 = classify_authorization_requirement("Must be new OEM product, genuine factory sealed.")
    assert authz2["bidder_authorization_required"] is False
    assert authz2["product_authenticity_required"] is True
    assert authz2["distinction"] == "new_oem_product_is_not_bidder_authorization"


def test_origin_domestic():
    o = classify_origin_compliance(RFQ_TEXT)
    assert o["requirement_found"] is True
    assert o["buy_american"] is True
    assert o["legal_advice"] is False


def test_delivery_invalidates_economics():
    inv = delivery_invalidates_economics(assumed_lead_time_days=30, required_delivery_days=10)
    assert inv["invalidated"] is True
    assert "economics" in inv["invalidate"]


def test_registrations_forms_submission_timezone_evaluation():
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    regs = extract_registration_requirements(RFQ_TEXT)
    assert regs["autonomous_registration_performed"] is False
    assert any(i["portal"] == "Bonfire" for i in regs["items"])
    forms = extract_required_forms(RFQ_TEXT + "\nNotarized affidavit required.")
    assert any(f["signature_required"] for f in forms["forms"])
    assert any(f.get("notary_required") for f in forms["forms"])
    sub = extract_submission_instructions(RFQ_TEXT)
    assert sub["submission_method"] in {"PORTAL", "EMAIL"}
    assert sub["timezone"] is not None
    assert sub["timezone_inferred"] is False
    bare = extract_submission_instructions("Submit by noon on Friday.")
    assert bare["timezone"] is None  # never guess
    ev = extract_evaluation_basis(RFQ_TEXT)
    assert ev["award_basis"] == "LPTA"
    assert ev["invented_preferences"] is False


def test_package_completeness_and_readiness_ladder():
    docs = [governing_document(solicitation_id="S1", filename="rfq.pdf", document_type="RFQ", governing=True, extracted_text=RFQ_TEXT)]
    pm = build_package_map(solicitation_id="S1", documents=docs)
    analysis = analyze_bid_compliance(
        solicitation_id="S1",
        documents=docs,
        document_texts={docs[0]["document_id"]: RFQ_TEXT},
        deadline_viability="ACTIONABLE",
    )
    assert analysis["package_completeness"]["ready_for_bid_assembly"] is False
    assert analysis["bid_readiness"]["independent_of_profit"] is True
    assert analysis["bid_readiness"]["state"] != "READY_FOR_BID_ASSEMBLY"
    # unresolved mandatory prevents assembly
    assert "unresolved_mandatory" in str(analysis["bid_readiness"]["blockers"]) or analysis["compliance_matrix"]["unresolved_mandatory"]


def test_amendment_invalidation_preserves_unrelated():
    base = governing_document(solicitation_id="S1", filename="rfq.pdf", document_type="RFQ", document_id="b1", governing=True)
    pm = build_package_map(solicitation_id="S1", documents=[base])
    out = process_amendment_or_qa(
        package_map=pm,
        amendment_doc={"document_id": "a1", "amendment_number": 1, "supersedes": "b1", "text_snippet": "qty change"},
        old_requirement_values={"quantity": 100},
        new_requirement_values={"quantity": 125},
        research_state={"conclusions": {"supplier_economics": {"ok": True}, "unrelated_naics": {"ok": True}}},
    )
    assert "QUANTITY_CHANGE" in out["material_change"]["categories"]
    assert out["research_state"]["conclusions"]["supplier_economics"]["status"] == "INVALIDATED"
    assert out["research_state"]["conclusions"]["unrelated_naics"]["ok"] is True
    assert out["full_unrelated_research_rerun"] is False


def test_qa_material_change():
    ch = classify_material_change(change_hints={"is_qa": True, "force_material": True})
    assert "Q_AND_A_CLARIFICATION" in ch["categories"]
    assert ch["operator_acknowledgment"] == "ACKNOWLEDGMENT_REQUIRED"


def test_freshness_blocks_readiness():
    ready = evaluate_bid_readiness_ladder(
        package_acquired=True,
        package_mapped=True,
        requirements_extracted=True,
        compliance_analyzed=True,
        freshness_gate={
            "passed": False,
            "blockers": ["unreviewed_material_or_critical_changes"],
            "would_allow_ready_for_submission": False,
        },
        compliance_matrix={"unresolved_mandatory": [], "hard_blocker_present": False},
    )
    assert "authoritative_freshness_blocked" in ready["blockers"]
    assert ready["ready_for_bid_assembly"] is False


def test_cost_governor_voi_and_reuse():
    denied = authorize_compliance_research(
        solicitation_id="S1",
        question="nice to have background fluff",
        could_change_readiness=False,
    )
    assert denied["authorized"] is False
    reused = authorize_compliance_research(
        solicitation_id="S1",
        question="OEM auth letter needed?",
        reusable_evidence_sufficient=True,
    )
    assert reused["authorized"] is False


def test_commercial_gate_no_outreach():
    gate = commercial_verification_gate(
        pursuit_worthy=True,
        package_materially_complete=True,
        compliance_achievable=True,
        economics_potentially_viable=True,
        deadline_actionable=True,
    )
    assert gate["performed"] is False
    assert gate["outreach_count"] == 0
    assert all(a["status"] == "FUTURE_ACTION_IF_PURSUED" for a in gate["actions"])


def test_no_outreach_in_analysis():
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    docs = [governing_document(solicitation_id="S1", filename="rfq.pdf", document_type="RFQ", governing=True)]
    analysis = analyze_bid_compliance(
        solicitation_id="S1",
        documents=docs,
        document_texts={"x": RFQ_TEXT},
    )
    assert analysis["outreach"]["emails_sent"] == 0
    assert analysis["outreach"]["bids_submitted"] == 0
    assert analysis["fabricated_requirements"] is False
    assert analysis["checklist"]["traceable_to_evidence"] is True


def test_api_bid_compliance_routes(monkeypatch):
    monkeypatch.setenv("APP_EMAIL", "")
    monkeypatch.setenv("APP_PASSWORD", "")
    # Re-import auth check is env-based each call
    from app import app

    client = TestClient(app)
    r = client.post(
        "/api/opportunities/TEST-OPP-1/analyze-compliance",
        json={
            "documents": [
                {
                    "filename": "rfq.pdf",
                    "document_type": "RFQ",
                    "governing": True,
                    "extracted_text": RFQ_TEXT,
                }
            ],
            "document_texts": {"rfq.pdf": RFQ_TEXT},
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["kind"] == "BidComplianceAnalysis"
    assert client.get("/api/opportunities/TEST-OPP-1/bid-requirements").status_code == 200
    assert client.get("/api/opportunities/TEST-OPP-1/compliance-matrix").status_code == 200
    assert client.get("/api/opportunities/TEST-OPP-1/bid-readiness").status_code == 200
    assert client.get("/api/opportunities/TEST-OPP-1/package-map").status_code == 200
    assert client.get("/api/opportunities/TEST-OPP-1/bid-checklist").status_code == 200


def test_idempotent_analysis_cache():
    from app import _BID_COMPLIANCE_CACHE, _bid_compliance_for

    _BID_COMPLIANCE_CACHE.clear()
    a = _bid_compliance_for("IDEM-1", {"documents": [], "force": True})
    b = _bid_compliance_for("IDEM-1", {})
    assert a["solicitation_id"] == b["solicitation_id"]
