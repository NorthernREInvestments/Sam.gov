"""Product-screen false-positive audit — measure before tightening."""

from __future__ import annotations

import re
from typing import Any

from product_category_yield import classify_product_category

AUDIT_LABELS = (
    "TRUE_PRODUCT_RESALE_CANDIDATE",
    "MIXED_BUT_POTENTIALLY_RESALE",
    "SERVICE_FALSE_POSITIVE",
    "CONSTRUCTION_PRIMARILY_SERVICE",
    "UNKNOWN",
)

_CONSTRUCTION = re.compile(
    r"\b(construction|renovation|remodel|paving|resurfacing|bridge\s+repair|"
    r"roadway|excavation|demolition|surfacing|shoreline|repaving|milling)\b",
    re.I,
)
_SERVICE = re.compile(
    r"\b((?:equipment|vehicle|hvac|janitorial|grounds?)\s+(?:maintenance|repair|service)s?|"
    r"installation\s+services?|professional\s+services?|consulting|staffing|"
    r"custodial\s+services?|transportation\s+services?|shuttle\s+service)\b",
    re.I,
)
_PRODUCT = re.compile(
    r"\b(purchase|supply|furnish|equipment|supplies|materials|hardware|parts?|"
    r"computers?|vehicles?|furniture|tools?|hose|ppe|commodit)\b",
    re.I,
)
_SYSTEMATIC_FP_RULES = [
    {
        "id": "equipment_maintenance_service",
        "pattern": re.compile(r"\bequipment\s+maintenance\b", re.I),
        "note": "Product keyword + maintenance service",
    },
    {
        "id": "vehicle_repair_service",
        "pattern": re.compile(r"\bvehicle\s+repair\b", re.I),
        "note": "Vehicle keyword used for repair services",
    },
    {
        "id": "hvac_service",
        "pattern": re.compile(r"\bhvac\s+(?:service|maintenance|repair)s?\b", re.I),
        "note": "HVAC service mistaken for HVAC equipment",
    },
    {
        "id": "janitorial_services",
        "pattern": re.compile(r"\bjanitorial\s+services?\b", re.I),
        "note": "Janitorial services vs supplies",
    },
    {
        "id": "installation_services",
        "pattern": re.compile(r"\binstallation\s+services?\b", re.I),
        "note": "Installation services without supply language",
    },
]


def audit_survivor(record: dict[str, Any]) -> dict[str, Any]:
    title = str(record.get("title") or "")
    desc = str(record.get("description") or "")
    blob = f"{title} {desc}"
    cat = classify_product_category(title, desc)

    if cat["category"] == "LIKELY_SERVICE_FALSE_POSITIVE" or (_SERVICE.search(blob) and not _PRODUCT.search(title)):
        label = "SERVICE_FALSE_POSITIVE"
    elif _CONSTRUCTION.search(blob) and not re.search(r"\b(supply|furnish|purchase\s+of|equipment\s+only)\b", blob, re.I):
        label = "CONSTRUCTION_PRIMARILY_SERVICE"
    elif cat["category"] == "MIXED_GOODS_SERVICES" or (
        _SERVICE.search(blob) and _PRODUCT.search(blob)
    ):
        label = "MIXED_BUT_POTENTIALLY_RESALE"
    elif cat["category"] not in {"UNKNOWN", "LIKELY_SERVICE_FALSE_POSITIVE"}:
        label = "TRUE_PRODUCT_RESALE_CANDIDATE"
    elif _PRODUCT.search(blob):
        label = "TRUE_PRODUCT_RESALE_CANDIDATE"
    else:
        label = "UNKNOWN"

    systematic = [r["id"] for r in _SYSTEMATIC_FP_RULES if r["pattern"].search(blob)]
    return {
        "title": title[:160],
        "product_category": cat["category"],
        "audit_label": label,
        "systematic_fp_rules_hit": systematic,
        "external_id": record.get("external_id") or record.get("canonical_id"),
        "source_id": record.get("source_id"),
    }


def run_false_positive_audit(survivors: list[dict[str, Any]], *, sample_size: int = 40) -> dict[str, Any]:
    sample = survivors[: max(1, sample_size)] if survivors else []
    audited = [audit_survivor(s) for s in sample]
    counts = {lab: 0 for lab in AUDIT_LABELS}
    for a in audited:
        counts[a["audit_label"]] = counts.get(a["audit_label"], 0) + 1
    n = len(audited) or 1
    systematic_hits = sum(1 for a in audited if a["systematic_fp_rules_hit"])

    # Only recommend correction if systematic rule dominates sample
    recommend_correction = systematic_hits >= max(3, int(0.25 * len(audited))) if audited else False

    return {
        "kind": "ProductFalsePositiveAudit",
        "sample_size": len(audited),
        "counts": counts,
        "pct_true_product": round(100.0 * counts.get("TRUE_PRODUCT_RESALE_CANDIDATE", 0) / n, 2),
        "pct_service_false_positive": round(100.0 * counts.get("SERVICE_FALSE_POSITIVE", 0) / n, 2),
        "pct_mixed": round(100.0 * counts.get("MIXED_BUT_POTENTIALLY_RESALE", 0) / n, 2),
        "pct_construction_service": round(100.0 * counts.get("CONSTRUCTION_PRIMARILY_SERVICE", 0) / n, 2),
        "systematic_fp_hits": systematic_hits,
        "recommend_screen_correction": recommend_correction,
        "correction_applied": False,  # measure-first; applied only if caller opts in with proven rule
        "audited": audited,
    }


def apply_conservative_fp_correction_if_proven(audit: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Return deterministic reject rules only when audit proves systematic FPs.
    Does NOT mutate the live screen automatically — caller decides.
    """
    if not audit.get("recommend_screen_correction"):
        return []
    return [
        {
            "rule_id": r["id"],
            "note": r["note"],
            "action": "REJECT_IF_TITLE_MATCHES_AND_NO_SUPPLY_LANGUAGE",
        }
        for r in _SYSTEMATIC_FP_RULES
    ]
