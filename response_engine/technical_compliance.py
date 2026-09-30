"""R2 technical compliance — characteristic-level, evidence required for PASS."""

from __future__ import annotations

import re
from typing import Any

from response_engine.models import new_id
from response_engine.product_offer import evaluate_exact_match
from response_engine.r2_constants import (
    BRAND_NAME_OR_EQUAL,
    EXACT_PART_NUMBER,
    OWNER_CONFIRMATION_REQUIRED,
    PASS_VERIFIED,
    REVIEW_REQUIRED,
    TECHNICAL_FAIL,
    TECHNICAL_UNKNOWN,
)


def new_compliance_item(
    *,
    line_item_id: str,
    requirement_id: str | None,
    characteristic: str,
    required_value: str | None,
    offered_value: str | None = None,
    compliance_status: str = TECHNICAL_UNKNOWN,
    evidence_id: str | None = None,
    evidence_location: str | None = None,
    materiality: str = "MATERIAL",
    confidence: str = "UNKNOWN",
    notes: str | None = None,
    comparison_operator: str = "EQUALS",
) -> dict[str, Any]:
    return {
        "kind": "TechnicalComplianceItem",
        "technical_item_id": new_id("TC"),
        "line_item_id": line_item_id,
        "requirement_id": requirement_id,
        "characteristic": characteristic,
        "required_value": required_value,
        "comparison_operator": comparison_operator,
        "offered_value": offered_value,
        "offered_unit": None,
        "evidence_id": evidence_id,
        "evidence_location": evidence_location,
        "compliance_status": compliance_status,
        "confidence": confidence,
        "materiality": materiality,
        "notes": notes,
    }


def new_product_evidence(
    *,
    source_type: str,
    source_name: str | None = None,
    source_url: str | None = None,
    document_hash: str | None = None,
    page: str | None = None,
    section: str | None = None,
    characteristic: str | None = None,
    extracted_value: str | None = None,
    confidence: str = "UNKNOWN",
    authoritative_level: str = "UNKNOWN",
) -> dict[str, Any]:
    return {
        "kind": "ProductEvidence",
        "evidence_id": new_id("PE"),
        "source_type": source_type,
        "source_name": source_name,
        "source_url": source_url,
        "document_hash": document_hash,
        "page": page,
        "section": section,
        "characteristic": characteristic,
        "extracted_value": extracted_value,
        "confidence": confidence,
        "authoritative_level": authoritative_level,
    }


def evaluate_line_technical_compliance(
    *,
    line: dict[str, Any],
    offered: dict[str, Any] | None,
    requirements: list[dict[str, Any]],
    evidence: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Compare line requirements vs offered product. Generic 'meets requirements' ≠ PASS."""
    evidence = evidence or []
    items: list[dict[str, Any]] = []
    mode = (offered or {}).get("product_mode") or line.get("product_mode") or ""

    # Exact part / brand
    if mode in {EXACT_PART_NUMBER, "BRAND_NAME_ONLY"} or line.get("required_mpn"):
        status_match = evaluate_exact_match(
            required_mpn=line.get("required_mpn"),
            offered_mpn=(offered or {}).get("MPN"),
            product_mode=mode or EXACT_PART_NUMBER,
        )
        if status_match == "EXACT_MATCH":
            st = PASS_VERIFIED
            conf = "HIGH"
        elif status_match == "MISMATCH":
            st = TECHNICAL_FAIL
            conf = "HIGH"
        else:
            st = TECHNICAL_UNKNOWN if offered else REVIEW_REQUIRED
            conf = "MEDIUM"
        # Superseding only with evidence
        if status_match == "MISMATCH" and (offered or {}).get("superseding_part"):
            if (offered or {}).get("supersession_evidence_id"):
                st = OWNER_CONFIRMATION_REQUIRED
                conf = "MEDIUM"
            else:
                st = TECHNICAL_FAIL
        items.append(
            new_compliance_item(
                line_item_id=line["line_item_id"],
                requirement_id=None,
                characteristic="EXACT_PART_NUMBER",
                required_value=line.get("required_mpn"),
                offered_value=(offered or {}).get("MPN"),
                compliance_status=st,
                confidence=conf,
                notes=status_match,
            )
        )

    # Brand-or-equal salient characteristics from requirements
    if mode == BRAND_NAME_OR_EQUAL or any(
        r.get("requirement_category") in {"BRAND_OR_EQUAL", "SALIENT_CHARACTERISTIC"} for r in requirements
    ):
        for req in requirements:
            if req.get("superseded"):
                continue
            cat = req.get("requirement_category")
            if cat not in {"SALIENT_CHARACTERISTIC", "BRAND_OR_EQUAL", "TECHNICAL_SPECIFICATION", "WARRANTY"}:
                continue
            # Reject generic equal claims
            text = (req.get("requirement_text") or "").lower()
            offered_claim = str((offered or {}).get("product_description") or "").lower()
            if "meets all requirements" in offered_claim or "or equal" == offered_claim.strip():
                items.append(
                    new_compliance_item(
                        line_item_id=line["line_item_id"],
                        requirement_id=req.get("requirement_id"),
                        characteristic=cat,
                        required_value=req.get("requirement_text"),
                        offered_value=(offered or {}).get("product_description"),
                        compliance_status=TECHNICAL_FAIL,
                        confidence="HIGH",
                        notes="generic equal statement rejected",
                    )
                )
                continue
            ev = _find_evidence(evidence, cat, req.get("requirement_text"))
            if ev and ev.get("extracted_value"):
                items.append(
                    new_compliance_item(
                        line_item_id=line["line_item_id"],
                        requirement_id=req.get("requirement_id"),
                        characteristic=cat,
                        required_value=_excerpt_required(req),
                        offered_value=ev.get("extracted_value"),
                        compliance_status=PASS_VERIFIED,
                        evidence_id=ev.get("evidence_id"),
                        evidence_location=_ev_loc(ev),
                        confidence=ev.get("confidence") or "MEDIUM",
                    )
                )
            else:
                items.append(
                    new_compliance_item(
                        line_item_id=line["line_item_id"],
                        requirement_id=req.get("requirement_id"),
                        characteristic=cat,
                        required_value=_excerpt_required(req),
                        offered_value=None,
                        compliance_status=REVIEW_REQUIRED if offered else TECHNICAL_UNKNOWN,
                        confidence="LOW",
                        notes="evidence required for PASS_VERIFIED",
                    )
                )

    # Condition
    if offered and line.get("required_condition"):
        if (offered.get("condition") or "").upper() != str(line["required_condition"]).upper():
            items.append(
                new_compliance_item(
                    line_item_id=line["line_item_id"],
                    requirement_id=None,
                    characteristic="CONDITION",
                    required_value=line.get("required_condition"),
                    offered_value=offered.get("condition"),
                    compliance_status=TECHNICAL_FAIL,
                    confidence="HIGH",
                )
            )

    # Country of origin when required
    for req in requirements:
        if req.get("superseded"):
            continue
        if req.get("requirement_category") == "COUNTRY_OF_ORIGIN":
            coo = (offered or {}).get("country_of_origin")
            items.append(
                new_compliance_item(
                    line_item_id=line["line_item_id"],
                    requirement_id=req.get("requirement_id"),
                    characteristic="COUNTRY_OF_ORIGIN",
                    required_value="required by solicitation",
                    offered_value=coo,
                    compliance_status=PASS_VERIFIED if coo else REVIEW_REQUIRED,
                    confidence="MEDIUM" if coo else "LOW",
                )
            )

    return items


def summarize_technical(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {
        PASS_VERIFIED: 0,
        TECHNICAL_FAIL: 0,
        TECHNICAL_UNKNOWN: 0,
        REVIEW_REQUIRED: 0,
        OWNER_CONFIRMATION_REQUIRED: 0,
    }
    for i in items:
        st = i.get("compliance_status")
        if st in counts:
            counts[st] += 1
    hard_fail = counts[TECHNICAL_FAIL] > 0
    return {
        "counts": counts,
        "hard_fail": hard_fail,
        "needs_review": counts[REVIEW_REQUIRED] + counts[OWNER_CONFIRMATION_REQUIRED] + counts[TECHNICAL_UNKNOWN] > 0,
        "all_pass": counts[TECHNICAL_FAIL] == 0 and counts[REVIEW_REQUIRED] == 0 and counts[TECHNICAL_UNKNOWN] == 0 and counts[PASS_VERIFIED] > 0,
    }


def _find_evidence(evidence: list[dict[str, Any]], cat: str, text: str | None) -> dict[str, Any] | None:
    for ev in evidence:
        if ev.get("characteristic") == cat:
            return ev
        if text and ev.get("extracted_value") and str(ev.get("characteristic") or "").lower() in text.lower():
            return ev
    return None


def _excerpt_required(req: dict[str, Any]) -> str:
    return (req.get("requirement_text") or "")[:200]


def _ev_loc(ev: dict[str, Any]) -> str | None:
    parts = [p for p in (ev.get("source_name"), f"p.{ev['page']}" if ev.get("page") else None, ev.get("section")) if p]
    return " / ".join(parts) if parts else None
