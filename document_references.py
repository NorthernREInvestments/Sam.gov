"""Discover document references inside solicitation text — evidence only, no inference of existence."""

from __future__ import annotations

import re
from typing import Any

_REFERENCE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ATTACHMENT", re.compile(r"\bAttachment\s+([A-Z0-9][\w\-]*)\b", re.I)),
    ("EXHIBIT", re.compile(r"\bExhibit\s+([A-Z0-9][\w\-]*)\b", re.I)),
    ("APPENDIX", re.compile(r"\bAppendix\s+([A-Z0-9][\w\-]*)\b", re.I)),
    ("SCHEDULE", re.compile(r"\bSchedule\s+([A-Z0-9][\w\-]*)\b", re.I)),
    ("AMENDMENT", re.compile(r"\bAmendment\s+(?:No\.?\s*)?(\d+)\b", re.I)),
    ("SF30", re.compile(r"\bSF[\s\-]?30\b", re.I)),
    ("Q_AND_A", re.compile(r"\b(?:Q\s*&\s*A|Questions?\s+and\s+Answers?)\b", re.I)),
    ("SOW", re.compile(r"\b(?:Statement\s+of\s+Work|SOW)\b", re.I)),
    ("PWS", re.compile(r"\b(?:Performance\s+Work\s+Statement|PWS)\b", re.I)),
    ("PRICING_SHEET", re.compile(r"\b(?:Pricing\s+Sheet|price\s+schedule)\b", re.I)),
    ("BRAND_JUSTIFICATION", re.compile(r"\b(?:Brand[\s\-]?Name\s+Justification|J\s*&\s*A|Justification\s+and\s+Approval)\b", re.I)),
    ("SPECIFICATION", re.compile(r"\b(?:Technical\s+Specifications?|Specifications)\b", re.I)),
    ("CLIN", re.compile(r"\bCLIN\b", re.I)),
    ("SECTION", re.compile(r"\bSection\s+([B-M])\b", re.I)),
    ("OEM_LETTER", re.compile(r"\b(?:OEM\s+letter|manufacturer\s+letter|letter\s+of\s+authorization)\b", re.I)),
    ("SEPARATE_ATTACHMENT", re.compile(r"\b(?:see\s+attachment|see\s+exhibit|separate\s+attachment|attached\s+document|incorporated\s+by\s+reference)\b", re.I)),
    ("SPREADSHEET", re.compile(r"\b(?:spreadsheet|\.xlsx|\.xls|Excel)\b", re.I)),
]


def extract_document_references(
    text: str,
    *,
    source_document: str | None = None,
) -> list[dict[str, Any]]:
    """
    Return reference candidates with exact evidence snippets.
    Does NOT assert that a separate file exists — only that text references it.
    """
    if not text:
        return []
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref_type, pat in _REFERENCE_PATTERNS:
        for m in pat.finditer(text):
            start = max(0, m.start() - 100)
            end = min(len(text), m.end() + 100)
            snippet = text[start:end].replace("\n", " ").strip()
            label = m.group(0)
            key = f"{ref_type}:{label.lower()}:{m.start()}"
            if key in seen:
                continue
            seen.add(key)
            # In-document section headers (Contents B/C/D...) are not external missing files
            in_document_section = bool(
                re.search(r"(?i)contents|section\s+[b-f]\.|^\s*[B-F]\.\s+", snippet)
            ) or (ref_type == "SECTION")
            external_likely = ref_type in {
                "ATTACHMENT",
                "EXHIBIT",
                "SF30",
                "Q_AND_A",
                "SPREADSHEET",
                "SEPARATE_ATTACHMENT",
            } and not in_document_section
            # Appendix inside same RFQ body often in-document
            if ref_type == "APPENDIX" and "APPENDIX" in text[m.start() : m.start() + 200].upper():
                # Check if followed by inline content vs "see attached"
                following = text[m.end() : m.end() + 80].lower()
                if "see attach" not in following and ".pdf" not in following:
                    external_likely = False
                    in_document_section = True

            found.append(
                {
                    "reference_type": ref_type,
                    "matched_text": label,
                    "capture": m.group(1) if m.lastindex else None,
                    "evidence_snippet": snippet,
                    "source_document": source_document,
                    "char_offset": m.start(),
                    "exists_as_separate_file": None,  # unknown — do not infer
                    "in_document_likely": in_document_section,
                    "external_file_likely": external_likely,
                    "required_for_package_completeness": False,  # set only with stronger evidence
                    "verification_status": "EVIDENCE_OF_REFERENCE_ONLY",
                }
            )
    return found


def required_external_references(references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only refs that look like separate required files — still not proof they exist on SAM."""
    out = []
    for r in references or []:
        if r.get("external_file_likely") and not r.get("in_document_likely"):
            # Explicit attachment letter forms are strongest
            if r.get("reference_type") in {"ATTACHMENT", "EXHIBIT", "SF30", "Q_AND_A", "SPREADSHEET"}:
                rr = dict(r)
                rr["required_for_package_completeness"] = True
                out.append(rr)
    return out
