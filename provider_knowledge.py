"""Financing provider profile knowledge — extend without unverified seed claims."""

from __future__ import annotations

from typing import Any

PROFILE_FIELDS = (
    "government_contract_experience",
    "startup_eligible",
    "prime_contractor_eligible",
    "minimum_transaction",
    "maximum_transaction",
    "supplier_direct_payment",
    "pg_policy",
    "personal_credit_policy",
    "business_credit_requirement",
    "owner_cash_equity_requirement",
    "advance_percentage",
    "fee_structure",
    "time_based_fees",
    "recourse",
    "collateral",
    "assignment_requirements",
    "required_documents",
    "expected_approval_time",
    "product_industry_restrictions",
    "last_verified_date",
    "evidence",
    "confidence",
)


def empty_profile() -> dict[str, Any]:
    return {k: None for k in PROFILE_FIELDS}


def merge_provider_profile(existing: dict[str, Any] | None, updates: dict[str, Any] | None) -> dict[str, Any]:
    """Safe merge — never overwrite VERIFIED facts with unverified claims."""
    base = dict(existing or empty_profile())
    for k, v in (updates or {}).items():
        if k not in PROFILE_FIELDS:
            continue
        cur = base.get(k)
        if isinstance(cur, dict) and cur.get("verification") == "VERIFIED":
            if isinstance(v, dict) and v.get("verification") != "VERIFIED":
                continue
        base[k] = v
    return base


def provider_profile_view(provider: Any) -> dict[str, Any]:
    profile = merge_provider_profile(empty_profile(), provider.profile_json if provider else None)
    return {
        "provider_id": provider.id if provider else None,
        "name": provider.name if provider else None,
        "verification_status": provider.verification_status if provider else "UNKNOWN",
        "profile": profile,
        "LIVE_API_REQUESTS": 0,
    }


def update_provider_profile(session: Any, provider_id: int, updates: dict[str, Any]) -> dict[str, Any]:
    from models import FinancingProvider

    row = session.query(FinancingProvider).filter_by(id=provider_id).first()
    if not row:
        return {"error": "provider_not_found", "LIVE_API_REQUESTS": 0}
    merged = merge_provider_profile(row.profile_json, updates)
    if "last_verified_date" not in updates:
        merged.setdefault("last_verified_date", None)
    row.profile_json = merged
    session.flush()
    return provider_profile_view(row)
