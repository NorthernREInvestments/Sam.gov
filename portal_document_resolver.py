"""Portal document resolver — detail/package URL → validated document bytes.

Distinguishes DETAIL_PAGE vs DOCUMENT_ENDPOINT vs ACTUAL_DOCUMENT_BYTES.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Protocol

from application_clock import now_utc

log = logging.getLogger("govtracker.portal_resolver")

# Explicit retrieval failure taxonomy
HTTP_403 = "HTTP_403"
HTTP_404 = "HTTP_404"
HTTP_429 = "HTTP_429"
REDIRECT_LOOP = "REDIRECT_LOOP"
LOGIN_REQUIRED = "LOGIN_REQUIRED"
REGISTRATION_REQUIRED = "REGISTRATION_REQUIRED"
MFA_REQUIRED = "MFA_REQUIRED"
BOT_CHALLENGE = "BOT_CHALLENGE"
ATTACHMENT_ID_UNRESOLVED = "ATTACHMENT_ID_UNRESOLVED"
DOWNLOAD_ENDPOINT_UNRESOLVED = "DOWNLOAD_ENDPOINT_UNRESOLVED"
SIGNED_URL_EXPIRED = "SIGNED_URL_EXPIRED"
INVALID_DOCUMENT_BYTES = "INVALID_DOCUMENT_BYTES"
LOGIN_HTML_INSTEAD_OF_DOCUMENT = "LOGIN_HTML_INSTEAD_OF_DOCUMENT"
UNSUPPORTED_DOCUMENT_TYPE = "UNSUPPORTED_DOCUMENT_TYPE"
PARSE_FAILED = "PARSE_FAILED"
PACKAGE_ROUTE_NOT_FOUND = "PACKAGE_ROUTE_NOT_FOUND"
AUTHORITATIVE_SOURCE_NOT_FOUND = "AUTHORITATIVE_SOURCE_NOT_FOUND"
SESSION_REQUIRED = "SESSION_REQUIRED"
OTHER_EXPLICIT_REASON = "OTHER_EXPLICIT_REASON"
DOCUMENT_BYTES_RECOVERED = "DOCUMENT_BYTES_RECOVERED"

# Package completeness
NO_PACKAGE = "NO_PACKAGE"
DETAIL_ONLY = "DETAIL_ONLY"
PARTIAL_PACKAGE = "PARTIAL_PACKAGE"
GOVERNING_SOLICITATION_RECOVERED = "GOVERNING_SOLICITATION_RECOVERED"
SPECIFICATIONS_RECOVERED = "SPECIFICATIONS_RECOVERED"
PRICING_SCHEDULE_RECOVERED = "PRICING_SCHEDULE_RECOVERED"
BOM_RECOVERED = "BOM_RECOVERED"
COMPLETE_ENOUGH_FOR_RESEARCH = "COMPLETE_ENOUGH_FOR_RESEARCH"
PACKAGE_COMPLETE = "PACKAGE_COMPLETE"
AUTH_BLOCKED = "AUTH_BLOCKED"
UNRESOLVED = "UNRESOLVED"

DETAIL_PAGE = "DETAIL_PAGE"
DOCUMENT_ENDPOINT = "DOCUMENT_ENDPOINT"
ACTUAL_DOCUMENT_BYTES = "ACTUAL_DOCUMENT_BYTES"


def _utc() -> str:
    return now_utc().isoformat()


def detect_file_format(content: bytes, content_type: str | None = None) -> dict[str, Any]:
    """Validate bytes via magic signature + headers."""
    ct = (content_type or "").lower()
    if not content:
        return {"ok": False, "format": None, "reason": INVALID_DOCUMENT_BYTES, "detail": "empty_body"}
    head = content[:16]
    if head.startswith(b"%PDF"):
        return {"ok": True, "format": "PDF", "reason": DOCUMENT_BYTES_RECOVERED}
    if head.startswith(b"PK\x03\x04"):
        # ZIP / DOCX / XLSX
        name = "ZIP"
        if b"word/" in content[:2000] or "wordprocessingml" in ct:
            name = "DOCX"
        elif b"xl/" in content[:2000] or "spreadsheetml" in ct:
            name = "XLSX"
        return {"ok": True, "format": name, "reason": DOCUMENT_BYTES_RECOVERED}
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return {"ok": True, "format": "XLS", "reason": DOCUMENT_BYTES_RECOVERED}
    # HTML mistaken for document
    text_head = content[:500].decode("utf-8", errors="ignore").lower()
    if "<html" in text_head or "<!doctype html" in text_head:
        if any(x in text_head for x in ("login", "sign in", "password")):
            return {"ok": False, "format": "HTML", "reason": LOGIN_HTML_INSTEAD_OF_DOCUMENT}
        if any(x in text_head for x in ("captcha", "cloudflare", "challenge")):
            return {"ok": False, "format": "HTML", "reason": BOT_CHALLENGE}
        return {"ok": True, "format": "HTML", "reason": DOCUMENT_BYTES_RECOVERED}
    if ct.startswith("text/") or head[:1] in (b"{", b"["):
        return {"ok": True, "format": "TXT", "reason": DOCUMENT_BYTES_RECOVERED}
    if "pdf" in ct:
        return {"ok": False, "format": None, "reason": INVALID_DOCUMENT_BYTES, "detail": "pdf_header_but_not_pdf_magic"}
    return {"ok": False, "format": None, "reason": UNSUPPORTED_DOCUMENT_TYPE, "detail": repr(head[:8])}


def content_fingerprint(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def live_http_get(
    url: str,
    *,
    source_id: str = "portal_resolver",
    headers: dict[str, str] | None = None,
    referer: str | None = None,
    max_bytes: int = 8_000_000,
) -> dict[str, Any]:
    """Authorized live GET with cookie-capable client."""
    try:
        from discovery.http_client import PublicProcurementHttpClient, RequestBudget

        req_headers = dict(headers or {})
        if referer:
            req_headers["Referer"] = referer
        client = PublicProcurementHttpClient(
            budget=RequestBudget(
                max_total_requests=40,
                max_requests_per_source=25,
                min_interval_seconds=0.35,
                timeout_seconds=45,
            ),
            authorize_live=True,
        )
        resp = client.get(url, source_id=source_id, headers=req_headers or None, use_cache=False)
        content = resp.content or b""
        if len(content) > max_bytes:
            content = content[:max_bytes]
        code = int(resp.status_code or 0)
        ctype = (resp.headers or {}).get("content-type")
        final = getattr(resp.meta, "url", url) if resp.meta else url
        failure = None
        if code == 403:
            failure = HTTP_403
        elif code == 404:
            failure = HTTP_404
        elif code == 429:
            failure = HTTP_429
        elif code in {401}:
            failure = LOGIN_REQUIRED
        return {
            "ok": 200 <= code < 400,
            "status_code": code,
            "content": content,
            "text": resp.text or "",
            "headers": dict(resp.headers or {}),
            "content_type": ctype,
            "url": url,
            "final_url": final,
            "failure": failure,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status_code": None,
            "content": b"",
            "text": "",
            "headers": {},
            "content_type": None,
            "url": url,
            "final_url": url,
            "failure": OTHER_EXPLICIT_REASON,
            "error": str(exc)[:300],
        }


class PortalDocumentResolver(Protocol):
    family: str

    def resolve(self, opportunity: dict[str, Any]) -> dict[str, Any]:
        """Return document candidates + retrieval context."""
        ...


def classify_portal_family(row: dict[str, Any]) -> str:
    src = str(row.get("source_id") or "").lower()
    url = str(row.get("detail_url") or row.get("source_url") or row.get("url") or "").lower()
    agency = str(row.get("agency") or "").lower()
    platform = str(row.get("platform_family") or row.get("platform") or "").lower()
    meta = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}
    meta_platform = str(meta.get("platform_family") or meta.get("platform") or "").lower()

    if (
        "sam.gov" in url
        or "sam.gov" in str(meta.get("uiLink") or meta.get("ui_link") or "").lower()
        or src.startswith("sam")
        or "sam_gov" in src
        or platform in {"sam", "sam_gov"}
        or meta_platform in {"sam", "sam_gov"}
    ):
        return "SAM"

    if any(x in src or x in url or x in agency for x in ("dibbs", "dla", "dla.mil", "piee")) or platform in {
        "dibbs",
        "dla",
        "piee",
    }:
        return "DLA"

    if src.startswith("state_ia") or "iowa" in agency or "das iowa" in agency or "customerorg=dasiowa" in url:
        return "IOWA"
    if src.startswith("state_mt") or "montana" in agency or "stateofmontana" in url:
        return "MONTANA"
    if "phoenix" in src or "phoenix" in agency:
        return "PHOENIX"
    if "sourcewell" in src or "sourcewell" in url or "sourcewell" in agency:
        return "SOURCEWELL"
    if "jaggaer" in url or "sciquest" in url or "viewsourcingevent" in url or platform in {"jaggaer", "sciquest"}:
        if "montana" in agency:
            return "MONTANA"
        if "iowa" in agency:
            return "IOWA"
        return "JAGGAER"

    if "bonfire" in src or "bonfire" in url or platform == "bonfire" or meta_platform == "bonfire":
        return "BONFIRE"
    if "bidnet" in src or "bidnet" in url or platform == "bidnet" or meta_platform == "bidnet":
        return "BIDNET"
    if "opengov" in src or "opengov" in url or "procurement.opengov" in url or platform == "opengov":
        return "OPENGOV"
    if (
        "publicpurchase" in src
        or "publicpurchase" in url
        or "public_purchase" in src
        or platform in {"publicpurchase", "public_purchase"}
    ):
        return "PUBLIC_PURCHASE"

    if src.startswith("state_") or platform in {"stateowned", "state_portal", "state"}:
        return "STATE_PORTAL"

    return "GENERIC"


def assess_package_completeness(row: dict[str, Any], *, documents: list[dict[str, Any]] | None = None) -> str:
    docs = documents if documents is not None else (row.get("documents") if isinstance(row.get("documents"), list) else [])
    has_bytes = any(isinstance(d, dict) and d.get("bytes_recovered") for d in docs)
    has_gov = bool(row.get("governing_documents") or any(isinstance(d, dict) and d.get("document_type") in {"SOLICITATION", "EVENT_PDF", "GOVERNING"} for d in docs))
    has_bom = bool(row.get("line_items") or row.get("bom"))
    access = str(row.get("source_access_state") or row.get("package_access") or "").upper()
    if access in {"AUTH_REQUIRED", "REGISTRATION_REQUIRED", "AUTH_GATED"} and not has_bytes:
        return AUTH_BLOCKED
    if has_bom and has_gov:
        return COMPLETE_ENOUGH_FOR_RESEARCH if not row.get("package_complete") else PACKAGE_COMPLETE
    if has_bom:
        return BOM_RECOVERED
    if has_gov and has_bytes:
        return GOVERNING_SOLICITATION_RECOVERED
    if has_bytes:
        return PARTIAL_PACKAGE
    if row.get("detail_url") or row.get("source_url"):
        return DETAIL_ONLY
    return NO_PACKAGE


def extract_jaggaer_product_line_items(text: str) -> list[dict[str, Any]]:
    """Parse Jaggaer event PDF 'Product Line Items' blocks into BOM rows."""
    if not text:
        return []
    lines: list[dict[str, Any]] = []
    uom = (
        r"(?:Pounds?|Lbs?|Each|EA|Units?|Boxes?|Cases?|Gallons?|GAL|Tons?|"
        r"Hours?|Days?|Lots?|Sets?|Pairs?|Kits?|Rolls?|Feet|FT|Yards?|YD|"
        r"Bags?|Bundles?|Drums?|Quarts?|QT|Ounces?|OZ|"
        r"LS|Lump\s*Sum|YR|Year|MO|Month|WK|Week)"
    )

    def _norm_unit(raw: str) -> str:
        u = (raw or "EA").upper().replace(" ", "")
        if u.startswith("POUND") or u.startswith("LB"):
            return "LB"
        if u.startswith("LUMP") or u == "LS":
            return "LS"
        if u.startswith("YEAR") or u == "YR":
            return "YR"
        return u[:12]

    def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for li in items:
            key = str(li.get("line_id") or "").lower() or str(li.get("description") or "").lower()[:80]
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(li)
        return out

    # Pattern A: P12 324.3 Pounds- Big bluestem ...
    for m in re.finditer(
        rf"\bP(?P<num>\d+)\s+(?P<qty>\d+(?:\.\d+)?)\s*(?P<unit>{uom})\s*[-–:]\s*(?P<desc>[^\n]{{5,160}})",
        text,
        re.I,
    ):
        lines.append(
            {
                "line_id": f"P{m.group('num')}",
                "description": m.group("desc").strip(),
                "quantity": float(m.group("qty")),
                "unit": _norm_unit(m.group("unit")),
                "confidence": "HIGH",
                "source": "jaggaer_event_pdf_product_line_items",
            }
        )
    if lines:
        return _dedupe(lines)

    # Pattern B: P1 IFB: Wheelchair Lift  1  LS - Lump Sum  (desc after UOM)
    for m in re.finditer(
        rf"\bP(?P<num>\d+)\s+(?P<desc>[^\n]{{3,120}}?)\s+(?P<qty>\d+(?:\.\d+)?)\s+(?P<unit>{uom})\b",
        text,
        re.I,
    ):
        desc = re.sub(r"\s+", " ", m.group("desc")).strip(" -:.")
        if len(desc) < 3:
            continue
        lines.append(
            {
                "line_id": f"P{m.group('num')}",
                "description": desc[:200],
                "quantity": float(m.group("qty")),
                "unit": _norm_unit(m.group("unit")),
                "confidence": "HIGH",
                "source": "jaggaer_event_pdf_product_line_items_b",
            }
        )
    if lines:
        return _dedupe(lines)

    # Alternate: Line Item / Item # with qty nearby
    for m in re.finditer(
        rf"(?:Line\s*Item|Item\s*#?|Product)\s*(?P<num>\d+)\s*[:.\-]?\s*"
        rf"(?P<desc>[^\n]{{8,140}}?)\s+(?P<qty>\d+(?:\.\d+)?)\s*(?P<unit>{uom})",
        text,
        re.I,
    ):
        lines.append(
            {
                "line_id": f"P{m.group('num')}",
                "description": m.group("desc").strip(" -:."),
                "quantity": float(m.group("qty")),
                "unit": _norm_unit(m.group("unit")),
                "confidence": "MEDIUM",
                "source": "jaggaer_event_pdf_line_item_block",
            }
        )
        if len(lines) >= 50:
            break
    if lines:
        return _dedupe(lines)

    # Fallback: Item Name blocks with Qty/UOM nearby (seed mixes etc.)
    for m in re.finditer(
        r"(?P<desc>(?:Big|Little|Side|Blue|Canada|Switch|Indian|Rough|Prairie|White|Swamp|Common|Butterfly)[^\n]{0,80})\s+"
        r"(?P<qty>\d+(?:\.\d+)?)\s*(?:LB|Pound)",
        text,
        re.I,
    ):
        lines.append(
            {
                "description": m.group("desc").strip(),
                "quantity": float(m.group("qty")),
                "unit": "LB",
                "confidence": "MEDIUM",
                "source": "jaggaer_event_pdf_fallback",
            }
        )
        if len(lines) >= 40:
            break
    return _dedupe(lines)


def resolve_portal_documents(opportunity: dict[str, Any]) -> dict[str, Any]:
    """Dispatch to family resolver."""
    family = classify_portal_family(opportunity)
    if family == "SAM":
        from portal_resolvers.sam import resolve_sam_documents

        return resolve_sam_documents(opportunity)
    if family == "DLA":
        from portal_resolvers.portal_families import resolve_dla_documents

        return resolve_dla_documents(opportunity)
    if family in {"IOWA", "MONTANA", "JAGGAER"}:
        from portal_resolvers.jaggaer import resolve_jaggaer_documents

        return resolve_jaggaer_documents(opportunity, family=family)
    if family == "SOURCEWELL":
        from portal_resolvers.sourcewell import resolve_sourcewell_documents

        return resolve_sourcewell_documents(opportunity)
    if family == "PHOENIX":
        from portal_resolvers.phoenix import resolve_phoenix_documents

        return resolve_phoenix_documents(opportunity)
    if family == "BONFIRE":
        from portal_resolvers.portal_families import resolve_bonfire_documents

        return resolve_bonfire_documents(opportunity)
    if family == "BIDNET":
        from portal_resolvers.portal_families import resolve_bidnet_documents

        return resolve_bidnet_documents(opportunity)
    if family == "OPENGOV":
        from portal_resolvers.portal_families import resolve_opengov_documents

        return resolve_opengov_documents(opportunity)
    if family == "PUBLIC_PURCHASE":
        from portal_resolvers.portal_families import resolve_public_purchase_documents

        return resolve_public_purchase_documents(opportunity)
    if family == "STATE_PORTAL":
        from portal_resolvers.portal_families import resolve_state_portal_documents

        return resolve_state_portal_documents(opportunity)
    from portal_resolvers.generic import resolve_generic_documents

    return resolve_generic_documents(opportunity)
