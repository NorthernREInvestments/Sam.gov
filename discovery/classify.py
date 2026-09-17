"""Deterministic product-first discovery classification — $0 AI cost."""

from __future__ import annotations

import re
from typing import Any

from discovery.constants import (
    CLASS_CLEARLY_IRRELEVANT,
    CLASS_CORE_PRODUCT,
    CLASS_PRODUCT_PLUS_SERVICE,
    CLASS_SERVICE,
    CLASS_UNKNOWN,
)

PRODUCT_POSITIVE = (
    r"\bequipment\b",
    r"\bsupplies\b",
    r"\bhardware\b",
    r"\bvehicles?\b",
    r"\bmachinery\b",
    r"\bparts?\b",
    r"\bmaterials?\b",
    r"\bservers?\b",
    r"\bnetwork\s+equipment\b",
    r"\belectronics?\b",
    r"\blaboratory\s+equipment\b",
    r"\bmedical\s+equipment\b",
    r"\bmedical\s+supplies\b",
    r"\bindustrial\s+equipment\b",
    r"\btools?\b",
    r"\bjanitorial\s+supplies\b",
    r"\bfurniture\b",
    r"\bappliances?\b",
    r"\bhvac\s+equipment\b",
    r"\belectrical\s+equipment\b",
    r"\bplumbing\s+equipment\b",
    r"\bsafety\s+equipment\b",
    r"\bmaintenance\s+supplies\b",
    r"\bcomputers?\b",
    r"\blaptops?\b",
    r"\bmonitors?\b",
    r"\bswitches?\b",
    r"\brouters?\b",
    r"\bstorage\s+drives?\b",
    r"\bpurchase\s+of\b",
    r"\bprocurement\s+of\b",
    r"\bit\s+hardware\b",
)

SERVICE_NEGATIVE = (
    r"\bprofessional\s+services?\b",
    r"\bconsulting\b",
    r"\bengineering\s+services?\b",
    r"\barchitectural\s+services?\b",
    r"\bstaffing\b",
    r"\bjanitorial\s+services?\b",
    r"\bcustodial\s+services?\b",
    r"\bgrounds?\s+maintenance\s+services?\b",
    r"\bmowing\s+services?\b",
    r"\bsoftware\s+development\b",
    r"\btraining\s+services?\b",
    r"\blegal\s+services?\b",
    r"\baudit\s+services?\b",
    r"\bmedical\s+services?\b",
    r"\bhvac\s+maintenance\s+services?\b",
    r"\brepair[\s-]only\b",
    r"\bconstruction\s+labor\b",
    r"\bconstruction\s+services?\b",
    r"\bmanagement\s+services?\b",
    r"\bconcessionaire\b",
    r"\bconcession\s+operations?\b",
    r"\bconcession\s+services?\b",
    r"\boperate\s+within\b",
    r"\bgolf\s+course\b",
    r"\bmarina\b.*\boperate\b",
)

PLUS_SERVICE = (
    r"\bsupply\s+and\s+install\b",
    r"\bfurnish\s+and\s+install\b",
    r"\bprovide\s+and\s+install\b",
    r"\bdelivery\s+and\s+installation\b",
    r"\bwith\s+installation\b",
)

IRRELEVANT = (
    r"\bawarded\b",
    r"\baward\s+notice\b",
    r"\bcancelled\b",
    r"\bcanceled\b",
)


def _hits(patterns: tuple[str, ...], text: str) -> list[str]:
    found = []
    for p in patterns:
        if re.search(p, text, re.I):
            found.append(p)
    return found


def classify_discovery_opportunity(
    *,
    title: str | None = None,
    description: str | None = None,
    commodity_hint: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """
    Free deterministic product-fit classification.
    Never calls AI. Never invents contract value.
    """
    blob = " ".join(x for x in [title or "", description or "", commodity_hint or ""] if x).lower()
    st = str(status or "").lower()

    if _hits(IRRELEVANT, blob) or st in {"cancelled", "canceled", "awarded"}:
        return {
            "classification": CLASS_CLEARLY_IRRELEVANT,
            "signals": {"irrelevant": True},
            "is_heuristic": True,
            "LIVE_API_REQUESTS": 0,
            "OpenAI": 0,
        }

    prod = _hits(PRODUCT_POSITIVE, blob)
    svc = _hits(SERVICE_NEGATIVE, blob)
    plus = _hits(PLUS_SERVICE, blob)

    if plus and (prod or not svc):
        cls = CLASS_PRODUCT_PLUS_SERVICE
    elif prod and not svc:
        cls = CLASS_CORE_PRODUCT
    elif svc and not prod:
        cls = CLASS_SERVICE
    elif prod and svc:
        cls = CLASS_PRODUCT_PLUS_SERVICE
    else:
        cls = CLASS_UNKNOWN

    return {
        "classification": cls,
        "signals": {
            "product_hits": len(prod),
            "service_hits": len(svc),
            "plus_service_hits": len(plus),
        },
        "is_heuristic": True,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


def early_reject_reasons(
    *,
    title: str | None = None,
    description: str | None = None,
    status: str | None = None,
    deadline_passed: bool = False,
    classification: str | None = None,
    estimated_value: str | None = None,
    estimated_value_status: str = "UNKNOWN",
) -> dict[str, Any]:
    """
    Cheap early rejects. UNKNOWN value alone is NEVER a reject.
    """
    reasons: list[str] = []
    blob = f"{title or ''} {description or ''}".lower()
    st = str(status or "").lower()

    if deadline_passed:
        reasons.append("deadline_already_passed")
    if st in {"cancelled", "canceled"}:
        reasons.append("cancelled")
    if "award notice" in blob or st == "awarded":
        reasons.append("award_notice_not_solicitation")
    if classification == CLASS_SERVICE:
        reasons.append("pure_service")
    if classification == CLASS_CLEARLY_IRRELEVANT:
        reasons.append("clearly_irrelevant")
    if re.search(r"\bconstruction[\s-]only\b", blob):
        reasons.append("construction_only")

    # Explicitly do NOT reject on unknown value
    value_reject = False
    if estimated_value_status == "UNKNOWN" or not estimated_value:
        value_reject = False

    return {
        "reject": bool(reasons),
        "reasons": reasons,
        "unknown_value_not_rejected": True,
        "value_reject_applied": value_reject,
        "LIVE_API_REQUESTS": 0,
    }
