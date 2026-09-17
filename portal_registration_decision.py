"""Portal registration decision support — not autonomous registration."""

from __future__ import annotations

from typing import Any

from public_evidence_constants import (
    DEADLINE_CONFLICT_UNRESOLVED,
    PORTAL_NOT_YET,
    PORTAL_REQUIRED,
    PORTAL_TOO_LATE,
    PORTAL_UNKNOWN,
    PORTAL_WORTHWHILE,
)


def assess_portal_registration(
    *,
    deadline_reconciliation: dict[str, Any] | None = None,
    product_id_state: str | None = None,
    economics: dict[str, Any] | None = None,
    authoritative_spec_available: bool = False,
    auth_blocked_critical_doc: bool = True,
    bid_submission_requires_registration: bool | None = None,
    supplier_quotes_possible_before_registration: bool = True,
    ten_k_plausible: str | None = None,  # YES | NO | UNKNOWN
    deadline_evaluation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Operator decision support only. Does not register or contact agency.
    """
    factors: list[str] = []
    dl = deadline_reconciliation or {}
    status = PORTAL_UNKNOWN

    # Runtime clock: expired deadline blocks registration regardless of portal labels
    de = deadline_evaluation or {}
    if de.get("deadline_status") == "EXPIRED" or de.get("portal_registration_temporally_viable") is False:
        return {
            "state": PORTAL_TOO_LATE,
            "factors": ["operational_deadline_passed_runtime_clock"],
            "recommendation": "Do not register for this solicitation; deadline appears passed.",
            "autonomous_registration": False,
            "agency_outreach": 0,
            "supplier_outreach": 0,
            "lender_outreach": 0,
            "bid_submissions": 0,
        }

    # Too late if operational deadline already passed (caller may pass viability)
    if dl.get("deadline_passed") is True:
        return {
            "state": PORTAL_TOO_LATE,
            "factors": ["operational_deadline_passed"],
            "recommendation": "Do not register for this solicitation; deadline appears passed.",
            "autonomous_registration": False,
            "agency_outreach": 0,
        }

    if dl.get("status") == DEADLINE_CONFLICT_UNRESOLVED and not dl.get("conflict_resolved"):
        factors.append("deadline_conflict_unresolved_conservative_date_in_force")

    if auth_blocked_critical_doc and not authoritative_spec_available:
        factors.append("authoritative_spec_requires_portal_or_operator_download")
        status = PORTAL_REQUIRED
    elif auth_blocked_critical_doc:
        factors.append("portal_still_needed_for_bid_package_even_if_spec_reconstructed")

    if product_id_state and product_id_state not in {None, "INSUFFICIENT_INFORMATION"}:
        factors.append(f"product_identification={product_id_state}")
    else:
        factors.append("product_identification_weak")

    econ = economics or {}
    if econ.get("status") == "EVIDENCE_BOUNDED":
        factors.append("evidence_bounded_economics_available")
        if ten_k_plausible == "YES":
            factors.append("ten_k_profit_plausible_on_evidence_range")
            if status != PORTAL_REQUIRED:
                status = PORTAL_WORTHWHILE
        elif ten_k_plausible == "NO":
            factors.append("ten_k_profit_not_supported_by_evidence_range")
            if status != PORTAL_REQUIRED:
                status = PORTAL_NOT_YET
        else:
            factors.append("ten_k_plausibility_unknown")
    else:
        factors.append("economics_unknown_insufficient_public_price_evidence")
        if status != PORTAL_REQUIRED:
            status = PORTAL_NOT_YET

    if supplier_quotes_possible_before_registration:
        factors.append("supplier_quotes_can_be_prepared_before_registration")
    else:
        factors.append("supplier_quotes_blocked_without_more_spec_detail")

    if bid_submission_requires_registration is True:
        factors.append("bid_submission_requires_registration")
        if status == PORTAL_NOT_YET and ten_k_plausible == "YES":
            status = PORTAL_WORTHWHILE
    elif bid_submission_requires_registration is False:
        factors.append("bid_submission_registration_requirement_unknown_or_false")
    else:
        factors.append("bid_submission_registration_requirement_not_fabricated")

    if status == PORTAL_UNKNOWN:
        status = PORTAL_NOT_YET if not auth_blocked_critical_doc else PORTAL_REQUIRED

    recs = {
        PORTAL_NOT_YET: "Continue public/supplier-quote prep; registration not yet justified by economics.",
        PORTAL_WORTHWHILE: "Registration appears worthwhile to obtain authoritative package if deadline allows.",
        PORTAL_REQUIRED: "Authoritative attachment/bid path blocked without authorized portal access or operator download.",
        PORTAL_TOO_LATE: "Do not register — deadline appears passed.",
        PORTAL_UNKNOWN: "Insufficient evidence to advise registration.",
    }

    return {
        "state": status,
        "factors": factors,
        "recommendation": recs.get(status, recs[PORTAL_UNKNOWN]),
        "autonomous_registration": False,
        "agency_outreach": 0,
        "supplier_outreach": 0,
        "lender_outreach": 0,
        "bid_submissions": 0,
    }
