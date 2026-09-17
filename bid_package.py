"""Bid package data structures — no live AI bid writing."""

from __future__ import annotations

from typing import Any

# Permanent distinction labels
FACT_GOVERNMENT_VERIFIED = "VERIFIED_GOVERNMENT_FACT"
FACT_COMPANY_PROPOSED_BID = "COMPANY_PROPOSED_BID_PRICE"
FACT_AI_DRAFTED_NARRATIVE = "AI_DRAFTED_NARRATIVE"
FACT_OPERATOR_SUPPLIED = "OPERATOR_SUPPLIED_FACT"


def empty_bid_package(
    *,
    solicitation_number: str | None = None,
    notice_id: str | None = None,
    contract_id: int | None = None,
) -> dict[str, Any]:
    return {
        "schema": "bid-package-v1",
        "solicitation_identifiers": {
            "solicitation_number": solicitation_number,
            "notice_id": notice_id,
            "contract_id": contract_id,
            "fact_class": FACT_GOVERNMENT_VERIFIED,
        },
        "operator_proposed_price": {
            "value": None,
            "status": "UNKNOWN",
            "fact_class": FACT_COMPANY_PROPOSED_BID,
            "notes": "Never label as verified government value",
        },
        "pricing_schedule": {"status": "UNKNOWN", "items": [], "fact_class": FACT_COMPANY_PROPOSED_BID},
        "technical_response": {
            "status": "UNKNOWN",
            "content": None,
            "fact_class": FACT_AI_DRAFTED_NARRATIVE,
            "notes": "AI draft is narrative — not a verified fact",
        },
        "past_performance": {"status": "UNKNOWN", "items": [], "fact_class": FACT_OPERATOR_SUPPLIED},
        "representations_certifications": {"status": "UNKNOWN", "items": []},
        "oem_letters": {"status": "UNKNOWN", "attachments": []},
        "supplier_evidence": {"status": "UNKNOWN", "items": []},
        "required_forms": {"status": "UNKNOWN", "items": []},
        "amendment_acknowledgments": {"status": "UNKNOWN", "items": []},
        "signatures": {"status": "UNKNOWN", "items": []},
        "attachments": {"status": "UNKNOWN", "items": []},
        "submission_instructions": {"status": "UNKNOWN", "method": None, "deadline": None},
        "final_compliance_checklist": {"status": "UNKNOWN", "items": []},
        "checklist": {
            "forms_complete": False,
            "signatures_complete": False,
            "pricing_complete": False,
            "technical_complete": None,
            "past_performance_complete": None,
            "oem_letters_attached": False,
            "mandatory_attachments_present": False,
            "submission_method_verified": False,
            "deadline_verified": False,
        },
        "mandatory_items": [],
        "LIVE_API_REQUESTS": 0,
    }


def bid_package_from_workspace(
    *,
    solicitation_number: str | None,
    notice_id: str | None,
    contract_id: int | None,
    operator_bid_amount: float | None = None,
    submission_method: str | None = None,
    deadline: str | None = None,
) -> dict[str, Any]:
    pkg = empty_bid_package(
        solicitation_number=solicitation_number,
        notice_id=notice_id,
        contract_id=contract_id,
    )
    if operator_bid_amount is not None:
        pkg["operator_proposed_price"] = {
            "value": operator_bid_amount,
            "status": "OPERATOR_SUPPLIED",
            "fact_class": FACT_COMPANY_PROPOSED_BID,
            "notes": "Company proposed bid — not verified government value",
        }
    if submission_method:
        pkg["submission_instructions"]["method"] = submission_method
        pkg["submission_instructions"]["status"] = "OPERATOR_SUPPLIED"
        pkg["checklist"]["submission_method_verified"] = True
    if deadline:
        pkg["submission_instructions"]["deadline"] = deadline
        pkg["checklist"]["deadline_verified"] = True
    return pkg
