"""Deterministic pre-bid funding research planning — gap-driven questions."""

from __future__ import annotations

from typing import Any

QUESTION_BANK: dict[str, str] = {
    "startup_eligible": "Can you finance a newly formed government reseller?",
    "personal_credit": "Is personal credit pulled?",
    "personal_guarantee": "Is a personal guarantee required?",
    "owner_cash": "Is any owner cash/equity required?",
    "minimum_transaction": "What transaction minimum applies?",
    "maximum_transaction": "What transaction maximum applies?",
    "finance_full_invoice": "Will you finance 100% of supplier invoice?",
    "freight_included": "Can freight be included in financed amount?",
    "supplier_direct": "Do you pay supplier directly?",
    "contingent_on_po": "Is final approval contingent on government PO?",
    "fees": "What fees apply?",
    "fee_timing": "How do fees change at 30/60/90 days?",
    "payment_delay": "What happens if government acceptance/payment is delayed?",
    "post_award_docs": "What documentation is required after award?",
    "close_after_award": "How quickly can funding close after award?",
    "recourse": "Is recourse involved?",
    "supplier_restrictions": "Are there supplier or product restrictions?",
    "government_experience": "Do you finance federal product resale to government end users?",
}


def questions_for_gaps(gaps: list[str] | None) -> list[dict[str, Any]]:
    """Generate provider questions from missing facts only."""
    gap_set = {g.lower() for g in (gaps or [])}
    out: list[dict[str, Any]] = []
    mapping = {
        "pg": "personal_guarantee",
        "personal guarantee": "personal_guarantee",
        "personal credit": "personal_credit",
        "cash": "owner_cash",
        "minimum": "minimum_transaction",
        "maximum": "maximum_transaction",
        "supplier direct": "supplier_direct",
        "freight": "freight_included",
        "fee": "fees",
        "recourse": "recourse",
    }
    keys_needed: set[str] = set()
    for g in gap_set:
        for needle, key in mapping.items():
            if needle in g:
                keys_needed.add(key)
    if not keys_needed:
        keys_needed = {"startup_eligible", "personal_guarantee", "personal_credit", "owner_cash", "supplier_direct"}
    for key in sorted(keys_needed):
        if key in QUESTION_BANK:
            out.append({"key": key, "question": QUESTION_BANK[key], "status": "GENERATED_FROM_GAP"})
    return out


def build_provider_call_sheet(
    *,
    provider_name: str,
    provider_id: int | None = None,
    contact: str | None = None,
    gaps: list[str] | None = None,
    transaction_description: str = "Federal product resale",
) -> dict[str, Any]:
    questions = questions_for_gaps(gaps)
    contact_block = (
        {"status": "CONTACT_INFORMATION_NEEDED", "name": None, "phone": None}
        if not contact
        else {"status": "OPERATOR_REPORTED", "name": contact, "phone": None}
    )
    return {
        "provider": provider_name,
        "provider_id": provider_id,
        "contact": contact_block,
        "transaction": transaction_description,
        "hard_requirements": {
            "no_pg": True,
            "no_personal_credit": True,
            "zero_personal_cash": True,
            "status": "POLICY",
        },
        "questions": questions,
        "what_to_record": [
            "PG required? (must be NO)",
            "Personal credit? (must be NO)",
            "Cash contribution? (must be ZERO)",
            "Fees and timing scenarios",
            "Supplier direct payment capability",
            "Approval contingent on government PO?",
        ],
        "LIVE_API_REQUESTS": 0,
    }


def build_funding_research_plan(
    *,
    funding_plan: dict[str, Any] | None = None,
    providers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    fp = funding_plan or {}
    pre = fp.get("pre_bid_viability") or {}
    gaps = list(pre.get("unknowns") or []) + list(pre.get("blockers") or [])
    sheets = []
    for p in providers or []:
        sheets.append(
            build_provider_call_sheet(
                provider_name=p.get("name") or "Unknown provider",
                provider_id=p.get("id"),
                contact=p.get("contact_name"),
                gaps=gaps,
            )
        )
    return {
        "what_we_know": fp.get("what_we_know") or [],
        "what_we_dont_know": fp.get("what_we_dont_know") or gaps,
        "must_know_before_bid": fp.get("must_know_before_bid") or [],
        "may_wait_until_award": fp.get("may_wait_until_award") or [],
        "who_to_contact": [p.get("name") for p in providers or [] if p.get("name")],
        "provider_call_sheets": sheets,
        "LIVE_API_REQUESTS": 0,
    }
