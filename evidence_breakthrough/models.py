"""Statuses, grades, and EvidenceMap shapes for evidence breakthrough."""

from __future__ import annotations

from typing import Any

BUILD = "20261003-m3-evidence-breakthrough-v1"

# Government-value match grades
GOV_EXACT_LINE_HISTORY = "GOV_EXACT_LINE_HISTORY"
GOV_EXACT_PRODUCT_HISTORY = "GOV_EXACT_PRODUCT_HISTORY"
GOV_EXACT_SOLICITATION_HISTORY = "GOV_EXACT_SOLICITATION_HISTORY"
GOV_CLOSE_PRODUCT_HISTORY = "GOV_CLOSE_PRODUCT_HISTORY"
GOV_BASKET_HISTORY = "GOV_BASKET_HISTORY"
GOV_CURRENT_ESTIMATE = "GOV_CURRENT_ESTIMATE"
GOV_CURRENT_BUDGET = "GOV_CURRENT_BUDGET"
GOV_NO_USABLE_HISTORY = "GOV_NO_USABLE_HISTORY"

# Acquisition price basis
PUBLIC_RETAIL_EXACT = "PUBLIC_RETAIL_EXACT"
PUBLIC_DISTRIBUTOR_EXACT = "PUBLIC_DISTRIBUTOR_EXACT"
PUBLIC_RESELLER_EXACT = "PUBLIC_RESELLER_EXACT"
PUBLIC_ALLOWED_EQUAL = "PUBLIC_ALLOWED_EQUAL"
SUPPLIER_QUOTE = "SUPPLIER_QUOTE"
NO_PUBLIC_PRICE = "NO_PUBLIC_PRICE"

# Shared stop / status
NOT_FOUND_AFTER_EXHAUSTIVE_SEARCH = "NOT_FOUND_AFTER_EXHAUSTIVE_SEARCH"
RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
INSUFFICIENT_IDENTITY = "INSUFFICIENT_IDENTITY"
AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
FOUND = "FOUND"

BOTH_SIDES_LINE_READY = "BOTH_SIDES_LINE_READY"
BOTH_SIDES_BASKET_READY = "BOTH_SIDES_BASKET_READY"

# Failure reasons
NO_BUYER_HISTORY = "NO_BUYER_HISTORY"
NO_MATCHING_HISTORY = "NO_MATCHING_HISTORY"
NO_PUBLIC_PRICE_FR = "NO_PUBLIC_PRICE"
UOM_MISMATCH = "UOM_MISMATCH"
INSUFFICIENT_IDENTITY_FR = "INSUFFICIENT_IDENTITY"
HISTORY_TOO_GENERIC = "HISTORY_TOO_GENERIC"
BASKET_INCOMPLETE = "BASKET_INCOMPLETE"
RETRYABLE_SOURCE_FAILURE = "RETRYABLE_SOURCE_FAILURE"
OTHER = "OTHER"

BASKET_READY_COVERAGE = 0.50  # 50% of material lines
BASKET_COMPLETE_COVERAGE = 0.80


def empty_resolver_result(*, side: str) -> dict[str, Any]:
    return {
        "side": side,
        "status": NOT_FOUND_AFTER_EXHAUSTIVE_SEARCH,
        "evidence": None,
        "confidence": None,
        "source": None,
        "provenance": [],
        "match_type": None,
        "failure_reason": None,
        "queries_attempted": [],
        "sources_attempted": [],
        "routes_attempted": [],
        "best_match": None,
        "stop_reason": None,
    }


def empty_evidence_map() -> dict[str, Any]:
    return {
        "kind": "EvidenceMap",
        "build": BUILD,
        "opportunity_id": None,
        "buyer": None,
        "current_solicitation": None,
        "line": None,
        "identity": None,
        "government_value": None,
        "public_cost": None,
        "uom_normalization": None,
        "freight": None,
        "financing": None,
        "confidence": None,
        "source_links": [],
        "missing_facts": [],
    }
