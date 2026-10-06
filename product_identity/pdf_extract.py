"""PDF purchasing-line extraction — prefer table-like rows over boilerplate."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from document_quality import extract_pdf_text

_OR_EQUAL = re.compile(
    r"\b(brand\s+or\s+equal|or\s+approved\s+equal|or\s+equivalent|or\s+equal|basis\s+of\s+design)\b",
    re.I,
)
_LINE = re.compile(
    r"^\s*(?P<item>\d{1,4}[A-Za-z]?|[A-Z]?\d{1,4})\s+"
    r"(?P<desc>.+?)\s+"
    r"(?P<qty>\d[\d,]*(?:\.\d+)?)\s+"
    r"(?P<uom>EA|EACH|CS|CASE|BX|BOX|PK|PACK|FT|LF|GAL|LB|SET|KIT|PAIR)?\b",
    re.I,
)
_QTY_UOM = re.compile(
    r"\b(?P<qty>\d[\d,]*(?:\.\d+)?)\s*(?P<uom>EA|EACH|CS|CASE|BX|BOX|PK|PACK|FT|LF|GAL|LB|SET|KIT)\b",
    re.I,
)
_LABELED = re.compile(
    r"\b(?:MPN|P/?N|PART\s*NO\.?|MODEL(?:\s*NO\.?)?|CAT(?:ALOG)?\s*NO\.?)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-./]{2,})\b",
    re.I,
)
_MFG_LINE = re.compile(
    r"^\s*(?:manufacturer|mfr|brand|make)\s*[:\-]\s*(.+)$",
    re.I,
)
_BOILER = re.compile(
    r"\b(terms\s+and\s+conditions|indemnif|insurance\s+requirements|page\s+\d+\s+of)\b",
    re.I,
)


def extract_pdf_rows(path: Path, *, max_pages: int = 40) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (pricing_like_rows, spec_context_rows)."""
    text = extract_pdf_text(str(path), max_pages=max_pages)
    if not text:
        return [], []
    pricing: list[dict[str, Any]] = []
    specs: list[dict[str, Any]] = []
    inherited_mfr: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) < 6 or _BOILER.search(line):
            continue
        mfg_m = _MFG_LINE.match(line)
        if mfg_m:
            cand = mfg_m.group(1).strip()[:80]
            # Reject prose / boilerplate captured as manufacturer
            if (
                2 <= len(cand) <= 48
                and len(cand.split()) <= 5
                and not re.search(r"\b(shall|must|requirement|solicitation|policy|insurance)\b", cand, re.I)
            ):
                inherited_mfr = cand
            continue
        equal = bool(_OR_EQUAL.search(line))
        # Ignore equal language in insurance / legal boilerplate
        if equal and re.search(r"\b(insurance|liability|indemnif|coverage|policy\s+limit)\b", line, re.I):
            equal = False
        labeled = _LABELED.search(line)
        lm = _LINE.match(line)
        if lm:
            pricing.append(
                {
                    "item": lm.group("item"),
                    "description": lm.group("desc").strip()[:500],
                    "quantity": lm.group("qty"),
                    "uom": lm.group("uom"),
                    "manufacturer": inherited_mfr,
                    "inherited_manufacturer": bool(inherited_mfr),
                    "part_number": labeled.group(1) if labeled else None,
                    "equal_allowed": equal,
                    "_source_path": str(path),
                    "_sheet": "pdf",
                }
            )
            continue
        qm = _QTY_UOM.search(line)
        if labeled or (qm and len(line) >= 20):
            pn = labeled.group(1) if labeled else None
            pricing.append(
                {
                    "item": None,
                    "description": line[:500],
                    "quantity": qm.group("qty") if qm else None,
                    "uom": qm.group("uom") if qm else None,
                    "manufacturer": inherited_mfr,
                    "inherited_manufacturer": bool(inherited_mfr),
                    "part_number": pn,
                    "model": pn if labeled and "MODEL" in labeled.group(0).upper() else None,
                    "equal_allowed": equal,
                    "_source_path": str(path),
                    "_sheet": "pdf",
                }
            )
            continue
        if equal or (inherited_mfr and _LABELED.search(line)):
            specs.append(
                {
                    "description": line[:800],
                    "manufacturer": inherited_mfr,
                    "equal_allowed": equal,
                    "part_number": labeled.group(1) if labeled else None,
                    "_source_path": str(path),
                    "_kind": "spec_context",
                }
            )
    return pricing, specs
