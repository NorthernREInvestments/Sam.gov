"""Manufacturer / distributor contact intelligence — no outreach.

Build: 20261004-m3-acquisition-scale-v1
"""

from __future__ import annotations

from typing import Any

from acquisition_scale.models import (
    CHANNEL_PATH_IDENTIFIED,
    DISTRIBUTOR_QUOTE_PATH_IDENTIFIED,
    DISTRIBUTOR_QUOTE_REQUIRED,
    NO_EXECUTABLE_CHANNEL_FOUND,
    OEM_QUOTE_PATH_IDENTIFIED,
    OEM_QUOTE_REQUIRED,
    PUBLIC_PRICE_AVAILABLE,
    PUBLIC_PRICE_BLOCKED_RETRYABLE,
    SUPPLIER_QUOTE_REQUIRED,
)
from public_price_search.queries import manufacturer_sites

_MFR_CONTACT: dict[str, dict[str, Any]] = {
    "CUMMINS": {
        "website": "https://www.cummins.com",
        "parts": "https://shop.cummins.com",
        "distributor_locator": "https://www.cummins.com/support/find-location",
        "quote_page": "https://shop.cummins.com",
        "sells_direct": True,
    },
    "FLEETGUARD": {
        "website": "https://www.cummins.com/parts/filters",
        "parts": "https://shop.cummins.com",
        "distributor_locator": "https://www.cummins.com/support/find-location",
        "sells_direct": True,
    },
    "FORD": {
        "website": "https://www.ford.com",
        "parts": "https://parts.ford.com",
        "distributor_locator": "https://www.ford.com/dealerships/",
        "sells_direct": False,
    },
    "GRAINGER": {
        "website": "https://www.grainger.com",
        "parts": "https://www.grainger.com",
        "sells_direct": True,
    },
    "DYNAREX": {
        "website": "https://www.dynarex.com",
        "parts": "https://www.dynarex.com",
        "sells_direct": True,
    },
    "SMITH-BLAIR": {
        "website": "https://www.smith-blair.com",
        "parts": "https://www.smith-blair.com",
        "sells_direct": True,
    },
    "CATERPILLAR": {
        "website": "https://www.cat.com",
        "parts": "https://parts.cat.com",
        "distributor_locator": "https://www.cat.com/en_US/support/dealer-locator.html",
        "sells_direct": False,
    },
    "MYERS": {
        "website": "https://www.femyers.com",
        "parts": "https://www.femyers.com",
        "distributor_locator": "https://www.femyers.com/where-to-buy",
        "quote_page": "https://www.femyers.com/contact",
        "sells_direct": False,
    },
    "FE MYERS": {
        "website": "https://www.femyers.com",
        "parts": "https://www.femyers.com",
        "distributor_locator": "https://www.femyers.com/where-to-buy",
        "quote_page": "https://www.femyers.com/contact",
        "sells_direct": False,
    },
    "PENTAIR": {
        "website": "https://www.pentair.com",
        "parts": "https://www.pentair.com",
        "distributor_locator": "https://www.pentair.com/en-us/support/find-a-dealer.html",
        "sells_direct": False,
    },
}


def _mfr_key(manufacturer: str | None) -> str | None:
    if not manufacturer:
        return None
    u = manufacturer.strip().upper()
    for k in _MFR_CONTACT:
        if k in u or u in k:
            return k
    # first token
    tok = u.split()[0] if u.split() else u
    return tok if len(tok) >= 3 else None


def identify_channel_quote_path(
    identity: dict[str, Any],
    *,
    channel: dict[str, Any] | None = None,
    public_price_found: bool = False,
    public_price_blocked: bool = False,
) -> dict[str, Any]:
    """Persist executable sourcing/quote path WITHOUT sending outreach."""
    mfr = identity.get("manufacturer") or identity.get("brand")
    key = _mfr_key(str(mfr) if mfr else None)
    known = _MFR_CONTACT.get(key or "", {})
    sites = manufacturer_sites(str(mfr) if mfr else None)
    ch = channel or {}

    website = known.get("website") or (f"https://www.{sites[0]}" if sites else None)
    parts = known.get("parts")
    locator = known.get("distributor_locator")
    quote_page = known.get("quote_page") or parts or website
    sells_direct = bool(known.get("sells_direct"))

    # Infer from channel signals
    open_reseller = bool(ch.get("open_reseller_channel"))
    incumbent = bool(ch.get("incumbent_advantage"))
    oem_direct = bool(ch.get("oem_direct_possible")) or sells_direct
    auth_dist = bool(ch.get("authorized_distributor_path")) or bool(locator)

    winners = ch.get("prior_winners") or []
    winner_classes = [w.get("class") for w in winners if isinstance(w, dict)]

    status = None
    quote_sub = None
    if public_price_found:
        status = PUBLIC_PRICE_AVAILABLE
    elif public_price_blocked and (oem_direct or auth_dist or open_reseller or website):
        status = PUBLIC_PRICE_BLOCKED_RETRYABLE
        if oem_direct:
            quote_sub = OEM_QUOTE_REQUIRED
            status = OEM_QUOTE_PATH_IDENTIFIED
        elif auth_dist or "DISTRIBUTOR" in winner_classes:
            quote_sub = DISTRIBUTOR_QUOTE_REQUIRED
            status = DISTRIBUTOR_QUOTE_PATH_IDENTIFIED
        elif open_reseller or "RESELLER" in winner_classes:
            quote_sub = SUPPLIER_QUOTE_REQUIRED
            status = CHANNEL_PATH_IDENTIFIED
    elif oem_direct:
        status = OEM_QUOTE_PATH_IDENTIFIED
        quote_sub = OEM_QUOTE_REQUIRED
    elif auth_dist or "DISTRIBUTOR" in winner_classes:
        status = DISTRIBUTOR_QUOTE_PATH_IDENTIFIED
        quote_sub = DISTRIBUTOR_QUOTE_REQUIRED
    elif open_reseller or website or "RESELLER" in winner_classes:
        status = CHANNEL_PATH_IDENTIFIED
        quote_sub = SUPPLIER_QUOTE_REQUIRED
    else:
        status = NO_EXECUTABLE_CHANNEL_FOUND

    return {
        "status": status,
        "quote_substatus": quote_sub,
        "manufacturer": mfr,
        "manufacturer_key": key,
        "website": website,
        "sales_quote_page": quote_page,
        "dealer_distributor_locator": locator,
        "authorized_distributor_list_public": bool(locator),
        "known_distributor_domains": sites[:6],
        "sells_direct": sells_direct,
        "authorization_requirement_unknown": True,
        "minimum_order_public": None,
        "quote_only_status": status in {
            OEM_QUOTE_PATH_IDENTIFIED,
            DISTRIBUTOR_QUOTE_PATH_IDENTIFIED,
            CHANNEL_PATH_IDENTIFIED,
        } and not public_price_found,
        "open_reseller_channel": open_reseller,
        "incumbent_channel_advantage": incumbent,
        "outreach_sent": False,
        "note": "Contact path identified only — no emails/calls sent",
        "used_history_as_acquisition_cost": False,
    }
