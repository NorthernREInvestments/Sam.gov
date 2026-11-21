"""Resolve unknown_mixed / uncertain product-lane candidates (Build 002A)."""

from __future__ import annotations

import re
from typing import Any

from m3.research.classify import (
    CATEGORY_COMMODITY,
    CONSTRUCTION_ONLY,
    INCIDENTAL_SERVICE,
    PRODUCT_HINTS,
    SERVICE_ONLY,
    classify_requirement,
)

SOFTWARE_ONLY = re.compile(
    r"(?i)\b("
    r"software license|saas|subscription software|cloud subscription|"
    r"microsoft 365|office 365|software as a service|erp software|"
    r"software only|annual software"
    r")\b"
)
HISTORICAL = re.compile(r"(?i)\b(award(?:ed)?|closed|tabulation|purchase order history|fy\d{2,4} award)\b")
FORECAST = re.compile(r"(?i)\b(buying plan|forecast|planned procurement|anticipated)\b")

# Extra product vocabulary for uncertain resolution (deterministic)
EXTRA_PRODUCT = re.compile(
    r"(?i)\b("
    r"screw|fastener|bolt|nut|washer|valve|pump|motor|compressor|"
    r"refrigerat|freezer|appliance|laundry|washer|dryer|range|oven|"
    r"generator|genset|forklift|pallet jack|mower|tractor|"
    r"furniture|desk|chair|cabinet|shelving|locker|"
    r"toner|cartridge|paper|toner|copier|printer|monitor|laptop|"
    r"ppe|glove|respirator|hard hat|safety vest|"
    r"conduit|breaker|luminaire|fixture|ballast|"
    r"chemical|disinfectant|soap|towel|liner|"
    r"tire|battery|filter|lubricant|oil|"
    r"uniform|boot|shoe|apparel"
    r")\b"
)


def resolve_uncertain_item(item: dict[str, Any]) -> dict[str, Any]:
    """Reclassify a single candidate. Does not invent commercial facts."""
    title = str(item.get("title") or "")
    cat = str(item.get("product_category") or "")
    evidence_cat = ""
    if isinstance(item.get("evidence"), dict):
        evidence_cat = str(item["evidence"].get("category_raw") or "")
    status = ""
    if isinstance(item.get("evidence"), dict):
        status = str(item["evidence"].get("status") or "")
    blob = f"{title} {cat} {evidence_cat} {item.get('procurement_type') or ''}"
    out = dict(item)
    reasons: list[str] = []

    if item.get("is_forecast") or FORECAST.search(blob):
        out["requirement_class"] = "forecast"
        out["uncertain_resolution"] = "forecast"
        reasons.append("forecast_or_buying_plan")
        out["classification_evidence"] = reasons
        return out
    if HISTORICAL.search(status) or HISTORICAL.search(blob) and not PRODUCT_HINTS.search(title):
        if re.search(r"(?i)\b(closed|award|awarded)\b", status):
            out["requirement_class"] = "historical_or_closed"
            out["uncertain_resolution"] = "historical_or_closed"
            reasons.append(f"status={status or 'historical_language'}")
            out["classification_evidence"] = reasons
            return out
    if SOFTWARE_ONLY.search(blob) and not EXTRA_PRODUCT.search(title):
        out["requirement_class"] = "software_only"
        out["uncertain_resolution"] = "software_only"
        reasons.append("software_subscription_language")
        out["classification_evidence"] = reasons
        return out

    # Stronger product signal
    if EXTRA_PRODUCT.search(title) or PRODUCT_HINTS.search(title):
        if INCIDENTAL_SERVICE.search(blob) or SERVICE_ONLY.search(blob):
            out["requirement_class"] = "product_with_incidental_services"
            out["uncertain_resolution"] = "product_with_incidental_services"
            reasons.append("product_terms_with_incidental_services")
        else:
            out["requirement_class"] = "product_supply"
            out["uncertain_resolution"] = "product_supply"
            reasons.append("deterministic_product_vocabulary")
        out["classification_evidence"] = reasons
        return out

    if SERVICE_ONLY.search(title) and not EXTRA_PRODUCT.search(title):
        out["requirement_class"] = "services"
        out["uncertain_resolution"] = "services_only"
        reasons.append("service_only_title")
        out["classification_evidence"] = reasons
        return out
    if CONSTRUCTION_ONLY.search(title) and not EXTRA_PRODUCT.search(title):
        out["requirement_class"] = "construction"
        out["uncertain_resolution"] = "construction_only"
        reasons.append("construction_only_title")
        out["classification_evidence"] = reasons
        return out

    # Commodity category alone is NOT enough to call verified product supply
    if evidence_cat and CATEGORY_COMMODITY.search(evidence_cat) and not EXTRA_PRODUCT.search(title):
        out["requirement_class"] = "unknown_mixed"
        out["uncertain_resolution"] = "requires_further_verification"
        reasons.append("commodity_label_without_product_terms")
        out["classification_evidence"] = reasons
        return out

    # Fall back to base classifier
    clf = classify_requirement(title, category_raw=evidence_cat or cat, procurement_type=item.get("procurement_type"))
    out["requirement_class"] = clf["requirement_class"]
    out["classification_evidence"] = clf.get("classification_evidence") or []
    if clf["requirement_class"] in {"product_supply", "product_with_incidental_services"}:
        out["uncertain_resolution"] = clf["requirement_class"]
    elif clf["requirement_class"] in {"services", "construction"}:
        out["uncertain_resolution"] = f"{clf['requirement_class']}_only"
    else:
        out["uncertain_resolution"] = "requires_further_verification"
    return out


def resolve_uncertain_batch(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve unknown_mixed / uncertain population; return counts + updated items."""
    updated: list[dict] = []
    counts: dict[str, int] = {}
    investigated = 0
    for it in items:
        req = (it.get("requirement_class") or "").lower()
        if req not in {"unknown_mixed", "unknown", ""} and it.get("uncertain_resolution"):
            updated.append(it)
            continue
        if req not in {"unknown_mixed", "unknown", "product_supply", "product_with_incidental_services"}:
            # Still allow re-check of unknown_mixed primarily
            if req != "unknown_mixed" and req != "unknown" and req:
                updated.append(it)
                continue
        investigated += 1
        resolved = resolve_uncertain_item(it)
        key = resolved.get("uncertain_resolution") or resolved.get("requirement_class") or "unresolved"
        counts[key] = counts.get(key, 0) + 1
        updated.append(resolved)
    still_uncertain = sum(
        1
        for i in updated
        if (i.get("uncertain_resolution") or i.get("requirement_class"))
        in {"requires_further_verification", "unknown_mixed", "unknown"}
    )
    return {
        "items": updated,
        "investigated": investigated,
        "resolved_counts": counts,
        "still_uncertain": still_uncertain,
    }
