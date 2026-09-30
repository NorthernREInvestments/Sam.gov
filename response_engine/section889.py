"""R3 Section 889 — never answer from absence of evidence."""

from __future__ import annotations

import re
from typing import Any

from response_engine.models import new_id
from response_engine.r3_constants import (
    APPLIES,
    DOES_NOT_APPLY,
    OWNER_CONFIRMATION_REQUIRED,
    POSSIBLY_APPLIES,
    REVIEW_REQUIRED,
    UNKNOWN,
)

_889_CLAUSE = re.compile(
    r"52\.204[-–]?2[456]|Section\s+889|covered\s+telecommunications\s+equipment|"
    r"prohibited\s+telecommunications|"
    r"\bHuawei\b|\bZTE\b|\bHytera\b|\bHikvision\b|\bDahua\b",
    re.I,
)
_889_SOLICITATION_SPECIFIC = re.compile(
    r"52\.204[-–]?24|representation\s+regarding\s+certain\s+telecommunications|"
    r"offeror\s+must\s+represent.*covered\s+telecom",
    re.I,
)


def evaluate_section_889(
    *,
    solicitation_text: str | None = None,
    clauses: list[str] | None = None,
    sam_annual_889: str | None = None,
    owner_attestation: dict[str, Any] | None = None,
    product_concern: bool | None = None,
    product_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    blob = " ".join([solicitation_text or "", " ".join(clauses or [])])
    found = bool(_889_CLAUSE.search(blob)) if blob.strip() else False
    sol_specific = bool(_889_SOLICITATION_SPECIFIC.search(blob)) if blob.strip() else False

    applicability = DOES_NOT_APPLY
    if found:
        applicability = APPLIES
    elif blob.strip():
        # Absence of clause text ≠ safe "does not apply" for all procurements —
        # mark POSSIBLY when federal commercial language is thin
        applicability = POSSIBLY_APPLIES if "FAR" in blob.upper() else DOES_NOT_APPLY
    else:
        applicability = UNKNOWN

    sam = (sam_annual_889 or "").upper()
    owner = owner_attestation or {}
    owner_confirmed = owner.get("owner_confirmed") is True
    owner_answer = owner.get("answer")

    dimensions = {
        "sam_annual_representation": {
            "status": sam if sam not in ("", "UNKNOWN") else UNKNOWN,
            "covers_annual": sam in ("VERIFIED_YES", "VERIFIED_NO", "OWNER_CONFIRMED_YES", "OWNER_CONFIRMED_NO", "YES", "NO"),
        },
        "solicitation_specific": {
            "required": sol_specific or (applicability == APPLIES),
            "status": UNKNOWN,
        },
        "company_use": {
            "status": UNKNOWN,
            "owner_confirmation_required": True,
        },
        "product_covered_telecom": {
            "concern": product_concern,
            "status": UNKNOWN if product_concern is None else ("FAIL" if product_concern else REVIEW_REQUIRED),
            "evidence": product_evidence,
        },
    }

    matrix_status = UNKNOWN
    plain = "Section 889 status unknown"
    next_action = None

    if applicability == UNKNOWN and not found:
        matrix_status = UNKNOWN
        plain = "Section 889 status unknown — solicitation text/clauses not available"
        dimensions["company_use"]["owner_confirmation_required"] = False
        next_action = "Load solicitation package before resolving Section 889"
    elif applicability == DOES_NOT_APPLY and not found:
        matrix_status = "NOT_APPLICABLE"
        plain = "No Section 889 clause identified in provided text"
        dimensions["company_use"]["owner_confirmation_required"] = False
    elif applicability == POSSIBLY_APPLIES and not found:
        # FAR mentions without an actual 889 clause — review, do not auto-ask owner cert
        matrix_status = REVIEW_REQUIRED
        plain = "Federal language present but Section 889 clause not clearly identified — review"
        dimensions["company_use"]["owner_confirmation_required"] = False
        next_action = "Confirm whether Section 889 representation is incorporated"
    elif product_concern is True:
        matrix_status = "FAIL"
        plain = "Product/source covered-telecom concern identified"
    elif owner_confirmed and owner_answer in ("YES", "NO", "OWNER_CONFIRMED_YES", "OWNER_CONFIRMED_NO"):
        matrix_status = "PASS_VERIFIED" if "NO" in str(owner_answer).upper() or owner_answer == "NO" else REVIEW_REQUIRED
        # "YES we use covered" is material — review, not silent pass
        if "YES" in str(owner_answer).upper() and "NO" not in str(owner_answer).upper():
            matrix_status = REVIEW_REQUIRED
            plain = "Owner confirmed company-use answer — legal review of representation required"
        else:
            plain = "Owner confirmed Section 889 company-use representation"
        dimensions["company_use"]["status"] = owner_answer
        dimensions["company_use"]["owner_confirmation_required"] = False
    elif dimensions["sam_annual_representation"]["covers_annual"] and not sol_specific:
        matrix_status = REVIEW_REQUIRED
        plain = "SAM annual 889 present — still confirm solicitation-specific needs"
        next_action = "Confirm whether solicitation-specific 889 representation is required"
    else:
        matrix_status = OWNER_CONFIRMATION_REQUIRED
        plain = "Need owner confirmation"
        next_action = (
            "Does the company use covered telecommunications equipment/services "
            "in the manner addressed by this representation?"
        )

    return {
        "kind": "Section889Decision",
        "decision_id": new_id("S889"),
        "applicability": applicability,
        "clause_signals": bool(found),
        "solicitation_specific": sol_specific,
        "dimensions": dimensions,
        "status": matrix_status,
        "plain": plain,
        "next_action": next_action,
        "owner_question": (
            "Does the company use covered telecommunications equipment/services "
            "in the manner addressed by this representation?"
            if matrix_status == OWNER_CONFIRMATION_REQUIRED
            else None
        ),
        "trace": {
            "sam_vs_solicitation": "distinguish annual SAM vs solicitation-specific",
            "absence_of_evidence": "never answers no from empty evidence",
        },
        "LIVE_API_REQUESTS": 0,
    }
