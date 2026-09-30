"""Lower-48 contiguous US constants + procurement coverage status taxonomy (L.17)."""

from __future__ import annotations

import re
from typing import Any

BUILD = "20260928-m3-phase-l17-lower48-exhaustive-procurement-coverage"
BUILD_L172 = "20260928-m3-phase-l172-national-coverage-saturation-accessible-now-expansion"

# Contiguous United States — excludes AK, HI, territories
LOWER_48: tuple[str, ...] = (
    "AL", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI",
    "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY",
    "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN",
    "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
)

LOWER_48_SET = set(LOWER_48)

# Procurement source status taxonomy
AUTOMATED_TIER1 = "AUTOMATED_TIER1"
AUTOMATED_TIER2 = "AUTOMATED_TIER2"
AUTOMATED_STATIC = "AUTOMATED_STATIC"
MANUAL_PUBLIC = "MANUAL_PUBLIC"
FREE_REGISTRATION_REQUIRED = "FREE_REGISTRATION_REQUIRED"
PAID_ACCESS_REQUIRED = "PAID_ACCESS_REQUIRED"
AUTH_BLOCKED = "AUTH_BLOCKED"
PORTAL_DISCOVERED_NOT_INTEGRATED = "PORTAL_DISCOVERED_NOT_INTEGRATED"
NO_PROCUREMENT_SOURCE_FOUND = "NO_PROCUREMENT_SOURCE_FOUND"
NO_INDEPENDENT_PROCUREMENT = "NO_INDEPENDENT_PROCUREMENT"
UNKNOWN_RESEARCH_PENDING = "UNKNOWN_RESEARCH_PENDING"

SOURCE_STATUSES = (
    AUTOMATED_TIER1,
    AUTOMATED_TIER2,
    AUTOMATED_STATIC,
    MANUAL_PUBLIC,
    FREE_REGISTRATION_REQUIRED,
    PAID_ACCESS_REQUIRED,
    AUTH_BLOCKED,
    PORTAL_DISCOVERED_NOT_INTEGRATED,
    NO_PROCUREMENT_SOURCE_FOUND,
    NO_INDEPENDENT_PROCUREMENT,
    UNKNOWN_RESEARCH_PENDING,
)

NONFEDERAL_ACCESSIBLE_NOW = "NONFEDERAL_ACCESSIBLE_NOW"
HIGH_VALUE_RECURRING_BUYER = "HIGH_VALUE_RECURRING_BUYER"
DIBBS_CAGE_REQUIRED = "DIBBS_CAGE_REQUIRED"
REGISTER_BEFORE_BID = "REGISTER_BEFORE_BID"
REGISTER_NOW_RECURRING_BUYER = "REGISTER_NOW_RECURRING_BUYER"

# Opportunity-level current access classification (L.17.2)
ACCESSIBLE_NOW = "ACCESSIBLE_NOW"
SIMPLE_VENDOR_SETUP = "SIMPLE_VENDOR_SETUP"
PAID_ACCESS_REQUIRED_OPP = "PAID_ACCESS_REQUIRED"
AUTH_REQUIRED_OPP = "AUTH_REQUIRED"
HARD_ELIGIBILITY_BLOCKER = "HARD_ELIGIBILITY_BLOCKER"

_ID_RE = re.compile(r"[^a-z0-9]+")


def normalize_jurisdiction_id(
    *,
    state: str,
    buyer_type: str,
    name: str,
    geoid: str | None = None,
) -> str:
    st = str(state or "").upper()[:2]
    bt = str(buyer_type or "UNKNOWN").upper()
    slug = _ID_RE.sub("_", str(name or "").lower()).strip("_")[:80]
    if geoid:
        return f"{st}:{bt}:{geoid}:{slug}"
    return f"{st}:{bt}:{slug}"


def classify_source_status(
    *,
    structured_tier: str | None = None,
    adapter_status: str | None = None,
    auth_required: bool = False,
    publicly_searchable: bool = False,
    platform: str | None = None,
    portal_url: str | None = None,
    automated: bool = False,
) -> str:
    """Map known signals → coverage status. Unknown stays UNKNOWN_RESEARCH_PENDING."""
    plat = str(platform or "").lower()
    st = str(adapter_status or "").upper()
    if structured_tier in {"TIER_1_OFFICIAL_STRUCTURED", "TIER_1"} or (
        automated and structured_tier and "TIER_1" in str(structured_tier)
    ):
        return AUTOMATED_TIER1
    if structured_tier in {"TIER_2_STABLE_PUBLIC_STRUCTURED", "TIER_2"} or (
        automated and "socrata" in plat
    ):
        return AUTOMATED_TIER2
    if automated and publicly_searchable and not auth_required:
        if "html" in plat or "static" in plat or st in {"LIVE_VERIFIED", "UNVERIFIED_LIVE"}:
            return AUTOMATED_STATIC
    if auth_required or st in {"AUTH_REQUIRED", "BLOCKED"}:
        if "bidnet" in plat or "demandstar" in plat or "public purchase" in plat:
            return FREE_REGISTRATION_REQUIRED
        return AUTH_BLOCKED
    if portal_url and publicly_searchable and not automated:
        return PORTAL_DISCOVERED_NOT_INTEGRATED
    if portal_url and not publicly_searchable:
        return MANUAL_PUBLIC if not auth_required else FREE_REGISTRATION_REQUIRED
    if portal_url:
        return PORTAL_DISCOVERED_NOT_INTEGRATED
    return UNKNOWN_RESEARCH_PENDING


def current_access_score(row: dict[str, Any]) -> dict[str, Any]:
    """Score how accessible an opportunity is for current business (no CAGE/SAM/DIBBS).

    Free/simple vendor registration is acceptable (not a hard block).
    """
    blob = " ".join(
        str(row.get(k) or "")
        for k in (
            "title",
            "description",
            "set_aside",
            "submission_method",
            "access_blocker",
            "competition_access_type",
            "jurisdiction",
            "buyer_type",
            "source_id",
            "source_portal",
            "registration_action",
            "platform_family",
        )
    ).lower()
    sid = str(row.get("source_id") or row.get("source_portal") or "").lower()
    factors = {
        "no_cage_required": "cage" not in blob and "dibbs" not in blob,
        "no_sam_required": "sam.gov registration required" not in blob and "sam registration" not in blob,
        "public_solicitation": str(row.get("our_bid_access") or "").upper()
        in {"YES", "CONDITIONAL", "REGISTER", ""},
        "free_registration": "paid registration" not in blob
        and "subscription required" not in blob
        and "paid_access" not in blob,
        "easy_registration": True,  # free/simple reg is an execution step, not rejection
        "no_restricted_vehicle": not any(
            x in blob for x in ("sewp only", "idiq only", "gwac only", "mas only", "bpa only")
        ),
        "no_source_approval": "source approval" not in blob and "qpl" not in blob,
        "nonfederal": str(row.get("jurisdiction") or row.get("source_level") or "").upper()
        not in {"FEDERAL", "FED"},
    }
    if "sam" in sid or "fed_" in sid or "usaspending" in sid or "dibbs" in sid or "piee" in sid:
        factors["nonfederal"] = False
        factors["no_cage_required"] = False if ("dibbs" in sid or "piee" in sid) else factors["no_cage_required"]
    # BidNet / similar free-reg networks are nonfederal-accessible when listing is public
    if "bidnet" in sid or "network_bidnet" in sid:
        factors["nonfederal"] = True
        factors["free_registration"] = True

    score = sum(10 for v in factors.values() if v)
    if (
        factors["nonfederal"]
        and factors["no_cage_required"]
        and factors["no_sam_required"]
        and factors["free_registration"]
        and factors["no_restricted_vehicle"]
        and factors["no_source_approval"]
    ):
        label = NONFEDERAL_ACCESSIBLE_NOW
    else:
        label = "FEDERAL_OR_RESTRICTED"
    return {"CurrentAccessScore": score, "label": label, "factors": factors}


def classify_opportunity_access(row: dict[str, Any]) -> str:
    """Opportunity-level access taxonomy (L.17.2 §22)."""
    blob = " ".join(
        str(row.get(k) or "")
        for k in ("access_blocker", "restrictions", "submission_method", "title", "source_id")
    ).lower()
    sid = str(row.get("source_id") or "").lower()
    if any(x in blob for x in ("source approval", "qpl", "cage required", "dibbs")) or "dibbs" in sid:
        return HARD_ELIGIBILITY_BLOCKER
    if "paid" in blob and ("subscription" in blob or "membership" in blob):
        return PAID_ACCESS_REQUIRED_OPP
    if str(row.get("our_bid_access") or "").upper() in {"NO", "BLOCKED"} or "login required" in blob:
        if "register" in blob or "bidnet" in sid or "publicpurchase" in sid:
            return FREE_REGISTRATION_REQUIRED
        return AUTH_REQUIRED_OPP
    if "bidnet" in sid or "register" in blob or str(row.get("registration_action") or ""):
        score = current_access_score(row)
        if score["label"] == NONFEDERAL_ACCESSIBLE_NOW:
            return FREE_REGISTRATION_REQUIRED  # still pursuable; unlock via reg
        return SIMPLE_VENDOR_SETUP
    if current_access_score(row)["label"] == NONFEDERAL_ACCESSIBLE_NOW:
        return ACCESSIBLE_NOW
    return AUTH_REQUIRED_OPP


def is_nonfederal_accessible_now(row: dict[str, Any]) -> bool:
    return current_access_score(row)["label"] == NONFEDERAL_ACCESSIBLE_NOW


def registration_unlock_score(
    *,
    buyers_unlocked: int,
    open_opportunities: int = 0,
    product_opportunities: int = 0,
    recurring: bool = False,
    cost: float = 0.0,
    setup_burden: str = "easy",
) -> dict[str, Any]:
    burden = {"easy": 1.0, "medium": 0.6, "hard": 0.25}.get(str(setup_burden).lower(), 0.5)
    cost_pen = 0.0 if cost <= 0 else (0.5 if cost < 100 else 0.1)
    score = (
        buyers_unlocked * 10
        + open_opportunities * 3
        + product_opportunities * 5
        + (40 if recurring else 0)
    ) * burden * (1.0 if cost <= 0 else cost_pen)
    return {
        "RegistrationUnlockScore": round(score, 2),
        "buyers_unlocked": buyers_unlocked,
        "open_opportunities": open_opportunities,
        "product_opportunities": product_opportunities,
        "cost": cost,
        "setup_burden": setup_burden,
    }
