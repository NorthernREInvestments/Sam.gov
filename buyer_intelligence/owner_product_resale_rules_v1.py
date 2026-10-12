"""Locked M3 owner business rules — durable tangible product resale.

Hard / strong exclusions: food, beverages, milk, perishables, spoilage-sensitive
products, labor-heavy / construction-heavy / installation-heavy / specialty field
execution scopes.

Use for early qualification AND final outreach safety gate.
Do not silently change these rules.
"""

from __future__ import annotations

import re
from typing import Any

BUILD = "20261008-m3-first-real-outreach-safety-send-v1"

# Rejection reason codes for strategic memory / Stage 0
FOOD_PERISHABLE = "FOOD_PERISHABLE"
SOD_PERISHABLE = "SOD_PERISHABLE"
CONSTRUCTION_PAY_ITEM = "CONSTRUCTION_PAY_ITEM"
HFST_APPLICATION = "HFST_APPLICATION"
SERVICE_INCLUDED = "SERVICE_INCLUDED"
INSTALLATION_HEAVY = "INSTALLATION_HEAVY"
SPECIALTY_EXECUTION = "SPECIALTY_EXECUTION"

_MILK_FOOD = re.compile(
    r"\b(milk|beverage|beverages|food\s+service|catering|perishable|"
    r"half[\-\s]?pint|chocolate\s+milk|strawberry\s+milk|dairy\s+pure|"
    r"school\s+lunch\s+milk|truumoo)\b",
    re.I,
)
_SOD = re.compile(
    r"\b(sod\b|turf\s*\(|performance\s+turf|bahia|st\.?\s*augustine\s+sod|"
    r"floratam|zoysia\s+sod)\b",
    re.I,
)
_ASPHALT_CONSTRUCTION = re.compile(
    r"\b(milling,?\s+asphalt|asphalt\s+milling|dense\s+graded\s+subbase|"
    r"quality\s+adjustments,?\s+failed\s+materials|paving|concrete\s+pour|"
    r"construction\s+pay\s+item|installed\s+asphalt|as[\-\s]?placed)\b",
    re.I,
)
_HFST = re.compile(
    r"\b(high\s+friction\s+surface\s+treatment|hfst)\b",
    re.I,
)
_SERVICE_LABOR = re.compile(
    r"\b(labor[\-\s]?only|installation\s+only|furnish\s+and\s+install|"
    r"supply\s+and\s+install|contractor\s+services|construction\s+services)\b",
    re.I,
)


def text_blob(*parts: Any) -> str:
    return " ".join(str(p or "") for p in parts)


def classify_owner_resale_text(text: str) -> dict[str, Any]:
    """Context-aware early filter. Does not reject material-only solely on a token."""
    t = text or ""
    if _MILK_FOOD.search(t):
        return {
            "PRODUCT_RESALE_FIT": "FAIL_FOOD_PERISHABLE",
            "PROHIBITED_CATEGORY": "FOOD_BEVERAGE_PERISHABLE",
            "REJECTION_CODE": FOOD_PERISHABLE,
            "PERISHABLE_PRODUCT": "YES",
            "SERVICE_INSTALL": "NO",
            "SCOPE_CLASS": "FOOD_PERISHABLE",
            "PASS": False,
        }
    if _SOD.search(t):
        return {
            "PRODUCT_RESALE_FIT": "FAIL_FOOD_PERISHABLE",
            "PROHIBITED_CATEGORY": "PERISHABLE_AGRICULTURAL_LANDSCAPE",
            "REJECTION_CODE": SOD_PERISHABLE,
            "PERISHABLE_PRODUCT": "YES",
            "SERVICE_INSTALL": "UNKNOWN",
            "SCOPE_CLASS": "SOD_PERISHABLE",
            "PASS": False,
            "NOTE": "Sod is spoilage/logistics-sensitive living material — outside durable resale model",
        }
    if _HFST.search(t):
        return {
            "PRODUCT_RESALE_FIT": "FAIL_SPECIALTY_EXECUTION",
            "PROHIBITED_CATEGORY": "SPECIALTY_PAVEMENT_SYSTEM",
            "REJECTION_CODE": HFST_APPLICATION,
            "PERISHABLE_PRODUCT": "NO",
            "SERVICE_INSTALL": "YES",
            "SCOPE_CLASS": "HFST_APPLICATION",
            "PASS": False,
            "NOTE": "HFST is specialty surface treatment application/install scope, not catalog commodity supply",
        }
    if _ASPHALT_CONSTRUCTION.search(t):
        # Installed/as-placed construction pay items — not product-only unless proven
        return {
            "PRODUCT_RESALE_FIT": "FAIL_CONSTRUCTION_HEAVY",
            "PROHIBITED_CATEGORY": "CONSTRUCTION_PAY_ITEM",
            "REJECTION_CODE": CONSTRUCTION_PAY_ITEM,
            "PERISHABLE_PRODUCT": "NO",
            "SERVICE_INSTALL": "YES",
            "SCOPE_CLASS": "CONSTRUCTION_SCOPE",
            "MATERIAL_SCOPE": "CONSTRUCTION_SCOPE",
            "PASS": False,
            "NOTE": "Pay items imply as-placed construction work; product-only supply not proven",
        }
    if _SERVICE_LABOR.search(t) and not re.search(
        r"\b(parts?|valve|glue|crayon|electrical|conduit|cable|filter|fan|lamp)\b", t, re.I
    ):
        return {
            "PRODUCT_RESALE_FIT": "FAIL_SERVICE_HEAVY",
            "PROHIBITED_CATEGORY": "SERVICE_LABOR",
            "REJECTION_CODE": SERVICE_INCLUDED,
            "PERISHABLE_PRODUCT": "NO",
            "SERVICE_INSTALL": "YES",
            "SCOPE_CLASS": "SERVICE_INCLUDED",
            "PASS": False,
        }
    return {
        "PRODUCT_RESALE_FIT": "PASS",
        "PROHIBITED_CATEGORY": None,
        "REJECTION_CODE": None,
        "PERISHABLE_PRODUCT": "NO",
        "SERVICE_INSTALL": "NO",
        "SCOPE_CLASS": "DURABLE_PRODUCT",
        "PASS": True,
    }


def classify_packet_resale(packet: dict[str, Any]) -> dict[str, Any]:
    """Final gate classification for a quote packet."""
    lines = packet.get("LINES") or []
    blob = text_blob(
        packet.get("PRODUCT_FAMILY"),
        packet.get("CATEGORY"),
        packet.get("BUYER"),
        *[ln.get("RAW_DESCRIPTION") for ln in lines],
        *[ln.get("SPEC_REQUIREMENTS") for ln in lines],
    )
    pid = packet.get("PACKET_ID")

    # Explicit packet overrides (truthful, evidence-based)
    if pid == "QP-QWOV1-07":
        return {
            **classify_owner_resale_text("school milk half-pint chocolate strawberry"),
            "PACKET_ID": pid,
            "PRODUCT_FAMILY": "School milk (half-pint flavored/white)",
            "REASON": "FOOD/BEVERAGE perishable — violates locked owner durable-product resale model",
        }
    if pid == "QP-QWOV1-04":
        return {
            **classify_owner_resale_text("PERFORMANCE TURF SOD BAHIA ST AUGUSTINE"),
            "PACKET_ID": pid,
            "PRODUCT_FAMILY": "Commercial landscape sod (Florida municipal)",
            "REASON": "Perishable agricultural/landscape material (sod) — spoilage/logistics-sensitive",
        }
    if pid == "QP-QWOV1-02":
        return {
            "PACKET_ID": pid,
            "PRODUCT_FAMILY": "Highway construction pay items (asphalt milling, subbase)",
            "PRODUCT_RESALE_FIT": "FAIL_CONSTRUCTION_HEAVY",
            "PROHIBITED_CATEGORY": "CONSTRUCTION_PAY_ITEM",
            "REJECTION_CODE": CONSTRUCTION_PAY_ITEM,
            "PERISHABLE_PRODUCT": "NO",
            "SERVICE_INSTALL": "YES",
            "SCOPE_CLASS": "CONSTRUCTION_SCOPE",
            "MATERIAL_SCOPE": "CONSTRUCTION_SCOPE",
            "PASS": False,
            "REASON": "INDOT-style pay items (SYS/CYS milling/subbase) imply construction execution; product-only not proven",
        }
    if pid == "QP-QWOV1-10":
        return {
            "PACKET_ID": pid,
            "PRODUCT_FAMILY": "High Friction Surface Treatment (HFST)",
            "PRODUCT_RESALE_FIT": "FAIL_SPECIALTY_EXECUTION",
            "PROHIBITED_CATEGORY": "SPECIALTY_PAVEMENT_SYSTEM",
            "REJECTION_CODE": HFST_APPLICATION,
            "PERISHABLE_PRODUCT": "NO",
            "SERVICE_INSTALL": "YES",
            "SCOPE_CLASS": "HFST_APPLICATION",
            "PASS": False,
            "REASON": "HFST is specialty pavement treatment system/application — not separable catalog resale",
        }
    if pid == "QP-QWOV1-03":
        base = classify_owner_resale_text("table cover disposable foodservice tray dessert dish")
        # Nonperishable disposables = durable shippable goods OK for model
        return {
            "PACKET_ID": pid,
            "PRODUCT_FAMILY": "Foodservice disposables — table covers, cold trays, dessert dishes",
            "PRODUCT_RESALE_FIT": "PASS",
            "PROHIBITED_CATEGORY": None,
            "REJECTION_CODE": None,
            "PERISHABLE_PRODUCT": "NO",
            "SERVICE_INSTALL": "NO",
            "SCOPE_CLASS": "DURABLE_PRODUCT",
            "PASS": True,
            "REASON": "Nonperishable disposable foodservice supplies — product resale OK; supplier exact fit evaluated separately",
            "EARLY_TEXT": base,
        }
    if pid == "QP-QWOV1-05":
        return {
            "PACKET_ID": pid,
            "PRODUCT_FAMILY": "Municipal gate/air-release valves with boxes (MRO)",
            "PRODUCT_RESALE_FIT": "PASS_WITH_CAUTION",
            "PROHIBITED_CATEGORY": None,
            "REJECTION_CODE": None,
            "PERISHABLE_PRODUCT": "NO",
            "SERVICE_INSTALL": "NO",
            "SCOPE_CLASS": "DURABLE_PRODUCT",
            "PASS": False,  # caution alone does not auto-approve without owner override
            "OWNER_OVERRIDE_REQUIRED": True,
            "REASON": "Durable MRO valves OK, but city Std Det / valve-box assemblies need caution; no owner override present",
        }
    if pid in {"QP-QWOV1-01", "QP-QWOV1-06", "QP-QWOV1-08", "QP-QWOV1-09"}:
        families = {
            "QP-QWOV1-01": "Transit bus OEM / fleet parts",
            "QP-QWOV1-06": "Electrical conduit/cable/PPE supplies",
            "QP-QWOV1-08": "Brand-specific school adhesive (Elmer's)",
            "QP-QWOV1-09": "School crayons / art supplies",
        }
        return {
            "PACKET_ID": pid,
            "PRODUCT_FAMILY": families[pid],
            "PRODUCT_RESALE_FIT": "PASS",
            "PROHIBITED_CATEGORY": None,
            "REJECTION_CODE": None,
            "PERISHABLE_PRODUCT": "NO",
            "SERVICE_INSTALL": "NO",
            "SCOPE_CLASS": "DURABLE_PRODUCT",
            "PASS": True,
            "REASON": "Durable shippable commercial goods matching owner preferred model",
        }

    # Generic fallback from text
    result = classify_owner_resale_text(blob)
    result["PACKET_ID"] = pid
    result["PRODUCT_FAMILY"] = packet.get("PRODUCT_FAMILY") or "UNKNOWN"
    result["REASON"] = result.get("NOTE") or result.get("REJECTION_CODE") or "generic_text_classify"
    return result


def early_stage0_owner_reject(blob: str) -> dict[str, Any] | None:
    """Hook for progressive funnel Stage 0 — returns reject dict or None."""
    c = classify_owner_resale_text(blob)
    if not c.get("PASS"):
        return {
            "pass": False,
            "reason": f"owner_rule:{c.get('REJECTION_CODE')}",
            "owner_rule": c,
        }
    return None


STRATEGIC_REJECTION_MEMORY = [
    {
        "CODE": FOOD_PERISHABLE,
        "PATTERNS": ["milk", "beverage", "half-pint", "school milk", "dairy", "catering", "perishable food"],
        "ACTION": "HARD_REJECT_EARLY",
        "STAGE": "STAGE_0_OR_QUOTE_GATE",
    },
    {
        "CODE": SOD_PERISHABLE,
        "PATTERNS": ["sod", "performance turf", "bahia sod", "st augustine sod"],
        "ACTION": "HARD_REJECT_EARLY",
        "STAGE": "STAGE_0_OR_QUOTE_GATE",
    },
    {
        "CODE": CONSTRUCTION_PAY_ITEM,
        "PATTERNS": ["milling asphalt", "dense graded subbase", "asphalt paving pay item"],
        "ACTION": "HARD_REJECT_UNLESS_PRODUCT_ONLY_PROVEN",
        "STAGE": "STAGE_0_OR_QUOTE_GATE",
    },
    {
        "CODE": HFST_APPLICATION,
        "PATTERNS": ["high friction surface treatment", "HFST"],
        "ACTION": "HARD_REJECT_EARLY",
        "STAGE": "STAGE_0_OR_QUOTE_GATE",
    },
    {
        "CODE": SERVICE_INCLUDED,
        "PATTERNS": ["furnish and install", "labor only", "construction services"],
        "ACTION": "HARD_REJECT_OR_SPLIT",
        "STAGE": "STAGE_1_OR_QUOTE_GATE",
    },
]
