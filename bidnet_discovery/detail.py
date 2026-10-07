"""Authenticated BidNet detail recovery with classified failure reasons."""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from application_clock import now_utc
from bidnet_recovery.parse_abstract import parse_bidnet_abstract

log = logging.getLogger("govtracker.bidnet_discovery.detail")

DETAIL_OK = "DETAIL_OK"
STALE_RESULT = "STALE_RESULT"
NOT_FOUND_404 = "404"
SUPPLIER_REGISTRATION_REDIRECT = "SUPPLIER_REGISTRATION_REDIRECT"
SESSION_LOST = "SESSION_LOST"
AUTH_REQUIRED = "AUTH_REQUIRED"
NO_DETAIL_AVAILABLE = "NO_DETAIL_AVAILABLE"
PARSER_FAILED = "PARSER_FAILED"
NETWORK_ERROR = "NETWORK_ERROR"
TIMEOUT = "TIMEOUT"
OTHER = "OTHER"

DETAIL_FAILURE_STATES = {
    STALE_RESULT,
    NOT_FOUND_404,
    SUPPLIER_REGISTRATION_REDIRECT,
    SESSION_LOST,
    AUTH_REQUIRED,
    NO_DETAIL_AVAILABLE,
    PARSER_FAILED,
    NETWORK_ERROR,
    TIMEOUT,
    OTHER,
}


def classify_detail_failure(*, final_url: str, html: str, error: str | None = None) -> str:
    blob = f"{final_url or ''}\n{html or ''}\n{error or ''}"
    low = blob.lower()
    if error and "AUTH_CHALLENGE" in (error or ""):
        return AUTH_REQUIRED
    if error and re.search(r"timeout|TimeoutError", error or "", re.I):
        return TIMEOUT
    if error and re.search(r"TargetClosed|net::|Navigation|Connection", error or "", re.I):
        return NETWORK_ERROR
    if re.search(r"supplier registration|abstractregisternow", low):
        return SUPPLIER_REGISTRATION_REDIRECT
    if re.search(r"public/authentication/login|please\s*log\s*in|sign\s*in\s*to\s*continue", low):
        return SESSION_LOST
    if re.search(r"\b404\b|page not found|solicitation (was )?not found|no longer available", low):
        return NOT_FOUND_404
    if re.search(r"expired|closed|cancelled|canceled|award(ed)?", low) and "mets-field" not in low:
        return STALE_RESULT
    if re.search(r"registered members only|member-only-info|get instant access", low) and not re.search(
        r"issuing organization", low
    ):
        return AUTH_REQUIRED
    return OTHER


def recover_detail(client: Any, row: dict[str, Any], *, retry: bool = True) -> dict[str, Any]:
    """Recover detail from current authenticated href. Classifies failures."""
    url = str(row.get("detail_url") or row.get("current_detail_href") or "")
    out = dict(row)
    stats: dict[str, Any] = {
        "detail_status": OTHER,
        "detail_opened": False,
        "detail_recovered": False,
        "issuing_org": False,
        "solicitation_number": False,
        "source_url": False,
        "description": False,
        "documents": 0,
        "documents_downloaded": 0,
        "final_url": None,
        "error": None,
        "attempts": 0,
    }
    if not url:
        stats["detail_status"] = NO_DETAIL_AVAILABLE
        stats["error"] = "no_detail_url"
        out["auth_detail"] = stats
        return out

    attempts = 2 if retry else 1
    last_html = ""
    last_url = url
    last_err: str | None = None

    for attempt in range(1, attempts + 1):
        stats["attempts"] = attempt
        try:
            html = client.fetch_html(url, timeout_ms=75_000 if attempt == 1 else 90_000)
            last_html = html or ""
            page = getattr(client, "_page", None)
            last_url = str(getattr(page, "url", None) or url)
            stats["final_url"] = last_url[:220]
            stats["detail_opened"] = True

            # Wait briefly for abstract fields if missing
            if page is not None and "mets-field" not in last_html.lower():
                for sel in (".mets-field-body", "text=Issuing Organization", "text=Closing Date", "h1"):
                    try:
                        page.wait_for_selector(sel, timeout=6_000)
                        last_html = page.content()
                        last_url = str(page.url or last_url)
                        break
                    except Exception:
                        continue

            status = classify_detail_failure(final_url=last_url, html=last_html)
            if status in {
                SUPPLIER_REGISTRATION_REDIRECT,
                SESSION_LOST,
                NOT_FOUND_404,
                STALE_RESULT,
                AUTH_REQUIRED,
            }:
                stats["detail_status"] = status
                stats["error"] = status
                out["auth_detail"] = stats
                if status == SESSION_LOST:
                    raise RuntimeError("SESSION_LOST")
                return out

            parsed = parse_bidnet_abstract(last_html, detail_url=last_url)
            if not parsed.get("parse_ok") and not parsed.get("title"):
                if attempt < attempts:
                    continue
                stats["detail_status"] = PARSER_FAILED
                stats["error"] = "parser_empty"
                out["auth_detail"] = stats
                return out

            _apply_parsed(out, parsed, last_url)
            docs = list(parsed.get("documents") or [])
            # Authenticated download of discovered docs (cap)
            downloaded = _download_docs(client, docs, row=out, limit=8)
            stats["documents"] = len(docs)
            stats["documents_downloaded"] = downloaded
            stats["issuing_org"] = bool(parsed.get("agency"))
            stats["solicitation_number"] = bool(parsed.get("solicitation_number"))
            stats["source_url"] = bool(parsed.get("agency_source_url"))
            stats["description"] = bool(parsed.get("description"))
            stats["detail_recovered"] = bool(
                parsed.get("agency")
                or parsed.get("solicitation_number")
                or parsed.get("description")
                or docs
                or parsed.get("material_improvement")
            )
            if stats["detail_recovered"]:
                stats["detail_status"] = DETAIL_OK
            else:
                # Page opened but only teaser / locked fields
                if parsed.get("auth_wall") or parsed.get("locked_fields"):
                    stats["detail_status"] = AUTH_REQUIRED
                else:
                    stats["detail_status"] = NO_DETAIL_AVAILABLE
            out["auth_detail"] = stats
            return out
        except RuntimeError as exc:
            msg = str(exc)
            if "SESSION_LOST" in msg or "AUTH_CHALLENGE" in msg:
                stats["detail_status"] = SESSION_LOST if "SESSION" in msg else AUTH_REQUIRED
                stats["error"] = msg[:120]
                out["auth_detail"] = stats
                raise
            last_err = type(exc).__name__
        except Exception as exc:
            last_err = type(exc).__name__
            if attempt < attempts:
                try:
                    getattr(client, "_page", None) and client._page.wait_for_timeout(1_200)
                except Exception:
                    pass
                continue

    stats["detail_status"] = classify_detail_failure(
        final_url=last_url, html=last_html, error=last_err
    )
    stats["error"] = last_err
    out["auth_detail"] = stats
    return out


def _apply_parsed(out: dict[str, Any], parsed: dict[str, Any], final_url: str) -> None:
    if parsed.get("title"):
        out["title"] = parsed["title"]
    if parsed.get("agency"):
        out["agency"] = parsed["agency"]
        out["buyer"] = parsed["agency"]
    if parsed.get("solicitation_number"):
        out["solicitation_number"] = parsed["solicitation_number"]
        out["solicitation_id"] = out.get("solicitation_id") or parsed["solicitation_number"]
    if parsed.get("description"):
        out["description"] = parsed["description"]
    if parsed.get("deadline"):
        out["deadline"] = parsed["deadline"]
        out["deadline_raw"] = parsed.get("deadline_raw") or out.get("deadline_raw")
    if parsed.get("location"):
        out["location"] = parsed["location"]
        out["jurisdiction"] = parsed["location"]
    if parsed.get("agency_source_url"):
        out["original_posting_url"] = parsed["agency_source_url"]
    docs = parsed.get("documents") or []
    if docs:
        out["document_links"] = docs
        out["attachments_metadata"] = docs
    out["detail_url"] = final_url
    out["authoritative_url"] = final_url
    out["current_detail_href"] = final_url
    meta = dict(out.get("raw_metadata") or {})
    meta["authenticated_detail"] = True
    meta["locked_fields"] = parsed.get("locked_fields") or []
    meta["auth_wall"] = parsed.get("auth_wall")
    meta["detail_recovered_at"] = now_utc().isoformat()
    out["raw_metadata"] = meta


def _download_docs(client: Any, docs: list[dict[str, Any]], *, row: dict[str, Any], limit: int = 20) -> int:
    """Download a capped set of document bytes when authenticated client supports it."""
    if not docs or not hasattr(client, "download_bytes"):
        return 0
    n = 0
    prefer = re.compile(
        r"\.(xlsx?|csv|pdf|docx?|zip)(?:$|\?)|bid\s*sheet|pricing|schedule|addend|spec|item\s*list|tab",
        re.I,
    )
    ordered = sorted(
        docs,
        key=lambda d: (0 if prefer.search(str(d.get("document_url") or d.get("document_name") or "")) else 1),
    )
    from m3_data_root import data_path

    sid = str(
        (row.get("raw_metadata") or {}).get("bidnet_internal_id")
        or row.get("solicitation_id")
        or row.get("external_id")
        or "unknown"
    )
    safe_sid = re.sub(r"[^a-zA-Z0-9_-]+", "_", sid)[:48]
    for doc in ordered[:limit]:
        url = str(doc.get("document_url") or "")
        if not url.startswith("http"):
            continue
        try:
            body = client.download_bytes(url, timeout_ms=60_000)
            if not body or len(body) < 40:
                doc["retrieval_status"] = "DOWNLOAD_EMPTY"
                continue
            h = hashlib.sha256(body).hexdigest()[:16]
            name = str(doc.get("document_name") or url.rsplit("/", 1)[-1] or "doc")[:120]
            safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", name)[:80]
            path = data_path("bidnet_auth", "documents", safe_sid, f"{h}_{safe}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            doc["retrieval_status"] = "DOWNLOADED"
            doc["content_hash"] = h
            doc["retrieved_at"] = now_utc().isoformat()
            doc["local_path"] = str(path)
            n += 1
        except Exception:
            doc["retrieval_status"] = "DOWNLOAD_FAILED"
    return n
