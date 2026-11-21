"""Traceable product-resale vs services/construction classification."""

from __future__ import annotations

import re
from typing import Any

PRODUCT_HINTS = re.compile(
    r"(?i)\b("
    r"furniture|desk|chair|janitorial|custodial|cleaning supplies?|"
    r"tools?|mro|electrical|plumbing|hvac|appliances?|refrigerators?|washer|dryer|"
    r"laptops?|computers?|printers?|toner|ppe|safety equipment|lighting|generators?|"
    r"forklifts?|equipment|supplies?|hardware|batteries|battery|shelving|"
    r"parts|lubricants?|oils?|filters?|uniforms?|gloves?|paper|commodity|goods|materials|"
    r"fixtures?|scanners?|multiplexers?|pumps?|valves?|transformers?|cables?|wires?|vehicles?"
    r")\b"
)

INCIDENTAL_SERVICE = re.compile(
    r"\b(delivery|shipping|freight|install(?:ation)?|warranty|setup|training|"
    r"maintenance included|assembly)\b",
    re.I,
)

SERVICE_ONLY = re.compile(
    r"\b("
    r"tree trimming|tree removal|landscap(?:e|ing) services?|lawn (?:care|maintenance)|"
    r"janitorial services?|custodial services?|collection services?|"
    r"professional services?|consulting|staffing|software as a service|"
    r"legal services?|audit services?|training services?|"
    r"repair services?|maintenance services?|investigation services?|"
    r"therapy|refuse.{0,20}collection|recyclable materials collection|"
    r"personal services?|design services?|engineering services?"
    r")\b",
    re.I,
)

CONSTRUCTION_ONLY = re.compile(
    r"\b("
    r"construction only|general contractor|design[- ]build|"
    r"paving|demolition|excavation|roofing systems?|"
    r"renovate existing structure|building renovation|"
    r"civil engineering construction|roadway construction|"
    r"new construction|tenant improvement construction"
    r")\b",
    re.I,
)

CATEGORY_SERVICE = re.compile(r"personal services|services", re.I)
CATEGORY_CONSTRUCTION = re.compile(r"construction", re.I)
CATEGORY_COMMODITY = re.compile(r"commodity|goods|supplies|equipment", re.I)


def classify_requirement(
    title: str,
    *,
    category_raw: str | None = None,
    procurement_type: str | None = None,
) -> dict[str, Any]:
    """Return requirement_class + evidence. Never invent commercial facts."""
    text = f"{title or ''} {category_raw or ''} {procurement_type or ''}"
    evidence: list[str] = []
    cat = (category_raw or "").strip()

    if cat and CATEGORY_CONSTRUCTION.search(cat) and not CATEGORY_COMMODITY.search(cat):
        evidence.append(f"source_category={cat}")
        return {
            "requirement_class": "construction",
            "classification_evidence": evidence,
            "include_in_product_resale": False,
        }
    title_text = title or ""
    # Title evidence wins over misleading source categories (e.g. tree trimming tagged Commodity).
    has_product_title = bool(PRODUCT_HINTS.search(title_text))
    has_service_title = bool(SERVICE_ONLY.search(title_text))
    has_construction_title = bool(CONSTRUCTION_ONLY.search(title_text))

    if has_service_title and not has_product_title:
        m = SERVICE_ONLY.search(title_text)
        evidence.append(f"matched service-only pattern in title: {(m.group(0) if m else '')[:80]}")
        if cat:
            evidence.append(f"source_category={cat} (overridden by title)")
        return {
            "requirement_class": "services",
            "classification_evidence": evidence,
            "include_in_product_resale": False,
        }
    if has_construction_title and not has_product_title:
        evidence.append("matched construction-only pattern in title")
        if cat:
            evidence.append(f"source_category={cat} (overridden by title)")
        return {
            "requirement_class": "construction",
            "classification_evidence": evidence,
            "include_in_product_resale": False,
        }

    if cat and CATEGORY_SERVICE.search(cat) and not CATEGORY_COMMODITY.search(cat) and not has_product_title:
        evidence.append(f"source_category={cat}")
        return {
            "requirement_class": "services",
            "classification_evidence": evidence,
            "include_in_product_resale": False,
        }

    has_product = has_product_title or bool(PRODUCT_HINTS.search(f"{category_raw or ''} {procurement_type or ''}"))
    has_service_only = bool(SERVICE_ONLY.search(text))
    has_construction = bool(CONSTRUCTION_ONLY.search(text))
    has_incidental = bool(INCIDENTAL_SERVICE.search(text))

    if has_construction and not has_product_title:
        evidence.append("matched construction-only pattern")
        return {
            "requirement_class": "construction",
            "classification_evidence": evidence,
            "include_in_product_resale": False,
        }
    if has_service_only and not has_product_title:
        m = SERVICE_ONLY.search(text)
        evidence.append(f"matched service-only pattern: {(m.group(0) if m else '')[:80]}")
        return {
            "requirement_class": "services",
            "classification_evidence": evidence,
            "include_in_product_resale": False,
        }
    if has_product_title and (has_incidental or has_service_only):
        evidence.append("product terms present with incidental service/delivery language")
        return {
            "requirement_class": "product_with_incidental_services",
            "classification_evidence": evidence,
            "include_in_product_resale": True,
        }
    if has_product_title or (cat and CATEGORY_COMMODITY.search(cat) and not has_service_only and not has_construction):
        if cat and CATEGORY_COMMODITY.search(cat) and not has_product_title:
            evidence.append(f"source_category={cat}")
        else:
            evidence.append("matched product-supply terms")
        return {
            "requirement_class": "product_supply",
            "classification_evidence": evidence,
            "include_in_product_resale": True,
        }
    if has_service_only:
        evidence.append("service language without clear product terms")
        return {
            "requirement_class": "services",
            "classification_evidence": evidence,
            "include_in_product_resale": False,
        }
    if has_construction:
        evidence.append("construction language without clear product terms")
        return {
            "requirement_class": "construction",
            "classification_evidence": evidence,
            "include_in_product_resale": False,
        }
    evidence.append("insufficient evidence to classify; treated as unknown/mixed")
    return {
        "requirement_class": "unknown_mixed",
        "classification_evidence": evidence,
        "include_in_product_resale": True,  # keep for operator review; not discarded
    }
