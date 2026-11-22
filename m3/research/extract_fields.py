"""Deterministic field extraction from solicitation text/documents (Build 002H).

Never invents quantities, specs, or deadlines. Returns only evidence-backed fields.
"""

from __future__ import annotations

import re
from typing import Any

# Quantity patterns — conservative; require explicit qty language or unit+number
QTY_PATTERNS = [
    re.compile(
        r"(?i)(?:quantity|qty|estimated\s+quantity|order\s+quantity|bid\s+quantity)"
        r"\s*(?:of|:|=|-)?\s*([\d,]+(?:\.\d+)?)\s*([a-zA-Z]{0,12})?"
    ),
    # Title forms: "(300 UNIT)", "(250 PER CUP)", "500 EA"
    re.compile(
        r"(?i)\(\s*([\d,]+(?:\.\d+)?)\s*(?:per\s+)?([a-zA-Z]{1,16})\s*\)"
    ),
    re.compile(
        r"(?i)([\d,]+(?:\.\d+)?)\s*(?:per\s+)?(each|ea\.?|units?|pcs?|pieces|lot|sets?|boxes|cases|"
        r"gallons?|tons?|lbs?|cups?|drums?|pallets?|bags?|rolls?|kits?)\b"
    ),
    re.compile(
        r"(?i)(?:furnish|provide|supply|purchase)\s+(?:approximately\s+)?([\d,]+)\s+"
        r"(each|ea\.?|units?|pcs?|pieces|sets?|[a-z]{2,12})"
    ),
]
LINE_ITEM_RE = re.compile(
    r"(?i)(?:line\s*(?:item|#|no\.?)|item\s*(?:#|no\.?))\s*[:\s]*([A-Z0-9\-]+).{0,80}?"
    r"(?:qty|quantity)\s*[:\s]*([\d,]+)",
)
MPN_RE = re.compile(
    r"(?i)(?:mfr\.?\s*part|manufacturer\s*part|mpn|part\s*#|model\s*(?:#|no\.?))\s*[:\s]*([A-Z0-9][A-Z0-9\-/.]{2,})"
)
NSN_RE = re.compile(r"(?i)\bNSN\s*[:\s]*(\d{4}-\d{2}-\d{3}-\d{4})\b")
DEADLINE_RE = re.compile(
    r"(?i)(?:bid\s*(?:due|opening|deadline)|proposals?\s*due|closing\s*date|due\s*date)"
    r"[^\n:]{0,20}[:\s]+([A-Za-z0-9,/\- ]{6,40})"
)
FIXED_QTY_MARKERS = re.compile(r"(?i)\b(firm\s+quantity|fixed\s+quantity|definite\s+quantity|one[\s-]time\s+purchase)\b")
ESTIMATED_QTY_MARKERS = re.compile(r"(?i)\b(estimated\s+quantity|approx(?:imate|imately)?|up\s+to|not\s+to\s+exceed)\b")
IDIQ_MARKERS = re.compile(r"(?i)\b(indefinite\s+quantity|idiq|requirements\s+contract|as[\s-]needed|blanket\s+purchase)\b")
# Spec/weight units are NOT purchase quantities (e.g. "19,500 LB GVWR")
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
    "mhz",
    "inch",
    "inches",
    "ft",
    "foot",
    "feet",
    "mm",
    "cm",
    "meter",
    "meters",
}
ACCEPT_PAREN_UNITS = {
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
    "box",
    "boxes",
    "case",
    "cases",
    "lot",
    "cup",
    "cups",
    "kit",
    "kits",
    "bag",
    "bags",
    "roll",
    "rolls",
    "drum",
    "drums",
    "pallet",
    "pallets",
    "gallon",
    "gallons",
    "ton",
    "tons",
}


def _num(s: str) -> float | None:
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def extract_quantity_fields(text: str, *, title: str = "") -> dict[str, Any]:
    """Extract quantity from published text. Empty result = unknown, not invented."""
    blob = f"{title}\n{text or ''}"
    evidence: list[str] = []
    qty_kind = "unknown"
    if IDIQ_MARKERS.search(blob):
        qty_kind = "indefinite"
        evidence.append("idiq_or_as_needed_language")
    elif ESTIMATED_QTY_MARKERS.search(blob):
        qty_kind = "estimated"
        evidence.append("estimated_quantity_language")
    elif FIXED_QTY_MARKERS.search(blob):
        qty_kind = "fixed"
        evidence.append("fixed_quantity_language")

    quantities: list[dict[str, Any]] = []
    for pat in QTY_PATTERNS:
        for m in pat.finditer(blob):
            groups = m.groups()
            raw = groups[0]
            unit = ((groups[1] if len(groups) > 1 else "") or "").strip()
            unit_l = unit.lower().rstrip(".")
            val = _num(raw)
            if val is None or val <= 0:
                continue
            if val > 10_000_000:
                continue
            # Avoid years mistaken as qty (e.g. 2026)
            if 1900 <= val <= 2100 and not unit:
                continue
            # Reject weight/spec units (GVWR, LB, PSI, etc.)
            if unit_l in REJECT_QTY_UNITS:
                continue
            if re.search(r"(?i)\bgvwr\b", m.group(0)):
                continue
            # Parenthetical captures require a purchase-like unit word
            if m.group(0).strip().startswith("(") and unit_l and unit_l not in ACCEPT_PAREN_UNITS:
                continue
            quantities.append(
                {
                    "value": val,
                    "unit": unit[:32] or None,
                    "snippet": m.group(0)[:120],
                    "pattern": pat.pattern[:60],
                }
            )
            if len(quantities) >= 8:
                break
        if len(quantities) >= 8:
            break

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
            }
        )
        if len(line_items) >= 20:
            break

    primary = quantities[0] if quantities else None
    if primary and qty_kind == "unknown":
        qty_kind = "fixed"  # numeric qty without estimated/idiq markers → treat as defined
        evidence.append("numeric_quantity_without_estimated_markers")

    result: dict[str, Any] = {
        "quantity": str(int(primary["value"])) if primary and primary["value"] == int(primary["value"]) else (
            str(primary["value"]) if primary else None
        ),
        "quantity_unit": primary.get("unit") if primary else None,
        "quantity_kind": qty_kind if primary or qty_kind != "unknown" else "unknown",
        "quantity_evidence": evidence + ([primary["snippet"]] if primary else []),
        "quantity_candidates": quantities[:5],
        "line_items": line_items[:20],
        "quantity_extracted": bool(primary),
    }
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
    if qty.get("quantity_extracted") and not out.get("quantity"):
        out["quantity"] = qty["quantity"]
        out["quantity_unit"] = qty.get("quantity_unit")
        out["quantity_kind"] = qty.get("quantity_kind")
        out["quantity_evidence"] = list(qty.get("quantity_evidence") or []) + [f"source:{source}"]
        out["line_items"] = qty.get("line_items") or out.get("line_items")
    elif qty.get("quantity_kind") in {"indefinite", "estimated"} and not out.get("quantity_kind"):
        out["quantity_kind"] = qty["quantity_kind"]
        out["quantity_evidence"] = qty.get("quantity_evidence")
    for k, v in ident.items():
        if v and not out.get(k):
            out[k] = v
    out["_extraction"] = {
        "quantity_extracted": bool(qty.get("quantity_extracted")),
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
