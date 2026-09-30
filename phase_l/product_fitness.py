"""Phase L.2.1 — product-resale fitness before market-price spend."""

from __future__ import annotations

import re
from typing import Any

PRODUCT_RESALE = "PRODUCT_RESALE"
PRODUCT_WITH_INCIDENTAL_SERVICE = "PRODUCT_WITH_INCIDENTAL_SERVICE"
MIXED_PRODUCT_SERVICE = "MIXED_PRODUCT_SERVICE"
SERVICE = "SERVICE"
REPAIR_OVERHAUL = "REPAIR_OVERHAUL"
ENGINEERING_SUPPORT = "ENGINEERING_SUPPORT"
UNKNOWN = "UNKNOWN"

_SERVICE_RE = re.compile(
    r"\b(services?\b|consulting|study\b|assessment|training\s+only|"
    r"staffing|labor\s+hour|time\s+and\s+material|"
    r"janitorial|custodial|landscap|grounds\s+maintenance|"
    r"bridge[\-\s]?study|design[\-\s]?build)\b",
    re.I,
)
_ENGINEERING_RE = re.compile(
    r"\b(sustaining\s+engineering|engineering\s+support|"
    r"systems?\s+engineering|technical\s+support\s+services?|"
    r"program\s+management|logistics\s+support)\b",
    re.I,
)
_REPAIR_RE = re.compile(
    r"\b(repair|overhaul|refurbish|remediation|maintenance\s+of|"
    r"BOA\s+repair|repair\s+renewal|depot\s+repair)\b",
    re.I,
)
_PRODUCT_RE = re.compile(
    r"\b(NSN|P/?N|part\s+number|MPN|SKU|monitor|printer|laptop|server|"
    r"generator|pump|tractor|vehicle|toolcat|bobcat|forklift|"
    r"switch|router|camera|FLIR|Dell|Canon|ASUS|supply|supplies|"
    r"equipment|hardware|assembly|valve|bearing|gasket|kit)\b",
    re.I,
)
_INCIDENTAL_RE = re.compile(
    r"\b(including\s+installation|delivery\s+and\s+setup|"
    r"with\s+installation|training\s+included)\b",
    re.I,
)


def classify_product_fitness(row: dict[str, Any], *, text: str | None = None) -> dict[str, Any]:
    blob = text or "\n".join(
        str(x or "")
        for x in (
            row.get("title"),
            row.get("description"),
            row.get("solicitation_text"),
            row.get("product_class"),
        )
    )
    if _ENGINEERING_RE.search(blob):
        fit = ENGINEERING_SUPPORT
    elif _REPAIR_RE.search(blob) and not _PRODUCT_RE.search(blob):
        fit = REPAIR_OVERHAUL
    elif _REPAIR_RE.search(blob) and _PRODUCT_RE.search(blob):
        fit = MIXED_PRODUCT_SERVICE
    elif _SERVICE_RE.search(blob) and not _PRODUCT_RE.search(blob):
        fit = SERVICE
    elif _SERVICE_RE.search(blob) and _PRODUCT_RE.search(blob):
        fit = MIXED_PRODUCT_SERVICE if not _INCIDENTAL_RE.search(blob) else PRODUCT_WITH_INCIDENTAL_SERVICE
    elif _PRODUCT_RE.search(blob) or row.get("is_product") or row.get("nsn"):
        fit = PRODUCT_WITH_INCIDENTAL_SERVICE if _INCIDENTAL_RE.search(blob) else PRODUCT_RESALE
    else:
        fit = UNKNOWN

    market_ok = fit in {PRODUCT_RESALE, PRODUCT_WITH_INCIDENTAL_SERVICE}
    return {
        "product_fitness": fit,
        "market_price_research_allowed": market_ok,
        "owner_hunt_priority": market_ok,
        "reason": fit,
    }
