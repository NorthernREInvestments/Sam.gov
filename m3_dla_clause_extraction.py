"""BUILD 12 — Deep DLA packaging / inspection clause extraction.

Evidence-backed extraction only. Does not invent standards, infer requirements,
change scoring, or replace m3_document_intelligence / dla_product_extract signals.

Every requirement stores: source document, page/section (if available),
extracted text, confidence, timestamp.
"""

from __future__ import annotations

import re
from typing import Any

from application_clock import now_utc

BUILD_TAG = "20260918-m3-dla-clause-extraction-1"

ST_UNKNOWN = "UNKNOWN"
ST_DETECTED = "DETECTED"
ST_RESEARCH = "RESEARCH_REQUIRED"
ST_VALIDATED = "VALIDATED"
ST_SATISFIED = "SATISFIED"
ST_BLOCKED = "BLOCKED"

# Pattern catalog: only emit when match exists in source text (never invent).
# Each entry: (field_key, category, label, regex, confidence_if_match)
_CLAUSE_PATTERNS: list[tuple[str, str, str, re.Pattern[str], str]] = [
    # Packaging / packing / preservation / marking
    (
        "mil_std_2073",
        "PACKAGING",
        "MIL-STD-2073 packaging",
        re.compile(r"\bMIL[\s-]?STD[\s-]?2073(?:[\s\-_/]?\d+[A-Z]?)?\b", re.I),
        "HIGH",
    ),
    (
        "mil_std_129",
        "MARKING",
        "MIL-STD-129 marking",
        re.compile(r"\bMIL[\s-]?STD[\s-]?129(?:[\s\-_/]?\d+[A-Z]?)?\b", re.I),
        "HIGH",
    ),
    (
        "astm_packaging",
        "PACKAGING",
        "ASTM packaging reference",
        re.compile(r"\bASTM\s+[A-Z]?\s?\d{2,5}(?:[\-/][A-Z0-9]+)?\b", re.I),
        "HIGH",
    ),
    (
        "commercial_packaging",
        "PACKAGING",
        "Commercial packaging",
        re.compile(
            r"\b(?:commercial\s+packaging|ASTM\s+D3951|best\s+commercial\s+practice)\b",
            re.I,
        ),
        "MEDIUM",
    ),
    (
        "preservation_method",
        "PACKAGING",
        "Preservation method",
        re.compile(
            r"(?:preservation(?:\s+method)?|method\s+of\s+preservation)\s*[:#]?\s*"
            r"([A-Z0-9][A-Z0-9\s,./\-]{2,80})",
            re.I,
        ),
        "MEDIUM",
    ),
    (
        "packaging_level",
        "PACKAGING",
        "Packaging level",
        re.compile(
            r"(?:packaging\s+level|level\s+of\s+packaging|pack\s+level)\s*[:#]?\s*"
            r"(A|B|C|military|commercial|level\s+[ABC])\b",
            re.I,
        ),
        "MEDIUM",
    ),
    (
        "packing_requirements",
        "PACKAGING",
        "Packing requirements",
        re.compile(
            r"(?:packing(?:\s+requirements?)?|unit\s+pack|intermediate\s+pack|"
            r"exterior\s+pack)\s*[:#]?\s*([^\n.]{5,120})",
            re.I,
        ),
        "MEDIUM",
    ),
    (
        "marking_requirements",
        "MARKING",
        "Marking requirements",
        re.compile(
            r"(?:marking(?:\s+requirements?)?|bar\s*code\s+marking|UID\s+marking|"
            r"IUID)\s*[:#]?\s*([^\n.]{5,120})",
            re.I,
        ),
        "MEDIUM",
    ),
    (
        "labeling_requirements",
        "MARKING",
        "Labeling requirements",
        re.compile(r"(?:label(?:ing|ling)?(?:\s+requirements?)?)\s*[:#]?\s*([^\n.]{5,100})", re.I),
        "MEDIUM",
    ),
    (
        "pallet_requirements",
        "PACKAGING",
        "Pallet requirements",
        re.compile(
            r"\b(?:pallet(?:ized|ization|s)?|unitization)\b[^\n.]{0,100}",
            re.I,
        ),
        "MEDIUM",
    ),
    (
        "special_packaging",
        "PACKAGING",
        "Special packaging instructions",
        re.compile(
            r"(?:special\s+packaging(?:\s+instructions?)?|SPI\s*[:#]?\s*\w+|"
            r"cFolders?|TDMT)\b[^\n.]{0,120}",
            re.I,
        ),
        "MEDIUM",
    ),
    # Inspection / acceptance / FAT
    (
        "inspection_location",
        "INSPECTION",
        "Inspection location",
        re.compile(
            r"inspection\s+(?:at|point|location|shall\s+be\s+(?:at|performed\s+at))\s*[:#]?\s*"
            r"(origin|destination|source|destination\s+acceptance|"
            r"[A-Za-z0-9 ,.\-/]{3,80})",
            re.I,
        ),
        "HIGH",
    ),
    (
        "inspection_party",
        "INSPECTION",
        "Inspection party",
        re.compile(
            r"(?:inspection\s+(?:by|performed\s+by)|DCMA|government\s+inspection)\s*"
            r"([^\n.]{0,80})",
            re.I,
        ),
        "MEDIUM",
    ),
    (
        "acceptance_location",
        "ACCEPTANCE",
        "Acceptance location",
        re.compile(
            r"acceptance\s+(?:at|point|location|shall\s+be\s+at)\s*[:#]?\s*"
            r"(origin|destination|source|[A-Za-z0-9 ,.\-/]{3,80})",
            re.I,
        ),
        "HIGH",
    ),
    (
        "acceptance_authority",
        "ACCEPTANCE",
        "Acceptance authority",
        re.compile(
            r"(?:acceptance\s+(?:by|authority)|accepted\s+by)\s*[:#]?\s*([^\n.]{3,80})",
            re.I,
        ),
        "MEDIUM",
    ),
    (
        "fat_required",
        "INSPECTION",
        "First Article Test (FAT)",
        re.compile(r"\b(?:FAT|first\s+article\s+test(?:ing)?)\b[^\n.]{0,100}", re.I),
        "HIGH",
    ),
    (
        "fat_waiver",
        "INSPECTION",
        "FAT waiver",
        re.compile(r"(?:FAT\s+waiver|waiver\s+of\s+FAT|first\s+article\s+waiver)[^\n.]{0,80}", re.I),
        "HIGH",
    ),
    (
        "test_requirements",
        "INSPECTION",
        "Test requirements",
        re.compile(
            r"(?:test(?:ing)?\s+requirements?|qualification\s+test|"
            r"conformance\s+inspection)[^\n.]{0,100}",
            re.I,
        ),
        "MEDIUM",
    ),
    (
        "certification_requirements",
        "CERTIFICATIONS",
        "Certification requirements",
        re.compile(
            r"(?:certificate\s+of\s+conformance|CoC\b|certification\s+required|"
            r"quality\s+assurance\s+provision)[^\n.]{0,100}",
            re.I,
        ),
        "MEDIUM",
    ),
    # Delivery
    (
        "fob_terms",
        "DELIVERY",
        "FOB terms",
        re.compile(r"\bFOB\s*[:#]?\s*(ORIGIN|DESTINATION)\b", re.I),
        "HIGH",
    ),
    (
        "delivery_destination",
        "DELIVERY",
        "Delivery destination",
        re.compile(
            r"(?:deliver(?:y)?\s+to|ship\s+to|destination)\s*[:#]?\s*"
            r"([A-Za-z0-9 ,.\-/#]{5,120})",
            re.I,
        ),
        "MEDIUM",
    ),
    (
        "delivery_schedule",
        "DELIVERY",
        "Delivery schedule",
        re.compile(
            r"(?:delivery\s+(?:within|in|schedule|date)|days?\s+ARO|"
            r"required\s+delivery)\s*[:#]?\s*([^\n.]{3,80})",
            re.I,
        ),
        "MEDIUM",
    ),
    (
        "shipping_instructions",
        "FREIGHT",
        "Shipping instructions",
        re.compile(
            r"(?:shipping\s+instructions?|freight\s+(?:prepaid|collect)|"
            r"transportation\s+charges)[^\n.]{0,100}",
            re.I,
        ),
        "MEDIUM",
    ),
]

# Section headers often used in DLA solicitations (for page/section hint)
_SECTION_RE = re.compile(
    r"(?:^|\n)\s*((?:SECTION|PART|PARA(?:GRAPH)?|CLAUSE)\s+[A-Z0-9.\-]+|"
    r"Section\s+[A-Z]|Packaging|Inspection\s+and\s+Acceptance|"
    r"Delivery|Marking)[^\n]{0,60}",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _context_snippet(text: str, start: int, end: int, *, radius: int = 80) -> str:
    a = max(0, start - radius)
    b = min(len(text), end + radius)
    snip = text[a:b].replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", snip).strip()[:240]


def _section_hint(text: str, pos: int) -> str | None:
    """Best-effort section label preceding match position."""
    window = text[max(0, pos - 800) : pos + 1]
    matches = list(_SECTION_RE.finditer(window))
    if not matches:
        return None
    label = matches[-1].group(1).strip()
    return label[:80] if label else None


def _page_hint(doc: dict[str, Any], text: str, pos: int) -> Any:
    if doc.get("page") not in (None, ""):
        return doc.get("page")
    if doc.get("page_number") not in (None, ""):
        return doc.get("page_number")
    # Form-feed page breaks
    prefix = text[:pos]
    if "\f" in prefix:
        return prefix.count("\f") + 1
    return "UNKNOWN"


def collect_document_texts(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Gather evidence text sources from pipeline row — no network."""
    sources: list[dict[str, Any]] = []
    for d in row.get("documents") or []:
        if not isinstance(d, dict):
            continue
        text = _clean(d.get("extracted_text") or d.get("text_preview") or d.get("text") or d.get("content"))
        if not text:
            continue
        sources.append(
            {
                "source_document": d.get("filename")
                or d.get("document_name")
                or d.get("document_id")
                or d.get("source_url")
                or "document",
                "document_type": d.get("document_type") or "UNKNOWN",
                "page": d.get("page") or d.get("page_number"),
                "text": text[:200000],
            }
        )
    for key, label in (
        ("governing_text", "governing_text"),
        ("solicitation_text", "solicitation_text"),
        ("attachment_text", "attachment_text"),
        ("evidence_text_excerpt", "evidence_text_excerpt"),
        ("package_text", "package_text"),
        ("recovered_description", "recovered_description"),
        ("description", "description"),
        ("description_text", "description_text"),
    ):
        text = _clean(row.get(key))
        if text and len(text) >= 20:
            sources.append(
                {
                    "source_document": label,
                    "document_type": "INLINE",
                    "page": "UNKNOWN",
                    "text": text[:200000],
                }
            )
    # Deduplicate identical text blobs
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for s in sources:
        h = s["text"][:200]
        if h in seen:
            continue
        seen.add(h)
        unique.append(s)
    return unique


def _evidence(
    *,
    source_document: str,
    page: Any,
    section: Any,
    extracted_text: str,
    confidence: str,
    match_span: tuple[int, int] | None = None,
) -> dict[str, Any]:
    return {
        "source_document": source_document or "UNKNOWN",
        "page": page if page not in (None, "") else "UNKNOWN",
        "section": section if section not in (None, "") else "UNKNOWN",
        "extracted_text": (extracted_text or "")[:240],
        "confidence": confidence or "UNKNOWN",
        "timestamp": _utc(),
        "match_span": list(match_span) if match_span else None,
    }


def extract_clauses_from_text(
    text: str,
    *,
    source_document: str = "UNKNOWN",
    document_type: str = "UNKNOWN",
    page: Any = None,
) -> list[dict[str, Any]]:
    """Extract evidenced clauses from a single text blob. No inference."""
    if not text or not str(text).strip():
        return []
    blob = str(text)
    out: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for field_key, category, label, pattern, conf in _CLAUSE_PATTERNS:
        m = pattern.search(blob)
        if not m:
            continue
        dedupe = f"{category}|{field_key}|{m.group(0).upper()[:60]}"
        if dedupe in seen_keys:
            continue
        seen_keys.add(dedupe)

        extracted = _context_snippet(blob, m.start(), m.end())
        # Prefer capturing group value when present and meaningful
        if m.lastindex and m.group(1):
            val = m.group(1).strip()
            if len(val) >= 2:
                extracted = f"{m.group(0)[:80]} → {val}"[:240]

        section = _section_hint(blob, m.start())
        page_val = page if page not in (None, "") else _page_hint({"page": page}, blob, m.start())

        # Specific standard / location match → VALIDATED; generic keyword → DETECTED
        specific = field_key in {
            "mil_std_2073",
            "mil_std_129",
            "astm_packaging",
            "fob_terms",
            "inspection_location",
            "acceptance_location",
            "fat_required",
            "fat_waiver",
        }
        status = ST_VALIDATED if specific and conf == "HIGH" else ST_DETECTED
        if conf == "MEDIUM" and specific:
            status = ST_DETECTED

        out.append(
            {
                "kind": "M3DlaClauseRequirement",
                "field_key": field_key,
                "category": category,
                "requirement": label,
                "status": status,
                "value": (m.group(1).strip() if m.lastindex and m.group(1) else m.group(0).strip())[:120],
                "evidence": _evidence(
                    source_document=source_document,
                    page=page_val,
                    section=section,
                    extracted_text=extracted,
                    confidence=conf,
                    match_span=(m.start(), m.end()),
                ),
                "document_type": document_type,
                "missing_action": _action_for(category, status),
                "fabricated": False,
            }
        )
    return out


def _action_for(category: str, status: str) -> str:
    actions = {
        "PACKAGING": "Review packaging / preservation / packing instructions",
        "MARKING": "Review marking / labeling requirements",
        "INSPECTION": "Confirm inspection point, tests, and FAT",
        "ACCEPTANCE": "Confirm acceptance location and authority",
        "DELIVERY": "Confirm delivery destination, schedule, and FOB",
        "FREIGHT": "Clarify shipping / freight instructions",
        "CERTIFICATIONS": "Obtain required certifications / CoC",
    }
    if status == ST_VALIDATED:
        return f"Apply evidenced {category.lower().replace('_', ' ')} requirement"
    return actions.get(category, "Research requirement")


def extract_dla_clauses(
    row: dict[str, Any] | None,
    *,
    texts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Build evidence-backed DLA clause extraction bundle.

    Missing documents → UNKNOWN (no invented MIL-STD).
    """
    row = row if isinstance(row, dict) else {}
    sources = texts if texts is not None else collect_document_texts(row)

    clauses: list[dict[str, Any]] = []
    for src in sources:
        if not isinstance(src, dict):
            continue
        clauses.extend(
            extract_clauses_from_text(
                str(src.get("text") or ""),
                source_document=str(src.get("source_document") or "UNKNOWN"),
                document_type=str(src.get("document_type") or "UNKNOWN"),
                page=src.get("page"),
            )
        )

    # Dedupe by category+field_key keeping highest status
    rank = {ST_VALIDATED: 3, ST_DETECTED: 2, ST_RESEARCH: 1, ST_UNKNOWN: 0}
    best: dict[str, dict[str, Any]] = {}
    for c in clauses:
        key = f"{c.get('category')}|{c.get('field_key')}"
        prev = best.get(key)
        if not prev or rank.get(c.get("status"), 0) >= rank.get(prev.get("status"), 0):
            best[key] = c
    clauses = list(best.values())

    by_category: dict[str, list[dict[str, Any]]] = {}
    for c in clauses:
        by_category.setdefault(str(c.get("category")), []).append(c)

    has_docs = bool(sources)
    # Category rollups for Offer Readiness
    category_status: dict[str, dict[str, Any]] = {}
    for cat in ("PACKAGING", "MARKING", "INSPECTION", "ACCEPTANCE", "DELIVERY", "FREIGHT", "CERTIFICATIONS"):
        items = by_category.get(cat) or []
        if items:
            if any(i.get("status") == ST_VALIDATED for i in items):
                st = ST_VALIDATED
            else:
                st = ST_DETECTED
            evidence = items[0].get("evidence")
            category_status[cat] = {
                "status": st,
                "count": len(items),
                "requirements": items,
                "evidence": evidence,
                "missing_action": _action_for(cat, st),
            }
        else:
            category_status[cat] = {
                "status": ST_UNKNOWN if not has_docs else ST_UNKNOWN,
                "count": 0,
                "requirements": [],
                "evidence": {
                    "source_document": "UNKNOWN",
                    "page": "UNKNOWN",
                    "section": "UNKNOWN",
                    "extracted_text": "UNKNOWN",
                    "confidence": "UNKNOWN",
                    "timestamp": _utc(),
                    "reason": "no_matching_clause_in_available_text"
                    if has_docs
                    else "no_document_text_available",
                },
                "missing_action": "Recover governing documents"
                if not has_docs
                else "No evidenced clause found — do not invent standards",
            }

    return {
        "kind": "M3DlaClauseExtraction",
        "build": BUILD_TAG,
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "document_source_count": len(sources),
        "clause_count": len(clauses),
        "clauses": clauses,
        "by_category": by_category,
        "category_status": category_status,
        "fabricated_requirements": False,
        "inferred_standards": False,
        "engines_unchanged": True,
        "document_intelligence_unchanged": True,
        "scoring_unchanged": True,
        "generated_at": _utc(),
        "read_only": True,
    }


def clauses_to_offer_requirements(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Map extraction bundle → Offer Readiness requirement rows."""
    out: list[dict[str, Any]] = []
    for c in bundle.get("clauses") or []:
        if not isinstance(c, dict):
            continue
        ev = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
        evidence_str = (
            f"{ev.get('source_document')}"
            f" · page {ev.get('page')}"
            f" · {ev.get('section')}"
            f" · \"{ev.get('extracted_text')}\""
        )
        status = str(c.get("status") or ST_DETECTED)
        # VALIDATED = evidence complete for offer readiness; DETECTED = soft research
        if status == ST_VALIDATED:
            btype = "COMPLETE"
            mapped = ST_VALIDATED
        else:
            btype = "SOFT_BLOCKER"
            mapped = ST_DETECTED
        out.append(
            {
                "kind": "M3OfferRequirement",
                "category": c.get("category"),
                "requirement": c.get("requirement"),
                "status": mapped,
                "evidence": evidence_str,
                "missing_action": c.get("missing_action"),
                "blocker_type": btype,
                "mandatory": False,
                "source": "dla_clause_extraction",
                "raw_status": status,
                "field_key": c.get("field_key"),
                "clause_evidence": ev,
            }
        )
    return out


def clauses_to_research_items(bundle: dict[str, Any], *, opportunity_id: str) -> list[dict[str, Any]]:
    """Soft research hints for DETECTED (not yet VALIDATED) clauses."""
    items = []
    for c in bundle.get("clauses") or []:
        if not isinstance(c, dict):
            continue
        if c.get("status") != ST_DETECTED:
            continue
        items.append(
            {
                "opportunity_id": opportunity_id,
                "research_type": "DOCUMENT_REVIEW",
                "status": "NEW",
                "priority": 45,
                "priority_label": "MEDIUM",
                "why_this_matters": f"{c.get('requirement')}: clause detected, details may need confirmation",
                "recommended_action": c.get("missing_action") or "Review clause in governing document",
                "missing_information": [c.get("field_key") or c.get("category")],
                "category": c.get("category"),
                "source": "dla_clause_extraction",
            }
        )
    return items[:15]


def attach_clauses_to_row(row: dict[str, Any], *, persist: bool = False) -> dict[str, Any]:
    """Attach extraction to a copy of the row (optional in-memory persist flag)."""
    bundle = extract_dla_clauses(row)
    out = dict(row)
    out["dla_clause_extraction"] = bundle
    return out
