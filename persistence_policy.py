"""Persistence policy — PERSIST KNOWLEDGE, NOT BULK DATA."""

from __future__ import annotations

from typing import Any

# Categories
PERSIST = "PERSIST"
PERSIST_SUMMARY_ONLY = "PERSIST_SUMMARY_ONLY"
PERSIST_REFERENCE_ONLY = "PERSIST_REFERENCE_ONLY"
TEMPORARY = "TEMPORARY"
OPERATOR_REVIEW = "OPERATOR_REVIEW"

# Artifact kinds
KIND_BULK_PDF = "bulk_pdf"
KIND_BULK_HTML = "bulk_html"
KIND_BULK_API_RESPONSE = "bulk_api_response"
KIND_SEARCH_SNIPPET = "search_snippet"
KIND_TEMP_TEXT = "temp_converted_text"
KIND_SUPPLIER_CATALOG_PAGE = "supplier_catalog_page"
KIND_MANUFACTURER_CATALOG = "manufacturer_catalog"
KIND_SOURCE_RECIPE = "source_recipe"
KIND_PORTAL_QUIRK = "portal_quirk"
KIND_DEAL_RECORD = "deal_research_record"
KIND_SOLICITATION_ID = "solicitation_identifier"
KIND_EXTRACTED_REQUIREMENT = "extracted_requirement"
KIND_BOM = "bom"
KIND_SUPPLIER_INTELLIGENCE = "supplier_intelligence"
KIND_SUPPLIER_QUOTE = "supplier_quote"
KIND_ECONOMICS = "economics"
KIND_FUNDING_FACT = "funding_provider_fact"
KIND_FINANCIER_CALL = "financier_call_outcome"
KIND_OPERATOR_NOTE = "operator_note"
KIND_BUYER_INTELLIGENCE = "buyer_intelligence"
KIND_URL_REFERENCE = "url_reference"
KIND_CONTENT_HASH = "content_hash"
KIND_PROVENANCE = "provenance"
KIND_DECISION = "decision"
KIND_BENCHMARK_ANSWER_KEY = "benchmark_answer_key"


# Defaults: bulk public → TEMPORARY; reusable knowledge → PERSIST*
_DEFAULTS: dict[str, str] = {
    KIND_BULK_PDF: TEMPORARY,
    KIND_BULK_HTML: TEMPORARY,
    KIND_BULK_API_RESPONSE: TEMPORARY,
    KIND_SEARCH_SNIPPET: TEMPORARY,
    KIND_TEMP_TEXT: TEMPORARY,
    KIND_SUPPLIER_CATALOG_PAGE: TEMPORARY,
    KIND_MANUFACTURER_CATALOG: TEMPORARY,
    KIND_SOURCE_RECIPE: PERSIST,
    KIND_PORTAL_QUIRK: PERSIST,
    KIND_DEAL_RECORD: PERSIST,
    KIND_SOLICITATION_ID: PERSIST,
    KIND_EXTRACTED_REQUIREMENT: PERSIST,
    KIND_BOM: PERSIST,
    KIND_SUPPLIER_INTELLIGENCE: PERSIST_SUMMARY_ONLY,
    KIND_SUPPLIER_QUOTE: PERSIST,  # business record
    KIND_ECONOMICS: PERSIST,
    KIND_FUNDING_FACT: PERSIST,
    KIND_FINANCIER_CALL: PERSIST,
    KIND_OPERATOR_NOTE: PERSIST,
    KIND_BUYER_INTELLIGENCE: PERSIST_SUMMARY_ONLY,
    KIND_URL_REFERENCE: PERSIST_REFERENCE_ONLY,
    KIND_CONTENT_HASH: PERSIST_REFERENCE_ONLY,
    KIND_PROVENANCE: PERSIST,
    KIND_DECISION: PERSIST,
    KIND_BENCHMARK_ANSWER_KEY: TEMPORARY,  # never promote into production recipes
}


def persistence_decision(
    kind: str,
    *,
    operator_retain: bool = False,
    explicit_retention: bool = False,
    is_business_record: bool = False,
    debug_retain: bool = False,
) -> dict[str, Any]:
    """Decide persistence category for an artifact kind."""
    if operator_retain or explicit_retention:
        return {
            "kind": kind,
            "category": PERSIST if is_business_record or operator_retain else OPERATOR_REVIEW,
            "reason": "operator_or_explicit_retention",
            "retain_body": True,
        }
    if debug_retain:
        return {"kind": kind, "category": TEMPORARY, "reason": "debug_retain_temp_only", "retain_body": True}
    if is_business_record and kind in {KIND_SUPPLIER_QUOTE, KIND_FINANCIER_CALL, KIND_OPERATOR_NOTE}:
        return {"kind": kind, "category": PERSIST, "reason": "business_record_policy", "retain_body": True}

    cat = _DEFAULTS.get(kind, TEMPORARY)
    retain_body = cat in {PERSIST, OPERATOR_REVIEW}
    # summaries/references persist metadata, not bulk body
    if cat in {PERSIST_SUMMARY_ONLY, PERSIST_REFERENCE_ONLY}:
        retain_body = False
    return {
        "kind": kind,
        "category": cat,
        "reason": "default_persistence_policy",
        "retain_body": retain_body,
        "note": "PERSIST KNOWLEDGE, NOT BULK DATA",
    }


def may_persist_bulk_body(kind: str, **kwargs: Any) -> bool:
    return bool(persistence_decision(kind, **kwargs).get("retain_body"))


def is_temporary_by_default(kind: str) -> bool:
    return _DEFAULTS.get(kind, TEMPORARY) == TEMPORARY
