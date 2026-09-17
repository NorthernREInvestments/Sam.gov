"""CO clarification gate, confidence, and template question drafts (no AI / no email)."""

from __future__ import annotations
from application_clock import now_utc, today_local

from datetime import date, datetime, timezone
from typing import Any

from missing_info import (
    CO_CLARIFICATION_CANDIDATE,
    CO_CLARIFICATION_REQUIRED,
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    EXTERNAL_REFERENCE_REQUIRED,
    FACT_COMMERCIAL,
    FACT_FINANCING,
    FACT_MANUFACTURER,
    FACT_REGULATORY,
    FACT_SOLICITATION,
    MATCH_DIRECT,
    MATCH_NONE,
    MATCH_POSSIBLE_INDIRECT,
    MATCH_RELATED,
    MISSING_CONFIRMED_LOCAL,
    POSSIBLE_MATCH_FOUND,
    RESOLVED,
    SECOND_PASS_PENDING,
)
from solicitation_package import PACKAGE_COMPLETE, PACKAGE_INCOMPLETE, PACKAGE_UNRESOLVED


def evaluate_co_clarification_gate(
    *,
    fact_class: str,
    necessary_for_bid: bool,
    necessary_for_execution: bool,
    search_audit: dict[str, Any] | None,
    package_status: str | None,
    already_answered: bool = False,
    clarification_deadline: date | str | None = None,
    today: date | None = None,
    unresolved_possible_match: bool | None = None,
    amendments_likely_unresolved: bool | None = None,
    external_source_more_appropriate: bool | None = None,
) -> dict[str, Any]:
    """
    SAFE_TO_ASK_CO only when all hard conditions pass.
    Incomplete package → acquire documents first, never auto-escalate to CO.
    """
    blockers: list[str] = []
    checked: dict[str, bool | str] = {
        "base_rfq_or_attachments": False,
        "bom_clin": False,
        "amendments": "unknown",
        "qa": "unknown",
        "pricing_schedule": False,
        "alternate_terminology": False,
        "cross_requirement_review": False,
        "package_completeness_considered": True,
    }

    if already_answered:
        return {
            "safe_to_ask_co": False,
            "status": RESOLVED,
            "blockers": ["already_answered"],
            "confidence_absent": CONFIDENCE_LOW,
            "checked": checked,
            "recommendation": "DO_NOT_ASK_ALREADY_ANSWERED",
        }

    # Deadline
    as_of = today or today_local()
    dl: date | None = None
    if isinstance(clarification_deadline, date):
        dl = clarification_deadline
    elif clarification_deadline:
        try:
            dl = date.fromisoformat(str(clarification_deadline)[:10])
        except ValueError:
            dl = None
    if dl is not None and dl < as_of:
        blockers.append("clarification_deadline_passed")

    fc = str(fact_class or "")
    if fc == FACT_COMMERCIAL:
        return {
            "safe_to_ask_co": False,
            "status": EXTERNAL_REFERENCE_REQUIRED,
            "blockers": ["commercial_fact_not_for_co"],
            "confidence_absent": CONFIDENCE_LOW,
            "checked": checked,
            "recommendation": "ASK_SUPPLIER_OR_MARKET_NOT_CO",
            "route": "COMMERCIAL",
        }
    if fc == FACT_FINANCING:
        return {
            "safe_to_ask_co": False,
            "status": EXTERNAL_REFERENCE_REQUIRED,
            "blockers": ["financing_fact_not_for_co"],
            "confidence_absent": CONFIDENCE_LOW,
            "checked": checked,
            "recommendation": "ASK_FINANCING_PROVIDER_NOT_CO",
            "route": "FINANCING",
        }
    if fc == FACT_MANUFACTURER:
        return {
            "safe_to_ask_co": False,
            "status": EXTERNAL_REFERENCE_REQUIRED,
            "blockers": ["manufacturer_fact_normally_external"],
            "confidence_absent": CONFIDENCE_LOW,
            "checked": checked,
            "recommendation": "ASK_MANUFACTURER_OR_AUTHORIZED_CHANNEL",
            "route": "MANUFACTURER",
        }
    if fc == FACT_REGULATORY and external_source_more_appropriate is not False:
        # Regulatory text often FAR/public — not CO unless solicitation-specific selection
        if external_source_more_appropriate is not False:
            return {
                "safe_to_ask_co": False,
                "status": EXTERNAL_REFERENCE_REQUIRED,
                "blockers": ["regulatory_fact_prefer_public_source"],
                "confidence_absent": CONFIDENCE_MEDIUM,
                "checked": checked,
                "recommendation": "CONSULT_REGULATORY_SOURCE_FIRST",
                "route": "REGULATORY",
            }

    if fc != FACT_SOLICITATION:
        blockers.append("fact_class_not_solicitation")

    if not (necessary_for_bid or necessary_for_execution):
        blockers.append("not_necessary_for_bid_or_execution")

    pkg = str(package_status or PACKAGE_UNRESOLVED)
    if pkg in {PACKAGE_INCOMPLETE, PACKAGE_UNRESOLVED}:
        blockers.append("solicitation_package_incomplete_acquire_documents_first")
        return {
            "safe_to_ask_co": False,
            "status": PACKAGE_INCOMPLETE if pkg == PACKAGE_INCOMPLETE else PACKAGE_UNRESOLVED,
            "blockers": blockers,
            "confidence_absent": CONFIDENCE_LOW,
            "checked": checked,
            "recommendation": "SOLICITATION_PACKAGE_INCOMPLETE — ACQUIRE MISSING DOCUMENTS FIRST",
            "route": "PACKAGE",
        }
    if pkg != PACKAGE_COMPLETE:
        blockers.append("package_not_complete")

    audit = search_audit or {}
    docs = audit.get("documents_checked") or []
    checked["base_rfq_or_attachments"] = any(
        d.get("checked") and d.get("document_type") in {"attachment", "rfq_rfp_ifb", "attachment_rollup", "base_solicitation"}
        for d in docs
    )
    checked["bom_clin"] = any(
        d.get("document_type") in {"bom_clin_schedule", "pricing_sheet", "attachment", "rfq_rfp_ifb", "attachment_rollup"}
        for d in docs
    )
    checked["pricing_schedule"] = any(
        d.get("document_type") in {"pricing_sheet", "attachment", "rfq_rfp_ifb"} for d in docs
    )
    checked["alternate_terminology"] = bool(audit.get("search_terms")) and len(audit.get("search_terms") or []) > 1
    checked["cross_requirement_review"] = True  # performed via match classification
    amd_docs = [d for d in docs if d.get("document_type") == "amendment"]
    qa_docs = [d for d in docs if d.get("document_type") == "qa"]
    checked["amendments"] = "checked" if amd_docs else "none_in_package"
    checked["qa"] = "checked" if qa_docs else "none_in_package"

    if not audit.get("exhaustive_local"):
        blockers.append("local_search_not_exhaustive")
    if not checked["alternate_terminology"]:
        blockers.append("alternate_terminology_not_searched")
    if len(docs) < 1:
        blockers.append("no_documents_checked")

    overall = audit.get("overall_match_class") or MATCH_NONE
    indirect = (
        unresolved_possible_match
        if unresolved_possible_match is not None
        else bool(audit.get("possible_indirect_remaining"))
    )
    if overall in {MATCH_DIRECT, MATCH_RELATED} or indirect or overall == MATCH_POSSIBLE_INDIRECT:
        return {
            "safe_to_ask_co": False,
            "status": POSSIBLE_MATCH_FOUND,
            "blockers": blockers + ["possible_or_related_match_requires_operator_review"],
            "confidence_absent": CONFIDENCE_LOW,
            "checked": checked,
            "recommendation": "REVIEW_POSSIBLE_MATCH_BEFORE_CO",
            "route": "REVIEW",
            "overall_match_class": overall,
        }

    if amendments_likely_unresolved:
        blockers.append("amendments_or_qa_may_contain_answer")

    if external_source_more_appropriate:
        blockers.append("external_source_more_appropriate")

    # Single failed exact search cannot escalate — require multi-term exhaustive
    if len(audit.get("search_terms") or []) < 2:
        blockers.append("insufficient_search_terms_single_search_not_proof")

    if blockers:
        return {
            "safe_to_ask_co": False,
            "status": MISSING_CONFIRMED_LOCAL if overall == MATCH_NONE and audit.get("exhaustive_local") else MISSING_CONFIRMED_LOCAL,
            "blockers": blockers,
            "confidence_absent": CONFIDENCE_MEDIUM if audit.get("exhaustive_local") else CONFIDENCE_LOW,
            "checked": checked,
            "recommendation": "CONTINUE_LOCAL_OR_PACKAGE_WORK",
            "route": "LOCAL",
            "overall_match_class": overall,
            "second_pass_status": SECOND_PASS_PENDING if overall == MATCH_NONE and audit.get("exhaustive_local") else None,
        }

    # All clear → candidate (REQUIRED still needs operator confirmation — hard to reach)
    confidence = CONFIDENCE_HIGH if (
        audit.get("exhaustive_local")
        and checked["alternate_terminology"]
        and overall == MATCH_NONE
        and pkg == PACKAGE_COMPLETE
    ) else CONFIDENCE_MEDIUM

    return {
        "safe_to_ask_co": True,
        "status": CO_CLARIFICATION_CANDIDATE,
        "blockers": [],
        "confidence_absent": confidence,
        "checked": checked,
        "recommendation": "CO_CLARIFICATION_RECOMMENDED",
        "route": "CO",
        "overall_match_class": overall,
        # REQUIRED only after operator explicitly promotes candidate
        "can_promote_to_required": True,
        "status_note": "CO_CLARIFICATION_REQUIRED requires explicit operator confirmation",
    }


def promote_to_co_required(gate: dict[str, Any], *, operator_confirmed: bool) -> dict[str, Any]:
    """CO_CLARIFICATION_REQUIRED is difficult — needs explicit operator confirmation."""
    out = dict(gate or {})
    if not operator_confirmed:
        out["status"] = out.get("status") or CO_CLARIFICATION_CANDIDATE
        out["promoted"] = False
        return out
    if not out.get("safe_to_ask_co"):
        out["promoted"] = False
        out["promote_blocked"] = out.get("blockers") or ["not_safe_to_ask_co"]
        return out
    out["status"] = CO_CLARIFICATION_REQUIRED
    out["promoted"] = True
    return out


def draft_co_question(
    *,
    verified_context: str,
    missing_item: str,
    documents_actually_checked: list[str] | None = None,
) -> dict[str, Any]:
    """
    Template draft only — never claims searches that did not occur.
    Operator must review before sending. No automatic email.
    """
    ctx = (verified_context or "").strip()
    missing = (missing_item or "").strip()
    checked = [c for c in (documents_actually_checked or []) if c]
    checked_clause = ""
    if checked:
        checked_clause = (
            " We reviewed the following local solicitation materials without locating this detail: "
            + "; ".join(checked[:12])
            + "."
        )
    if ctx:
        body = (
            f"{ctx} However, we have been unable to identify {missing} in the solicitation "
            f"or associated local attachments.{checked_clause} "
            f"Please confirm {missing}."
        )
    else:
        body = (
            f"We have been unable to identify {missing} in the solicitation or associated "
            f"local attachments.{checked_clause} Please confirm {missing}."
        )
    return {
        "draft": body,
        "verified_context_used": ctx or None,
        "missing_item": missing,
        "documents_cited_as_checked": checked,
        "operator_must_review": True,
        "auto_email": False,
        "provenance": "TEMPLATE_DRAFT",
        "LIVE_API_REQUESTS": 0,
        "created_at": now_utc().isoformat(),
    }
