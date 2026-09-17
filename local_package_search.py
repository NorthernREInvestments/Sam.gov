"""Deterministic exhaustive local solicitation-package search (no paid AI)."""

from __future__ import annotations

import re
from typing import Any

from missing_info import (
    MATCH_DIRECT,
    MATCH_NONE,
    MATCH_POSSIBLE_INDIRECT,
    MATCH_RELATED,
    search_terms_for,
)

# Patterns that suggest a quantity near a memory/storage keyword
_QTY_NEAR = re.compile(
    r"(?i)(?:qty|quantity|x\s*|×\s*|#\s*)?\s*(\d{1,3})\s*(?:x|×|ea|each|modules?|dimms?|drives?|disks?)?",
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


def collect_local_corpus(session: Any, contract_id: int) -> list[dict[str, Any]]:
    """Gather searchable local documents: attachments, solicitation docs, rolled-up text."""
    from models import Contract, ContractAttachment, SolicitationDocument

    corpus: list[dict[str, Any]] = []
    contract = session.query(Contract).filter_by(id=contract_id).first()
    if contract and contract.attachment_text:
        corpus.append(
            {
                "doc_id": f"contract_rollupext_{contract_id}",
                "document_type": "attachment_rollup",
                "filename": "(contract.attachment_text)",
                "source": "gt_contracts.attachment_text",
                "text": contract.attachment_text,
            }
        )
    atts = session.query(ContractAttachment).filter_by(contract_id=contract_id).all()
    for a in atts:
        text = a.extracted_text or ""
        corpus.append(
            {
                "doc_id": f"attachment_{a.id}",
                "document_type": "attachment",
                "filename": a.filename,
                "source": a.source or "local_attachment",
                "local_attachment_id": a.id,
                "text": text,
                "metadata": {
                    "content_type": a.content_type,
                    "file_size_bytes": a.file_size_bytes,
                    "downloaded_at": a.downloaded_at.isoformat() if a.downloaded_at else None,
                },
            }
        )
    docs = session.query(SolicitationDocument).filter_by(contract_id=contract_id).all()
    for d in docs:
        text = ""
        if d.local_attachment_id:
            att = session.query(ContractAttachment).filter_by(id=d.local_attachment_id).first()
            if att and att.extracted_text:
                text = att.extracted_text
        # Metadata-only docs still counted as "checked"
        corpus.append(
            {
                "doc_id": f"solicitation_doc_{d.id}",
                "document_type": d.document_type,
                "filename": d.filename,
                "source": d.source,
                "text": text,
                "metadata": {
                    "version_amendment": d.version_amendment,
                    "current": d.current,
                    "superseded": d.superseded,
                    "review_status": d.review_status,
                },
                "checked_even_if_empty": True,
            }
        )
    return corpus


def _snippet(text: str, idx: int, width: int = 120) -> str:
    start = max(0, idx - width // 2)
    end = min(len(text), idx + width // 2)
    return text[start:end].replace("\n", " ")


def search_term_in_text(term: str, text: str) -> list[dict[str, Any]]:
    """Case-insensitive substring search — never invents values."""
    if not term or not text:
        return []
    matches = []
    lower = text.lower()
    needle = term.lower()
    start = 0
    while True:
        idx = lower.find(needle, start)
        if idx < 0:
            break
        matches.append(
            {
                "term": term,
                "index": idx,
                "snippet": _snippet(text, idx),
            }
        )
        start = idx + max(1, len(needle))
        if len(matches) >= 20:
            break
    return matches


def classify_match(
    *,
    fact_key: str,
    term: str,
    snippet: str,
) -> str:
    """Heuristic match class — POSSIBLE_INDIRECT blocks CO escalation."""
    sn = _norm(snippet)
    fk = fact_key.lower()
    term_l = term.lower()

    # Direct: term + nearby digit quantity for memory/storage facts
    if any(x in fk for x in ("memory", "storage", "drive", "dimm", "quantity")):
        if term_l in sn and _QTY_NEAR.search(snippet or ""):
            # Still not proof of the exact answer — related unless clear "per server"
            if "per server" in sn or "each server" in sn or "per unit" in sn:
                return MATCH_DIRECT
            return MATCH_RELATED
        if term_l in sn:
            return MATCH_POSSIBLE_INDIRECT

    if any(x in fk for x in ("install", "freight", "fob")):
        if term_l in sn:
            # Presence of word is related, not confirmation of requirement value
            if any(w in sn for w in ("not required", "n/a", "not applicable", "excluded")):
                return MATCH_RELATED
            return MATCH_POSSIBLE_INDIRECT

    if term_l in sn:
        return MATCH_RELATED
    return MATCH_NONE


def exhaustive_local_search(
    *,
    fact_key: str,
    description: str,
    corpus: list[dict[str, Any]],
    extra_terms: list[str] | None = None,
) -> dict[str, Any]:
    """
    Search all local docs with synonyms.
    A single failed exact-string search is NEVER proof of absence.
    """
    terms = search_terms_for(fact_key, extra_terms)
    documents_checked: list[dict[str, Any]] = []
    all_hits: list[dict[str, Any]] = []
    match_classes: list[str] = []

    for doc in corpus:
        text = doc.get("text") or ""
        doc_hits = []
        for term in terms:
            for m in search_term_in_text(term, text):
                klass = classify_match(fact_key=fact_key, term=term, snippet=m["snippet"])
                hit = {
                    **m,
                    "match_class": klass,
                    "doc_id": doc.get("doc_id"),
                    "document_type": doc.get("document_type"),
                    "filename": doc.get("filename"),
                }
                doc_hits.append(hit)
                all_hits.append(hit)
                match_classes.append(klass)
        documents_checked.append(
            {
                "doc_id": doc.get("doc_id"),
                "document_type": doc.get("document_type"),
                "filename": doc.get("filename"),
                "source": doc.get("source"),
                "text_present": bool(text.strip()),
                "text_length": len(text),
                "hit_count": len(doc_hits),
                "checked": True,
                "metadata": doc.get("metadata"),
            }
        )

    has_direct = MATCH_DIRECT in match_classes
    has_related = MATCH_RELATED in match_classes
    has_indirect = MATCH_POSSIBLE_INDIRECT in match_classes
    docs_with_text = sum(1 for d in documents_checked if d["text_present"])
    terms_with_hits = {h["term"] for h in all_hits}

    if has_direct:
        overall = MATCH_DIRECT
    elif has_related:
        overall = MATCH_RELATED
    elif has_indirect:
        overall = MATCH_POSSIBLE_INDIRECT
    else:
        overall = MATCH_NONE

    return {
        "fact_key": fact_key,
        "description": description,
        "search_terms": terms,
        "terms_with_hits": sorted(terms_with_hits),
        "terms_without_hits": [t for t in terms if t not in terms_with_hits],
        "documents_checked": documents_checked,
        "documents_checked_count": len(documents_checked),
        "documents_with_text_count": docs_with_text,
        "matches": all_hits[:50],  # cap stored matches
        "match_count": len(all_hits),
        "overall_match_class": overall,
        "possible_indirect_remaining": has_indirect and not has_direct,
        "single_failed_exact_search_is_not_proof": True,
        "exhaustive_local": True if docs_with_text > 0 else False,
        "LIVE_API_REQUESTS": 0,
    }
