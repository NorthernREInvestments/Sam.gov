"""Owner-approved NextBid search filters for live product discovery.

Current stack (one search call — no extras):
1) open=true
2) tradelines=commodities
3) sources=state/local portals only (excludes sam.gov / dibbs federal)

After search (same credit, before get_opportunity): drop non-competitive notices
and obvious service titles — no extra API call.
"""

from __future__ import annotations

import re
from typing import Any

# Non-federal portals only. InstantMarkets omitted for now — can syndicate federal notices.
OWNER_NON_FEDERAL_SOURCES = "opengov,bonfire,planetbids,calprocure,publicpurchase"

OWNER_PRODUCT_SEARCH_FILTERS: dict[str, Any] = {
    "open": "true",
    "tradelines": "commodities",
    "sources": OWNER_NON_FEDERAL_SOURCES,
}

# Not open competitive product bids — do not spend get_opportunity on these.
NON_COMPETITIVE_LISTING_RE = re.compile(
    r"(?i)\b("
    r"sole\s*source|single[\s-]*source|only\s+source|"
    r"request\s+for\s+exemption|notice\s+of\s+exemption|exemption\s+notice|"
    r"intent\s+to\s+(?:award|negotiate|sole)|notice\s+of\s+intent|"
    r"product\s+demo|request\s+for\s+product\s+demos?|demonstration\s+only|"
    r"brand\s+name\s+only|proprietary\s+purchase|"
    r"piggyback\s+only|cooperative\s+contract\s+use\s+notice|"
    r"informational\s+only|no\s+bids?\s+accepted"
    r")\b"
)

# Obvious service / labor titles — skip before paying for get_opportunity.
# Keep narrow enough not to kill "Passenger Van", "Switch Vehicles", "Furniture".
SERVICE_LISTING_RE = re.compile(
    r"(?i)("
    r"\bservices\b|"
    r"\bstaffing\b|"
    r"\bjanitorial\b|"
    r"\bconsulting\b|"
    r"\bhelicopter\s+transport\b|"
    r"\btransport\s+services\b|"
    r"\bprofessional\s+services\b|"
    r"\bmanaged\s+services\b|"
    r"\bpest\s+control\s+services\b|"
    r"\bfire\s+protection\s+services\b|"
    r"\bsecurity\s+services\b|"
    r"\btraining\s+services\b|"
    r"\bmaintenance\s+services\b|"
    r"\brepair\s+services\b|"
    r"\binstallation\s+services\b"
    r")"
)


def owner_product_search_params(*, page: int = 1, page_size: int = 25, sort: str = "deadline") -> dict[str, Any]:
    return {
        **OWNER_PRODUCT_SEARCH_FILTERS,
        "page": page,
        "page_size": page_size,
        "sort": sort,
    }


def listing_non_competitive_reason(title: str, *, notice_type: str | None = None) -> str | None:
    """Return skip reason if listing is clearly not an open competitive bid."""
    blob = f"{title or ''} {notice_type or ''}".strip()
    if not blob:
        return None
    m = NON_COMPETITIVE_LISTING_RE.search(blob)
    if m:
        return f"non_competitive_listing:{m.group(0).lower()}"
    return None


def listing_service_title_reason(title: str, *, notice_type: str | None = None) -> str | None:
    """Return skip reason if listing title is an obvious service/labor buy."""
    blob = f"{title or ''} {notice_type or ''}".strip()
    if not blob:
        return None
    m = SERVICE_LISTING_RE.search(blob)
    if m:
        return f"service_title:{m.group(0).lower()}"
    return None


def listing_skip_before_detail_reason(title: str, *, notice_type: str | None = None) -> str | None:
    """Single pre-get_opportunity gate: non-competitive OR obvious service title."""
    return listing_non_competitive_reason(title, notice_type=notice_type) or listing_service_title_reason(
        title, notice_type=notice_type
    )


def is_competitive_listing_candidate(title: str, *, notice_type: str | None = None) -> bool:
    """True only if we should spend a get_opportunity credit on this listing."""
    return listing_skip_before_detail_reason(title, notice_type=notice_type) is None
