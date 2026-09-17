"""Structured local evidence extraction — tables/CLINs/adjacent quantities without invention."""

from __future__ import annotations

import re
from typing import Any

from data_integrity import STATUS_ASSESSMENT, STATUS_CALCULATED, STATUS_UNKNOWN, STATUS_VERIFIED

# Row-like: Description ... Part ... Qty  OR Description \n value \n part \n qty
_LINE_QTY = re.compile(
    r"(?P<label>[^\n]{3,80}?)\n(?P<value>[^\n]{3,120}?)\n(?P<part>\d{3}-[A-Z0-9]{4})\n(?P<qty>\d{1,5})\b",
    re.I,
)
# Flattened single-line style from PDF extract
_FLAT_QTY = re.compile(
    r"(?P<label>Memory Capacity|Hard Drives|Deployment Services|Shipping(?:\s+Material)?|"
    r"Power Cords|Memory DIMM Type[^\n]{0,20}|Memory Configuration Type)"
    r"[\s\S]{0,120}?"
    r"(?P<value>16GB RDIMM[^\n]{0,60}|2\.4TB[^\n]{0,80}|On-Site Installation Declined|"
    r"6400MT/s RDIMMs|Performance Optimized|PowerEdge[^\n]{0,60}|Power Cord[^\n]{0,80})?"
    r"[\s\S]{0,80}?"
    r"(?P<part>\d{3}-[A-Z0-9]{4})\s+(?P<qty>\d{1,5})\b",
    re.I,
)


def _snippet(text: str, start: int, end: int, pad: int = 40) -> str:
    a = max(0, start - pad)
    b = min(len(text), end + pad)
    return text[a:b].replace("\n", " | ")


def extract_bom_line_candidates(text: str, *, source_document: str | None = None) -> list[dict[str, Any]]:
    """Extract configuration line candidates with part numbers and qty columns."""
    if not text:
        return []
    candidates: list[dict[str, Any]] = []
    for pat in (_LINE_QTY, _FLAT_QTY):
        for m in pat.finditer(text):
            gd = m.groupdict()
            label = (gd.get("label") or "").strip()
            value = (gd.get("value") or "").strip()
            part = (gd.get("part") or "").strip()
            qty_s = (gd.get("qty") or "").strip()
            try:
                qty = int(qty_s)
            except ValueError:
                continue
            # Reject absurd association: qty must be plausible for RFQ scale
            if qty <= 0 or qty > 10000:
                continue
            field = "unknown_line"
            conf = "MEDIUM"
            if re.search(r"memory capacity|16gb rdimm", f"{label} {value}", re.I):
                field = "memory_module_total_qty"
                conf = "HIGH"
            elif re.search(r"hard drives|2\.4tb", f"{label} {value}", re.I):
                field = "storage_drive_total_qty"
                conf = "HIGH"
            elif re.search(r"installation declined|deployment services", f"{label} {value}", re.I):
                field = "installation_declined"
                conf = "HIGH"
            elif re.search(r"shipping", label, re.I):
                field = "shipping_sku_line"
                conf = "MEDIUM"
            candidates.append(
                {
                    "document": source_document,
                    "matched_text": m.group(0)[:300],
                    "surrounding_context": _snippet(text, m.start(), m.end()),
                    "candidate_field": field,
                    "candidate_value": {
                        "label": label,
                        "description": value,
                        "part_number": part,
                        "qty_column": qty,
                    },
                    "confidence": conf,
                    "verification_status": STATUS_ASSESSMENT,
                    "notes": "Qty column from configuration table — not yet promoted",
                }
            )
    # Deduplicate by part+qty+field
    seen: set[str] = set()
    out = []
    for c in candidates:
        v = c["candidate_value"]
        k = f"{c['candidate_field']}:{v.get('part_number')}:{v.get('qty_column')}"
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    return out


def resolve_quantity_from_candidates(
    candidates: list[dict[str, Any]],
    *,
    field: str,
    server_qty: int | None = None,
) -> dict[str, Any]:
    """
    Promote only with sufficient evidence.
    Nearby unrelated numbers never become quantity.
    """
    hits = [c for c in candidates if c.get("candidate_field") == field and c.get("confidence") == "HIGH"]
    if not hits:
        return {
            "status": STATUS_UNKNOWN,
            "value": None,
            "verification_status": STATUS_UNKNOWN,
            "reason": "no_high_confidence_candidate",
        }
    # Prefer unique qty
    qtys = {int(h["candidate_value"]["qty_column"]) for h in hits}
    if len(qtys) != 1:
        return {
            "status": "SOURCE_CONFLICT",
            "value": None,
            "verification_status": "SOURCE_CONFLICT",
            "reason": "conflicting_qty_candidates",
            "candidates": hits,
        }
    total = next(iter(qtys))
    evidence = hits[0]
    result: dict[str, Any] = {
        "total_qty": {
            "value": total,
            "status": STATUS_VERIFIED,
            "evidence": evidence,
            "source_field": "configuration_table_qty_column",
        },
        "verification_status": STATUS_VERIFIED,
    }
    if server_qty and server_qty > 0 and total % server_qty == 0:
        per = total // server_qty
        result["per_server_qty"] = {
            "value": per,
            "status": STATUS_CALCULATED,
            "formula": f"{total}/{server_qty}",
            "inputs": {"total_qty": total, "server_qty": server_qty},
        }
    elif server_qty and server_qty > 0:
        result["per_server_qty"] = {
            "value": None,
            "status": STATUS_UNKNOWN,
            "reason": "total_not_divisible_by_server_qty",
        }
    return result


def resolve_installation_from_candidates(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    hits = [c for c in candidates if c.get("candidate_field") == "installation_declined"]
    if not hits:
        return {"status": STATUS_UNKNOWN, "installation_required": None}
    h = hits[0]
    blob = " ".join(
        [
            str((h.get("candidate_value") or {}).get("description") or ""),
            str((h.get("candidate_value") or {}).get("label") or ""),
            str(h.get("matched_text") or ""),
            str(h.get("surrounding_context") or ""),
        ]
    )
    if re.search(r"installation\s+declined", blob, re.I):
        return {
            "status": STATUS_VERIFIED,
            "installation_required": False,
            "cost_status": "NOT_APPLICABLE",
            "evidence": h,
            "verification_status": STATUS_VERIFIED,
        }
    return {"status": STATUS_UNKNOWN, "installation_required": None, "evidence": h}


def nearby_number_is_not_automatic_qty(
    *,
    term: str,
    nearby_number: int,
    has_part_number_row: bool,
    label_matches_field: bool,
) -> bool:
    """Guard: unrelated nearby number cannot become quantity."""
    if not has_part_number_row or not label_matches_field:
        return True  # blocked from becoming qty
    return False
