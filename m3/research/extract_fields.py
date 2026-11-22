"""Deterministic field extraction from solicitation text/documents (Build 002I).

Never invents quantities, specs, or deadlines. Returns only evidence-backed fields.

Quantity semantics (002I): distinguish ORDERED_QUANTITY from packaging, capacity,
dimensions, weights, model numbers, and ambiguous parenthetical numbers.
"""

from __future__ import annotations

import re
from typing import Any

# Affirmative ordered-quantity language
ORDERED_QTY_RE = re.compile(
    r"(?i)(?:quantity|qty|estimated\s+quantity|order\s+quantity|bid\s+quantity|"
    r"ordered\s+quantity|line\s*item\s*quantity|purchase\s+quantity)"
    r"\s*(?:of|:|=|-)?\s*([\d,]+(?:\.\d+)?)\s*([a-zA-Z]{0,16})?"
)
FURNISH_QTY_RE = re.compile(
    r"(?i)(?:furnish|provide|supply|purchase|buy|procure)\s+"
    r"(?:of\s+|approximately\s+|approx\.?\s+|up\s+to\s+)?"
    r"([\d,]+)\s+"
    r"(each|ea\.?|units?|pcs?|pieces|sets?|kits?|[a-z]{2,16})\b"
)
# Government titles: "Five (5) New ... Apparatus" — TITLE ONLY (not body boilerplate)
WORD_PAREN_QTY_RE = re.compile(
    r"(?i)\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s*\(\s*(\d{1,4})\s*\)"
)
# Reject legal/time boilerplate near word-paren matches in body text
WORD_PAREN_REJECT_FOLLOW = re.compile(
    r"(?i)^\s*(?:calendar\s+)?(?:days?|years?|months?|weeks?|hours?|copies|originals?|"
    r"duplicates?|pages?|business\s+days?|working\s+days?|sets?\s+of\s+documents)"
)
# Explicit "N each of <product>"
N_EACH_OF_RE = re.compile(
    r"(?i)\b([\d,]+)\s*(?:each|ea\.?|units?)\s+(?:of\s+)?([a-z][a-z0-9 \-/]{2,40})"
)
LINE_ITEM_RE = re.compile(
    r"(?i)(?:line\s*(?:item|#|no\.?)|item\s*(?:#|no\.?))\s*[:\s]*([A-Z0-9\-]+).{0,80}?"
    r"(?:qty|quantity)\s*[:\s]*([\d,]+)",
)
# Packaging / per-unit capacity — NOT total ordered quantity
PACKAGE_PER_RE = re.compile(
    r"(?i)\(\s*([\d,]+(?:\.\d+)?)\s*per\s+([a-zA-Z]{1,16})\s*\)"
    r"|"
    r"([\d,]+(?:\.\d+)?)\s*(?:units?|ea\.?|each|pcs?|pieces)?\s*per\s+"
    r"(case|box|cup|bag|drum|pallet|roll|pack|carton|gallon|kit)\b"
)
# Ambiguous parenthetical "(300 UNIT)" / "(250 CUP)" without "per"
PAREN_UNIT_RE = re.compile(
    r"(?i)\(\s*([\d,]+(?:\.\d+)?)\s*([a-zA-Z]{1,16})\s*\)"
)
# Bare "500 EA" without quantity keyword — weak; only accept with purchase context nearby
BARE_EA_RE = re.compile(
    r"(?i)(?<!per\s)([\d,]+(?:\.\d+)?)\s*"
    r"(each|ea\.?|units?|pcs?|pieces|sets?|boxes|cases|kits?)\b"
)

MPN_RE = re.compile(
    r"(?i)(?:mfr\.?\s*part|manufacturer\s*part|mpn|part\s*#|model\s*(?:#|no\.?))\s*[:\s]*([A-Z0-9][A-Z0-9\-/.]{2,})"
)
NSN_RE = re.compile(r"(?i)\bNSN\s*[:\s]*(\d{4}-\d{2}-\d{3}-\d{4})\b")
DEADLINE_RE = re.compile(
    r"(?i)(?:bid\s*(?:due|opening|deadline)|proposals?\s*due|closing\s*date|due\s*date)"
    r"[^\n:]{0,20}[:\s]+([A-Za-z0-9,/\- ]{6,40})"
)
FIXED_QTY_MARKERS = re.compile(
    r"(?i)\b(firm\s+quantity|fixed\s+quantity|definite\s+quantity|one[\s-]time\s+purchase)\b"
)
ESTIMATED_QTY_MARKERS = re.compile(
    r"(?i)\b(estimated\s+quantity|approx(?:imate|imately)?|up\s+to|not\s+to\s+exceed)\b"
)
IDIQ_MARKERS = re.compile(
    r"(?i)\b(indefinite\s+quantity|idiq|requirements\s+contract|blanket\s+purchase\s+agreement|"
    r"purchase\s+as[\s-]needed|ordered\s+as[\s-]needed|quantit(?:y|ies)\s+as[\s-]needed)\b"
)

# Spec/weight/dimension units are NOT purchase quantities
REJECT_QTY_UNITS = {
    "lb",
    "lbs",
    "pound",
    "pounds",
    "gvwr",
    "kg",
    "kilogram",
    "kilograms",
    "mhz",
    "ghz",
    "vac",
    "vdc",
    "volt",
    "volts",
    "amp",
    "amps",
    "psi",
    "psig",
    "btu",
    "hp",
    "kw",
    "inch",
    "inches",
    "ft",
    "foot",
    "feet",
    "mm",
    "cm",
    "meter",
    "meters",
    "year",
    "years",
    "yr",
    "yrs",
    "month",
    "months",
    "day",
    "days",
    "watt",
    "watts",
    "va",
    "kva",
}
PACKAGE_UNITS = {
    "case",
    "cases",
    "box",
    "boxes",
    "cup",
    "cups",
    "bag",
    "bags",
    "drum",
    "drums",
    "pallet",
    "pallets",
    "pack",
    "packs",
    "carton",
    "cartons",
    "roll",
    "rolls",
}
PURCHASE_LIKE_UNITS = {
    "unit",
    "units",
    "each",
    "ea",
    "pc",
    "pcs",
    "piece",
    "pieces",
    "set",
    "sets",
    "kit",
    "kits",
    "lot",
    "gallon",
    "gallons",
    "ton",
    "tons",
}

# Semantics that may count as defined purchase quantity for Stage C
ORDERED_SEMANTICS = {
    "ORDERED_QUANTITY",
    "LINE_ITEM_QUANTITY",
    "ESTIMATED_QUANTITY",
    "MINIMUM_GUARANTEED_QUANTITY",
    "MAXIMUM_AUTHORIZED_QUANTITY",
}


def _num(s: str) -> float | None:
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def _unit_norm(unit: str | None) -> str:
    return ((unit or "").strip().lower().rstrip("."))


def extract_quantity_fields(text: str, *, title: str = "") -> dict[str, Any]:
    """Extract quantity with semantic class. Empty / non-ordered = not a Stage-C fit basis."""
    blob = f"{title}\n{text or ''}"
    evidence: list[str] = []
    qty_kind = "unknown"
    # Prefer title + first 4k for contract-type markers so vendor-form boilerplate
    # ("as needed" help text) does not override affirmative line quantities.
    marker_blob = f"{title}\n{(text or '')[:4000]}"
    if IDIQ_MARKERS.search(marker_blob) and not ORDERED_QTY_RE.search(blob):
        qty_kind = "indefinite"
        evidence.append("idiq_or_as_needed_language")
    elif ESTIMATED_QTY_MARKERS.search(marker_blob):
        qty_kind = "estimated"
        evidence.append("estimated_quantity_language")
    elif FIXED_QTY_MARKERS.search(marker_blob):
        qty_kind = "fixed"
        evidence.append("fixed_quantity_language")

    candidates: list[dict[str, Any]] = []

    def _add(
        val: float,
        unit: str | None,
        snippet: str,
        semantic: str,
        *,
        ordered: bool,
    ) -> None:
        unit_l = _unit_norm(unit)
        if val is None or val <= 0 or val > 10_000_000:
            return
        if 1900 <= val <= 2100 and not unit_l:
            return
        if unit_l in REJECT_QTY_UNITS:
            return
        if re.search(r"(?i)\bgvwr\b", snippet):
            return
        candidates.append(
            {
                "value": val,
                "unit": (unit or "")[:32] or None,
                "snippet": snippet[:120],
                "semantic": semantic,
                "counts_as_ordered": ordered,
            }
        )

    # 1) Packaging / per-unit — record but never treat as ordered total
    for m in PACKAGE_PER_RE.finditer(blob):
        raw = m.group(1) or m.group(3)
        unit = m.group(2) or m.group(4)
        val = _num(raw or "")
        if val is None:
            continue
        _add(val, unit, m.group(0), "PACKAGE_QUANTITY", ordered=False)

    # 2) Affirmative "Quantity: N"
    for m in ORDERED_QTY_RE.finditer(blob):
        val = _num(m.group(1))
        unit = m.group(2)
        if val is None:
            continue
        sem = "ESTIMATED_QUANTITY" if qty_kind == "estimated" else "ORDERED_QUANTITY"
        _add(val, unit, m.group(0), sem, ordered=True)

    # 3) "furnish/purchase N units" / "purchase of N ..."
    for m in FURNISH_QTY_RE.finditer(blob):
        val = _num(m.group(1))
        unit = m.group(2)
        if val is None:
            continue
        _add(val, unit, m.group(0), "ORDERED_QUANTITY", ordered=True)

    # 3b) "Five (5) New ..." — TITLE ONLY. Body legal text ("one (1) original") is not ordered qty.
    for m in WORD_PAREN_QTY_RE.finditer(title or ""):
        val = _num(m.group(1))
        if val is None:
            continue
        after = (title or "")[m.end() : m.end() + 48]
        if WORD_PAREN_REJECT_FOLLOW.match(after):
            continue
        _add(val, "EA", m.group(0), "ORDERED_QUANTITY", ordered=True)

    # 4) Line-item qty columns
    line_items: list[dict[str, Any]] = []
    for m in LINE_ITEM_RE.finditer(blob):
        val = _num(m.group(2))
        if val is None:
            continue
        line_items.append(
            {
                "line_ref": m.group(1),
                "quantity": val,
                "snippet": m.group(0)[:160],
                "semantic": "LINE_ITEM_QUANTITY",
            }
        )
        _add(val, None, m.group(0), "LINE_ITEM_QUANTITY", ordered=True)
        if len(line_items) >= 20:
            break

    # 5) Ambiguous parenthetical "(300 UNIT)" — NOT ordered without more evidence
    for m in PAREN_UNIT_RE.finditer(blob):
        # Skip if already classified as PACKAGE via "per"
        if re.search(r"(?i)\bper\b", m.group(0)):
            continue
        val = _num(m.group(1))
        unit = m.group(2)
        unit_l = _unit_norm(unit)
        if val is None:
            continue
        if unit_l in PACKAGE_UNITS:
            _add(val, unit, m.group(0), "PACKAGE_QUANTITY", ordered=False)
        elif unit_l in PURCHASE_LIKE_UNITS:
            _add(val, unit, m.group(0), "UNKNOWN_NUMERIC_VALUE", ordered=False)
        else:
            _add(val, unit, m.group(0), "UNKNOWN_NUMERIC_VALUE", ordered=False)

    # Prefer ordered candidates
    ordered = [c for c in candidates if c.get("counts_as_ordered")]
    primary = ordered[0] if ordered else None
    semantic = primary["semantic"] if primary else (
        candidates[0]["semantic"] if candidates else None
    )

    if primary and qty_kind == "unknown":
        if primary["semantic"] == "ESTIMATED_QUANTITY":
            qty_kind = "estimated"
        else:
            qty_kind = "fixed"
        evidence.append("affirmative_ordered_quantity_language")
    elif candidates and not primary:
        evidence.append(f"non_ordered_numeric:{candidates[0]['semantic']}")

    result: dict[str, Any] = {
        "quantity": (
            str(int(primary["value"]))
            if primary and primary["value"] == int(primary["value"])
            else (str(primary["value"]) if primary else None)
        ),
        "quantity_unit": primary.get("unit") if primary else None,
        "quantity_kind": qty_kind if primary or qty_kind != "unknown" else "unknown",
        "quantity_semantic": semantic,
        "quantity_evidence": evidence + ([primary["snippet"]] if primary else []),
        "quantity_candidates": candidates[:8],
        "line_items": line_items[:20],
        "quantity_extracted": bool(primary),
        "quantity_is_ordered": bool(primary),
    }
    # Surface first non-ordered candidate for investigation (never as quantity field)
    if not primary and candidates:
        result["ambiguous_quantity_candidates"] = candidates[:5]
        result["quantity_evidence"] = evidence + [candidates[0]["snippet"]]
    return result


def extract_identity_fields(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    m = MPN_RE.search(text or "")
    if m:
        out["manufacturer_part_number"] = m.group(1)[:64]
        out["mpn_evidence"] = m.group(0)[:100]
    m = NSN_RE.search(text or "")
    if m:
        out["nsn"] = m.group(1)
        out["nsn_evidence"] = m.group(0)[:80]
    m = DEADLINE_RE.search(text or "")
    if m:
        out["deadline_extracted"] = m.group(1).strip()[:80]
        out["deadline_evidence"] = m.group(0)[:120]
    return out


def enrich_item_from_text(item: dict[str, Any], text: str, *, source: str = "document") -> dict[str, Any]:
    """Return copy of item with extracted fields when missing. Does not overwrite verified values."""
    out = dict(item)
    qty = extract_quantity_fields(text, title=str(out.get("title") or ""))
    ident = extract_identity_fields(text)
    if qty.get("quantity_extracted") and qty.get("quantity_is_ordered") and not out.get("quantity"):
        out["quantity"] = qty["quantity"]
        out["quantity_unit"] = qty.get("quantity_unit")
        out["quantity_kind"] = qty.get("quantity_kind")
        out["quantity_semantic"] = qty.get("quantity_semantic")
        out["quantity_evidence"] = list(qty.get("quantity_evidence") or []) + [f"source:{source}"]
        out["line_items"] = qty.get("line_items") or out.get("line_items")
    else:
        # Preserve investigation trail for packaging / ambiguous numbers — do not set quantity
        if qty.get("ambiguous_quantity_candidates") and not out.get("quantity"):
            out["quantity_semantic"] = qty.get("quantity_semantic") or "UNKNOWN_NUMERIC_VALUE"
            out["quantity_kind"] = out.get("quantity_kind") or "unknown"
            out["quantity_evidence"] = list(qty.get("quantity_evidence") or []) + [
                f"source:{source}",
                "rejected_non_ordered_quantity_semantics",
            ]
            out["ambiguous_quantity_candidates"] = qty.get("ambiguous_quantity_candidates")
        elif qty.get("quantity_kind") in {"indefinite", "estimated"} and not out.get("quantity_kind"):
            out["quantity_kind"] = qty["quantity_kind"]
            out["quantity_evidence"] = qty.get("quantity_evidence")
    for k, v in ident.items():
        if v and not out.get(k):
            out[k] = v
    out["_extraction"] = {
        "quantity_extracted": bool(qty.get("quantity_extracted")),
        "quantity_is_ordered": bool(qty.get("quantity_is_ordered")),
        "quantity_semantic": qty.get("quantity_semantic"),
        "source": source,
        "quantity_kind": out.get("quantity_kind") or qty.get("quantity_kind"),
    }
    return out


def ocr_pdf_bytes(data: bytes, *, max_pages: int = 5) -> dict[str, Any]:
    """OCR fallback for scanned PDFs. Optional deps; never fabricates text."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return {"ok": False, "reason": "pypdfium2_not_installed", "text": ""}
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return {"ok": False, "reason": "pytesseract_or_pillow_not_installed", "text": ""}

    try:
        pdf = pdfium.PdfDocument(data)
        parts: list[str] = []
        n = min(len(pdf), max_pages)
        for i in range(n):
            page = pdf[i]
            # 150 dpi — bounded cost
            bitmap = page.render(scale=150 / 72)
            pil = bitmap.to_pil()
            if pil.mode not in ("RGB", "L"):
                pil = pil.convert("RGB")
            txt = pytesseract.image_to_string(pil) or ""
            if txt.strip():
                parts.append(f"[page {i+1}]\n{txt.strip()}")
        text = "\n\n".join(parts)
        text = re.sub(r"\s+", " ", text).strip()[:20000]
        return {
            "ok": len(text) >= 40,
            "text": text,
            "reason": None if len(text) >= 40 else "ocr_insufficient_text",
            "pages_ocrd": n,
            "method": "pypdfium2+tesseract",
        }
    except Exception as exc:
        return {"ok": False, "reason": f"ocr_error:{type(exc).__name__}", "text": ""}
