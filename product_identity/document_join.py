"""Join pricing lines with specification / brand-or-equal context from package docs."""

from __future__ import annotations

import re
from typing import Any

_OR_EQUAL = re.compile(
    r"\b(brand\s+or\s+equal|or\s+approved\s+equal|or\s+equivalent|or\s+equal|basis\s+of\s+design)\b",
    re.I,
)
_ITEM_REF = re.compile(r"\b(?:item|line|clin)\s*#?\s*([0-9A-Z]{1,8})\b", re.I)
_MODEL = re.compile(
    r"\b(?:model|p/?n|part\s*no\.?|catalog)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-./]{2,})\b",
    re.I,
)
_MFG = re.compile(
    r"\b(?:manufacturer|mfr|brand|make)\s*[:#]?\s*([A-Za-z][A-Za-z0-9 &\-.]{1,50})\b",
    re.I,
)


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{3,}", (s or "").lower()) if t not in {"the", "and", "for", "with"}}


def join_specs_into_lines(
    lines: list[dict[str, Any]],
    spec_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Enrich pricing lines using separate specification document context."""
    if not lines or not spec_rows:
        return lines

    # Index specs by item number and keep free-text blobs
    by_item: dict[str, list[dict[str, Any]]] = {}
    free: list[dict[str, Any]] = []
    for s in spec_rows:
        desc = str(s.get("description") or "")
        m = _ITEM_REF.search(desc)
        if m:
            by_item.setdefault(m.group(1).upper(), []).append(s)
        else:
            free.append(s)

    for line in lines:
        item = str(line.get("item") or "").strip().upper()
        desc = str(line.get("description") or line.get("raw_description") or "")
        hits: list[dict[str, Any]] = []
        if item and item in by_item:
            hits.extend(by_item[item])
        # Token overlap fallback
        if not hits:
            lt = _tokens(desc)
            if lt:
                scored = []
                for s in free[:400]:
                    st = _tokens(str(s.get("description") or ""))
                    if not st:
                        continue
                    ov = len(lt & st) / max(1, len(lt))
                    if ov >= 0.35:
                        scored.append((ov, s))
                scored.sort(key=lambda x: -x[0])
                hits = [s for _, s in scored[:2]]

        if not hits:
            continue

        enriched = False
        for s in hits:
            sdesc = str(s.get("description") or "")
            if not line.get("manufacturer"):
                m = _MFG.search(sdesc)
                if m:
                    line["manufacturer"] = m.group(1).strip()
                    line["inherited_manufacturer"] = True
                    enriched = True
                elif s.get("manufacturer"):
                    line["manufacturer"] = s.get("manufacturer")
                    line["inherited_manufacturer"] = True
                    enriched = True
            if not line.get("model") and not line.get("part_number"):
                m = _MODEL.search(sdesc)
                if m:
                    label = m.group(0).upper()
                    if "MODEL" in label:
                        line["model"] = m.group(1)
                    else:
                        line["part_number"] = m.group(1)
                    enriched = True
            if _OR_EQUAL.search(sdesc) or s.get("equal_allowed"):
                line["equal_allowed"] = True
                line["brand_or_equal_context"] = True
                enriched = True
            # Append salient bits
            if enriched and sdesc:
                prev = str(line.get("description") or "")
                if sdesc[:120] not in prev:
                    line["description"] = (prev + " | SPEC: " + sdesc[:240]).strip(" |")[:700]
        if enriched:
            line["enriched_from_spec"] = True
    return lines
