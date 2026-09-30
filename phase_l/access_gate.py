"""Phase L accessibility model — access before expensive pricing.

Wraps eligibility_gate with Phase L vocabulary:
  competition_access_type / our_bid_access / access_blocker
Hard rule: actionable only when our_bid_access == YES.
"""

from __future__ import annotations

import re
from typing import Any

from application_clock import now_utc
from eligibility_gate import (
    ELIGIBLE_CONDITIONAL,
    ELIGIBLE_CONFIRMED,
    ELIGIBILITY_NOT_APPLICABLE,
    NOT_CURRENTLY_ELIGIBLE,
    evaluate_eligibility_gate,
    load_company_eligibility_profile,
)
from phase_l.registration_tracker import (
    BLOCKED,
    REGISTER_BEFORE_BID,
    VERIFY_REGISTRATION_TIMING,
    classify_registration_gate,
)

# competition_access_type
OPEN_MARKET = "OPEN_MARKET"
TOTAL_SMALL_BUSINESS = "TOTAL_SMALL_BUSINESS"
UNRESTRICTED = "UNRESTRICTED"
VEHICLE_ONLY = "VEHICLE_ONLY"
BPA_ONLY = "BPA_ONLY"
IDIQ_ONLY = "IDIQ_ONLY"
GWAC_ONLY = "GWAC_ONLY"
MAS_ONLY = "MAS_ONLY"
SEWP_ONLY = "SEWP_ONLY"
SOLE_SOURCE = "SOLE_SOURCE"
LIMITED_SOURCE = "LIMITED_SOURCE"
SPECIAL_SET_ASIDE = "SPECIAL_SET_ASIDE"
SOURCE_APPROVAL_REQUIRED = "SOURCE_APPROVAL_REQUIRED"
UNKNOWN = "UNKNOWN"

# our_bid_access
ACCESS_YES = "YES"
ACCESS_NO = "NO"
ACCESS_CONDITIONAL = "CONDITIONAL"
ACCESS_UNKNOWN = "UNKNOWN"

_SOLE_RE = re.compile(
    r"sole[\s\-]?source|only[\s\-]?one[\s\-]?source|single[\s\-]?source|"
    r"restricted\s+to\s+[A-Z][A-Za-z0-9 &\-]{2,40}\s+(?:Inc|LLC|Corp|CAGE)",
    re.I,
)
_LIMITED_RE = re.compile(r"limited\s+sources?|brand[\s\-]?name\s+only(?!\s+or\s+equal)", re.I)
_COOP_MEMBER_RE = re.compile(
    r"cooperative\s+members?\s+only|members?\s+only|"
    r"must\s+be\s+a\s+(?:Sourcewell|NASPO|OMNIA|HGAC|BuyBoard)\s+member|"
    r"participating\s+agencies?\s+only",
    re.I,
)
_LOCAL_RESIDENT_RE = re.compile(
    r"resident\s+vendor\s+required|must\s+be\s+(?:a\s+)?(?:local|in[\s\-]?state)\s+vendor|"
    r"local\s+bidder\s+only|in[\s\-]?state\s+vendors?\s+only",
    re.I,
)
_LOCAL_PREF_RE = re.compile(
    r"local\s+preference|in[\s\-]?state\s+preference|resident\s+bidder\s+preference|"
    r"(\d{1,2})\s*%\s*(?:local|in[\s\-]?state)\s+preference",
    re.I,
)
_VENDOR_REG_RE = re.compile(
    r"vendor\s+registration\s+required|must\s+register\s+(?:as\s+a\s+)?vendor|"
    r"registration\s+required\s+prior\s+to\s+bid|bidder\s+must\s+be\s+registered",
    re.I,
)
_BOND_RE = re.compile(r"\bbid\s+bond\b|\bperformance\s+bond\b|\bsurety\b", re.I)
_LICENSE_RE = re.compile(
    r"(?:contractor|electrical|plumbing|hvac)\s+license\s+required|"
    r"must\s+(?:hold|possess)\s+(?:a\s+)?(?:valid\s+)?license",
    re.I,
)
_SB_RE = re.compile(
    r"total\s+small\s+business|small\s+business\s+set[\s\-]?aside|"
    r"\bSBA\b|set[\s\-]?aside[:\s]+(?:total\s+)?small\s+business",
    re.I,
)
_SPECIAL_SA_RE = re.compile(
    r"\bWOSB\b|\bSDVOSB\b|\b8\s*\(\s*a\s*\)|\bHUBZone\b|\bISBEE\b|"
    r"women[\s\-]?owned|service[\s\-]?disabled\s+veteran",
    re.I,
)
_UNRESTRICTED_RE = re.compile(r"\bunrestricted\b|\bfull\s+and\s+open\b", re.I)


def _utc() -> str:
    return now_utc().isoformat()


def _blob(row: dict[str, Any], text: str | None = None) -> str:
    parts = [
        str(row.get("title") or ""),
        str(row.get("description") or ""),
        str(row.get("set_aside") or ""),
        str(row.get("typeOfSetAsideDescription") or ""),
        str(row.get("typeOfSetAside") or ""),
        str(row.get("procurement_method") or ""),
        text or "",
    ]
    return "\n".join(parts)


def classify_competition_access_type(row: dict[str, Any], *, text: str | None = None) -> str:
    """Deterministic competition_access_type from solicitation signals."""
    blob = _blob(row, text)
    elig = evaluate_eligibility_gate(row, text=text)
    vehicles = elig.get("vehicles_detected") or []
    codes = {str(v.get("code") or "") for v in vehicles if isinstance(v, dict)}

    if _SOLE_RE.search(blob):
        return SOLE_SOURCE
    if any(c == "SEWP" for c in codes) or re.search(r"\bSEWP\b", blob):
        if re.search(r"holders?\s+only|only\s+(?:SEWP|contractors?)", blob, re.I):
            return SEWP_ONLY
    if any(c == "BPA_HOLDER" for c in codes):
        return BPA_ONLY
    if any(c == "IDIQ_HOLDER" for c in codes):
        return IDIQ_ONLY
    if any(c == "GSA_SCHEDULE" for c in codes) or re.search(
        r"MAS\s+holders?\s+only|GSA\s+(?:MAS|Schedule)\s+holders?\s+only", blob, re.I
    ):
        return MAS_ONLY
    if any(c in {"OASIS", "CIO_SP", "SEAPORT_NXG"} for c in codes):
        return GWAC_ONLY
    if vehicles and any(v.get("holder_restriction_likely") for v in vehicles if isinstance(v, dict)):
        return VEHICLE_ONLY
    if elig.get("approved_source_required") or re.search(
        r"source\s+approval|approved\s+source", blob, re.I
    ):
        return SOURCE_APPROVAL_REQUIRED
    if _SPECIAL_SA_RE.search(blob):
        return SPECIAL_SET_ASIDE
    if _LIMITED_RE.search(blob):
        return LIMITED_SOURCE
    if _SB_RE.search(blob) or str(row.get("set_aside") or "").upper() in {
        "SBA",
        "SBP",
        "SB",
        "TOTAL SB",
        "SMALL BUSINESS",
    }:
        return TOTAL_SMALL_BUSINESS
    if _UNRESTRICTED_RE.search(blob):
        return UNRESTRICTED
    # Open IFB/RFQ without holder language
    if re.search(r"\b(IFB|RFQ|RFP|Invitation\s+for\s+Bid)\b", blob, re.I) and not vehicles:
        return OPEN_MARKET
    if not blob.strip():
        return UNKNOWN
    return OPEN_MARKET if not vehicles else UNKNOWN


def evaluate_phase_l_access(
    row: dict[str, Any],
    *,
    text: str | None = None,
    profile: dict[str, Any] | None = None,
    vendor_registration_lead_time_days: float | None = None,
    runway_days: float | None = None,
) -> dict[str, Any]:
    """
    Phase L access determination.

    Distinguishes can_compete (our_bid_access) from submission readiness.
    Easy administrative vendor registration must NOT force CONDITIONAL/UNKNOWN
    when the solicitation is otherwise open.
    """
    profile = profile or load_company_eligibility_profile()
    blob = _blob(row, text)
    elig = evaluate_eligibility_gate(row, profile=profile, text=text)
    access_type = classify_competition_access_type(row, text=text)

    blockers: list[str] = []
    readiness_blockers: list[str] = []
    local_preference_exists = bool(_LOCAL_PREF_RE.search(blob))
    local_preference_pct = None
    m_pref = re.search(r"(\d{1,2})\s*%\s*(?:local|in[\s\-]?state)\s+preference", blob, re.I)
    if m_pref:
        try:
            local_preference_pct = float(m_pref.group(1))
        except ValueError:
            local_preference_pct = None
    resident_vendor_required = bool(_LOCAL_RESIDENT_RE.search(blob))
    vendor_registration_required = bool(_VENDOR_REG_RE.search(blob)) or bool(
        row.get("vendor_registration_required")
    )
    # State/local portals often require signup even when text omits the phrase
    source_level = str(row.get("source_level") or row.get("buyer_type") or "").upper()
    if source_level in {"STATE", "LOCAL", "CITY", "COUNTY", "SCHOOL", "NETWORK"} and not vendor_registration_required:
        # Infer ordinary portal registration as likely for non-federal
        if row.get("source_id") and not str(row.get("source_id")).startswith("fed_"):
            vendor_registration_required = True
    bond_required = bool(_BOND_RE.search(blob))
    license_required = bool(_LICENSE_RE.search(blob))
    cooperative_membership_required = bool(_COOP_MEMBER_RE.search(blob)) or (
        source_level == "COOPERATIVE"
        and bool(re.search(r"members?\s+only|participating\s+agenc", blob, re.I))
    )

    # Baseline from eligibility: NOT_APPLICABLE means no vehicle gate applies → can compete
    overall = elig.get("overall_status")
    if overall == NOT_CURRENTLY_ELIGIBLE:
        our = ACCESS_NO
        blockers.append(str(elig.get("blocking_reason") or elig.get("plain") or "eligibility_blocked"))
    elif overall == ELIGIBLE_CONDITIONAL:
        our = ACCESS_CONDITIONAL
        blockers.append(str(elig.get("blocking_reason") or "conditional_eligibility"))
    elif overall in {ELIGIBLE_CONFIRMED, ELIGIBILITY_NOT_APPLICABLE}:
        our = ACCESS_YES
    else:
        our = ACCESS_UNKNOWN

    # Hard access-type demotions when company holds no matching vehicle/source
    if access_type in {
        VEHICLE_ONLY,
        BPA_ONLY,
        IDIQ_ONLY,
        GWAC_ONLY,
        MAS_ONLY,
        SEWP_ONLY,
    }:
        our = ACCESS_NO
        blockers.append(f"vehicle_required:{access_type}")
    if access_type == SOLE_SOURCE:
        our = ACCESS_NO
        blockers.append("sole_source")
    if access_type == SOURCE_APPROVAL_REQUIRED:
        approved = {str(a).upper() for a in (profile.get("approved_sources") or [])}
        if not approved:
            our = ACCESS_NO
            blockers.append("source_approval_required")
    if access_type == SPECIAL_SET_ASIDE:
        quals = {str(q).upper() for q in (profile.get("set_aside_qualifications") or [])}
        held_ok = False
        try:
            from company_eligibility import held_certifications, set_aside_eligibility

            sa = set_aside_eligibility(
                row.get("set_aside") or row.get("typeOfSetAsideDescription") or blob[:200]
            )
            held_ok = bool(quals) or sa.get("eligible") is True
            # Special socioeconomic (WOSB etc.) — held_certifications default SB does NOT cover
            if sa.get("eligible") is False:
                our = ACCESS_NO
                blockers.append(f"special_set_aside_not_held:{sa.get('matched')}")
                held_ok = False
        except Exception:
            held_ok = bool(quals)
        if our != ACCESS_NO and not held_ok:
            our = ACCESS_UNKNOWN
            blockers.append("special_socioeconomic_set_aside_unconfirmed")

    if access_type == TOTAL_SMALL_BUSINESS:
        sba = str(profile.get("sba_size_status") or "UNKNOWN").upper()
        sb_ok = False
        try:
            from company_eligibility import held_certifications, set_aside_eligibility

            sa = set_aside_eligibility(
                row.get("set_aside")
                or row.get("typeOfSetAsideDescription")
                or "Total Small Business"
            )
            certs = held_certifications()
            sb_ok = sa.get("eligible") is True or bool(
                certs & {"SB", "SMALLBUSINESS", "TOTALSMALLBUSINESS", "SBA"}
            )
        except Exception:
            sb_ok = False
        if sba in {"OTHER_THAN_SMALL", "LARGE"}:
            our = ACCESS_NO
            blockers.append("not_small_business")
        elif sb_ok or sba in {"SMALL", "SMALL_BUSINESS", "YES", "TRUE", "HELD"}:
            if our != ACCESS_NO:
                our = ACCESS_YES
        else:
            # Profile UNKNOWN and no SB cert signal
            if our != ACCESS_NO:
                our = ACCESS_UNKNOWN
            blockers.append("small_business_status_unconfirmed")

    if resident_vendor_required:
        our = ACCESS_NO
        blockers.append("resident_vendor_required")
    if cooperative_membership_required:
        our = ACCESS_NO
        blockers.append("cooperative_membership_required")
    if license_required:
        licenses = profile.get("licenses") or []
        if not licenses:
            # Mandatory license unconfirmed — readiness, not automatic NO unless explicit MISSING
            readiness_blockers.append("license_required_unconfirmed")
            if str(profile.get("licenses_status") or "").upper() == "MISSING":
                our = ACCESS_NO
                blockers.append("license_required_missing")
            elif our == ACCESS_YES:
                # Keep YES for can_compete only if license not proven mandatory-impossible
                pass
    if bond_required:
        bond = str(profile.get("bonding_capability") or "UNKNOWN").upper()
        if bond in {"MISSING", "NO"}:
            our = ACCESS_NO
            blockers.append("bond_required_unavailable")
        elif bond in {"UNKNOWN", ""}:
            readiness_blockers.append("bond_required_unconfirmed")

    # Registration classification — easy admin must not demote can_compete
    reg_days = vendor_registration_lead_time_days
    if reg_days is None and row.get("vendor_registration_lead_time_days") is not None:
        try:
            reg_days = float(row.get("vendor_registration_lead_time_days"))
        except (TypeError, ValueError):
            reg_days = None

    reg_gate = classify_registration_gate(
        vendor_registration_required=vendor_registration_required,
        lead_time_days=reg_days,
        runway_days=runway_days,
        source_access_level=str(row.get("source_access_level") or row.get("access_level") or ""),
        restrictive=False,
    )
    registration_action = reg_gate.get("registration_action") or "NONE"
    registration_gate_type = reg_gate.get("registration_gate_type") or "NONE"
    is_easy_reg = bool(reg_gate.get("is_easy_registration"))

    if reg_gate.get("registration_action") == BLOCKED:
        our = ACCESS_NO
        blockers.append(
            f"registration_lead_time_exceeds_runway:reg={reg_days}d_runway={runway_days}d"
        )
    elif registration_action == VERIFY_REGISTRATION_TIMING and our == ACCESS_YES:
        our = ACCESS_CONDITIONAL
        blockers.append("tight_effective_runway_after_registration")
    elif vendor_registration_required and is_easy_reg and our != ACCESS_NO:
        # Easy registration: remain YES; track readiness separately
        if our in {ACCESS_UNKNOWN, ACCESS_CONDITIONAL} and access_type in {
            OPEN_MARKET,
            UNRESTRICTED,
            TOTAL_SMALL_BUSINESS,
        }:
            # Do not use registration alone to keep CONDITIONAL
            if access_type == TOTAL_SMALL_BUSINESS and "small_business_status_unconfirmed" in blockers:
                pass  # SB unknown still unknown
            elif access_type != TOTAL_SMALL_BUSINESS or our == ACCESS_YES:
                if access_type in {OPEN_MARKET, UNRESTRICTED}:
                    our = ACCESS_YES
        readiness_blockers.append("vendor_registration_incomplete")
        if registration_action == "NONE":
            registration_action = REGISTER_BEFORE_BID

    # Open / unrestricted: if no true blockers remain, YES (Phase L.1 repair)
    hard_blocker_codes = {
        "sole_source",
        "source_approval_required",
        "resident_vendor_required",
        "cooperative_membership_required",
        "not_small_business",
        "bond_required_unavailable",
        "license_required_missing",
    }
    has_hard = our == ACCESS_NO or any(
        b.split(":")[0] in hard_blocker_codes or b.startswith("vehicle_required") for b in blockers
    )
    if (
        access_type in {OPEN_MARKET, UNRESTRICTED}
        and not has_hard
        and our != ACCESS_NO
        and "special_socioeconomic_set_aside_unconfirmed" not in blockers
    ):
        our = ACCESS_YES
        # Strip obsolete company-registration conditional reason if present
        blockers = [
            b
            for b in blockers
            if b not in {"open_competition_but_company_registration_unconfirmed", "conditional_eligibility"}
        ]

    # SAM active MISSING only blocks when solicitation explicitly requires federal award path
    # and profile says MISSING/NO — UNKNOWN does not invent a block on open market
    sam = str(profile.get("sam_active") or "UNKNOWN").upper()
    if sam in {"MISSING", "NO", "INACTIVE"} and source_level in {"FEDERAL", ""}:
        if access_type in {OPEN_MARKET, UNRESTRICTED, TOTAL_SMALL_BUSINESS} and re.search(
            r"\bSAM\.gov\b|must\s+be\s+registered\s+in\s+SAM|active\s+SAM\s+registration",
            blob,
            re.I,
        ):
            our = ACCESS_NO
            blockers.append("sam_registration_missing")
        elif sam in {"MISSING", "NO", "INACTIVE"}:
            readiness_blockers.append("sam_registration_incomplete")

    access_blocker = "; ".join(dict.fromkeys(blockers)) if blockers else None
    readiness_blocker = "; ".join(dict.fromkeys(readiness_blockers)) if readiness_blockers else None

    # L.2.1: early access eligibility must NOT read as bid-ready
    if our == ACCESS_YES and not readiness_blockers and not vendor_registration_required:
        submission_readiness = "ACCESS_ELIGIBLE"
    elif vendor_registration_required and our == ACCESS_YES:
        submission_readiness = "REGISTRATION_PENDING"
    else:
        submission_readiness = "NOT_READY"

    return {
        "kind": "PhaseLAccess",
        "competition_access_type": access_type,
        "our_bid_access": our,
        "can_compete": our == ACCESS_YES,
        "access_eligibility": "CAN_COMPETE" if our == ACCESS_YES else our,
        "access_blocker": access_blocker,
        "readiness_blocker": readiness_blocker,
        "submission_readiness": submission_readiness,
        "ready_to_bid": False,  # never true from access gate alone
        "eligibility": {
            "overall_status": elig.get("overall_status"),
            "actionable_for_quote_or_bid": elig.get("actionable_for_quote_or_bid"),
            "plain": elig.get("plain"),
            "blocking_reason": elig.get("blocking_reason"),
        },
        "jurisdiction": row.get("jurisdiction") or row.get("state_code") or row.get("buyer_type"),
        "portal_source": row.get("source_id") or row.get("portal_source"),
        "vendor_registration_required": vendor_registration_required,
        "vendor_registration_status": row.get("vendor_registration_status") or "UNKNOWN",
        "vendor_registration_lead_time_days": reg_days,
        "registration_gate_type": registration_gate_type,
        "registration_action": registration_action,
        "registration_can_be_fixed": bool(reg_gate.get("registration_can_be_fixed", True)),
        "registration_deadline_risk": bool(reg_gate.get("registration_deadline_risk")),
        "is_easy_registration": is_easy_reg and vendor_registration_required,
        "local_preference_exists": local_preference_exists,
        "local_preference_pct": local_preference_pct,
        "resident_vendor_required": resident_vendor_required,
        "bond_required": bond_required,
        "license_required": license_required,
        "cooperative_membership_required": cooperative_membership_required,
        "actionable": our == ACCESS_YES,
        "evaluated_at": _utc(),
    }
