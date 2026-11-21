"""Centralized requirement classification — Stage 3 (never a Stage 1 discovery gate)."""

from __future__ import annotations

import re
from typing import Any

# Product vocabulary for classification evidence only — not an adapter retrieval gate.
PRODUCT_HINTS = re.compile(
    r"(?i)\b("
    r"furniture|desk|chair|janitorial supplies?|custodial supplies?|cleaning supplies?|"
    r"tools?|mro|electrical supplies?|plumbing supplies?|hvac|appliances?|"
    r"refrigerators?|freezer|washer|dryer|range|"
    r"laptops?|computers?|printers?|toner|ppe|safety equipment|lighting|generators?|"
    r"forklifts?|equipment|supplies?|hardware|batteries|battery|shelving|"
    r"parts|lubricants?|oils?|filters?|uniforms?|gloves?|paper|commodity|goods|materials|"
    r"fixtures?|scanners?|multiplexers?|pumps?|valves?|transformers?|cables?|wires?|"
    r"vehicles?|tires?|"
    r"screws?|fasteners?|bolts?|nuts?|washers?|butterfly valve|ball valve|gate valve|"
    r"commercial laundry|fitness equipment|material handling|grounds equipment|"
    r"industrial equipment|ice machine|ice maker|construction materials?"
    r")\b"
)

SUPPLY_PHRASE = re.compile(
    r"(?i)\b("
    r"maintenance supplies?|janitorial supplies?|custodial supplies?|"
    r"cleaning supplies?|office supplies?|construction materials?|"
    r"water treatment equipment|medical supplies?|safety supplies?"
    r")\b"
)

INCIDENTAL_SERVICE = re.compile(
    r"\b(delivery|shipping|freight|warranty|setup|training|"
    r"maintenance included|assembly)\b",
    re.I,
)

SIGNIFICANT_INSTALL = re.compile(
    r"(?i)\b("
    r"including installation|and installation|with installation|"
    r"install(?:ation)? (?:of|and)|"
    r"furnish and install|supply and install|turnkey installation"
    r")\b"
)

RENTAL_LEASE = re.compile(
    r"(?i)\b(rental|lease|leasing|rent(?:al)? of)\b"
)

SOFTWARE_ONLY = re.compile(
    r"(?i)\b("
    r"software license|saas|subscription software|cloud subscription|"
    r"microsoft 365|office 365|software as a service|erp software|"
    r"software only|annual software|software maintenance agreement"
    r")\b"
)

SERVICE_ONLY = re.compile(
    r"(?i)\b("
    r"tree trimming|tree removal|landscap(?:e|ing) services?|lawn (?:care|maintenance)|"
    r"janitorial services?|custodial services?|collection services?|"
    r"professional services?|consulting|staffing|"
    r"legal services?|audit services?|training services?|"
    r"repair services?|maintenance services?|investigation services?|"
    r"equipment maintenance\b|facility maintenance\b|"
    r"therapy|refuse.{0,20}collection|recyclable materials collection|"
    r"personal services?|design services?|engineering services?"
    r")\b"
)

MAINTENANCE_REPAIR = re.compile(
    r"(?i)\b("
    r"maintenance and repair|repair and maintenance|"
    r"preventive maintenance|corrective maintenance|"
    r"equipment maintenance|vehicle maintenance|hvac maintenance|"
    r"elevator maintenance|generator maintenance"
    r")\b"
)

CONSTRUCTION_ONLY = re.compile(
    r"(?i)\b("
    r"construction only|general contractor|design[- ]build|"
    r"paving|demolition|excavation|roofing systems?|"
    r"renovate existing structure|building renovation|"
    r"civil engineering construction|roadway construction|"
    r"new construction|tenant improvement construction|"
    r"construction of|construction services?"
    r")\b"
)

CATEGORY_SERVICE = re.compile(r"personal services|(?<!product )services", re.I)
CATEGORY_CONSTRUCTION = re.compile(r"construction", re.I)
CATEGORY_COMMODITY = re.compile(r"commodity|goods|supplies|equipment|materials", re.I)

# Purchasing-context signals (not a closed product whitelist)
PURCHASE_OF_GOODS = re.compile(
    r"(?i)\b("
    r"purchase of|procurement of|acquisition of|furnish(?:ing)? of|"
    r"supply of|provide(?:ing)? .+ (?:equipment|materials|supplies|goods|units)|"
    r"qty\.?|quantity|unit price|each\b|lot of|cases? of|boxes of|pallets? of|"
    r"n\.?i\.?g\.?p\.?|unspsc|commodity code|line item"
    r")\b"
)
LABOR_PRIMARY = re.compile(
    r"(?i)\b("
    r"labor only|hourly (?:rate|services)|staff augmentation|"
    r"contractor shall perform|scope of services|statement of work for services"
    r")\b"
)

# Backward-compatible aliases used by filters / UI
PRODUCT_CLASSES = {
    "product_supply",
    "product_with_incidental_services",
    "product_with_significant_installation",
    "product_rental_leasing",
}
NON_PRODUCT_CLASSES = {
    "services",
    "professional_services",
    "maintenance_repair_services",
    "construction",
    "software_only",
}


def classify_requirement(
    title: str,
    *,
    category_raw: str | None = None,
    procurement_type: str | None = None,
    description: str | None = None,
    document_text: str | None = None,
    line_items_text: str | None = None,
) -> dict[str, Any]:
    """Classify purchasing requirement. Never invent commercial facts.

    Classification is independent of owner preferences / filters.
    """
    parts = [
        title or "",
        category_raw or "",
        procurement_type or "",
        description or "",
        line_items_text or "",
        (document_text or "")[:4000],
    ]
    text = " ".join(parts)
    evidence: list[str] = []
    sources_used = ["title"]
    if category_raw:
        sources_used.append("category")
    if description:
        sources_used.append("description")
    if line_items_text:
        sources_used.append("line_items")
    if document_text:
        sources_used.append("document_text")
    cat = (category_raw or "").strip()
    title_text = title or ""

    # Strong phrase disambiguation first
    if SUPPLY_PHRASE.search(title_text) or SUPPLY_PHRASE.search(text):
        m = SUPPLY_PHRASE.search(title_text) or SUPPLY_PHRASE.search(text)
        evidence.append(f"supply_phrase={(m.group(0) if m else '')[:80]}")
        if SIGNIFICANT_INSTALL.search(text):
            evidence.append("significant_installation_language")
            return _out("product_with_significant_installation", evidence, True, sources_used)
        if INCIDENTAL_SERVICE.search(text):
            evidence.append("incidental_service_language")
            return _out("product_with_incidental_services", evidence, True, sources_used)
        return _out("product_supply", evidence, True, sources_used)

    if LABOR_PRIMARY.search(text) and not PRODUCT_HINTS.search(title_text) and not PURCHASE_OF_GOODS.search(text):
        evidence.append("labor_primary_purchasing_context")
        return _out("services", evidence, False, sources_used)

    if PURCHASE_OF_GOODS.search(text) and not SERVICE_ONLY.search(title_text) and not CONSTRUCTION_ONLY.search(title_text):
        # Purchasing context suggests goods even when product vocabulary is unfamiliar
        if not MAINTENANCE_REPAIR.search(title_text):
            evidence.append("purchase_of_goods_context")
            if SIGNIFICANT_INSTALL.search(text):
                return _out("product_with_significant_installation", evidence, True, sources_used)
            if INCIDENTAL_SERVICE.search(text) or SERVICE_ONLY.search(text):
                return _out("product_with_incidental_services", evidence, True, sources_used)
            return _out("product_supply", evidence, True, sources_used)

    if SOFTWARE_ONLY.search(text) and not PRODUCT_HINTS.search(title_text):
        evidence.append("software_subscription_language")
        return _out("software_only", evidence, False, sources_used)

    if RENTAL_LEASE.search(title_text) and PRODUCT_HINTS.search(text):
        evidence.append("rental_or_lease_of_goods")
        return _out("product_rental_leasing", evidence, True, sources_used)

    # "Equipment Maintenance" is typically a service; "Maintenance Supplies" is a product (SUPPLY_PHRASE).
    if MAINTENANCE_REPAIR.search(title_text) and not SUPPLY_PHRASE.search(title_text):
        evidence.append("maintenance_repair_services_language")
        return _out("maintenance_repair_services", evidence, False, sources_used)

    if cat and CATEGORY_CONSTRUCTION.search(cat) and not CATEGORY_COMMODITY.search(cat):
        if not PRODUCT_HINTS.search(title_text) and not SUPPLY_PHRASE.search(title_text):
            evidence.append(f"source_category={cat}")
            return _out("construction", evidence, False, sources_used)

    has_product_title = bool(PRODUCT_HINTS.search(title_text) or SUPPLY_PHRASE.search(title_text))
    has_service_title = bool(SERVICE_ONLY.search(title_text))
    has_construction_title = bool(CONSTRUCTION_ONLY.search(title_text))

    if has_service_title and not has_product_title:
        m = SERVICE_ONLY.search(title_text)
        evidence.append(f"service_pattern_title={(m.group(0) if m else '')[:80]}")
        if "professional" in (m.group(0) if m else "").lower() or "consulting" in title_text.lower():
            return _out("professional_services", evidence, False, sources_used)
        return _out("services", evidence, False, sources_used)

    if has_construction_title and not has_product_title:
        evidence.append("construction_pattern_title")
        return _out("construction", evidence, False, sources_used)

    if cat and CATEGORY_SERVICE.search(cat) and not CATEGORY_COMMODITY.search(cat) and not has_product_title:
        evidence.append(f"source_category={cat}")
        return _out("services", evidence, False, sources_used)

    has_product = has_product_title or bool(PRODUCT_HINTS.search(f"{category_raw or ''} {procurement_type or ''} {description or ''}"))
    has_service_only = bool(SERVICE_ONLY.search(text))
    has_construction = bool(CONSTRUCTION_ONLY.search(text))
    has_incidental = bool(INCIDENTAL_SERVICE.search(text))
    has_sig_install = bool(SIGNIFICANT_INSTALL.search(text))

    if has_construction and not has_product_title:
        evidence.append("construction_pattern")
        return _out("construction", evidence, False, sources_used)
    if has_service_only and not has_product_title:
        m = SERVICE_ONLY.search(text)
        evidence.append(f"service_pattern={(m.group(0) if m else '')[:80]}")
        return _out("services", evidence, False, sources_used)

    if has_product and has_service_only and has_construction:
        evidence.append("mixed_product_service_construction_signals")
        return _out("mixed_product_service", evidence, True, sources_used)

    if has_product and has_sig_install:
        evidence.append("product_with_significant_installation")
        return _out("product_with_significant_installation", evidence, True, sources_used)

    if has_product and (has_incidental or has_service_only):
        evidence.append("product_with_incidental_services")
        return _out("product_with_incidental_services", evidence, True, sources_used)

    if has_product or (cat and CATEGORY_COMMODITY.search(cat) and not has_service_only and not has_construction):
        if cat and CATEGORY_COMMODITY.search(cat) and not has_product_title:
            evidence.append(f"source_category={cat}")
        else:
            evidence.append("product_supply_terms")
        return _out("product_supply", evidence, True, sources_used)

    if has_service_only:
        evidence.append("service_without_clear_product")
        return _out("services", evidence, False, sources_used)
    if has_construction:
        evidence.append("construction_without_clear_product")
        return _out("construction", evidence, False, sources_used)

    evidence.append("insufficient_evidence")
    return _out("unknown_mixed", evidence, True, sources_used)


def _out(cls: str, evidence: list[str], include: bool, sources_used: list[str]) -> dict[str, Any]:
    return {
        "requirement_class": cls,
        "classification_evidence": evidence,
        "include_in_product_resale": include and cls in PRODUCT_CLASSES | {"unknown_mixed", "mixed_product_service"},
        "evidence_sources": sources_used,
        "classification_stage": "central",
    }


def classify_solicitation_item(item: dict[str, Any]) -> dict[str, Any]:
    """Apply central classification onto a normalized discovery item (mutates copy)."""
    out = dict(item)
    # Build 002G: do not spend classification on portals/forecasts/awards/unrelated
    rt = str(out.get("record_type") or "")
    if rt in {
        "PROCUREMENT_PORTAL",
        "HISTORICAL_AWARD",
        "PROCUREMENT_FORECAST",
        "PURCHASING_PLAN",
        "VENDOR_REGISTRATION",
        "GENERAL_PROCUREMENT_INFORMATION",
        "UNRELATED_CONTENT",
    }:
        out["requirement_class"] = "not_applicable_non_solicitation"
        out["classification_evidence"] = [f"skipped_identity:{rt}"]
        out["include_in_product_resale"] = False
        out["evidence_sources"] = ["identity"]
        out["classification_stage"] = "identity_skip"
        if not out.get("solicitation_status"):
            out["solicitation_status"] = _infer_status(out)
        return out
    desc = out.get("description")
    if not desc and isinstance(out.get("evidence"), dict):
        desc = out["evidence"].get("category_raw")
    clf = classify_requirement(
        str(out.get("title") or ""),
        category_raw=(
            (out.get("evidence") or {}).get("category_raw")
            if isinstance(out.get("evidence"), dict)
            else out.get("product_category")
        ),
        procurement_type=out.get("procurement_type"),
        description=str(desc) if desc else None,
        document_text=out.get("document_text"),
        line_items_text=out.get("line_items_text"),
    )
    out["requirement_class"] = clf["requirement_class"]
    out["classification_evidence"] = clf["classification_evidence"]
    out["include_in_product_resale"] = clf["include_in_product_resale"]
    out["evidence_sources"] = clf.get("evidence_sources")
    out["classification_stage"] = "central"
    # Preserve solicitation_status if already set
    if not out.get("solicitation_status"):
        out["solicitation_status"] = _infer_status(out)
    return out


def _infer_status(item: dict[str, Any]) -> str:
    if item.get("is_forecast") or (item.get("procurement_type") or "").lower() == "forecast":
        return "forecast"
    st = ""
    if isinstance(item.get("evidence"), dict):
        st = str(item["evidence"].get("status") or "")
    if re.search(r"(?i)\b(cancel)", st):
        return "cancelled"
    if re.search(r"(?i)\b(award)", st):
        return "historical_award"
    if re.search(r"(?i)\b(closed|expired|inactive)", st):
        return "closed"
    if re.search(r"(?i)\b(amend)", st):
        return "amended"
    if re.search(r"(?i)\b(open|active|posted|advertised)", st):
        return "open"
    # Past deadline heuristic — does not invent; marks likely closed
    dl = item.get("deadline_at")
    if dl:
        try:
            raw = str(dl).replace("Z", "+00:00")
            from datetime import datetime, timezone

            dt = datetime.fromisoformat(raw[:19])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt.year >= 2000 and dt < datetime.now(timezone.utc):
                return "closed_by_deadline"
        except Exception:
            pass
    if not st and not dl:
        return "unknown"
    return "unknown"


def classify_batch(items: list[dict[str, Any]]) -> dict[str, Any]:
    classified = [classify_solicitation_item(i) for i in items]
    breakdown: dict[str, int] = {}
    for i in classified:
        k = i.get("requirement_class") or "unknown_mixed"
        breakdown[k] = breakdown.get(k, 0) + 1
    status_breakdown: dict[str, int] = {}
    for i in classified:
        k = i.get("solicitation_status") or "unknown"
        status_breakdown[k] = status_breakdown.get(k, 0) + 1
    return {
        "items": classified,
        "classification_breakdown": breakdown,
        "status_breakdown": status_breakdown,
    }
