"""General-purpose bid-schedule / pricing-table line-item extraction.

Handles PDF and portal tables where cells land on separate lines (multi-page
schedules, wrapped descriptions, split UOMs). Evidence-only: never invents
quantities or product identity. Not solicitation-specific.
"""

from __future__ import annotations

import re
from typing import Any

from m3.research.extract_fields import normalize_quantity_unit

# Header cells that mark a numbered pricing / bid table (any commodity/service).
_HEADER_LINE_ITEM = re.compile(r"(?i)^line\s*item$")
_HEADER_DESC = re.compile(r"(?i)^description$")
_HEADER_QTY = re.compile(r"(?i)^quantity$")
_HEADER_UOM = re.compile(r"(?i)^(?:unit(?:\s+of)?(?:\s+measure)?|uom|measure)$")

_ITEM_NUM = re.compile(r"^\d{1,4}[A-Za-z]?$")
_QTY_ONLY = re.compile(r"^[\d,]+(?:\.\d+)?$")
_PAGE_MARK = re.compile(r"(?i)^page\s+\d+\s*$")
_SECTION_STOP = re.compile(
    r"(?i)^(?:section\s+\d+|qq\s*#|total\s*$|please\s+provide|primary\s+responses|"
    r"the\s+vendor\s+must|bids?\s+must\s+be\s+submitted|base\s+bid\s*$|"
    r"modifications,\s*limitations|as\s+needed\s+services\s*$)"
)
_SKIP_CELL = re.compile(
    r"(?i)^(?:unit\s+cost|total|vendor\s*notes?|notes?|n/?a|—|-|–)?$"
)

# Fragments that compose a UOM when PDF splits "Per Replacement" / "Unit of Measure"
_UOM_FRAG = re.compile(
    r"(?i)^(?:"
    r"ea|each|gal(?:lon)?s?|lb|lbs|pounds?|ton(?:s)?|cy|yds?|ft|lf|sf|kit|kits|"
    r"lot|lots|set|sets|unit|units|hr|hrs|hours?|days?|weeks?|months?|yr|years?|"
    r"annual|annually|ls|lump(?:\s*sum)?|box(?:es)?|bx|cs|cases?|pk|pkg|pack(?:s|ages?)?|"
    r"drum(?:s)?|bag(?:s)?|pallet(?:s)?|sheet(?:s)?|"
    r"per|replacement|delivery|deliveries|occurrence|event|call|response|"
    r"month|weekly|daily|service|job|trip"
    r")$"
)

_EVAL_QTY = re.compile(
    r"(?i)\b(?:"
    r"evaluat(?:ion|e)|bid\s+evaluat|for\s+bidding\s+purposes|planning\s+allowance|"
    r"estimated\s+(?:annual\s+)?(?:quantity|usage|consumption)|index(?:\s+price)?|"
    r"fixed\s+markup|markup\s+over"
    r")\b"
)
_ANCILLARY = re.compile(
    r"(?i)\b(?:"
    r"fee|surcharge|after[- ]?hours|emergency(?:\s*/\s*|\s+)after|"
    r"additional\s+costs?|compatibility\s+limitations?"
    r")\b"
)
_SERVICE = re.compile(
    r"(?i)\b(?:"
    r"monitoring|service\s+coordination|maintenance|inspection|"
    r"replacement/?exchange|cylinder\s+exchange|modifications?"
    r")\b"
)
_PRODUCTISH_UOM = {
    "GAL", "TON", "LB", "LBS", "EA", "BX", "CS", "KIT", "LOT", "SET", "CY", "YD",
    "FT", "LF", "SF", "DRUM", "BAG", "PALLET",
}


def preserve_layout(text: str | None) -> str:
    """Keep newlines (table structure); collapse only horizontal whitespace."""
    if not text:
        return ""
    t = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(re.sub(r"[ \t]+", " ", ln).rstrip() for ln in t.split("\n")).strip()


def _nearest_page(lines: list[str], idx: int) -> int | None:
    for j in range(idx, max(-1, idx - 40), -1):
        m = re.search(r"(?i)\bpage\s+(\d+)\b", lines[j])
        if m:
            return int(m.group(1))
    return None


def _is_header_at(lines: list[str], i: int) -> int | None:
    """If lines[i] starts a Line Item/Description/Quantity/UOM header, return index after header."""
    if i >= len(lines) or not _HEADER_LINE_ITEM.match(lines[i]):
        return None
    window = lines[i : i + 12]
    has_desc = any(_HEADER_DESC.match(x) for x in window)
    has_qty = any(_HEADER_QTY.match(x) for x in window)
    has_uom = any(
        _HEADER_UOM.match(x) or x.lower() in {"unit", "of", "measure", "uom"}
        for x in window
    )
    # Split PDF headers: "Unit of" + "Measure" on consecutive lines
    if not has_uom:
        for a, b in zip(window, window[1:]):
            if a.lower() == "unit of" and b.lower() == "measure":
                has_uom = True
                break
            if a.lower() == "unit" and b.lower() in {"of", "measure"}:
                has_uom = True
                break
    if not (has_desc and has_qty and has_uom):
        return None
    j = i + 1
    while j < len(lines) and j < i + 14:
        cell = lines[j]
        if _ITEM_NUM.match(cell):
            return j
        if _HEADER_DESC.match(cell) or _HEADER_QTY.match(cell) or _HEADER_UOM.match(cell):
            j += 1
            continue
        if cell.lower() in {
            "unit", "of", "measure", "unit of", "unit cost", "total",
            "vendor", "notes", "vendor notes",
        }:
            j += 1
            continue
        if not cell or _SKIP_CELL.match(cell):
            j += 1
            continue
        break
    for k in range(i + 1, min(len(lines), i + 16)):
        if _ITEM_NUM.match(lines[k]):
            return k
    return None


def _looks_like_uom_start(lines: list[str], i: int) -> bool:
    if i >= len(lines) or not _QTY_ONLY.match(lines[i]):
        return False
    # Peek next non-empty
    for k in range(i + 1, min(len(lines), i + 5)):
        cell = lines[k]
        if not cell or _SKIP_CELL.match(cell) or _PAGE_MARK.match(cell):
            continue
        if _ITEM_NUM.match(cell) and not _UOM_FRAG.match(cell):
            return False
        if _UOM_FRAG.match(cell) or normalize_quantity_unit(cell):
            return True
        # Multi-token UOM already on one line
        if normalize_quantity_unit(cell) or normalize_quantity_unit(cell.replace("\n", " ")):
            return True
        # "Per Replacement" may be two lines; first fragment "Per"
        if _UOM_FRAG.match(cell):
            return True
        return False
    return False


def _consume_uom(lines: list[str], i: int) -> tuple[str | None, int]:
    parts: list[str] = []
    j = i
    while j < len(lines) and len(parts) < 4:
        cell = lines[j]
        if not cell or _SKIP_CELL.match(cell) or _PAGE_MARK.match(cell):
            j += 1
            if parts:
                break
            continue
        if _ITEM_NUM.match(cell) and not parts:
            break
        if _ITEM_NUM.match(cell) and parts:
            break
        if cell.upper() == "TOTAL" or _SECTION_STOP.match(cell):
            break
        # Stop if this looks like a new description (long prose) after we already have UOM
        if parts and len(cell) > 40 and not _UOM_FRAG.match(cell):
            break
        if _UOM_FRAG.match(cell) or (len(cell) <= 24 and normalize_quantity_unit(cell)):
            parts.append(cell)
            j += 1
            # After a complete canonical UOM, stop unless next is a continuation fragment
            joined = " ".join(parts)
            if normalize_quantity_unit(joined) and not (
                j < len(lines) and _UOM_FRAG.match(lines[j]) and parts[-1].lower() in {"per", "unit", "of", "lump"}
            ):
                # Allow "Per" + "Replacement"
                if parts[-1].lower() == "per" and j < len(lines) and _UOM_FRAG.match(lines[j]):
                    continue
                break
            continue
        # Single-line multi-word UOM
        if normalize_quantity_unit(cell):
            parts.append(cell)
            j += 1
            break
        break
    raw = " ".join(parts).strip() or None
    return raw, j


def _classify_kind(desc: str, uom: str | None) -> str:
    d = desc or ""
    u = (uom or "").upper()
    if _ANCILLARY.search(d) or u in {"PER DELIVERY", "PER CALL", "PER TRIP", "PER EVENT"}:
        return "ancillary_charge"
    if u in {"ANNUAL", "LS", "HR", "HOUR", "HOURS", "PER REPLACEMENT", "PER OCCURRENCE"}:
        return "service"
    if _SERVICE.search(d) and u not in _PRODUCTISH_UOM:
        return "service"
    if u in _PRODUCTISH_UOM or (u and u not in {"ANNUAL", "LS"}):
        return "product"
    if _SERVICE.search(d):
        return "service"
    return "product"


def _quantity_semantic(desc: str, kind: str) -> str:
    if _EVAL_QTY.search(desc or ""):
        return "EVALUATION_QUANTITY"
    if kind in {"service", "ancillary_charge"}:
        return "LINE_ITEM_QUANTITY"
    return "LINE_ITEM_QUANTITY"


def _parse_table_body(lines: list[str], start: int, *, page: int | None) -> tuple[list[dict[str, Any]], int]:
    items: list[dict[str, Any]] = []
    i = start
    while i < len(lines):
        cell = lines[i]
        if not cell or _PAGE_MARK.match(cell):
            i += 1
            continue
        if cell.upper() == "TOTAL" or _SECTION_STOP.match(cell):
            # End of this table section (another header may follow later)
            i += 1
            break
        # Nested header (next table on following page)
        hdr = _is_header_at(lines, i)
        if hdr is not None and items:
            break
        if not _ITEM_NUM.match(cell):
            i += 1
            continue
        # Avoid treating years / odd numbers outside table flow: require following description
        item_no = cell
        i += 1
        desc_parts: list[str] = []
        while i < len(lines):
            nxt = lines[i]
            if not nxt or _SKIP_CELL.match(nxt) or _PAGE_MARK.match(nxt):
                i += 1
                continue
            if nxt.upper() == "TOTAL" or _SECTION_STOP.match(nxt):
                break
            if _ITEM_NUM.match(nxt) and desc_parts and _looks_like_uom_start(lines, i) is False:
                # Next line item without qty — abort this incomplete row
                break
            if _looks_like_uom_start(lines, i):
                break
            if _ITEM_NUM.match(nxt) and not desc_parts:
                # Stray — no description yet
                break
            if _is_header_at(lines, i) is not None:
                break
            desc_parts.append(nxt)
            i += 1
            if len(desc_parts) > 40:
                break
        if not desc_parts:
            continue
        if i >= len(lines) or not _QTY_ONLY.match(lines[i]):
            continue
        qty_raw = lines[i]
        i += 1
        uom_raw, i = _consume_uom(lines, i)
        try:
            qty = float(qty_raw.replace(",", ""))
        except ValueError:
            continue
        uom = normalize_quantity_unit(uom_raw) if uom_raw else None
        # Multi-word UOMs not in short canon — keep uppercased source when grounded
        if uom is None and uom_raw:
            u_try = re.sub(r"\s+", " ", uom_raw).strip().lower()
            # Accept composed per-* and annual/ls after fragment join
            if u_try in {"per replacement", "per delivery", "per occurrence", "per call", "per trip", "per event",
                         "lump sum", "annual", "annually"}:
                uom = {
                    "per replacement": "PER REPLACEMENT",
                    "per delivery": "PER DELIVERY",
                    "per occurrence": "PER OCCURRENCE",
                    "per call": "PER CALL",
                    "per trip": "PER TRIP",
                    "per event": "PER EVENT",
                    "lump sum": "LS",
                    "annual": "ANNUAL",
                    "annually": "ANNUAL",
                }[u_try]
            elif normalize_quantity_unit(u_try.split()[0] if u_try else None):
                # First token only (shouldn't normally happen)
                uom = normalize_quantity_unit(u_try)
        desc = re.sub(r"\s+", " ", " ".join(desc_parts)).strip()
        kind = _classify_kind(desc, uom)
        span = f"{item_no} | {desc[:120]} | {qty_raw} | {uom_raw or ''}".strip()
        items.append(
            {
                "clin": f"Line Item {item_no}",
                "line_ref": item_no,
                "description": desc[:400] or None,
                "product_description": desc[:200] or None,
                "quantity": qty,
                "unit_of_measure": uom,
                "unit": uom,
                "uom_raw": uom_raw,
                "line_kind": kind,
                "quantity_semantic": _quantity_semantic(desc, kind),
                "complete": bool(desc and qty is not None and uom),
                "page": page,
                "snippet": span[:220],
                "source_passage": span[:220],
                "evidence": {
                    "quantity": f"{qty_raw} {uom_raw or ''}".strip(),
                    "description": desc[:180],
                    "table": "numbered_bid_schedule",
                    "page": page,
                },
                "semantic": "LINE_ITEM_QUANTITY",
                "counts_as_ordered": kind == "product" and _quantity_semantic(desc, kind) != "EVALUATION_QUANTITY",
            }
        )
    return items, i


def parse_bid_schedule_tables(text: str | None) -> list[dict[str, Any]]:
    """Extract priced schedule rows from numbered bid/pricing tables.

    Returns one dict per line item with description, quantity, UOM, kind, page,
    and evidence spans. Empty when no recognizable table header+rows exist.
    """
    layout = preserve_layout(text)
    if not layout:
        return []
    lines = layout.split("\n")
    found: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        after = _is_header_at(lines, i)
        if after is None:
            i += 1
            continue
        page = _nearest_page(lines, i)
        rows, nxt = _parse_table_body(lines, after, page=page)
        found.extend(rows)
        i = max(nxt, after + 1, i + 1)
    # Dedupe by line_ref + quantity + uom (same row repeated across page headers)
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for row in found:
        key = (
            str(row.get("line_ref")),
            row.get("quantity"),
            str(row.get("unit_of_measure") or ""),
            (row.get("description") or "")[:60].lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    # Prefer stable numeric order
    def _sort_key(r: dict[str, Any]) -> tuple[int, str]:
        ref = str(r.get("line_ref") or "")
        try:
            return (int(re.match(r"\d+", ref).group(0)), ref)  # type: ignore[union-attr]
        except (AttributeError, ValueError, TypeError):
            return (99999, ref)

    out.sort(key=_sort_key)
    return out
