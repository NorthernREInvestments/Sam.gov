"""Minimal unit checks for document_quality + sweep import."""
from document_quality import (
    UNRELATED_DOCUMENTS,
    VALID_SOLICITATION_PACKAGE,
    classify_package_documents,
    score_document_against_opportunity,
)


def test_rejects_w9():
    s = score_document_against_opportunity(
        document_name="Form_W9.pdf",
        document_text="Request for Taxpayer Identification Number and Certification Form W-9",
        title="Water Meter Bid ITB-2024-01",
        buyer="Town of Example",
        solicitation_number="ITB-2024-01",
    )
    assert s["quality"] == UNRELATED_DOCUMENTS


def test_accepts_solicitation():
    text = (
        "INVITATION TO BID ITB-2024-01 Town of Example\n"
        "Bid Schedule line item quantity unit price\n"
        "Specifications for water meters Model ABC brand or equal\n"
        "Closing date January 15 2025 bid instructions"
    )
    s = score_document_against_opportunity(
        document_name="ITB-2024-01_Bid_Documents.pdf",
        document_text=text,
        title="Water Meter Bid ITB-2024-01",
        buyer="Town of Example",
        solicitation_number="ITB-2024-01",
    )
    assert s["quality"] == VALID_SOLICITATION_PACKAGE
    agg = classify_package_documents(
        [{"document_name": "ITB-2024-01_Bid_Documents.pdf", "text": text}],
        title="Water Meter Bid ITB-2024-01",
        buyer="Town of Example",
        solicitation_number="ITB-2024-01",
    )
    assert agg["usable"] is True


def test_sweep_imports():
    from full_funnel_sweep import BUILD_TARGET, format_owner_summary, run_full_funnel_sweep

    assert BUILD_TARGET.startswith("20261003")
    assert callable(run_full_funnel_sweep)
    assert callable(format_owner_summary)
