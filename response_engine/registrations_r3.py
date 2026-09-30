"""R3 registrations — REGISTER_BEFORE_BID vs hard federal blockers."""

from __future__ import annotations

import re
from typing import Any

from response_engine.company_profile_r3 import cage_status
from response_engine.models import new_id
from response_engine.r3_constants import (
    DIBBS_CAGE_REQUIRED,
    FAIL,
    PASS_VERIFIED,
    REGISTER_BEFORE_BID,
    REVIEW_REQUIRED,
    UNKNOWN,
)

_DIBBS = re.compile(r"\bDIBBS\b|DLA\s+Internet\s+Bid\s+Board", re.I)
_PIEE = re.compile(r"\bPIEE\b|Wide\s+Area\s+Workflow|WAWF", re.I)
_JCP = re.compile(r"\bJCP\b|DD[\s\-]?2345|Joint\s+Certification\s+Program", re.I)
_STATE_VENDOR = re.compile(
    r"vendor\s+registration|supplier\s+registration|bidders?\s+list|"
    r"must\s+register\s+(?:with|in)\s+(?:the\s+)?(?:state|city|county)",
    re.I,
)
_SAM_REQ = re.compile(r"\bSAM\.gov\b|System\s+for\s+Award\s+Management|active\s+SAM\s+registration", re.I)


def evaluate_registrations(
    *,
    profile: dict[str, Any],
    solicitation_text: str | None = None,
    portal: str | None = None,
    jurisdiction: str | None = None,
    existing_registrations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    blob = solicitation_text or ""
    cage = cage_status(profile)
    items: list[dict[str, Any]] = []

    juris = (jurisdiction or "").lower()
    federalish = juris in ("federal", "dod", "dla") or bool(_SAM_REQ.search(blob)) or bool(_DIBBS.search(blob))

    # SAM — hard-required for federal/DLA; otherwise surface as unknown/review
    sam_status = str(profile.get("SAM_registration_status") or "").upper()
    sam_req = federalish or bool(_SAM_REQ.search(blob))
    if sam_status in ("ACTIVE", "TRUE", "YES", "1"):
        items.append(
            {
                "registration": "SAM",
                "required": bool(sam_req),
                "timing": "AT_SUBMISSION",
                "current_status": "ACTIVE",
                "operator_mode": None,
                "status": PASS_VERIFIED,
                "plain": "SAM active",
            }
        )
    else:
        items.append(
            {
                "registration": "SAM",
                "required": bool(sam_req),
                "timing": "AT_SUBMISSION",
                "current_status": sam_status or UNKNOWN,
                "operator_mode": "HARD_BLOCK" if sam_req else REVIEW_REQUIRED,
                "status": FAIL if sam_req else UNKNOWN,
                "plain": "SAM not verified active",
                "url": "https://sam.gov",
            }
        )

    # CAGE — hard block only when federal/DLA/DIBBS path needs it
    cage_required = federalish or juris == "dla" or bool(_DIBBS.search(blob))
    items.append(
        {
            "registration": "CAGE",
            "required": cage_required,
            "timing": "AT_SUBMISSION",
            "current_status": cage["status"],
            "operator_mode": DIBBS_CAGE_REQUIRED if (cage_required and cage["block"]) else None,
            "status": (
                PASS_VERIFIED
                if cage["status"] == "ACTIVE"
                else (FAIL if cage_required else UNKNOWN)
            ),
            "plain": cage["plain"] if cage_required else "CAGE not required for this jurisdiction path",
            "block": cage.get("block") if cage_required else None,
        }
    )

    # DIBBS
    if _DIBBS.search(blob) or (portal or "").upper() == "DIBBS" or (jurisdiction or "").upper() == "DLA":
        dibbs_ok = cage.get("dibbs_eligible") is True
        items.append(
            {
                "registration": "DIBBS",
                "required": True,
                "timing": "AT_SUBMISSION",
                "current_status": "ELIGIBLE" if dibbs_ok else DIBBS_CAGE_REQUIRED,
                "operator_mode": None if dibbs_ok else DIBBS_CAGE_REQUIRED,
                "status": PASS_VERIFIED if dibbs_ok else FAIL,
                "plain": "DIBBS ready" if dibbs_ok else "DIBBS unavailable until CAGE",
                "depends_on": "CAGE",
            }
        )

    # PIEE
    if _PIEE.search(blob):
        items.append(
            {
                "registration": "PIEE",
                "required": True,
                "timing": "PRE_AWARD",
                "current_status": UNKNOWN,
                "operator_mode": REGISTER_BEFORE_BID,
                "status": REVIEW_REQUIRED,
                "plain": "PIEE account may be needed — verify timing",
                "url": "https://piee.eb.mil",
            }
        )

    # JCP
    if _JCP.search(blob):
        jcp = str(profile.get("jcp_status") or "").upper()
        items.append(
            {
                "registration": "JCP",
                "required": True,
                "timing": "PRE_QUESTION_DEADLINE",
                "current_status": jcp or UNKNOWN,
                "operator_mode": "HARD_BLOCK" if jcp not in ("APPROVED", "ACTIVE") else None,
                "status": PASS_VERIFIED if jcp in ("APPROVED", "ACTIVE") else FAIL,
                "plain": "JCP required for export-controlled data access",
            }
        )

    # Easy state/local vendor registration
    if _STATE_VENDOR.search(blob) or (jurisdiction or "").lower() in ("state", "local"):
        already = False
        for r in existing_registrations or profile.get("state_local_registrations") or []:
            if str(r.get("status") or "").upper() in ("ACTIVE", "REGISTERED"):
                already = True
                break
        items.append(
            {
                "registration": "STATE_LOCAL_VENDOR",
                "required": True,
                "timing": "AT_SUBMISSION",
                "current_status": "ACTIVE" if already else "NEEDED",
                "operator_mode": None if already else REGISTER_BEFORE_BID,
                "status": PASS_VERIFIED if already else REGISTER_BEFORE_BID,
                "plain": "Vendor registration active" if already else "Register before bid (easy path — not immediate reject)",
                "estimated_effort": "low",
                "free_or_paid": UNKNOWN,
            }
        )

    hard_blocks = [i for i in items if i.get("status") == FAIL and i.get("operator_mode") != REGISTER_BEFORE_BID]
    soft = [i for i in items if i.get("operator_mode") == REGISTER_BEFORE_BID]

    return {
        "kind": "RegistrationDecision",
        "decision_id": new_id("REG"),
        "items": items,
        "hard_blocks": hard_blocks,
        "register_before_bid": soft,
        "cage": cage,
        "overall_status": FAIL if hard_blocks else (REGISTER_BEFORE_BID if soft else PASS_VERIFIED),
        "LIVE_API_REQUESTS": 0,
    }
