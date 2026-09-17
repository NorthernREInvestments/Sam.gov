"""Phoenix solicitation document resolution.

Current public state (forensic):
- solicitations.phoenix.gov is an awards/tabulations mirror — no package PDFs.
- Active solicitations moved to OpenGov (procurement.opengov.com/portal/phoenix),
  which returns Cloudflare BOT_CHALLENGE to ordinary HTTP clients.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urljoin

from portal_document_resolver import (
    ACTUAL_DOCUMENT_BYTES,
    AUTHORITATIVE_SOURCE_NOT_FOUND,
    BOT_CHALLENGE,
    DETAIL_PAGE,
    DOCUMENT_BYTES_RECOVERED,
    DOWNLOAD_ENDPOINT_UNRESOLVED,
    PACKAGE_ROUTE_NOT_FOUND,
    _utc,
    content_fingerprint,
    detect_file_format,
    live_http_get,
)

PHOENIX_BASE = "https://solicitations.phoenix.gov"
OPENGOV_PORTAL = "https://procurement.opengov.com/portal/phoenix"


def _absolute(url: str | None, base: str = PHOENIX_BASE) -> str | None:
    if not url:
        return None
    u = str(url).strip()
    if u.startswith("http"):
        return u
    return urljoin(base + "/", u.lstrip("/"))


def _search_awards_for_detail(sol: str, title: str, attempts: list[dict[str, Any]]) -> str | None:
    if sol:
        search_url = (
            f"{PHOENIX_BASE}/?page=1&pageSize=25&selectedSearchType=searchByNumber"
            f"&SearchTerm={quote(sol)}"
        )
        hit = live_http_get(search_url, source_id="phoenix_search")
        attempts.append(
            {
                "step": "awards_search",
                "url": search_url,
                "status": hit.get("status_code"),
                "ok": hit.get("ok"),
                "at": _utc(),
            }
        )
        body = hit.get("text") or ""
        m = re.search(rf'href=["\'](/Solicitations/Details/\d+)["\'][^>]*>\s*{re.escape(sol)}', body, re.I)
        if m:
            return _absolute(m.group(1))
        m2 = re.search(r'href=["\'](/Solicitations/Details/\d+)["\']', body, re.I)
        if m2 and sol in body:
            return _absolute(m2.group(1))
    # listing scan by title tokens
    hit = live_http_get(PHOENIX_BASE + "/", source_id="phoenix_list")
    attempts.append(
        {
            "step": "awards_listing",
            "url": PHOENIX_BASE + "/",
            "status": hit.get("status_code"),
            "ok": hit.get("ok"),
            "at": _utc(),
        }
    )
    body = hit.get("text") or ""
    title_toks = [w for w in re.findall(r"[a-z0-9]{5,}", (title or "").lower())][:4]
    for m in re.finditer(
        r'<a href="(/Solicitations/Details/\d+)">([^<]+)</a></td>\s*<td>(.*?)</td>',
        body,
        re.I | re.S,
    ):
        href, number, label = m.group(1), m.group(2), re.sub(r"<[^>]+>", " ", m.group(3))
        label_l = (number + " " + label).lower()
        if sol and sol.lower() in label_l:
            return _absolute(href)
        if title_toks and sum(1 for w in title_toks if w in label_l) >= 2:
            return _absolute(href)
    return None


def resolve_phoenix_documents(row: dict[str, Any]) -> dict[str, Any]:
    detail = _absolute(row.get("detail_url") or row.get("source_url") or row.get("url"))
    attempts: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    title = str(row.get("title") or "")
    sol = str(row.get("solicitation_number") or row.get("external_id") or "")
    failure = None
    access_state = None
    awarded = False
    status_note = None

    if not detail or "phoenix.gov" not in detail.lower() or detail.rstrip("/").endswith("solicitations"):
        found = _search_awards_for_detail(sol, title, attempts)
        if found:
            detail = found

    # OpenGov active portal (often bot-challenged)
    og = live_http_get(OPENGOV_PORTAL, source_id="phoenix_opengov")
    attempts.append(
        {
            "step": "opengov_portal",
            "url": OPENGOV_PORTAL,
            "status": og.get("status_code"),
            "ok": og.get("ok"),
            "at": _utc(),
            "failure": BOT_CHALLENGE
            if (og.get("status_code") == 403 or "just a moment" in (og.get("text") or "").lower())
            else og.get("failure"),
        }
    )
    og_text = (og.get("text") or "").lower()
    if og.get("status_code") == 403 or "just a moment" in og_text or "cf-challenge" in og_text:
        access_state = "BOT_PROTECTED"
        failure = BOT_CHALLENGE

    if not detail:
        return {
            "family": "PHOENIX",
            "ok": False,
            "failure": failure or PACKAGE_ROUTE_NOT_FOUND,
            "documents": [],
            "line_items": [],
            "attempts": attempts,
            "access_state": access_state,
            "note": "awards_mirror_only_active_on_opengov_bot_challenged",
            "opengov_url": OPENGOV_PORTAL,
        }

    hit = live_http_get(str(detail), source_id="phoenix_detail")
    attempts.append({"step": DETAIL_PAGE, "url": detail, "status": hit.get("status_code"), "ok": hit.get("ok"), "at": _utc()})
    if not hit.get("ok"):
        return {
            "family": "PHOENIX",
            "ok": False,
            "failure": failure or hit.get("failure") or DOWNLOAD_ENDPOINT_UNRESOLVED,
            "documents": [],
            "line_items": [],
            "attempts": attempts,
            "detail_url": detail,
            "access_state": access_state,
            "opengov_url": OPENGOV_PORTAL,
        }

    body = hit.get("text") or ""
    awarded = bool(
        re.search(
            r"pending council approval|tabulation|award(?:ed)?|recommendation",
            body[:8000],
            re.I,
        )
    )
    if awarded:
        status_note = "AWARDS_TABULATION_NOT_OPEN_SOLICITATION"

    candidates = []
    for m in re.finditer(r'href=["\']([^"\']+\.(?:pdf|docx|xlsx|xls|zip)(?:\?[^"\']*)?)["\']', body, re.I):
        href = m.group(1)
        candidates.append(href if href.startswith("http") else urljoin(str(detail), href))

    for url in candidates[:5]:
        phit = live_http_get(url, source_id="phoenix_doc", referer=str(detail))
        attempts.append(
            {
                "step": ACTUAL_DOCUMENT_BYTES,
                "url": url[:180],
                "status": phit.get("status_code"),
                "ok": phit.get("ok"),
                "bytes": len(phit.get("content") or b""),
                "at": _utc(),
            }
        )
        content = phit.get("content") or b""
        fmt = detect_file_format(content, phit.get("content_type"))
        if not fmt.get("ok"):
            continue
        text = ""
        if fmt.get("format") == "PDF":
            try:
                from pdf_text import extract_pdf_text

                text = extract_pdf_text(content) or ""
            except Exception:
                text = ""
        documents.append(
            {
                "url": url.split("?")[0],
                "download_url": url,
                "title": url.rsplit("/", 1)[-1][:120],
                "document_type": "SOLICITATION",
                "format": fmt.get("format"),
                "bytes_recovered": True,
                "byte_count": len(content),
                "content_fingerprint": content_fingerprint(content),
                "text_preview": (text or "")[:8000] or None,
                "extracted_text": (text or "")[:200000] or None,
                "authority": "authoritative",
                "source": "phoenix_detail",
                "layer": ACTUAL_DOCUMENT_BYTES,
                "retrieved_at": _utc(),
                "validation": fmt.get("reason"),
            }
        )
        if len(documents) >= 3:
            break

    # Persist awards/listing HTML as secondary evidence when no binary package exists
    if not documents and body:
        documents.append(
            {
                "url": str(detail),
                "title": row.get("title") or "phoenix-awards-row",
                "document_type": "AWARDS_TABULATION_HTML" if awarded else "DETAIL_HTML",
                "format": "HTML",
                "bytes_recovered": True,
                "byte_count": len(body.encode("utf-8", errors="ignore")),
                "content_fingerprint": content_fingerprint(body.encode("utf-8", errors="ignore")),
                "text_preview": body[:8000],
                "extracted_text": body[:100000],
                "authority": "secondary",
                "source": "phoenix_awards_mirror",
                "layer": DETAIL_PAGE,
                "retrieved_at": _utc(),
                "validation": DOCUMENT_BYTES_RECOVERED,
                "awarded_or_informational": awarded,
            }
        )

    ok = bool(documents)
    # Prefer explicit external blocker when OpenGov is the real package host
    if awarded and not candidates:
        failure = failure or AUTHORITATIVE_SOURCE_NOT_FOUND
    elif not documents:
        failure = failure or DOWNLOAD_ENDPOINT_UNRESOLVED
    else:
        # keep BOT_CHALLENGE note for OpenGov even if awards HTML recovered
        pass

    return {
        "family": "PHOENIX",
        "ok": ok,
        "failure": None if (ok and not awarded) else failure,
        "documents": documents,
        "line_items": [],
        "attempts": attempts,
        "detail_url": detail,
        "access_state": access_state,
        "awarded_or_informational": awarded,
        "status_note": status_note,
        "opengov_url": OPENGOV_PORTAL,
        "retrieval_method": "phoenix_awards_mirror_plus_opengov_probe",
        "status": DOCUMENT_BYTES_RECOVERED if documents else (failure or DOWNLOAD_ENDPOINT_UNRESOLVED),
    }
