"""Phase J — deterministic product identity extraction and confidence."""

from __future__ import annotations

import re
from typing import Any

BUILD_TAG = "20260925-m3-phase-j-identity-1"

# Confidence hierarchy
LEVEL_EXACT = "EXACT"  # Level 1
LEVEL_STRONG = "STRONG"  # Level 2
LEVEL_PARTIAL = "PARTIAL"  # Level 3
LEVEL_UNKNOWN = "UNKNOWN"  # Level 4

# Map to Phase H readiness vocabulary
MAP_TO_PHASE_H = {
    LEVEL_EXACT: "EXACT_CONFIRMED",
    LEVEL_STRONG: "STRONG_MATCH",
    LEVEL_PARTIAL: "PARTIAL",
    LEVEL_UNKNOWN: "UNKNOWN",
}

_NSN_RE = re.compile(r"\b(\d{4}-\d{2}-\d{3}-\d{4})\b")
_NSN_COMPACT_RE = re.compile(r"\bNSN\s*[:#]?\s*(\d{13})\b", re.I)
_PN_RE = re.compile(
    r"\b(?:P/?N|PART\s*NUMBER|MFR\s*PART\s*NUMBER|MPN)\s*[:#=\s]+\s*([A-Z0-9][A-Z0-9._/-]{2,})\b",
    re.I,
)
_PN_INLINE_RE = re.compile(r"\bP/?N\s*[:#=]\s*([A-Z0-9][A-Z0-9._/-]{2,})\b", re.I)
_CAGE_RE = re.compile(r"\b(?:MFR\s*)?CAGE\s*[:#]?\s*([A-Z0-9]{5})\b", re.I)
_DRAWING_RE = re.compile(r"\b(?:DWG|DRAWING)\s*(?:NO\.?|NUMBER)?\s*[:#]?\s*([A-Z0-9][A-Z0-9._/-]{2,})\b", re.I)

_ABBREV = {
    "ASSY": "ASSEMBLY",
    "ASM": "ASSEMBLY",
    "PNL": "PANEL",
    "ELEC": "ELECTRICAL",
    "ELECT": "ELECTRICAL",
    "HYD": "HYDRAULIC",
    "HYDRAUL": "HYDRAULIC",
    "PNEU": "PNEUMATIC",
    "PNEUM": "PNEUMATIC",
    "CONTR": "CONTROL",
    "CTRL": "CONTROL",
    "ACTUA": "ACTUATOR",
    "ACTUAT": "ACTUATOR",
    "COMPR": "COMPRESSOR",
    "RECIPROCATIN": "RECIPROCATING",
    "SEAL REPLACE": "SEAL REPLACEMENT",
}


def normalize_identifier(raw: str | None) -> str | None:
    if not raw:
        return None
    s = str(raw).strip().upper()
    s = re.sub(r"\s+", "", s)
    s = s.replace("_", "-")
    return s or None


def normalize_nsn(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 13:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:9]}-{digits[9:13]}"
    m = _NSN_RE.search(str(raw))
    return m.group(1) if m else None


def normalize_nomenclature(title: str | None) -> str:
    """Supportive title normalize — never sole identity proof."""
    t = str(title or "")
    t = re.sub(r"^\d{2}--+", "", t)  # FSC-- prefix
    t = t.replace(",", " ")
    t = re.sub(r"[()]", " ", t)
    for abbr, full in _ABBREV.items():
        t = re.sub(rf"\b{re.escape(abbr)}\b", full, t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip().upper()
    return t


def extract_nsns(*texts: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if not text:
            continue
        for m in _NSN_RE.finditer(text):
            n = m.group(1)
            if n not in seen:
                seen.add(n)
                found.append(n)
        for m in _NSN_COMPACT_RE.finditer(text):
            n = normalize_nsn(m.group(1))
            if n and n not in seen:
                seen.add(n)
                found.append(n)
    return found


def extract_mpns(*texts: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if not text:
            continue
        for rx in (_PN_RE, _PN_INLINE_RE):
            for m in rx.finditer(text):
                raw = m.group(1).rstrip(".,;)")
                norm = normalize_identifier(raw)
                if norm and norm not in seen and len(norm) >= 3:
                    # Reject pure NSNs mistaken as PN
                    if _NSN_RE.fullmatch(raw.replace(" ", "")):
                        continue
                    seen.add(norm)
                    found.append(raw)
    return found


def extract_cages(*texts: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if not text:
            continue
        for m in _CAGE_RE.finditer(text):
            c = m.group(1).upper()
            if c not in seen:
                seen.add(c)
                found.append(c)
    return found


def build_product_identity(
    row: dict[str, Any] | None = None,
    *,
    text: str | None = None,
    extracted_facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    title = str(row.get("title") or "")
    desc = str(
        text
        or row.get("description")
        or row.get("solicitation_text")
        or ""
    )
    facts_blob = ""
    if isinstance(extracted_facts, dict):
        facts_blob = json_dumps_safe(extracted_facts)

    nsns = extract_nsns(title, desc, facts_blob, str(row.get("nsn") or ""))
    mpns = extract_mpns(title, desc, facts_blob)
    cages = extract_cages(title, desc, facts_blob)
    drawings = []
    for m in _DRAWING_RE.finditer(f"{title}\n{desc}"):
        drawings.append(m.group(1))

    primary_nsn = nsns[0] if nsns else None
    primary_mpn = mpns[0] if mpns else None
    nomenclature = normalize_nomenclature(title)
    fsc = primary_nsn[:4] if primary_nsn else None
    if not fsc and re.match(r"^\d{2}--", title):
        fsc = title[:2] + "XX"  # weak FSC hint only

    provenance: list[dict[str, str]] = []
    if primary_nsn and primary_nsn in title:
        provenance.append({"field": "nsn", "source": "notice_title"})
    elif primary_nsn and primary_nsn in desc:
        provenance.append({"field": "nsn", "source": "notice_description"})
    if primary_mpn:
        provenance.append({"field": "mpn", "source": "title_or_description"})

    if primary_nsn:
        level = LEVEL_EXACT
        reason = "exact_nsn"
    elif primary_mpn and cages:
        level = LEVEL_EXACT
        reason = "exact_mpn_plus_cage"
    elif primary_mpn:
        level = LEVEL_STRONG
        reason = "exact_mpn_manufacturer_missing"
    elif drawings:
        level = LEVEL_STRONG
        reason = "drawing_reference"
    elif nomenclature and len(nomenclature) >= 12 and not re.search(r"WHEEL ASSEMBLY|PARTS KIT", nomenclature):
        level = LEVEL_PARTIAL
        reason = "nomenclature_only"
    elif nomenclature:
        level = LEVEL_PARTIAL if "ASSEMBLY" in nomenclature or "KIT" in nomenclature else LEVEL_UNKNOWN
        reason = "truncated_or_generic_nomenclature"
    else:
        level = LEVEL_UNKNOWN
        reason = "insufficient_identity"

    # Truncated FSC-- titles without NSN stay UNKNOWN/PARTIAL — never EXACT
    if re.match(r"^\d{2}--+", title) and not primary_nsn and not primary_mpn:
        level = LEVEL_UNKNOWN
        reason = "truncated_fsc_title_no_nsn_mpn"

    return {
        "kind": "ProductIdentity",
        "build": BUILD_TAG,
        "nsn": primary_nsn,
        "nsns": nsns,
        "mpn": primary_mpn,
        "mpns": mpns,
        "manufacturer": None,  # not invented
        "oem": None,
        "brand": None,
        "model": primary_mpn,
        "nomenclature": nomenclature or None,
        "fsc": fsc,
        "psc": None,
        "cage": cages[0] if cages else None,
        "cages": cages,
        "drawing_number": drawings[0] if drawings else None,
        "specification_number": None,
        "alternate_part_numbers": mpns[1:],
        "superseded_part_number": None,
        "substitute_relationship": None,
        "assembly_relationship": None,
        "kit_relationship": "KIT" if nomenclature and "KIT" in nomenclature else None,
        "identity_level": level,
        "identity_confidence": MAP_TO_PHASE_H[level],
        "identity_reason": reason,
        "provenance": provenance,
        "raw_title": title[:200],
        "normalized_mpn": normalize_identifier(primary_mpn),
        "normalized_nsn": normalize_nsn(primary_nsn),
    }


def json_dumps_safe(obj: Any) -> str:
    import json

    try:
        return json.dumps(obj, default=str)[:8000]
    except Exception:
        return str(obj)[:8000]
