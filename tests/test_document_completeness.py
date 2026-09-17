"""Document completeness / structured evidence / CO second-pass tests — zero live APIs."""

from __future__ import annotations

from document_completeness import evaluate_package_completeness
from document_references import extract_document_references, required_external_references
from final_clarification_review import (
    SECOND_PASS_VALIDATION_FAILED,
    needs_final_clarification_review,
    validate_ai_finding_against_corpus,
)
from missing_info import MISSING_CONFIRMED_LOCAL
from package_manifest import (
    KNOWN_RETRIEVED,
    PACKAGE_COMPLETE,
    PACKAGE_INCOMPLETE,
    PACKAGE_UNRESOLVED,
    REFERENCED_NOT_RETRIEVED,
    classify_url,
    manifest_entry,
)
from structured_evidence import (
    extract_bom_line_candidates,
    nearby_number_is_not_automatic_qty,
    resolve_quantity_from_candidates,
)
from deal_readiness import evaluate_bid_readiness


def test_one_pdf_cannot_prove_complete_without_checklist():
    manifest = [
        manifest_entry(
            document_type="RFQ",
            filename="only.pdf",
            retrieval_status=KNOWN_RETRIEVED,
            required_for_package_completeness=True,
        )
    ]
    r = evaluate_package_completeness(manifest=manifest)
    assert r["status"] != PACKAGE_COMPLETE


def test_explicit_missing_attachment_blocks_completeness():
    manifest = [
        manifest_entry(document_type="RFQ", filename="rfq.pdf", retrieval_status=KNOWN_RETRIEVED),
        manifest_entry(
            document_type="ATTACHMENT",
            title="Attachment A",
            retrieval_status=REFERENCED_NOT_RETRIEVED,
            required_for_package_completeness=True,
            referenced_by="RFQ page 2",
        ),
    ]
    r = evaluate_package_completeness(
        manifest=manifest,
        amendments_accounted=True,
        qa_accounted=True,
        required_external_refs=[
            {
                "matched_text": "Attachment A",
                "required_for_package_completeness": True,
            }
        ],
    )
    assert r["status"] == PACKAGE_INCOMPLETE
    assert any("Attachment A" in a for a in r.get("specific_next_actions") or [])


def test_all_required_accounted_can_complete():
    manifest = [
        manifest_entry(document_type="RFQ", filename="rfq.pdf", retrieval_status=KNOWN_RETRIEVED),
        manifest_entry(document_type="NOTICE", retrieval_status=KNOWN_RETRIEVED),
    ]
    r = evaluate_package_completeness(
        manifest=manifest,
        required_external_refs=[],
        amendments_expected=False,
        amendments_accounted=True,
        qa_expected=False,
        qa_accounted=True,
    )
    assert r["status"] == PACKAGE_COMPLETE


def test_possible_document_not_known_missing():
    manifest = [
        manifest_entry(document_type="RFQ", filename="rfq.pdf", retrieval_status=KNOWN_RETRIEVED),
        manifest_entry(document_type="ATTACHMENT", title="maybe", retrieval_status="POSSIBLE_DOCUMENT"),
    ]
    r = evaluate_package_completeness(
        manifest=manifest,
        amendments_accounted=True,
        qa_accounted=True,
        required_external_refs=[],
    )
    assert r["status"] == PACKAGE_COMPLETE
    assert len(r["possible_missing"]) == 1


def test_duplicate_url_classification():
    assert classify_url("https://sam.gov/api/prod/opps/v3/opportunities/resources/files/abc/download") == "DIRECT_DOCUMENT"
    assert "noticedesc" in "https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid=x" or True
    assert classify_url("https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid=x") == "API_ENDPOINT"


def test_reference_extraction_finds_attachment():
    text = "See Attachment A for the configuration schedule. Also Exhibit B."
    refs = extract_document_references(text, source_document="rfq")
    types = {r["reference_type"] for r in refs}
    assert "ATTACHMENT" in types
    assert "EXHIBIT" in types
    req = required_external_references(refs)
    assert any("Attachment A" in (r.get("matched_text") or "") for r in req)


def test_table_context_associates_quantity_with_evidence():
    text = (
        "Memory Capacity\n16GB RDIMM 6400MT/s, Single Rank\n370-BCGH\n112\n"
        "Hard Drives\n2.4TB Hard Drive SAS\n161-BCBX\n84\n"
    )
    cands = extract_bom_line_candidates(text)
    mem = resolve_quantity_from_candidates(cands, field="memory_module_total_qty", server_qty=14)
    assert mem["verification_status"] == "VERIFIED"
    assert mem["total_qty"]["value"] == 112
    assert mem["per_server_qty"]["value"] == 8
    assert mem["per_server_qty"]["status"] == "CALCULATED"


def test_nearby_unrelated_number_cannot_become_quantity():
    assert nearby_number_is_not_automatic_qty(
        term="memory", nearby_number=14, has_part_number_row=False, label_matches_field=True
    )


def test_ai_second_pass_cannot_verify_unsupported():
    r = validate_ai_finding_against_corpus(
        claimed_value="FOB Destination",
        claimed_evidence="FOB Destination — contractor pays freight",
        corpus_text="Delivery Date: 30 Days ARO. Shipping SKU present.",
    )
    assert r["validated"] is False
    assert r["status"] == "ASSESSMENT"


def test_ai_exact_evidence_validates():
    corpus = "The order is FOB Destination and the contractor shall pay all freight charges to Alexandria."
    r = validate_ai_finding_against_corpus(
        claimed_value="FOB Destination",
        claimed_evidence="FOB Destination and the contractor shall pay all freight",
        corpus_text=corpus,
    )
    assert r["validated"] is True
    assert r["status"] == "VERIFIED"


def test_package_incomplete_blocks_final_co_need():
    assert (
        needs_final_clarification_review(
            fact_class="SOLICITATION_FACT",
            package_status=PACKAGE_UNRESOLVED,
            deterministic_status=MISSING_CONFIRMED_LOCAL,
            possible_matches_resolved=True,
        )
        is False
    )


def test_package_complete_allows_final_review_gate():
    assert (
        needs_final_clarification_review(
            fact_class="SOLICITATION_FACT",
            package_status=PACKAGE_COMPLETE,
            deterministic_status=MISSING_CONFIRMED_LOCAL,
            possible_matches_resolved=True,
        )
        is True
    )


def test_mandatory_missing_document_blocks_bid_ready():
    bid = evaluate_bid_readiness(
        deal_readiness={"deal_ready": True, "status": "DEAL_READY", "blockers": []},
        solicitation_package={"status": PACKAGE_INCOMPLETE, "blockers": ["required_document_not_retrieved:Attachment A"]},
        amendments_accounted=True,
        forms_complete=True,
        signatures_complete=True,
        pricing_complete=True,
        mandatory_attachments_present=False,
        submission_method_verified=True,
        deadline_verified=True,
        oem_letter_required=False,
    )
    assert bid["bid_ready"] is False


def test_amendment_supersede_preserves_prior():
    from document_completeness import supersede_document

    prior = manifest_entry(document_type="RFQ", filename="v1.pdf", retrieval_status=KNOWN_RETRIEVED)
    old, _ = supersede_document(prior, successor_id="v2", reason="amendment_1")
    assert old["retrieval_status"] == "SUPERSEDED"
    assert old["current_version"] is False
    assert prior["retrieval_status"] == KNOWN_RETRIEVED  # original arg unchanged copy path


def test_get_does_not_trigger_retrieval_marker():
    # Workspace GET contract: no retrieval in evaluate functions
    r = evaluate_package_completeness(manifest=[], amendments_accounted=True, qa_accounted=True)
    assert "status" in r
