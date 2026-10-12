"""RFQ quoteability — exact ID / brand-or-equal / supplier-facing spec.

RFQ_READY must NOT require revenue, economics, recipient, or public price.
"""

from __future__ import annotations

import re
from typing import Any

BUILD = "20261010-m3-real-line-attrition-rfq-yield-recovery-v1"

_SERVICE = re.compile(
    r"\b(services?|consulting|construction|renovation|install(?:ation)?|"
    r"maintenance|repair\s+of|staffing|training|architectural|engineering)\b",
    re.I,
)
_BRAND = re.compile(
    r"\b(3m|grainger|milwaukee|dewalt|makita|honeywell|brady|ansi|ul\b|oem|"
    r"john\s*deere|caterpillar|cummins|honda|bosch|stanley|ridgid|klein|"
    r"fluke|appleton|square\s*d|eaton|ge\b|siemens|philips|lithonia|cooper|"
    r"odyssey|mcelroy|zoro|homelite)\b",
    re.I,
)
_SPEC_HINT = re.compile(
    r"\b(\d+(\.\d+)?\s*(v|volt|vac|vdc|w|watt|a|amp|hz|psi|gpm|gal|in|inch|"
    r"mm|cm|ft|gauge|awg|npt|mph|rpm|lumens?|k\b|kelvin)|"
    r"size\s*[:#]?\s*\w+|model\s*[:#]?\s*[\w\-]+|"
    r"part\s*(no|number|#)?\s*[:#]?\s*[\w\-]+|"
    r"nsn\s*[:#]?\s*\d|catalog\s*[:#]?\s*[\w\-]+)\b",
    re.I,
)
_QTY = re.compile(r"\b(qty|quantity|ea\b|each|unit|units|lot|case|box|pack)\b", re.I)
_PRODUCT_NOUN = re.compile(
    r"\b(battery|batteries|filter|glove|ppe|paper|chair|screw|hardware|tool|"
    r"bulb|lamp|fixture|chemical|cleaner|mop|hose|valve|pump|cable|wire|"
    r"connector|adapter|bearing|belt|blade|bit|drill|saw|wrench|socket|"
    r"helmet|vest|boot|coverall|respirator|goggle|tape|sealant|lubricant|"
    r"oil|grease|fitting|coupling|clamp|bracket|gasket|orifice|nozzle|"
    r"led|ballast|breaker|contactor|relay|switch|outlet|receptacle|"
    r"transformer|motor|compressor|cylinder|tank|trailer|utv|vehicle)\b",
    re.I,
)


def _line_blob(ln: dict[str, Any]) -> str:
    parts = [
        ln.get("description") or ln.get("DESCRIPTION"),
        ln.get("manufacturer") or ln.get("MANUFACTURER"),
        ln.get("brand") or ln.get("BRAND"),
        ln.get("mpn") or ln.get("MPN"),
        ln.get("model") or ln.get("MODEL"),
        ln.get("catalog") or ln.get("CATALOG"),
        ln.get("nsn") or ln.get("NSN"),
        ln.get("size") or ln.get("SIZE"),
        ln.get("material") or ln.get("MATERIAL"),
        ln.get("voltage") or ln.get("VOLTAGE"),
        ln.get("amperage") or ln.get("AMPERAGE"),
        ln.get("wattage") or ln.get("WATTAGE"),
        ln.get("capacity") or ln.get("CAPACITY"),
        ln.get("uom") or ln.get("UOM"),
        str(ln.get("qty") if ln.get("qty") is not None else ln.get("QTY") or ""),
    ]
    return " ".join(str(p) for p in parts if p).strip()


def classify_line_quoteability(ln: dict[str, Any], *, title: str = "") -> dict[str, Any]:
    """Classify one product line for supplier RFQ purposes."""
    blob = _line_blob(ln)
    text = f"{title} {blob}".strip()
    if not blob and not title:
        return {"MODE": "NOT_QUOTEABLE", "REASON": "EMPTY"}
    if _SERVICE.search(blob) and not _PRODUCT_NOUN.search(blob):
        return {"MODE": "NOT_QUOTEABLE", "REASON": "SERVICE_LINE"}

    nsn = str(ln.get("nsn") or ln.get("NSN") or "").strip()
    mpn = str(ln.get("mpn") or ln.get("MPN") or "").strip()
    catalog = str(ln.get("catalog") or ln.get("CATALOG") or "").strip()
    model = str(ln.get("model") or ln.get("MODEL") or "").strip()
    brand = str(ln.get("brand") or ln.get("BRAND") or ln.get("manufacturer") or ln.get("MANUFACTURER") or "").strip()
    qty = ln.get("qty") if ln.get("qty") is not None else ln.get("QTY")
    uom = str(ln.get("uom") or ln.get("UOM") or "").strip()

    qty_ok = qty is not None and str(qty).strip() not in {"", "0", "None"}
    uom_ok = bool(uom)
    qty_basis = "EXPLICIT" if qty_ok else ("IMPLIED_UOM" if uom_ok or _QTY.search(text) else "UNKNOWN")
    # RFQ may proceed with implied "1 EA" for single commodity purchase lines
    qty_rfq_ok = qty_ok or (bool(_PRODUCT_NOUN.search(text)) and len(blob) >= 12)

    missing: list[str] = []
    if not qty_rfq_ok:
        missing.append("QTY")
    if not uom_ok and not qty_ok:
        missing.append("UOM")

    # Exact ID
    if nsn or mpn or catalog:
        return {
            "MODE": "QUOTE_READY_EXACT_ID",
            "QTY_BASIS": qty_basis,
            "QTY_RFQ_OK": qty_rfq_ok,
            "UOM_OK": uom_ok,
            "MISSING": missing,
            "BRAND": brand or None,
            "MODEL": model or None,
            "MPN": mpn or None,
            "NSN": nsn or None,
            "CATALOG": catalog or None,
        }

    # Brand or equal
    brand_hit = brand or (_BRAND.search(text).group(0) if _BRAND.search(text) else "")
    if brand_hit and (model or _SPEC_HINT.search(text) or _PRODUCT_NOUN.search(text)):
        return {
            "MODE": "QUOTE_READY_BRAND_OR_EQUAL",
            "QTY_BASIS": qty_basis,
            "QTY_RFQ_OK": qty_rfq_ok,
            "UOM_OK": uom_ok,
            "MISSING": missing,
            "BRAND": brand_hit,
            "MODEL": model or None,
        }

    # Spec-based
    if _PRODUCT_NOUN.search(text) and (_SPEC_HINT.search(text) or len(blob) >= 28) and not _SERVICE.search(blob[:80]):
        return {
            "MODE": "QUOTE_READY_BY_SPEC",
            "QTY_BASIS": qty_basis,
            "QTY_RFQ_OK": qty_rfq_ok,
            "UOM_OK": uom_ok,
            "MISSING": missing,
            "SPEC_SIGNAL": True,
        }

    # Short clear commodity title-as-line (BidNet DESCRIPTION_LINE)
    if _PRODUCT_NOUN.search(text) and len(text) >= 10 and len(text) <= 180 and not _SERVICE.search(text):
        return {
            "MODE": "QUOTE_READY_BY_SPEC",
            "QTY_BASIS": qty_basis,
            "QTY_RFQ_OK": qty_rfq_ok or True,
            "UOM_OK": uom_ok,
            "MISSING": [m for m in missing if m != "QTY"],
            "SPEC_SIGNAL": "COMMODITY_NOUN",
        }

    return {
        "MODE": "NOT_QUOTEABLE",
        "REASON": "INSUFFICIENT_DEFINITION",
        "QTY_BASIS": qty_basis,
        "QTY_RFQ_OK": qty_rfq_ok,
        "MISSING": missing + ["IDENTITY_OR_SPEC"],
    }


def assess_opportunity_quoteability(l23: dict[str, Any], *, title: str | None = None) -> dict[str, Any]:
    title = str(title or l23.get("title") or "")
    lines = l23.get("line_items") if isinstance(l23.get("line_items"), list) else []
    if not lines:
        # synthesize from line_texts / description
        texts = l23.get("line_texts") if isinstance(l23.get("line_texts"), list) else []
        if texts:
            lines = [{"DESCRIPTION": t} for t in texts if t]
        elif l23.get("description"):
            lines = [{"DESCRIPTION": str(l23.get("description"))[:500]}]
        else:
            lines = [{"DESCRIPTION": title}] if title else []

    classified = [classify_line_quoteability(ln, title=title) for ln in lines if isinstance(ln, dict)]
    quoteable = [c for c in classified if c.get("MODE", "").startswith("QUOTE_READY")]
    modes = Counterish(c.get("MODE") for c in quoteable)
    total = len(classified) or 1
    pct = round(100.0 * len(quoteable) / total, 1)

    if any(c.get("MODE") == "QUOTE_READY_EXACT_ID" for c in quoteable):
        best = "QUOTE_READY_EXACT_ID"
    elif any(c.get("MODE") == "QUOTE_READY_BRAND_OR_EQUAL" for c in quoteable):
        best = "QUOTE_READY_BRAND_OR_EQUAL"
    elif quoteable:
        best = "QUOTE_READY_BY_SPEC"
    else:
        best = "NOT_QUOTE_READY"

    state = "NOT_QUOTE_READY"
    if len(quoteable) == total and quoteable:
        state = "FULLY_QUOTE_READY"
    elif quoteable:
        state = "PARTIALLY_QUOTE_READY"

    return {
        "build": BUILD,
        "BEST_MODE": best,
        "OPPORTUNITY_QUOTE_STATE": state,
        "QUOTEABLE_LINE_COUNT": len(quoteable),
        "TOTAL_LINE_COUNT": total,
        "QUOTEABLE_LINE_PERCENT": pct,
        "MODE_COUNTS": modes,
        "PRODUCT_QUOTEABLE": bool(quoteable),
        "IDENTITY_QUOTE_READY": best == "QUOTE_READY_EXACT_ID",
        "BRAND_OR_EQUAL_READY": best in {"QUOTE_READY_EXACT_ID", "QUOTE_READY_BRAND_OR_EQUAL"},
        "SPEC_QUOTE_READY": best.startswith("QUOTE_READY"),
        "LINES": classified[:40],
    }


def Counterish(vals) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in vals:
        k = str(v or "NONE")
        out[k] = out.get(k, 0) + 1
    return out


def detect_partial_award(l23: dict[str, Any]) -> str:
    blob = " ".join(
        str(x or "")
        for x in (
            l23.get("description"),
            l23.get("title"),
            " ".join(str(t) for t in (l23.get("line_texts") or [])[:20]),
        )
    ).lower()
    if re.search(r"\b(all[- ]or[- ]none|entire\s+award|single\s+award\s+only)\b", blob):
        return "ALL_OR_NONE"
    if re.search(r"\b(line[- ]item\s+award|award\s+by\s+line|multiple\s+award|partial\s+bid|may\s+award\s+by\s+item)\b", blob):
        return "LINE_ITEM_AWARD"
    if re.search(r"\bpartial\b", blob) and re.search(r"\b(bid|award|quote)\b", blob):
        return "PARTIAL_BID_ALLOWED"
    return "UNKNOWN"
