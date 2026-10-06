"""Package quality gate + document inventory for recovered packages."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from application_clock import now_utc

_LANDING_REJECT = re.compile(
    r"(register|login|sign[\s-]?in|home\s*page|welcome|portal\s*home|vendor\s*registration)",
    re.I,
)
_GOOD_DOC = re.compile(
    r"\.(pdf|xlsx?|docx?|csv|zip)(\?|$)|solicitation|specification|bid\s*form|"
    r"pricing|price\s*schedule|attachment|addendum|amendment|ITB|RFP|RFQ|IFB",
    re.I,
)


def classify_doc_type(name: str, url: str = "") -> str:
    blob = f"{name} {url}".lower()
    if any(k in blob for k in ("amend", "addendum")):
        return "amendment"
    if any(k in blob for k in ("price", "pricing", "schedule", "bid tab", "xlsx", "xls")):
        return "pricing_sheet"
    if any(k in blob for k in ("spec", "scope", "technical")):
        return "specification"
    if any(k in blob for k in ("form", "certif", "affidavit", "w-9", "signature")):
        return "bid_form"
    if blob.endswith(".pdf") or ".pdf" in blob:
        return "solicitation_pdf"
    return "attachment"


def is_valid_package_evidence(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Reject landing/registration-only pages; require real solicitation evidence."""
    if not documents:
        return {"valid": False, "reason": "no_documents", "accepted": [], "rejected": []}
    accepted = []
    rejected = []
    for d in documents:
        name = str(d.get("document_name") or d.get("filename") or d.get("name") or "")
        url = str(d.get("document_url") or d.get("url") or d.get("source_url") or "")
        blob = f"{name} {url}"
        if _LANDING_REJECT.search(blob) and not _GOOD_DOC.search(blob):
            rejected.append({**d, "reject_reason": "landing_or_registration_page"})
            continue
        if _GOOD_DOC.search(blob) or d.get("local_path") or d.get("content_hash"):
            dtype = classify_doc_type(name, url)
            accepted.append({**d, "document_type": dtype})
        else:
            # Keep HTML-ish only if marked as attachment with body
            if d.get("bytes") or d.get("size_bytes"):
                accepted.append({**d, "document_type": classify_doc_type(name, url)})
            else:
                rejected.append({**d, "reject_reason": "notice_or_landing_only"})
    return {
        "valid": len(accepted) >= 1,
        "accepted": accepted,
        "rejected": rejected,
        "counts": {
            "solicitation_pdf": sum(1 for d in accepted if d.get("document_type") == "solicitation_pdf"),
            "pricing_sheets": sum(1 for d in accepted if d.get("document_type") == "pricing_sheet"),
            "specifications": sum(1 for d in accepted if d.get("document_type") == "specification"),
            "bid_forms": sum(1 for d in accepted if d.get("document_type") == "bid_form"),
            "amendments": sum(1 for d in accepted if d.get("document_type") == "amendment"),
            "invalid_landing_rejected": len(rejected),
        },
    }


def inventory_documents(opportunity_id: str, documents: list[dict[str, Any]], *, source_url: str | None = None) -> list[dict[str, Any]]:
    out = []
    for d in documents:
        path = d.get("local_path")
        content_hash = d.get("content_hash")
        if path and Path(path).exists() and not content_hash:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            content_hash = h.hexdigest()
        out.append(
            {
                "opportunity_id": opportunity_id,
                "filename": d.get("document_name") or d.get("filename") or d.get("name"),
                "document_type": d.get("document_type") or classify_doc_type(str(d.get("document_name") or ""), str(d.get("document_url") or "")),
                "content_hash": content_hash,
                "content_hash_algorithm": "SHA-256" if content_hash else None,
                "source_url": d.get("document_url") or d.get("url") or source_url,
                "source_url_status": "PRESENT" if (d.get("document_url") or d.get("url") or source_url) else "SOURCE_URL_UNAVAILABLE",
                "amendment": d.get("document_type") == "amendment",
                "retrieved_at": d.get("retrieved_at") or now_utc().isoformat(),
                "size_bytes": d.get("size_bytes"),
            }
        )
    return out
