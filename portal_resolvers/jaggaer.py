"""Jaggaer / SciQuest portal document resolution for Iowa + Montana."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from portal_document_resolver import (
    ACTUAL_DOCUMENT_BYTES,
    DETAIL_PAGE,
    DOCUMENT_ENDPOINT,
    DOCUMENT_BYTES_RECOVERED,
    DOWNLOAD_ENDPOINT_UNRESOLVED,
    LOGIN_REQUIRED,
    PACKAGE_ROUTE_NOT_FOUND,
    SIGNED_URL_EXPIRED,
    _utc,
    content_fingerprint,
    detect_file_format,
    extract_jaggaer_product_line_items,
    live_http_get,
)

_CUSTOMER_ORG = {
    "IOWA": "DASIowa",
    "MONTANA": "StateOfMontana",
    "JAGGAER": None,
}


def _list_url_for(family: str, row: dict[str, Any]) -> str | None:
    org = _CUSTOMER_ORG.get(family)
    if not org:
        # try from existing URL
        for u in (row.get("source_url"), row.get("list_url"), row.get("detail_url")):
            if not u:
                continue
            qs = parse_qs(urlparse(str(u)).query)
            if qs.get("CustomerOrg"):
                org = qs["CustomerOrg"][0]
                break
    if not org:
        return None
    return f"https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg={org}"


def _match_score(opp: Any, row: dict[str, Any]) -> int:
    score = 0
    sol = str(row.get("solicitation_number") or row.get("external_id") or "").lower()
    title = str(row.get("title") or "").lower()
    opp_sol = str(getattr(opp, "solicitation_number", None) or "").lower()
    opp_title = str(getattr(opp, "title", None) or "").lower()
    if sol and opp_sol and (sol in opp_sol or opp_sol in sol or sol.replace("-", "") in opp_sol.replace("-", "")):
        score += 50
    # token overlap
    t_tokens = [w for w in re.findall(r"[a-z0-9]{4,}", title) if w not in {"request", "services", "state"}]
    o_tokens = set(re.findall(r"[a-z0-9]{4,}", opp_title))
    score += 5 * sum(1 for w in t_tokens[:6] if w in o_tokens)
    return score


def resolve_jaggaer_documents(row: dict[str, Any], *, family: str = "JAGGAER") -> dict[str, Any]:
    """
    Flow:
    stale AuthToken detail → refresh PublicEvent listing → match solicitation
    → signed Sourcingevent/{id}-event.pdf → validate bytes → parse Product Line Items
    """
    from discovery.sciquest import parse_sciquest_public_events

    list_url = _list_url_for(family, row)
    detail = row.get("detail_url") or row.get("source_url") or row.get("url")
    attempts: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    line_items: list[dict[str, Any]] = []
    failure = None
    access_state = None

    if not list_url and not detail:
        return {
            "family": family,
            "ok": False,
            "failure": PACKAGE_ROUTE_NOT_FOUND,
            "documents": [],
            "line_items": [],
            "attempts": attempts,
            "retrieval_method": None,
        }

    matched = None
    listing_text = ""
    if list_url:
        hit = live_http_get(list_url, source_id=f"jaggaer_list_{family.lower()}")
        attempts.append(
            {
                "step": "public_event_listing",
                "url": list_url,
                "status": hit.get("status_code"),
                "ok": hit.get("ok"),
                "at": _utc(),
                "failure": hit.get("failure"),
            }
        )
        if hit.get("ok"):
            listing_text = hit.get("text") or ""
            opps = parse_sciquest_public_events(listing_text, list_url=list_url, source_id=str(row.get("source_id") or "jaggaer"))
            ranked = sorted(opps, key=lambda o: _match_score(o, row), reverse=True)
            if ranked and _match_score(ranked[0], row) >= 10:
                matched = ranked[0]

    pdf_candidates: list[dict[str, Any]] = []
    if matched and matched.document_links:
        for d in matched.document_links:
            if isinstance(d, dict) and d.get("url"):
                pdf_candidates.append(
                    {
                        "url": d["url"],
                        "kind": d.get("kind") or "event_pdf",
                        "layer": DOCUMENT_ENDPOINT,
                        "source": "public_event_listing",
                    }
                )
        if matched.detail_url:
            detail = matched.detail_url

    # Also try current detail page (often AuthToken; may lack attachments)
    if detail:
        dhit = live_http_get(str(detail), source_id=f"jaggaer_detail_{family.lower()}", referer=list_url)
        attempts.append(
            {
                "step": DETAIL_PAGE,
                "url": detail,
                "status": dhit.get("status_code"),
                "ok": dhit.get("ok"),
                "at": _utc(),
                "failure": dhit.get("failure"),
            }
        )
        body = dhit.get("text") or ""
        if dhit.get("ok"):
            for m in re.finditer(
                r'href=["\'](https?://[^"\']+Sourcingevent/\d+-event\.pdf[^"\']*)["\']',
                body,
                re.I,
            ):
                pdf_candidates.append({"url": m.group(1), "kind": "event_pdf", "layer": DOCUMENT_ENDPOINT, "source": "detail_page"})
            if re.search(r"sign\s*in|log\s*in|password", body[:4000], re.I) and "Sourcingevent" not in body:
                access_state = "AUTH_REQUIRED"
                failure = LOGIN_REQUIRED

    if not pdf_candidates:
        return {
            "family": family,
            "ok": False,
            "failure": failure or DOWNLOAD_ENDPOINT_UNRESOLVED,
            "documents": [],
            "line_items": [],
            "attempts": attempts,
            "access_state": access_state,
            "detail_url": detail,
            "list_url": list_url,
            "matched_title": getattr(matched, "title", None) if matched else None,
            "retrieval_method": "jaggaer_public_event_signed_pdf",
        }

    # Download first valid PDF
    for cand in pdf_candidates[:3]:
        phit = live_http_get(
            cand["url"],
            source_id=f"jaggaer_pdf_{family.lower()}",
            referer=list_url or detail,
        )
        attempts.append(
            {
                "step": ACTUAL_DOCUMENT_BYTES,
                "url": cand["url"][:180],
                "status": phit.get("status_code"),
                "ok": phit.get("ok"),
                "bytes": len(phit.get("content") or b""),
                "at": _utc(),
                "failure": phit.get("failure"),
            }
        )
        content = phit.get("content") or b""
        if phit.get("status_code") == 403:
            failure = SIGNED_URL_EXPIRED if "X-Amz" in cand["url"] else phit.get("failure")
            continue
        fmt = detect_file_format(content, phit.get("content_type"))
        if not fmt.get("ok"):
            failure = fmt.get("reason")
            continue
        text = ""
        if fmt.get("format") == "PDF":
            try:
                from pdf_text import extract_pdf_text

                text = extract_pdf_text(content) or ""
            except Exception as exc:  # noqa: BLE001
                text = ""
                attempts.append({"step": "pdf_parse", "error": str(exc)[:200], "at": _utc()})
        documents.append(
            {
                "url": cand["url"].split("?")[0],
                "download_url": cand["url"],
                "title": f"{row.get('title') or 'event'}-event.pdf",
                "document_type": "SOLICITATION",
                "format": fmt.get("format"),
                "bytes_recovered": True,
                "byte_count": len(content),
                "content_fingerprint": content_fingerprint(content),
                "text_preview": text[:8000] if text else None,
                "extracted_text": text[:200000] if text else None,
                "authority": "authoritative",
                "source": cand.get("source"),
                "layer": ACTUAL_DOCUMENT_BYTES,
                "retrieved_at": _utc(),
                "validation": fmt.get("reason"),
            }
        )
        if text:
            line_items = extract_jaggaer_product_line_items(text)
        failure = None
        access_state = None  # authoritative PDF recovered anonymously
        break

    return {
        "family": family,
        "ok": bool(documents),
        "failure": failure,
        "documents": documents,
        "line_items": line_items,
        "attempts": attempts,
        "access_state": access_state,
        "detail_url": detail,
        "list_url": list_url,
        "matched_title": getattr(matched, "title", None) if matched else None,
        "matched_solicitation": getattr(matched, "solicitation_number", None) if matched else None,
        "retrieval_method": "jaggaer_public_event_signed_pdf",
        "status": DOCUMENT_BYTES_RECOVERED if documents else (failure or DOWNLOAD_ENDPOINT_UNRESOLVED),
    }
