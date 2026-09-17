"""Funding source / supplier knowledge base — evidence-first, no guessed underwriting."""

from __future__ import annotations
from application_clock import now_utc

from datetime import datetime, timedelta, timezone
from typing import Any

from funding_path_constants import (
    DATA_FIXTURE,
    DATA_PRODUCTION,
    EV_INFERRED,
    EV_PROPOSED_AI,
    EV_STALE,
    EV_UNKNOWN,
    EV_VERIFIED_BY_CALL,
    EV_VERIFIED_PUBLIC,
    PG_CONDITIONAL,
    PG_NOT_REQUIRED,
    PG_REQUIRED,
    PG_UNKNOWN,
    SOURCE_ACTIVE,
    SOURCE_NEEDS_REVERIFY,
    VERIFIED_EVIDENCE,
)

DEFAULT_REVERIFY_DAYS = 90


def _utc(now: datetime | None = None) -> datetime:
    n = now or now_utc()
    if n.tzinfo is None:
        n = n.replace(tzinfo=timezone.utc)
    return n


def criterion_evidence(
    *,
    criterion_name: str,
    value: Any = None,
    verification_status: str = EV_UNKNOWN,
    source_url: str | None = None,
    source_title: str | None = None,
    source_quote_or_summary: str | None = None,
    verified_at: str | None = None,
    contact_name: str | None = None,
    contact_title: str | None = None,
    contact_phone: str | None = None,
    contact_email: str | None = None,
    operator_notes: str | None = None,
    confidence: str | None = None,
    reverification_due_at: str | None = None,
) -> dict[str, Any]:
    """One criterion with mandatory provenance fields."""
    status = (verification_status or EV_UNKNOWN).upper()
    # Marketing language cannot become NOT_REQUIRED without stronger evidence
    if status == EV_INFERRED and criterion_name in {
        "personal_guarantee",
        "borrower_cash_contribution_required",
        "personal_credit_checked",
    }:
        # Keep value but mark as non-verified
        pass
    return {
        "criterion_name": criterion_name,
        "value": value,
        "verification_status": status,
        "is_verified": status in VERIFIED_EVIDENCE,
        "source_url": source_url,
        "source_title": source_title,
        "source_quote_or_summary": source_quote_or_summary,
        "verified_at": verified_at,
        "contact_name": contact_name,
        "contact_title": contact_title,
        "contact_phone": contact_phone,
        "contact_email": contact_email,
        "operator_notes": operator_notes,
        "confidence": confidence or ("HIGH" if status in VERIFIED_EVIDENCE else "LOW"),
        "reverification_due_at": reverification_due_at,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


def interpret_pg_marketing(quote: str | None) -> dict[str, Any]:
    """
    Marketing like 'No personal guarantee options available' ≠ NOT_REQUIRED.
    Returns CONDITIONAL / NEEDS_VERIFICATION evidence, never auto NOT_REQUIRED.
    """
    q = (quote or "").strip()
    lower = q.lower()
    if not q:
        return criterion_evidence(
            criterion_name="personal_guarantee",
            value=PG_UNKNOWN,
            verification_status=EV_UNKNOWN,
        )
    if "no personal guarantee option" in lower or "no pg option" in lower:
        return criterion_evidence(
            criterion_name="personal_guarantee",
            value=PG_CONDITIONAL,
            verification_status=EV_INFERRED,
            source_quote_or_summary=q,
            confidence="LOW",
            operator_notes=(
                "Marketing mentions no-PG options; treat as CONDITIONAL until underwriting "
                "criteria are verified by call/email/document."
            ),
        )
    if "personal guarantee required" in lower or "pg required" in lower:
        return criterion_evidence(
            criterion_name="personal_guarantee",
            value=PG_REQUIRED,
            verification_status=EV_INFERRED,
            source_quote_or_summary=q,
            confidence="LOW",
        )
    return criterion_evidence(
        criterion_name="personal_guarantee",
        value=PG_UNKNOWN,
        verification_status=EV_INFERRED,
        source_quote_or_summary=q,
        confidence="LOW",
    )


def empty_funding_source_profile(*, data_class: str = DATA_PRODUCTION) -> dict[str, Any]:
    """All criteria UNKNOWN unless explicitly set with evidence."""
    return {
        "source_name": None,
        "source_type": None,
        "website": None,
        "general_phone": None,
        "government_contract_phone": None,
        "preferred_contact_name": None,
        "preferred_contact_title": None,
        "preferred_department": None,
        "contact_email": None,
        "geography_served": None,
        "government_customer_types_supported": None,  # list or UNKNOWN
        "product_categories_supported": None,
        "excluded_categories": None,
        "minimum_transaction": None,
        "maximum_transaction": None,
        "minimum_funding_amount": None,
        "maximum_funding_amount": None,
        "minimum_margin_pct": None,
        "startup_allowed": None,
        "new_entity_allowed": None,
        "first_contract_allowed": None,
        "first_government_contract_allowed": None,
        "minimum_time_in_business": None,
        "minimum_annual_revenue": None,
        "minimum_historical_revenue": None,
        "personal_credit_checked": None,
        "minimum_personal_fico": None,
        "personal_guarantee": PG_UNKNOWN,
        "business_credit_required": None,
        "borrower_cash_contribution_required": None,
        "minimum_cash_contribution_pct": None,
        "supplier_must_be_established": None,
        "supplier_verification_required": None,
        "direct_supplier_payment_supported": None,
        "direct_ship_supported": None,
        "assignment_of_claims_required": None,
        "controlled_account_required": None,
        "lockbox_required": None,
        "UCC_required": None,
        "government_award_required": None,
        "signed_PO_required": None,
        "executed_contract_required": None,
        "pre_bid_screening_available": None,
        "post_award_only": None,
        "typical_approval_days": None,
        "typical_funding_days": None,
        "fee_type": None,
        "estimated_fee_min": None,
        "estimated_fee_max": None,
        "recourse_type": None,
        "documents_required": None,
        "notes": None,
        "status": SOURCE_ACTIVE,
        "data_class": data_class,
        "criteria_evidence": {},  # criterion_name -> evidence dict
        "last_verified_at": None,
        "reverification_due_at": None,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
        "SAM": 0,
        "USAspending": 0,
        "paid": 0,
    }


def empty_supplier_funding_profile(*, data_class: str = DATA_PRODUCTION) -> dict[str, Any]:
    return {
        "supplier_name": None,
        "product_categories": None,
        "brands": None,
        "government_sales_experience": None,
        "deposit_required": None,
        "deposit_pct": None,
        "payment_before_ship": None,
        "payment_on_ship": None,
        "payment_after_delivery": None,
        "net_terms": None,
        "terms_verified": False,
        "new_customer_terms_available": None,
        "credit_application_required": None,
        "personal_credit_required": None,
        "personal_guarantee_required": None,
        "direct_ship_available": None,
        "direct_ship_to_government_available": None,
        "will_accept_financier_direct_payment": None,
        "will_accept_assignment_payment_control": None,
        "quote_turnaround_hours": None,
        "quote_validity_days": None,
        "contact_name": None,
        "contact_title": None,
        "phone": None,
        "email": None,
        "data_class": data_class,
        "criteria_evidence": {},
        "last_verified_at": None,
        "reverification_due_at": None,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


def set_criterion(
    profile: dict[str, Any],
    criterion_name: str,
    value: Any,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Attach evidence and optionally set top-level field when VERIFIED."""
    out = dict(profile)
    ev = dict(evidence)
    ev["criterion_name"] = criterion_name
    ev["value"] = value if ev.get("value") is None else ev.get("value")
    criteria = dict(out.get("criteria_evidence") or {})
    criteria[criterion_name] = ev
    out["criteria_evidence"] = criteria
    # Only promote VERIFIED (not INFERRED / AI-proposed) to top-level facts
    if ev.get("verification_status") in VERIFIED_EVIDENCE and criterion_name in out:
        out[criterion_name] = value
        out["last_verified_at"] = ev.get("verified_at") or out.get("last_verified_at")
    return out


def propose_ai_criterion_update(
    *,
    criterion_name: str,
    proposed_value: Any,
    reasoning: str | None = None,
) -> dict[str, Any]:
    """AI-proposed facts require human confirmation before becoming VERIFIED_BY_CALL."""
    return {
        "proposed": criterion_evidence(
            criterion_name=criterion_name,
            value=proposed_value,
            verification_status=EV_PROPOSED_AI,
            operator_notes=reasoning,
            confidence="UNCONFIRMED",
        ),
        "requires_confirmation": True,
        "becomes_verified_on_confirm": EV_VERIFIED_BY_CALL,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


def confirm_proposed_criterion(
    profile: dict[str, Any],
    proposed: dict[str, Any],
    *,
    actor: str,
    contact_name: str | None = None,
) -> dict[str, Any]:
    """Human confirms AI/operator proposed fact → VERIFIED_BY_CALL."""
    prop = proposed.get("proposed") or proposed
    if not actor or not str(actor).strip():
        raise ValueError("confirmation requires actor")
    ev = criterion_evidence(
        criterion_name=prop["criterion_name"],
        value=prop.get("value"),
        verification_status=EV_VERIFIED_BY_CALL,
        verified_at=_utc().isoformat(),
        contact_name=contact_name,
        operator_notes=f"Confirmed by {actor}. {prop.get('operator_notes') or ''}".strip(),
        confidence="HIGH",
        reverification_due_at=(_utc() + timedelta(days=DEFAULT_REVERIFY_DAYS)).isoformat(),
    )
    return set_criterion(profile, prop["criterion_name"], prop.get("value"), ev)


def mark_stale_criteria(
    profile: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Flip verified criteria past reverification_due_at to STALE."""
    out = dict(profile)
    now_u = _utc(now)
    criteria = dict(out.get("criteria_evidence") or {})
    changed = False
    for name, ev in list(criteria.items()):
        if not isinstance(ev, dict):
            continue
        due = ev.get("reverification_due_at")
        if not due:
            continue
        try:
            due_dt = datetime.fromisoformat(str(due).replace("Z", "+00:00"))
            if due_dt.tzinfo is None:
                due_dt = due_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if due_dt < now_u and ev.get("verification_status") in VERIFIED_EVIDENCE:
            criteria[name] = {**ev, "verification_status": EV_STALE, "is_verified": False}
            changed = True
    out["criteria_evidence"] = criteria
    if changed:
        out["status"] = SOURCE_NEEDS_REVERIFY
    return out


def criteria_needing_reverification(profile: dict[str, Any]) -> list[str]:
    return [
        name
        for name, ev in (profile.get("criteria_evidence") or {}).items()
        if isinstance(ev, dict) and ev.get("verification_status") in {EV_STALE, EV_UNKNOWN, EV_INFERRED}
    ]


def unknown_criteria_count(profile: dict[str, Any]) -> int:
    keys = [
        "personal_guarantee",
        "borrower_cash_contribution_required",
        "personal_credit_checked",
        "minimum_transaction",
        "maximum_transaction",
        "minimum_margin_pct",
        "startup_allowed",
        "first_contract_allowed",
        "first_government_contract_allowed",
        "government_customer_types_supported",
        "direct_supplier_payment_supported",
    ]
    n = 0
    for k in keys:
        ev = (profile.get("criteria_evidence") or {}).get(k)
        val = profile.get(k)
        if ev and ev.get("verification_status") in VERIFIED_EVIDENCE:
            continue
        if val is None or val == PG_UNKNOWN or val == EV_UNKNOWN:
            n += 1
    return n


def fixture_compatible_po_lender() -> dict[str, Any]:
    """TEST/FIXTURE only — verified-compatible lender for unit tests."""
    p = empty_funding_source_profile(data_class=DATA_FIXTURE)
    p["source_name"] = "Fixture GovPO Capital"
    p["source_type"] = "PO_FINANCE"
    p["general_phone"] = "555-0100"
    p["preferred_contact_name"] = "Underwriting Desk"
    p["preferred_department"] = "Underwriting"
    now = _utc().isoformat()
    due = (_utc() + timedelta(days=DEFAULT_REVERIFY_DAYS)).isoformat()

    def v(name: str, value: Any) -> dict[str, Any]:
        return criterion_evidence(
            criterion_name=name,
            value=value,
            verification_status=EV_VERIFIED_BY_CALL,
            verified_at=now,
            contact_name="Test Underwriter",
            operator_notes="TEST_FIXTURE evidence",
            reverification_due_at=due,
            confidence="HIGH",
        )

    specs = {
        "personal_guarantee": PG_NOT_REQUIRED,
        "borrower_cash_contribution_required": False,
        "minimum_cash_contribution_pct": 0,
        "personal_credit_checked": False,
        "minimum_personal_fico": None,
        "minimum_transaction": 50000,
        "maximum_transaction": 5000000,
        "minimum_margin_pct": 8.0,
        "startup_allowed": True,
        "new_entity_allowed": True,
        "first_contract_allowed": True,
        "first_government_contract_allowed": True,
        "government_customer_types_supported": ["federal", "state", "local", "cooperative"],
        "product_categories_supported": ["IT", "equipment", "product_resale"],
        "direct_supplier_payment_supported": True,
        "direct_ship_supported": True,
        "pre_bid_screening_available": True,
        "post_award_only": False,
        "typical_approval_days": 5,
        "typical_funding_days": 3,
    }
    for k, val in specs.items():
        p = set_criterion(p, k, val, v(k, val))
        p[k] = val
    p["last_verified_at"] = now
    p["reverification_due_at"] = due
    return p


def fixture_pg_required_lender() -> dict[str, Any]:
    p = empty_funding_source_profile(data_class=DATA_FIXTURE)
    p["source_name"] = "Fixture PG Lender"
    p["source_type"] = "PO_FINANCE"
    now = _utc().isoformat()
    ev = criterion_evidence(
        criterion_name="personal_guarantee",
        value=PG_REQUIRED,
        verification_status=EV_VERIFIED_BY_CALL,
        verified_at=now,
        operator_notes="TEST_FIXTURE",
    )
    p = set_criterion(p, "personal_guarantee", PG_REQUIRED, ev)
    p["personal_guarantee"] = PG_REQUIRED
    p["borrower_cash_contribution_required"] = False
    p["minimum_transaction"] = 10000
    p["maximum_transaction"] = 1000000
    return p


def production_knowledge_base_seed() -> dict[str, Any]:
    """
    Production KB populated from evidence-backed public research.
    See funding_source_research.build_production_funding_kb().
    """
    try:
        from funding_source_research import build_production_funding_kb

        return build_production_funding_kb()
    except Exception:
        return {
            "data_class": DATA_PRODUCTION,
            "private_lenders": [],
            "verified_private_lender_count": 0,
            "government_program_notes": [
                {
                    "topic": "FAR_PART_32",
                    "summary": (
                        "FAR Part 32 recognizes multiple contract-financing mechanisms and "
                        "gives preference to private financing when contract financing is requested."
                    ),
                    "verification_status": EV_VERIFIED_PUBLIC,
                    "source_title": "FAR Part 32 (contextual)",
                    "applies_to_specific_solicitation": False,
                    "note": "Do not infer solicitation qualifies merely because FAR Part 32 exists.",
                }
            ],
            "LIVE_API_REQUESTS": 0,
            "OpenAI": 0,
            "SAM": 0,
            "USAspending": 0,
            "paid": 0,
        }
