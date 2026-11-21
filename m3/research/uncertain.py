"""Stage 4 — evidence-based resolution of uncertain / unknown_mixed candidates."""

from __future__ import annotations

import re
from typing import Any

from m3.research.classify import (
    CONSTRUCTION_ONLY,
    INCIDENTAL_SERVICE,
    MAINTENANCE_REPAIR,
    PRODUCT_HINTS,
    SERVICE_ONLY,
    SIGNIFICANT_INSTALL,
    SOFTWARE_ONLY,
    SUPPLY_PHRASE,
    classify_requirement,
)

EXTRA_PRODUCT = re.compile(
    r"(?i)\b("
    r"screw|fastener|bolt|nut|washer|valve|pump|motor|compressor|"
    r"refrigerat|freezer|appliance|laundry|washer|dryer|range|oven|"
    r"generator|genset|forklift|pallet jack|mower|tractor|"
    r"furniture|desk|chair|cabinet|shelving|locker|"
    r"toner|cartridge|paper|copier|printer|monitor|laptop|"
    r"ppe|glove|respirator|hard hat|safety vest|"
    r"conduit|breaker|luminaire|fixture|ballast|"
    r"chemical|disinfectant|soap|towel|liner|"
    r"tire|battery|filter|lubricant|oil|"
    r"uniform|boot|shoe|apparel|commodity|goods|materials|supplies|equipment"
    r")\b"
)
HISTORICAL = re.compile(r"(?i)\b(award(?:ed)?|closed|tabulation|purchase order history)\b")
FORECAST = re.compile(r"(?i)\b(buying plan|forecast|planned procurement|anticipated)\b")


def _fetch_detail_snippet(url: str) -> str:
    if not url or not str(url).startswith("http"):
        return ""
    try:
        from m3.research.http_util import fetch_text

        status, _final, html = fetch_text(url, timeout=12)
        if status >= 400 or not html:
            return ""
        text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
        text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
        return text.replace("\x00", "")[:6000]
    except Exception:
        return ""


def resolve_uncertain_item(item: dict[str, Any], *, detail_text: str | None = None) -> dict[str, Any]:
    """Reclassify using available evidence. Does not invent commercial facts."""
    title = str(item.get("title") or "")
    cat = str(item.get("product_category") or "")
    evidence_cat = ""
    if isinstance(item.get("evidence"), dict):
        evidence_cat = str(item["evidence"].get("category_raw") or "")
    status = ""
    if isinstance(item.get("evidence"), dict):
        status = str(item["evidence"].get("status") or "")
    blob = f"{title} {cat} {evidence_cat} {item.get('procurement_type') or ''} {detail_text or ''}"
    out = dict(item)
    reasons: list[str] = []
    if detail_text:
        clean = detail_text.replace("\x00", "")[:4000]
        out["document_text"] = clean
        out["_document_searchable"] = True
        reasons.append("detail_page_retrieved")

    if item.get("is_forecast") or FORECAST.search(blob):
        out["requirement_class"] = "unknown_mixed"
        out["solicitation_status"] = "forecast"
        out["uncertain_resolution"] = "forecast"
        reasons.append("forecast_or_buying_plan")
        out["classification_evidence"] = reasons
        return out

    if HISTORICAL.search(status) or (
        HISTORICAL.search(blob) and not PRODUCT_HINTS.search(title) and not SUPPLY_PHRASE.search(title)
    ):
        if re.search(r"(?i)\b(closed|award|awarded)\b", status):
            out["requirement_class"] = out.get("requirement_class") or "unknown_mixed"
            out["solicitation_status"] = "historical_award" if "award" in status.lower() else "closed"
            out["uncertain_resolution"] = "historical_or_closed"
            reasons.append(f"status={status or 'historical_language'}")
            out["classification_evidence"] = reasons
            return out

    clf = classify_requirement(
        title,
        category_raw=evidence_cat or cat,
        procurement_type=item.get("procurement_type"),
        description=item.get("description"),
        document_text=detail_text or item.get("document_text"),
        line_items_text=item.get("line_items_text"),
    )
    out["requirement_class"] = clf["requirement_class"]
    out["classification_evidence"] = list(clf.get("classification_evidence") or []) + reasons
    out["include_in_product_resale"] = clf.get("include_in_product_resale")
    out["evidence_sources"] = clf.get("evidence_sources")
    out["classification_stage"] = "central_evidence"

    cls = clf["requirement_class"]
    if cls in {
        "product_supply",
        "product_with_incidental_services",
        "product_with_significant_installation",
        "product_rental_leasing",
        "mixed_product_service",
    }:
        out["uncertain_resolution"] = cls
    elif cls in {
        "services",
        "professional_services",
        "maintenance_repair_services",
        "construction",
        "software_only",
    }:
        out["uncertain_resolution"] = cls
    else:
        # Commodity label alone is not enough
        if evidence_cat and re.search(r"(?i)commodity", evidence_cat) and not EXTRA_PRODUCT.search(title):
            out["uncertain_resolution"] = "requires_further_verification"
            out["classification_evidence"] = (out.get("classification_evidence") or []) + [
                "commodity_label_without_product_terms"
            ]
        else:
            out["uncertain_resolution"] = "requires_further_verification"
    return out


def resolve_uncertain_batch(
    items: list[dict[str, Any]],
    *,
    fetch_details: bool = False,
    detail_limit: int = 80,
) -> dict[str, Any]:
    """Resolve unknown_mixed / pending population; optionally fetch official detail pages."""
    updated: list[dict] = []
    counts: dict[str, int] = {}
    investigated = 0
    details_fetched = 0
    newly_product = 0
    unresolved_reasons: dict[str, int] = {}

    for it in items:
        req = (it.get("requirement_class") or "").lower()
        needs = req in {"unknown_mixed", "unknown", "pending_classification", ""} or (
            it.get("uncertain_resolution") == "requires_further_verification"
        )
        if not needs:
            updated.append(it)
            continue

        investigated += 1
        detail = None
        if fetch_details and details_fetched < detail_limit:
            url = it.get("official_url") or ""
            # Prefer solicitation detail URLs over raw dataset JSON endpoints
            if url and ".json" not in url.split("?")[0].lower():
                detail = _fetch_detail_snippet(url)
                if detail:
                    details_fetched += 1

        before = req
        resolved = resolve_uncertain_item(it, detail_text=detail)
        key = resolved.get("uncertain_resolution") or resolved.get("requirement_class") or "unresolved"
        counts[key] = counts.get(key, 0) + 1
        after = (resolved.get("requirement_class") or "").lower()
        if before in {"unknown_mixed", "unknown", "pending_classification"} and after in {
            "product_supply",
            "product_with_incidental_services",
            "product_with_significant_installation",
            "product_rental_leasing",
            "mixed_product_service",
        }:
            newly_product += 1
        if key == "requires_further_verification":
            reason = (resolved.get("classification_evidence") or ["insufficient_evidence"])[-1]
            unresolved_reasons[str(reason)[:80]] = unresolved_reasons.get(str(reason)[:80], 0) + 1
        updated.append(resolved)

    still_uncertain = sum(
        1
        for i in updated
        if (i.get("uncertain_resolution") or i.get("requirement_class"))
        in {"requires_further_verification", "unknown_mixed", "unknown", "pending_classification"}
    )
    return {
        "items": updated,
        "investigated": investigated,
        "resolved_counts": counts,
        "still_uncertain": still_uncertain,
        "details_fetched": details_fetched,
        "newly_product": newly_product,
        "unresolved_reasons": unresolved_reasons,
    }
