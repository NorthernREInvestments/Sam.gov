"""Phase L.2.9 — explicit source role taxonomy (never mix silently)."""

from __future__ import annotations

from typing import Any

# Evidence roles
LIVE_OPPORTUNITY = "LIVE_OPPORTUNITY"
HISTORICAL_AWARD = "HISTORICAL_AWARD"
HISTORICAL_GOV_PRICE = "HISTORICAL_GOV_PRICE"
CURRENT_GOV_CONTRACT_PRICE = "CURRENT_GOV_CONTRACT_PRICE"
CURRENT_COMMERCIAL_PRICE = "CURRENT_COMMERCIAL_PRICE"
CURRENT_ACQUISITION_PRICE = "CURRENT_ACQUISITION_PRICE"
PRODUCT_IDENTITY = "PRODUCT_IDENTITY"
MPN_CROSS_REFERENCE = "MPN_CROSS_REFERENCE"
AUTHORIZED_DISTRIBUTOR = "AUTHORIZED_DISTRIBUTOR"
MANUFACTURER_IDENTITY = "MANUFACTURER_IDENTITY"
CONFIGURATION_REFERENCE = "CONFIGURATION_REFERENCE"
COMPETITION = "COMPETITION"
RECURRING_BUY = "RECURRING_BUY"
BUYER_CADENCE = "BUYER_CADENCE"
PRICE_LEAD_ONLY = "PRICE_LEAD_ONLY"

# Access / failure states
SOURCE_HEALTHY = "HEALTHY"
SOURCE_DEGRADED = "DEGRADED"
SOURCE_AUTH_REQUIRED = "AUTH_REQUIRED"
SOURCE_BOT_BLOCKED = "BOT_BLOCKED"
SOURCE_PARSE_BROKEN = "PARSE_BROKEN"
SOURCE_NO_RESULTS = "NO_RESULTS"
SOURCE_DISABLED = "DISABLED"
SOURCE_RESTRICTED = "SOURCE_RESTRICTED"

# Bot-block taxonomy (never collapse to PRICE_NOT_AVAILABLE)
PRIMARY_SOURCE_BLOCKED = "PRIMARY_SOURCE_BLOCKED"
PRICE_NOT_RECOVERED_AUTOMATICALLY = "PRICE_NOT_RECOVERED_AUTOMATICALLY"
PUBLIC_BROWSER_RENDER_REQUIRED = "PUBLIC_BROWSER_RENDER_REQUIRED"
CORROBORATED_STRONG_PRICE = "CORROBORATED_STRONG_PRICE"
FREIGHT_NOT_YET_RESEARCHED = "FREIGHT_NOT_YET_RESEARCHED"

# Permanent architecture flags — single source of truth is acquisition_lanes
from phase_l.acquisition_lanes import (  # noqa: E402
    DEEP_RESEARCH_NO_FIXED_COUNT,
    MANUAL_QUEUE_NO_FIXED_CAP,
    STAGE3_NO_ROW_CAP,
)

ALL_ROLES = (
    LIVE_OPPORTUNITY,
    HISTORICAL_AWARD,
    HISTORICAL_GOV_PRICE,
    CURRENT_GOV_CONTRACT_PRICE,
    CURRENT_COMMERCIAL_PRICE,
    CURRENT_ACQUISITION_PRICE,
    PRODUCT_IDENTITY,
    MPN_CROSS_REFERENCE,
    AUTHORIZED_DISTRIBUTOR,
    MANUFACTURER_IDENTITY,
    CONFIGURATION_REFERENCE,
    COMPETITION,
    RECURRING_BUY,
    BUYER_CADENCE,
    PRICE_LEAD_ONLY,
)


def classify_evidence_roles(
    *,
    source_family: str,
    prefer_history: bool = False,
    is_lead_only: bool = False,
    is_gov_channel: bool = False,
) -> list[str]:
    """Map a source family to one or more evidence roles."""
    if is_lead_only:
        return [PRICE_LEAD_ONLY]
    fam = (source_family or "").lower()
    roles: list[str] = []
    if prefer_history or any(x in fam for x in ("usaspending", "award", "bid tab", "board", "dibbs", "fpds")):
        roles.append(HISTORICAL_GOV_PRICE)
        if "award" in fam or "usaspending" in fam:
            roles.append(HISTORICAL_AWARD)
    if any(x in fam for x in ("omnia", "sourcewell", "naspo", "state contract", "gsa", "sewp", "cooperative")):
        roles.append(CURRENT_GOV_CONTRACT_PRICE)
        if is_gov_channel:
            roles.append(PRODUCT_IDENTITY)
    if any(x in fam for x in ("oem", "distributor", "dealer", "commercial", "mro", "newegg")):
        roles.extend([CURRENT_COMMERCIAL_PRICE, CURRENT_ACQUISITION_PRICE])
    if "identity" in fam or "cross" in fam:
        roles.extend([PRODUCT_IDENTITY, MPN_CROSS_REFERENCE])
    if "recurring" in fam:
        roles.append(RECURRING_BUY)
    if not roles:
        roles.append(PRICE_LEAD_ONLY if is_lead_only else CURRENT_COMMERCIAL_PRICE)
    # de-dupe preserve order
    out: list[str] = []
    for r in roles:
        if r not in out:
            out.append(r)
    return out


def tag_record(record: dict[str, Any], roles: list[str]) -> dict[str, Any]:
    out = dict(record)
    existing = list(out.get("roles") or [])
    for r in roles:
        if r not in existing:
            existing.append(r)
    out["roles"] = existing
    return out
