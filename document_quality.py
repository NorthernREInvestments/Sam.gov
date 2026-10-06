"""Solicitation package document quality gate.

Rejects non-solicitation PDFs (W-9, strategic plans, marketing, etc.).
Does not invent matches — evidence must appear in document text/name.
"""

from __future__ import annotations

import re
from typing import Any

VALID_SOLICITATION_PACKAGE = "VALID_SOLICITATION_PACKAGE"
PARTIAL_SOLICITATION_PACKAGE = "PARTIAL_SOLICITATION_PACKAGE"
UNRELATED_DOCUMENTS = "UNRELATED_DOCUMENTS"
AMBIGUOUS_DOCUMENTS = "AMBIGUOUS_DOCUMENTS"

# Package-access phase aliases / finer labels
VALID_FREE_PACKAGE_FOUND = "VALID_FREE_PACKAGE_FOUND"
PARTIAL_FREE_PACKAGE_FOUND = "PARTIAL_FREE_PACKAGE_FOUND"
UNRELATED_DOCUMENT = "UNRELATED_DOCUMENT"
GENERIC_AGENCY_DOCUMENT = "GENERIC_AGENCY_DOCUMENT"
MARKETING_DOCUMENT = "MARKETING_DOCUMENT"
AMBIGUOUS_DOCUMENT = "AMBIGUOUS_DOCUMENT"

_JUNK = re.compile(
    r"\b("
    r"form\s*w-?9|w-9\b|request\s+for\s+taxpayer|"
    r"strategic\s+plan|comprehensive\s+plan|"
    r"sell\s+sheet|product\s+brochure|marketing\s+flyer|"
    r"vendor\s+registration\s+only|privacy\s+policy|"
    r"procurement\s+services\s+overview|co-?op\s+brochure|"
    r"company\s+overview|capabilities\s+statement"
    r")\b",
    re.I,
)

_SOLICITATION = re.compile(
    r"\b("
    r"invitation\s+to\s+bid|request\s+for\s+(?:bid|proposal|quote)|"
    r"solicitation|ITB|IFB|RFP|RFQ|"
    r"bid\s+(?:document|package|schedule|form|instructions)|"
    r"pricing\s+sheet|bid\s+sheet|line\s+item|bill\s+of\s+materials|"
    r"scope\s+of\s+work|specifications?|addend(?:a|um)|"
    r"unit\s+price|quantity|unit\s+of\s+measure|CLIN|"
    r"proposal\s+form|bid\s+opening|due\s+date|closing\s+date"
    r")\b",
    re.I,
)

_IDENTITY_MARKERS = re.compile(
    r"\b("
    r"solicitation\s*(?:no\.?|number|#)|bid\s*(?:no\.?|number|#)|"
    r"project\s*(?:no\.?|number|#)|requisition|"
    r"NSN|P/?N|MPN|SKU|model\s*#|part\s*number|"
    r"manufacturer|brand\s+name|or\s+equal"
    r")\b",
    re.I,
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _token_hit(needle: str, hay: str) -> bool:
    n = _norm(needle)
    if len(n) < 4:
        return False
    return n in _norm(hay)


def score_document_against_opportunity(
    *,
    document_name: str,
    document_text: str,
    title: str | None = None,
    buyer: str | None = None,
    solicitation_number: str | None = None,
) -> dict[str, Any]:
    """Score one document. Returns quality label + evidence flags."""
    blob = f"{document_name}\n{(document_text or '')[:120000]}"
    junk = bool(_JUNK.search(blob))
    sol = bool(_SOLICITATION.search(blob))
    identity = bool(_IDENTITY_MARKERS.search(blob))

    hits = {
        "solicitation_number": bool(
            solicitation_number and _token_hit(str(solicitation_number), blob)
        ),
        "buyer": bool(buyer and _token_hit(str(buyer), blob)),
        "title": bool(title and _token_hit(str(title)[:80], blob)),
        "solicitation_language": sol,
        "identity_markers": identity,
        "junk_language": junk,
        "text_chars": len((document_text or "").strip()),
    }
    evidence_count = sum(
        1
        for k in (
            "solicitation_number",
            "buyer",
            "title",
            "solicitation_language",
            "identity_markers",
        )
        if hits[k]
    )

    if junk and not sol and evidence_count <= 1:
        label = UNRELATED_DOCUMENTS
    elif hits["text_chars"] < 80 and not sol:
        label = AMBIGUOUS_DOCUMENTS
    elif evidence_count >= 3 or (sol and (hits["solicitation_number"] or hits["title"])):
        label = VALID_SOLICITATION_PACKAGE
    elif sol or (evidence_count >= 2 and not junk):
        label = PARTIAL_SOLICITATION_PACKAGE
    elif junk:
        label = UNRELATED_DOCUMENTS
    else:
        label = AMBIGUOUS_DOCUMENTS

    return {
        "document_name": (document_name or "")[:160],
        "quality": label,
        "evidence_hits": hits,
        "evidence_count": evidence_count,
        "usable": label in {VALID_SOLICITATION_PACKAGE, PARTIAL_SOLICITATION_PACKAGE},
    }


def classify_package_documents(
    documents: list[dict[str, Any]],
    *,
    title: str | None = None,
    buyer: str | None = None,
    solicitation_number: str | None = None,
) -> dict[str, Any]:
    """Aggregate quality across recovered package documents."""
    scored: list[dict[str, Any]] = []
    for d in documents or []:
        if not isinstance(d, dict):
            continue
        scored.append(
            score_document_against_opportunity(
                document_name=str(d.get("document_name") or d.get("name") or ""),
                document_text=str(d.get("text") or d.get("extracted_text") or ""),
                title=title,
                buyer=buyer,
                solicitation_number=solicitation_number,
            )
        )

    usable = [s for s in scored if s.get("usable")]
    unrelated = [s for s in scored if s.get("quality") == UNRELATED_DOCUMENTS]
    ambiguous = [s for s in scored if s.get("quality") == AMBIGUOUS_DOCUMENTS]
    valid = [s for s in scored if s.get("quality") == VALID_SOLICITATION_PACKAGE]
    partial = [s for s in scored if s.get("quality") == PARTIAL_SOLICITATION_PACKAGE]

    if valid:
        overall = VALID_SOLICITATION_PACKAGE
    elif partial:
        overall = PARTIAL_SOLICITATION_PACKAGE
    elif unrelated and not usable:
        overall = UNRELATED_DOCUMENTS
    elif scored:
        overall = AMBIGUOUS_DOCUMENTS
    else:
        overall = AMBIGUOUS_DOCUMENTS

    return {
        "package_quality": overall,
        "usable": overall in {VALID_SOLICITATION_PACKAGE, PARTIAL_SOLICITATION_PACKAGE},
        "documents_scored": len(scored),
        "valid_count": len(valid),
        "partial_count": len(partial),
        "unrelated_count": len(unrelated),
        "ambiguous_count": len(ambiguous),
        "documents": scored[:40],
    }


def extract_pdf_text(path: str, max_pages: int = 50) -> str:
    try:
        import fitz
    except Exception:
        return ""
    try:
        doc = fitz.open(path)
    except Exception:
        return ""
    parts: list[str] = []
    try:
        page_count = int(getattr(doc, "page_count", 0) or 0)
        limit = min(max_pages, page_count) if page_count > 0 else max_pages
        for i in range(limit):
            try:
                page = doc.load_page(i)
                parts.append(page.get_text("text") or "")
            except Exception:
                continue
    except Exception:
        return "\n".join(parts)
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return "\n".join(parts)
