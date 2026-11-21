"""Configurable initial M3 commercial qualification (Build 002G).

These criteria do NOT restrict discovery. Unknown requirements remain unknown.
Never label profitable/financeable without commercial evidence.
"""

from __future__ import annotations

import re
from typing import Any

from m3.research.classify import PRODUCT_CLASSES

INITIAL_PRODUCT_OK = {"product_supply", "product_with_incidental_services"}
INITIAL_EXCLUDE_CLASSES = {
    "services",
    "professional_services",
    "maintenance_repair_services",
    "construction",
    "software_only",
    "product_rental_leasing",
    "product_with_significant_installation",
    "not_applicable_non_solicitation",
}
IT_MARKERS = re.compile(
    r"(?i)\b(software|saas|cloud\s*license|microsoft\s*365|cisco|network\s*equipment|"
    r"server|laptop|chromebook|endpoint\s*protection|cyber\s*security|it\s*services)\b"
)
BOND_MARKERS = re.compile(
    r"(?i)\b(bid\s*bond|performance\s*bond|payment\s*bond|surety|"
    r"performance\s*and\s*payment\s*bond)\b"
)
INSURANCE_MARKERS = re.compile(
    r"(?i)\b(professional\s*liability|errors?\s*and\s*omissions|"
    r"\$\s*5,?\d{3},?\d{3}\s*(?:general\s*)?liability|ocip|ccip)\b"
)


def apply_initial_m3_criteria(item: dict[str, Any]) -> dict[str, Any]:
    """Evaluate initial business-model fit. Does not invent missing facts."""
    req = (item.get("requirement_class") or "unknown").lower()
    status = (item.get("solicitation_status") or "unknown").lower()
    title = str(item.get("title") or "")
    desc = str(item.get("description") or "")
    blob = f"{title}\n{desc}\n{item.get('document_text') or ''}"
    reasons: list[str] = []
    unknown_flags: list[str] = []

    if not item.get("verified_individual_solicitation") and item.get("record_type") not in {
        None,
        "",
        "INDIVIDUAL_SOLICITATION",
    }:
        return {
            "initial_m3_status": "not_eligible",
            "reasons": [f"record_type:{item.get('record_type')}"],
            "unknown_flags": [],
        }

    if status in {"closed", "cancelled", "historical_award", "forecast"}:
        return {
            "initial_m3_status": "not_eligible",
            "reasons": [f"status:{status}"],
            "unknown_flags": [],
        }

    if req in INITIAL_EXCLUDE_CLASSES:
        return {
            "initial_m3_status": "not_eligible",
            "reasons": [f"class:{req}"],
            "unknown_flags": [],
        }

    if req in {"unknown", "unknown_mixed", "pending_classification", ""}:
        unknown_flags.append("requirement_class_unknown")

    if req not in INITIAL_PRODUCT_OK and req not in PRODUCT_CLASSES and "requirement_class_unknown" not in unknown_flags:
        if req == "mixed_product_service":
            unknown_flags.append("mixed_requires_line_review")
        else:
            reasons.append(f"class_not_initial_product:{req}")

    qty = item.get("quantity")
    if qty in (None, "", "unknown"):
        unknown_flags.append("quantity_unknown")
    elif str(qty).lower() in {"n/a", "na", "not applicable"}:
        reasons.append("quantity_not_applicable")

    if IT_MARKERS.search(blob):
        reasons.append("it_focus_detected")

    if BOND_MARKERS.search(blob):
        # Flag — do not auto-exclude without confirmation of mandatory incompatibility
        unknown_flags.append("bonding_language_present_unverified")
    if INSURANCE_MARKERS.search(blob):
        unknown_flags.append("insurance_language_present_unverified")

    if reasons and not unknown_flags:
        return {"initial_m3_status": "not_eligible", "reasons": reasons, "unknown_flags": []}
    if unknown_flags or reasons:
        return {
            "initial_m3_status": "unknown",
            "reasons": reasons,
            "unknown_flags": unknown_flags,
        }
    if req in INITIAL_PRODUCT_OK and item.get("verified_individual_solicitation"):
        return {
            "initial_m3_status": "verified_initial_fit",
            "reasons": ["product_class_and_identity_ok"],
            "unknown_flags": [],
        }
    return {
        "initial_m3_status": "unknown",
        "reasons": [],
        "unknown_flags": ["insufficient_verified_fields"],
    }
