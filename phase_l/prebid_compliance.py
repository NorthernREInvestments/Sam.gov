"""Phase L.2.1 pre-bid solicitation compliance safety gate.

Data model only — NO submission automation, NO outreach.
READY_TO_BID requires complete package review.
"""

from __future__ import annotations

from typing import Any

from application_clock import now_utc

READY_TO_BID = "READY_TO_BID"
PRE_BID_OWNER_REVIEW = "PRE_BID_OWNER_REVIEW"
COMPLIANCE_INCOMPLETE = "COMPLIANCE_INCOMPLETE"
ACCESS_ELIGIBLE = "ACCESS_ELIGIBLE"
CAN_COMPETE = "CAN_COMPETE"

# Mandatory review categories (plain keys)
MANDATORY_CATEGORIES = [
    "solicitation_documents_reviewed",
    "attachments_reviewed",
    "amendments_reviewed",
    "amendment_acknowledgement",
    "exact_product_specification",
    "acceptable_equals",
    "quantity",
    "uom",
    "clin_structure",
    "delivery_date",
    "delivery_destination",
    "fob_terms",
    "freight",
    "packaging",
    "marking_labeling",
    "warranty",
    "support",
    "installation",
    "training",
    "inspection_acceptance",
    "country_of_origin",
    "buy_american",
    "taa",
    "specialty_metals",
    "authorized_reseller",
    "source_approval",
    "traceability",
    "certificate_of_conformance",
    "insurance",
    "bonding",
    "licensing",
    "representations_certifications",
    "technical_literature",
    "quote_format",
    "pricing_format",
    "submission_method",
    "submission_portal_or_email",
    "deadline",
    "timezone",
    "signatures",
    "required_forms",
    "past_performance",
    "evaluation_criteria",
    "invoicing_payment",
    "liquidated_damages",
    "unusual_performance_obligations",
    "execution_cost_risk_clauses",
]


def empty_compliance_checklist() -> dict[str, Any]:
    return {
        cat: {"status": "UNREVIEWED", "notes": None, "evidence": None}
        for cat in MANDATORY_CATEGORIES
    }


def evaluate_prebid_compliance(
    row: dict[str, Any],
    *,
    checklist: dict[str, Any] | None = None,
    force_incomplete: bool = True,
) -> dict[str, Any]:
    """
    Hard rule: if any mandatory requirement is UNKNOWN/UNSATISFIED/UNREVIEWED
    → READY_TO_BID = False.
    Default for live hunt: incomplete (we have not reviewed full packages).
    """
    checklist = checklist or empty_compliance_checklist()
    # Allow row to carry partial overrides
    overrides = row.get("prebid_compliance_checklist")
    if isinstance(overrides, dict):
        for k, v in overrides.items():
            if k in checklist and isinstance(v, dict):
                checklist[k] = {**checklist[k], **v}

    unreviewed: list[str] = []
    unsatisfied: list[str] = []
    unknown: list[str] = []
    for cat, cell in checklist.items():
        st = str((cell or {}).get("status") or "UNREVIEWED").upper()
        if st in {"UNREVIEWED", "NOT_REVIEWED"}:
            unreviewed.append(cat)
        elif st in {"UNSATISFIED", "FAIL", "MISSING"}:
            unsatisfied.append(cat)
        elif st in {"UNKNOWN", "UNCLEAR"}:
            unknown.append(cat)

    if force_incomplete and not row.get("prebid_package_fully_reviewed"):
        # Live enrichment never claims full package review
        unreviewed = list(dict.fromkeys(unreviewed + MANDATORY_CATEGORIES[:3]))

    blockers = []
    if unreviewed:
        blockers.append(f"unreviewed:{len(unreviewed)}")
    if unsatisfied:
        blockers.append(f"unsatisfied:{','.join(unsatisfied[:5])}")
    if unknown:
        blockers.append(f"unknown:{','.join(unknown[:5])}")

    ready = not blockers and bool(row.get("prebid_package_fully_reviewed"))
    state = READY_TO_BID if ready else (
        PRE_BID_OWNER_REVIEW
        if row.get("prebid_owner_authorized_review") and not unsatisfied
        else COMPLIANCE_INCOMPLETE
    )

    promising = build_what_we_are_promising(row, checklist)

    return {
        "kind": "PhaseL21PrebidCompliance",
        "ready_to_bid": False if not ready else True,
        "READY_TO_BID": ready,
        "compliance_state": state,
        "blockers": blockers,
        "unreviewed_count": len(unreviewed),
        "unsatisfied": unsatisfied,
        "unknown": unknown,
        "checklist": checklist,
        "WHAT_WE_ARE_PROMISING": promising,
        "auto_submit": False,
        "evaluated_at": now_utc().isoformat(),
        "note": "No bid submission. No outreach. Compliance gate is advisory until full package review.",
    }


def build_what_we_are_promising(row: dict[str, Any], checklist: dict[str, Any]) -> str:
    title = row.get("title") or "this solicitation"
    qty = row.get("quantity")
    uom = row.get("uom") or "units"
    product = (row.get("product_identity") or {}).get("mpn") or (row.get("nsn") or "the specified product")
    parts = [
        f"If the Government accepts a quote for '{title}', the company would be promising to supply "
        f"{qty if qty is not None else '[QUANTITY UNCONFIRMED]'} {uom} of {product} "
        f"meeting all solicitation specifications and amendments.",
        "Delivery, FOB, packaging, certifications, and submission formalities remain "
        "subject to completed solicitation-package review.",
        "M3 has NOT authorized bid submission.",
    ]
    unknown_qty = qty is None
    if unknown_qty:
        parts.append("Quantity is currently UNKNOWN — cannot promise a definite delivery quantity.")
    return " ".join(parts)
