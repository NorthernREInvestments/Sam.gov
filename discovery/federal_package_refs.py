"""Classify Federal/DLA package and document references — no access bypass."""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import urlparse

from federal_dla_product_constants import (
    REF_AMENDMENT,
    REF_ATTACHMENT,
    REF_AUTH_REQUIRED,
    REF_BOT_BLOCKED,
    REF_CLAUSE_DOCUMENT,
    REF_CONTROLLED_DATA,
    REF_DRAWING_REFERENCE,
    REF_EXTERNAL_PORTAL,
    REF_PACKAGING_REFERENCE,
    REF_SAM_DESCRIPTION,
    REF_SAM_RESOURCE,
    REF_SOLICITATION_DOCUMENT,
    REF_SPECIFICATION_REFERENCE,
    REF_SUBMISSION_PORTAL,
    REF_TECHNICAL_DATA,
    REF_UNKNOWN,
    TECH_AUTH_REQUIRED,
    TECH_CONTROLLED,
    TECH_DIBBS_REF,
    TECH_EXTERNAL_SOURCE,
    TECH_JCP_REQUIRED,
    TECH_NO_GOVT_DATA,
    TECH_PUBLIC_AVAILABLE,
    TECH_PUBLIC_PACKAGE,
    TECH_TDMT_REF,
    TECH_UNKNOWN,
)

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def classify_reference_url(url: str, *, kind_hint: str | None = None) -> str:
    u = (url or "").strip()
    if not u:
        return REF_UNKNOWN
    low = u.lower()
    hint = (kind_hint or "").lower()
    host = _host(u)
    if "dibbs" in host or "dibbs" in low:
        return REF_EXTERNAL_PORTAL
    if "tdmt" in low or "technical data management" in low:
        return REF_TECHNICAL_DATA
    if any(x in low for x in ("jcp", "export.?control", "controlled.?unclassified", "/cui")):
        return REF_CONTROLLED_DATA
    if "piee" in host or "eb.mil" in host:
        return REF_SUBMISSION_PORTAL if "sol" in low or "offer" in low else REF_EXTERNAL_PORTAL
    if hint in {"sam_description", "description"}:
        return REF_SAM_DESCRIPTION
    if hint in {"sam_resource", "sam_link"}:
        return REF_SAM_RESOURCE
    if "amend" in low:
        return REF_AMENDMENT
    if any(x in low for x in ("drawing", "dwg", ".dwg")):
        return REF_DRAWING_REFERENCE
    if any(x in low for x in ("mil-std-2073", "mil-std-129", "packag", "preservation")):
        return REF_PACKAGING_REFERENCE
    if any(x in low for x in ("spec", "standard", "astm", "mil-prf", "mil-dtl")):
        return REF_SPECIFICATION_REFERENCE
    if any(x in low for x in ("clause", "far ", "dfars")):
        return REF_CLAUSE_DOCUMENT
    if any(x in low for x in (".pdf", "attachment", "document", "solicitation")):
        return REF_SOLICITATION_DOCUMENT if "solicit" in low else REF_ATTACHMENT
    if "sam.gov" in host and "noticedesc" in low:
        return REF_SAM_DESCRIPTION
    if "sam.gov" in host:
        return REF_SAM_RESOURCE
    return REF_UNKNOWN


def extract_urls_from_text(text: str) -> list[str]:
    return list(dict.fromkeys(_URL_RE.findall(text or "")))


def enumerate_package_references(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Deduped package/document references from notice metadata + description."""
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(url: str, *, kind_hint: str | None = None, source: str = "metadata") -> None:
        u = (url or "").strip()
        if not u or u in seen:
            return
        seen.add(u)
        klass = classify_reference_url(u, kind_hint=kind_hint)
        refs.append(
            {
                "url": u,
                "reference_class": klass,
                "source": source,
                "url_hash": hashlib.sha256(u.encode()).hexdigest()[:16],
                "host": _host(u),
            }
        )

    for link in row.get("document_links") or []:
        if isinstance(link, dict) and link.get("url"):
            _add(str(link["url"]), kind_hint=str(link.get("kind") or ""), source="document_links")
    for key in ("detail_url", "source_url"):
        if row.get(key):
            _add(str(row[key]), kind_hint="sam_ui", source=key)
    desc = str(row.get("description") or "")
    for u in extract_urls_from_text(desc):
        _add(u, source="description_body")
    meta = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}
    for key in ("resourceLinks", "additionalInfoLink", "uiLink"):
        val = meta.get(key)
        if isinstance(val, list):
            for u in val:
                if u:
                    _add(str(u), kind_hint="sam_resource", source=key)
        elif isinstance(val, str) and val.startswith("http"):
            _add(val, kind_hint="sam_resource", source=key)
    return refs


def classify_technical_data_state(row: dict[str, Any], refs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    refs = refs if refs is not None else enumerate_package_references(row)
    blob = f"{row.get('title') or ''}\n{row.get('description') or ''}".lower()
    classes = {r.get("reference_class") for r in refs}
    hosts = " ".join(str(r.get("host") or "") for r in refs)
    state = TECH_UNKNOWN
    reasons: list[str] = []
    if "dibbs" in hosts or "dibbs" in blob:
        state = TECH_DIBBS_REF
        reasons.append("dibbs_reference")
    if "tdmt" in blob or any("tdmt" in str(r.get("url") or "").lower() for r in refs):
        state = TECH_TDMT_REF
        reasons.append("tdmt_reference")
    if REF_CONTROLLED_DATA in classes or re.search(r"\bjcp\b|export control|controlled technical", blob):
        state = TECH_CONTROLLED if "jcp" not in blob else TECH_JCP_REQUIRED
        reasons.append("controlled_or_jcp")
    if re.search(r"no government[-\s]?furnished|no technical data|vendor[-\s]?furnished data", blob):
        state = TECH_NO_GOVT_DATA
        reasons.append("no_government_technical_data")
    if REF_AUTH_REQUIRED in classes or REF_BOT_BLOCKED in classes:
        state = TECH_AUTH_REQUIRED
        reasons.append("auth_or_bot")
    if any(
        c in classes
        for c in {REF_SOLICITATION_DOCUMENT, REF_ATTACHMENT, REF_SAM_DESCRIPTION, REF_SPECIFICATION_REFERENCE}
    ):
        if state in {TECH_UNKNOWN, TECH_DIBBS_REF}:
            state = TECH_PUBLIC_PACKAGE if REF_SOLICITATION_DOCUMENT in classes else TECH_PUBLIC_AVAILABLE
            reasons.append("public_refs_present")
    if any(c in classes for c in {REF_EXTERNAL_PORTAL, REF_SUBMISSION_PORTAL}) and state == TECH_UNKNOWN:
        state = TECH_EXTERNAL_SOURCE
        reasons.append("external_portal")
    return {
        "technical_data_state": state,
        "reasons": reasons,
        "reference_count": len(refs),
        "dibbs_reference": any("dibbs" in str(r.get("host") or "") for r in refs) or "dibbs" in blob,
        "tdmt_reference": "tdmt" in blob or any("tdmt" in str(r.get("url") or "").lower() for r in refs),
    }
