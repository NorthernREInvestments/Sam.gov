"""Strict evidence-based product-line validation.

Rejects insurance clauses, contract boilerplate, section headings, and
garbage quantities that previously inflated \"fully specified\" counts.
"""

from __future__ import annotations

import re
from typing import Any

EXACT_PRODUCT_IDENTIFIED = "EXACT_PRODUCT_IDENTIFIED"
COMMERCIALLY_SOURCEABLE_GENERIC = "COMMERCIALLY_SOURCEABLE_GENERIC"
SUPPLIER_CLARIFICATION_REQUIRED = "SUPPLIER_CLARIFICATION_REQUIRED"
SPECIFICATION_INCOMPLETE = "SPECIFICATION_INCOMPLETE"
RESTRICTED_OR_UNSOURCEABLE = "RESTRICTED_OR_UNSOURCEABLE"
INVALID_EXTRACTION = "INVALID_EXTRACTION"

_BOILERPLATE = re.compile(
    r"(terms\s+and\s+conditions|general\s+conditions|instructions\s+to\s+bidders|"
    r"proposal\s+instructions|scope\s+of\s+work\s+overview|insurance\s+requirement|"
    r"workers['’]?\s*compensation|commercial\s+general\s+liability|automobile\s+liability|"
    r"professional\s+liability|cyber\s+liability|umbrella\s+liability|"
    r"indemnification|hold\s+harmless|liquidated\s+damages|bid\s+bond|"
    r"performance\s+bond|payment\s+bond|w-?9| Affidavit|non[-\s]?collusion|"
    r"equal\s+opportunity|davis[-\s]?bacon|prevailing\s+wage|"
    r"part\s+number/?trade\s+name|or\s+equal\s+clause|brand\s+name\s+or\s+equal|"
    r"section\s+\d+(\.\d+)*\s*$|article\s+\d+|exhibit\s+[a-z]\b|"
    r"attachment\s+[a-z0-9]+\b|table\s+of\s+contents|signature\s+page|"
    r"acknowledgement|certification\s+of\s+compliance)",
    re.I,
)

_SERVICE_HEAVY = re.compile(
    r"\b(labor|installation|construction|renovation|remodel|painting|roofing|"
    r"landscap|janitorial\s+service|staffing|consulting|professional\s+services|"
    r"ambulance|transportation\s+services|security\s+guard|software\s+development|"
    r"training\s+services|maintenance\s+agreement)\b",
    re.I,
)

_GOODS_HINT = re.compile(
    r"\b(paper|towel|tissue|glove|nitrile|ppe|mask|cleaner|disinfectant|bulb|"
    r"lamp|fixture|chair|desk|furniture|toner|cartridge|battery|filter|hose|"
    r"valve|pump|tool|wrench|drill|screwdriver|fastener|bolt|nut|screw|"
    r"oil|fluid|lubricant|tire|brake|automotive|mro|supply|supplies|"
    r"equipment|widget|kit|case|carton|box|each|ea\b|ream|gallon|quart|"
    r"pound|lb\b|sheet|roll|pack|pkg)\b",
    re.I,
)

_JUNK_MPN = re.compile(r"^(and|or|the|a|an|of|to|for|with|n/?a|na|none|tbd|see\s*above|\d{1,3})$", re.I)
_SECTION_QTY = re.compile(r"^\d+(\.\d+)+$")  # 13.3, 15.2
_MONEY_QTY = re.compile(r"^\$[\d,]+(\.\d+)?$")


def _s(v: Any) -> str:
    return str(v or "").strip()


def validate_product_line(line: dict[str, Any] | None) -> dict[str, Any]:
    """Classify one extracted line; sets commercially_usable bool."""
    line = line if isinstance(line, dict) else {}
    desc = _s(
        line.get("description")
        or line.get("DESCRIPTION")
        or line.get("product_description")
        or line.get("title")
        or line.get("TITLE")
        or line.get("item_description")
    )
    mfr = _s(line.get("manufacturer") or line.get("MANUFACTURER") or line.get("mfr"))
    mpn = _s(
        line.get("part_number")
        or line.get("PART_NUMBER")
        or line.get("mpn")
        or line.get("model")
        or line.get("MODEL")
    )
    qty_raw = line.get("quantity") if line.get("quantity") is not None else line.get("QUANTITY")
    qty_s = _s(qty_raw)
    uom = _s(line.get("uom") or line.get("UOM") or line.get("unit") or line.get("unit_of_measure"))

    reasons: list[str] = []
    classification = SPECIFICATION_INCOMPLETE

    if not desc or len(desc) < 4:
        return {
            "classification": INVALID_EXTRACTION,
            "commercially_usable": False,
            "reasons": ["empty_or_too_short_description"],
            "description": desc,
        }

    if _BOILERPLATE.search(desc):
        return {
            "classification": INVALID_EXTRACTION,
            "commercially_usable": False,
            "reasons": ["boilerplate_or_insurance_or_legal"],
            "description": desc[:200],
        }

    if mpn and _JUNK_MPN.match(mpn):
        reasons.append("junk_mpn_rejected")
        mpn = ""

    if qty_s and (_SECTION_QTY.match(qty_s) or _MONEY_QTY.match(qty_s)):
        reasons.append("quantity_looks_like_section_or_money")
        qty_s = ""

    qty_ok = False
    try:
        qf = float(str(qty_raw).replace(",", "")) if qty_raw is not None and qty_s else None
        qty_ok = qf is not None and qf > 0 and qf < 1_000_000
    except Exception:
        qty_ok = False

    goods_like = bool(_GOODS_HINT.search(desc))
    service_like = bool(_SERVICE_HEAVY.search(desc)) and not goods_like

    if service_like and not (mfr and mpn):
        return {
            "classification": INVALID_EXTRACTION,
            "commercially_usable": False,
            "reasons": ["service_misidentified_as_goods", *reasons],
            "description": desc[:200],
        }

    if mfr and mpn and len(mpn) >= 3 and qty_ok and uom:
        classification = EXACT_PRODUCT_IDENTIFIED
    elif goods_like and qty_ok and uom and len(desc) >= 12:
        classification = COMMERCIALLY_SOURCEABLE_GENERIC
    elif goods_like and (qty_ok or uom or mpn or mfr):
        classification = SUPPLIER_CLARIFICATION_REQUIRED
        reasons.append("missing_qty_uom_or_identity_fields")
    elif goods_like:
        classification = SPECIFICATION_INCOMPLETE
        reasons.append("goods_hint_but_insufficient_fields")
    else:
        classification = SPECIFICATION_INCOMPLETE
        reasons.append("no_goods_signal")

    usable = classification in {EXACT_PRODUCT_IDENTIFIED, COMMERCIALLY_SOURCEABLE_GENERIC}
    return {
        "classification": classification,
        "commercially_usable": usable,
        "reasons": reasons,
        "description": desc[:240],
        "manufacturer": mfr or None,
        "part_number": mpn or None,
        "quantity_ok": qty_ok,
        "uom": uom or None,
    }


def filter_usable_product_lines(lines: list[dict[str, Any]] | None) -> dict[str, Any]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for ln in lines or []:
        if not isinstance(ln, dict):
            continue
        verdict = validate_product_line(ln)
        counts[verdict["classification"]] = counts.get(verdict["classification"], 0) + 1
        enriched = {**ln, "_validation": verdict}
        if verdict["commercially_usable"]:
            accepted.append(enriched)
        else:
            rejected.append(enriched)
    return {
        "kind": "ProductLineValidationBatch",
        "input_count": len(lines or []),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "by_classification": counts,
        "accepted": accepted,
        "rejected_sample": rejected[:20],
    }


def is_fully_specified_commercial(line: dict[str, Any] | None) -> bool:
    """Replacement for naive complete_spec=true scorecard inflation."""
    return bool(validate_product_line(line).get("commercially_usable"))
