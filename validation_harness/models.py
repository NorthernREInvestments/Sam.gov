"""Golden case schema + gap / severity constants."""

from __future__ import annotations

from typing import Any

# --- Severity ---
SEV_CRITICAL = "CRITICAL"
SEV_HIGH = "HIGH"
SEV_MEDIUM = "MEDIUM"
SEV_LOW = "LOW"
SEV_INFO = "INFO"

SEVERITIES = (SEV_CRITICAL, SEV_HIGH, SEV_MEDIUM, SEV_LOW, SEV_INFO)

# --- Gap taxonomy ---
GAP_CATEGORIES = (
    "DISCOVERY_MISS",
    "SOURCE_RESOLUTION_MISS",
    "DOCUMENT_MISS",
    "CLASSIFICATION_ERROR",
    "IDENTITY_ERROR",
    "QUANTITY_UOM_ERROR",
    "HISTORY_ERROR",
    "SUPPLIER_ERROR",
    "ECONOMICS_ERROR",
    "PACKAGING_ERROR",
    "DELIVERY_ERROR",
    "INSPECTION_ERROR",
    "ACCEPTANCE_ERROR",
    "TECHNICAL_DATA_ERROR",
    "CLAUSE_ERROR",
    "SUBMISSION_ERROR",
    "AMENDMENT_ERROR",
    "FINANCING_ERROR",
    "POST_AWARD_ERROR",
    "INVOICE_ERROR",
    "PAYMENT_ERROR",
    "WORKFLOW_ERROR",
    "FALSE_POSITIVE",
    "FALSE_REJECTION",
    "FALSE_READINESS",
    "STALE_DATA",
    "PROVENANCE_ERROR",
    "OTHER",
)

# Domain → default gap category
DOMAIN_GAP_MAP = {
    "product_identity": "IDENTITY_ERROR",
    "quantity_uom": "QUANTITY_UOM_ERROR",
    "packaging": "PACKAGING_ERROR",
    "shipping_delivery": "DELIVERY_ERROR",
    "inspection_acceptance": "INSPECTION_ERROR",
    "submission": "SUBMISSION_ERROR",
    "financing": "FINANCING_ERROR",
    "post_award": "POST_AWARD_ERROR",
    "invoice_payment": "INVOICE_ERROR",
    "operator_workflow": "WORKFLOW_ERROR",
    "owner_readiness": "FALSE_READINESS",
    "amendment": "AMENDMENT_ERROR",
    "supplier": "SUPPLIER_ERROR",
    "economics": "ECONOMICS_ERROR",
    "provenance": "PROVENANCE_ERROR",
}

GAP_STATUS_OPEN = "OPEN"
GAP_STATUS_FIXED = "FIXED"
GAP_STATUS_ACCEPTED = "ACCEPTED_LIMITATION"
GAP_STATUS_FALSE_ALARM = "FALSE_ALARM"
GAP_STATUS_DEFERRED = "DEFERRED"

SOURCE_TYPE_SYNTHETIC = "SYNTHETIC_VALIDATION_FIXTURE"
SOURCE_TYPE_CURATED = "CURATED_DOCUMENT_FIXTURE"
# Phase F — realistic procurement cases (never confuse with synthetic Phase E)
SOURCE_TYPE_REAL_SOURCE = "REAL_SOURCE_FIXTURE"
SOURCE_TYPE_REALISTIC_FROZEN = "REALISTIC_FROZEN_FIXTURE"
SOURCE_TYPE_SYNTHETIC_EDGE = "SYNTHETIC_EDGE_CASE"
SOURCE_TYPE_REALITY = "REALITY_CASE"  # umbrella label for Phase F corpus entries

CASE_REQUIRED_KEYS = (
    "case_id",
    "case_name",
    "source_type",
    "expected",
)


def empty_case(**overrides: Any) -> dict[str, Any]:
    base = {
        "case_id": "CASE_000",
        "case_name": "Unnamed",
        "description": "",
        "source_type": SOURCE_TYPE_SYNTHETIC,
        "source_fixture": "",
        "documents": [],
        "row_overrides": {},
        "expected": {},
        "allowed_unknowns": [],
        "expected_blockers": [],
        "expected_operator_state": None,
        "expected_owner_readiness": None,
        "tags": [],
        "notes": "",
        "adversarial": False,
        # execution_path | broader_pipeline | adversarial_readiness | positive_ready | reality
        "validation_depth": "execution_path",
        "reality_class": None,  # REAL_SOURCE_FIXTURE | REALISTIC_FROZEN_FIXTURE | SYNTHETIC_EDGE_CASE
        "ui_expectations": {},  # Phase F — screen-level expected behavior
        "next_action_quality": None,  # curated plain-English next action expectation
        "blocker_quality": None,
    }
    base.update(overrides)
    return base


def empty_gap(
    *,
    case_id: str,
    category: str,
    severity: str,
    field_path: str,
    expected: Any,
    actual: Any,
    likely_module: str = "UNKNOWN",
    consequence: str = "",
    run_id: str | None = None,
) -> dict[str, Any]:
    cat = category if category in GAP_CATEGORIES else "OTHER"
    sev = severity if severity in SEVERITIES else SEV_MEDIUM
    return {
        "gap_id": f"{case_id}:{field_path}:{cat}",
        "case_id": case_id,
        "category": cat,
        "severity": sev,
        "field_path": field_path,
        "expected": expected,
        "actual": actual,
        "source": "golden_case",
        "likely_module": likely_module,
        "consequence": consequence,
        "status": GAP_STATUS_OPEN,
        "run_reference": run_id,
        "regression": False,
    }
