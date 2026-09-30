"""Phase L cross-government normalization into one ranking row."""

from __future__ import annotations

import re
from typing import Any

from application_clock import now_utc
from phase_l.access_gate import evaluate_phase_l_access
from phase_l.competition import annotate_competition
from phase_l.economics import build_phase_l_economics

FEDERAL = "FEDERAL"
STATE = "STATE"
LOCAL = "LOCAL"
COOPERATIVE = "COOPERATIVE"

EXACT = "EXACT"
APPROVED_SOURCE_EXACT = "APPROVED_SOURCE_EXACT"
OR_EQUAL_MATCH = "OR_EQUAL_MATCH"
SIMILAR_ALLOWED_MATCH = "SIMILAR_ALLOWED_MATCH"
IDENTITY_UNKNOWN = "UNKNOWN"

_NSN_RE = re.compile(r"\b(\d{4}-\d{2}-\d{3}-\d{4})\b")
_OR_EQUAL_RE = re.compile(
    r"\bor\s+equal\b|\bor\s+similar\b|approved\s+equal|equivalent(?:\s+product)?|"
    r"brand[\s\-]?name\s+or\s+equal",
    re.I,
)

# Actionability states
DISCOVERED = "DISCOVERED"
PRODUCT_CONFIRMED = "PRODUCT_CONFIRMED"
ACCESS_CHECK_PENDING = "ACCESS_CHECK_PENDING"
INACCESSIBLE = "INACCESSIBLE"
ACCESS_CONFIRMED = "ACCESS_CONFIRMED"
HISTORY_RESEARCH_PENDING = "HISTORY_RESEARCH_PENDING"
RETAIL_RESEARCH_PENDING = "RETAIL_RESEARCH_PENDING"
ECONOMICS_PENDING = "ECONOMICS_PENDING"
ECONOMIC_FAIL = "ECONOMIC_FAIL"
FINANCEABILITY_PENDING = "FINANCEABILITY_PENDING"
READY_FOR_OWNER_REVIEW = "READY_FOR_OWNER_REVIEW"


def infer_source_level(row: dict[str, Any]) -> str:
    kind = str(row.get("source_level") or row.get("kind") or row.get("buyer_type") or "").upper()
    sid = str(row.get("source_id") or "").lower()
    if kind in {FEDERAL, STATE, LOCAL, COOPERATIVE}:
        return kind
    if kind in {"CITY", "COUNTY", "SCHOOL", "UTILITY", "TRANSIT", "AIRPORT", "AGENCY"}:
        return LOCAL
    if kind == "NETWORK":
        return LOCAL
    if sid.startswith("state_") or "state" in kind.lower():
        return STATE
    if sid.startswith("coop_") or "coop" in sid:
        return COOPERATIVE
    if sid.startswith(("fed_", "sam", "dla", "dibbs")) or "federal" in sid:
        return FEDERAL
    if sid.startswith("agency_"):
        return LOCAL
    jur = str(row.get("jurisdiction") or "").upper()
    if jur in {"FEDERAL", "US", "USA"}:
        return FEDERAL
    return STATE if row.get("state_code") else LOCAL


def detect_identity_match_type(row: dict[str, Any], *, text: str | None = None) -> dict[str, Any]:
    blob = f"{row.get('title') or ''}\n{row.get('description') or ''}\n{text or ''}"
    nsn = row.get("nsn")
    if not nsn:
        m = _NSN_RE.search(blob)
        nsn = m.group(1) if m else None
    mpn = row.get("mpn") or row.get("part_number")
    or_equal = bool(_OR_EQUAL_RE.search(blob))
    if row.get("approved_source") and nsn:
        match = APPROVED_SOURCE_EXACT
    elif nsn or mpn:
        match = EXACT
    elif or_equal and row.get("matched_retail_product"):
        match = OR_EQUAL_MATCH
    elif or_equal:
        match = OR_EQUAL_MATCH
    elif row.get("matched_retail_product") and row.get("similar_allowed"):
        match = SIMILAR_ALLOWED_MATCH
    else:
        match = IDENTITY_UNKNOWN
    return {
        "identity_match_type": match,
        "nsn": nsn,
        "mpn": mpn,
        "or_equal_permitted": or_equal,
        "matched_retail_product": row.get("matched_retail_product"),
        "required_specs": row.get("required_specs"),
        "matched_specs": row.get("matched_specs"),
        "unresolved_deviations": row.get("unresolved_deviations"),
        "identity_confidence": row.get("identity_confidence") or ("HIGH" if match == EXACT else "LOW"),
    }


def owner_decision_state(row: dict[str, Any]) -> str:
    """Concise owner decision chip."""
    access = str(row.get("our_bid_access") or "")
    if access in {"NO"}:
        return "ACCESS_BLOCKED"
    if access in {"UNKNOWN", "CONDITIONAL"}:
        return "ACCESS_BLOCKED" if access == "NO" else "NEEDS_HISTORY" if not row.get("historical_award_price") else "ACCESS_CHECK_PENDING"
    if access != "YES":
        return "ACCESS_CHECK_PENDING"
    if (
        not row.get("historical_award_price")
        and not row.get("historical_award_unit_price")
        and not row.get("buyer_budget")
    ):
        return "NEEDS_HISTORY"
    if not row.get("public_retail_price") and not row.get("public_retail_unit_price"):
        return "NEEDS_RETAIL"
    if row.get("profit_tier") in {None, "FAIL"} or not row.get("meets_floor"):
        return "ECONOMIC_FAIL"
    if row.get("actionable_state") == READY_FOR_OWNER_REVIEW:
        return "READY_FOR_OWNER_REVIEW"
    if row.get("meets_floor"):
        return "READY_FOR_OWNER_REVIEW"
    return "NEEDS_FINANCING_REVIEW"


def normalize_opportunity(
    row: dict[str, Any],
    *,
    text: str | None = None,
    access: dict[str, Any] | None = None,
    economics: dict[str, Any] | None = None,
    competition: dict[str, Any] | None = None,
    runway_days: float | None = None,
) -> dict[str, Any]:
    """Project any source row into the Phase L common opportunity model."""
    source_level = infer_source_level(row)
    access = access or evaluate_phase_l_access(
        {**row, "source_level": source_level},
        text=text,
        runway_days=runway_days,
        vendor_registration_lead_time_days=row.get("vendor_registration_lead_time_days"),
    )
    identity = detect_identity_match_type(row, text=text)
    competition = competition or annotate_competition(
        historical_offers_received=row.get("historical_offers_received") or row.get("offer_count"),
        historical_competition_type=row.get("historical_competition_type"),
        competition_access_type=access.get("competition_access_type"),
        historical_accessibility_match=row.get("historical_accessibility_match"),
    )
    economics = economics or (
        build_phase_l_economics(
            quantity=row.get("quantity"),
            uom=row.get("uom"),
            historical_unit_price=row.get("historical_award_unit_price") or row.get("historical_unit_price"),
            expected_bid_unit_price=row.get("expected_bid_unit_price"),
            public_retail_unit_price=row.get("public_retail_unit_price") or row.get("public_retail_price"),
            public_retail_source=row.get("public_retail_source"),
            freight=row.get("freight"),
            packaging=row.get("packaging"),
            compliance_direct_costs=row.get("compliance_direct_costs"),
            contingency=row.get("contingency"),
        )
        if access.get("our_bid_access") == "YES"
        else None
    )

    actionable_state = DISCOVERED
    if row.get("is_product"):
        actionable_state = PRODUCT_CONFIRMED
    if access.get("our_bid_access") == "NO":
        actionable_state = INACCESSIBLE
    elif access.get("our_bid_access") in {"UNKNOWN", "CONDITIONAL"}:
        actionable_state = ACCESS_CHECK_PENDING
    elif access.get("our_bid_access") == "YES":
        actionable_state = ACCESS_CONFIRMED
        if not (row.get("historical_award_price") or row.get("historical_award_unit_price")):
            actionable_state = HISTORY_RESEARCH_PENDING
        elif not (row.get("public_retail_unit_price") or row.get("public_retail_price")):
            actionable_state = RETAIL_RESEARCH_PENDING
        elif economics and economics.get("blocker"):
            actionable_state = ECONOMIC_FAIL if economics.get("blocker") != "MISSING_PUBLIC_RETAIL" else RETAIL_RESEARCH_PENDING
        elif economics and economics.get("meets_floor"):
            # Full READY only when all hard gates clear
            qty_ok = row.get("quantity") is not None and row.get("uom")
            live_ok = str(row.get("live_status") or row.get("status") or "").upper() in {
                "OPEN",
                "ACTIVE",
                "PUBLISHED",
                "LIVE",
            }
            if live_ok and qty_ok and identity["identity_match_type"] != IDENTITY_UNKNOWN:
                actionable_state = READY_FOR_OWNER_REVIEW
            else:
                actionable_state = economics.get("profit_tier") or ECONOMICS_PENDING
        elif economics:
            actionable_state = ECONOMIC_FAIL if not economics.get("meets_floor") else ECONOMICS_PENDING

    out = {
        "kind": "PhaseLOpportunity",
        "source_level": source_level,
        "source_portal": row.get("source_id") or row.get("portal_source"),
        "jurisdiction": access.get("jurisdiction") or row.get("jurisdiction") or row.get("state_code"),
        "solicitation_id": row.get("solicitation_number") or row.get("solicitation_id") or row.get("external_id"),
        "agency": row.get("agency") or row.get("buyer"),
        "title": row.get("title"),
        "live_status": row.get("live_status") or row.get("status"),
        "deadline": row.get("response_deadline") or row.get("deadline"),
        "deadline_timezone": row.get("deadline_timezone"),
        "runway_days": runway_days if runway_days is not None else row.get("runway_days"),
        "quantity": row.get("quantity"),
        "uom": row.get("uom"),
        "product_identity": {
            "nsn": identity.get("nsn"),
            "mpn": identity.get("mpn"),
            "title": row.get("title"),
        },
        "identity_match_type": identity.get("identity_match_type"),
        "identity": identity,
        "set_aside": row.get("set_aside"),
        "competition_access_type": access.get("competition_access_type"),
        "our_bid_access": access.get("our_bid_access"),
        "access_blocker": access.get("access_blocker"),
        "access": access,
        "historical_award_price": row.get("historical_award_price") or row.get("historical_award_total"),
        "historical_award_unit_price": row.get("historical_award_unit_price") or row.get("historical_unit_price"),
        "historical_offers_received": competition.get("historical_offers_received"),
        "historical_competition_type": competition.get("historical_competition_type"),
        "historical_accessibility_match": competition.get("historical_accessibility_match"),
        "effective_competition_signal": competition.get("effective_competition_signal"),
        "competition": competition,
        "public_retail_price": (economics or {}).get("public_retail_unit_price")
        or row.get("public_retail_unit_price")
        or row.get("public_retail_price"),
        "acquisition_cost": (economics or {}).get("acquisition_cost"),
        "financing_cost": (economics or {}).get("estimated_financing_cost"),
        "direct_costs": (economics or {}).get("estimated_direct_costs"),
        "expected_revenue": (economics or {}).get("expected_revenue"),
        "expected_net_profit": (economics or {}).get("expected_net_profit"),
        "profit_tier": (economics or {}).get("profit_tier"),
        "meets_floor": (economics or {}).get("meets_floor"),
        "economics": economics,
        "actionable_state": actionable_state,
        "owner_decision": None,
        "source_url": row.get("detail_url") or row.get("source_url") or row.get("link"),
        "last_live_verification_at": row.get("retrieved_at") or row.get("generated_at") or now_utc().isoformat(),
        "is_product": row.get("is_product"),
        "raw_ref": {
            "external_id": row.get("external_id") or row.get("notice_id"),
            "source_id": row.get("source_id"),
        },
    }
    out["owner_decision"] = owner_decision_state(out)
    return out
