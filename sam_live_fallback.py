"""SAM Contract Opportunities multi-source live retrieval.

PREFERRED_STRUCTURED_SOURCE = Opportunities API (personal SAM_GOV_API_KEY).
On API failure, continue with legitimate public SAM web/detail/attachment paths.
Never treat a personal-API failure as opportunity verification failure by itself.
Does not bypass CAPTCHA, auth walls, or anti-bot controls.
"""

from __future__ import annotations

import json
import os
import re
from html import unescape
from typing import Any, Callable

from application_clock import now_utc

# --- Source confirmation states ---
LIVE_API_CONFIRMED = "LIVE_API_CONFIRMED"
LIVE_PUBLIC_WEB_CONFIRMED = "LIVE_PUBLIC_WEB_CONFIRMED"
LIVE_AGENCY_SOURCE_CONFIRMED = "LIVE_AGENCY_SOURCE_CONFIRMED"
LIVE_ATTACHMENT_CONFIRMED = "LIVE_ATTACHMENT_CONFIRMED"
STALE_CACHE_ONLY = "STALE_CACHE_ONLY"
LIVE_SOURCE_UNAVAILABLE = "LIVE_SOURCE_UNAVAILABLE"
PUBLIC_WEB_BLOCKED = "PUBLIC_WEB_BLOCKED"
SOURCE_CONFLICT = "SOURCE_CONFLICT"
DISCOVERY_DEGRADED = "DISCOVERY_DEGRADED"

# --- API failure classification (distinct from verification failure) ---
SAM_API_AUTH_FAILED = "SAM_API_AUTH_FAILED"
SAM_API_RATE_LIMITED = "SAM_API_RATE_LIMITED"
SAM_API_TIMEOUT = "SAM_API_TIMEOUT"
SAM_API_OTHER_ERROR = "SAM_API_OTHER_ERROR"
SAM_API_OK = "SAM_API_OK"
SAM_API_NOT_ATTEMPTED = "SAM_API_NOT_ATTEMPTED"

# Public SPA endpoints (same surfaces the SAM.gov browser UI uses)
_PUBLIC_OPP_URL = "https://sam.gov/api/prod/opps/v2/opportunities/{notice_id}"
_PUBLIC_RESOURCES_URL = "https://sam.gov/api/prod/opps/v3/opportunities/{notice_id}/resources"
_PUBLIC_FILE_URL = "https://sam.gov/api/prod/opps/v3/opportunities/resources/files/{resource_id}/download"
_PUBLIC_LISTING_URL = "https://sam.gov/opp/{notice_id}/view"
_UMBRELLA_RE = re.compile(r'"API_UMBRELLA_KEY"\s*:\s*"([^"]+)"')

# Preference when sources disagree (highest first after latest amendment)
_SOURCE_RANK = {
    "LATEST_AMENDMENT": 100,
    "SOLICITATION_ATTACHMENT": 90,
    "PUBLIC_SAM_DETAIL": 80,
    "PUBLIC_SAM_PAGE": 70,
    "SAM_OPPORTUNITIES_API": 60,
    "AGENCY_PUBLIC_SOURCE": 50,
    "STALE_CACHE": 10,
}

_QTY_PATTERNS = [
    # Prefer explicit schedule qty before delivery days
    re.compile(
        r"Toolkit\s*\([^)]*\)\s*\n\s*([1-9]\d{0,3}(?:\.\d+)?)\s*\n\s*180\s*Days",
        re.I,
    ),
    re.compile(
        r"Quantity:\s*(?:\n[^\n]{0,60}){0,3}\n\s*([1-9]\d{0,3}(?:\.\d+)?)\s*\n\s*180\s*Days",
        re.I,
    ),
    re.compile(r"Quantity:\s*([1-9][\d,]{0,6}(?:\.\d+)?)\s*(?:180\s*Days)?", re.I),
    re.compile(r"\b(?:quantity|qty|quan\.?)\s*[:#]?\s*([1-9][\d,]{0,6}(?:\.\d+)?)\b", re.I),
    re.compile(r"\b([1-9][\d,]{0,6}(?:\.\d+)?)\s*(EA|EACH|KT|KIT)\b", re.I),
]

_UOM_RE = re.compile(r"\b(EA|EACH|KT|KIT|SE|SET|BX|BOX|PK|PACK|PR|PAIR)\b", re.I)
_HTML_STRIP = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _utc() -> str:
    return now_utc().isoformat()


def classify_sam_api_failure(
    *,
    status_code: int | None = None,
    error: str | None = None,
    exc: BaseException | None = None,
) -> str:
    """Map transport/HTTP errors to explicit SAM API failure codes."""
    if exc is not None:
        name = type(exc).__name__.lower()
        msg = str(exc).lower()
        if "timeout" in name or "timeout" in msg:
            return SAM_API_TIMEOUT
    err = (error or "").upper()
    if status_code == 401 or "401" in err or "AUTH" in err or "INVALID CREDENTIALS" in err:
        return SAM_API_AUTH_FAILED
    if status_code == 429 or "429" in err or "RATE" in err:
        return SAM_API_RATE_LIMITED
    if status_code is not None and status_code >= 400:
        return SAM_API_OTHER_ERROR
    if err:
        if "TIMEOUT" in err:
            return SAM_API_TIMEOUT
        return SAM_API_OTHER_ERROR
    return SAM_API_OTHER_ERROR


def listing_url(notice_id: str) -> str:
    return _PUBLIC_LISTING_URL.format(notice_id=notice_id)


def field_provenance(
    value: Any,
    *,
    source_type: str,
    source_url: str | None = None,
    retrieved_at: str | None = None,
    snippet: str | None = None,
) -> dict[str, Any]:
    return {
        "value": value,
        "source_type": source_type,
        "source_url": source_url,
        "retrieved_at": retrieved_at or _utc(),
        "snippet": (snippet or "")[:240] if snippet else None,
    }


def prefer_live_field(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick best live field value; flag SOURCE_CONFLICT when values disagree."""
    usable = [c for c in candidates if c.get("value") not in (None, "", [])]
    if not usable:
        return {"value": None, "conflict": False, "candidates": candidates}
    ranked = sorted(
        usable,
        key=lambda c: _SOURCE_RANK.get(str(c.get("source_type") or ""), 0),
        reverse=True,
    )
    best = ranked[0]
    values = {str(c.get("value")) for c in usable}
    conflict = len(values) > 1
    out = dict(best)
    out["conflict"] = conflict
    out["conflict_flag"] = SOURCE_CONFLICT if conflict else None
    out["candidates"] = candidates
    return out


def _normalize_uom(uom: str | None) -> str | None:
    if not uom:
        return None
    u = uom.upper()
    return {
        "EACH": "EA",
        "KIT": "KT",
        "SET": "SE",
        "BOX": "BX",
        "PACK": "PK",
        "PAIR": "PR",
    }.get(u, u)


def parse_qty_uom_from_text(text: str) -> dict[str, Any]:
    qty = None
    uom = None
    provenance = None
    option_qty = None
    for rx in _QTY_PATTERNS:
        m = rx.search(text or "")
        if not m:
            continue
        try:
            raw = m.group(1)
            # Reject CLIN-looking zero-padded codes (0001/0002/0003)
            if re.fullmatch(r"0\d+", str(raw).replace(",", "")):
                continue
            qty = float(str(raw).replace(",", ""))
            if qty <= 0:
                continue
            provenance = m.group(0)[:120]
            if m.lastindex and m.lastindex >= 2 and m.group(2):
                uom = _normalize_uom(m.group(2))
            break
        except (TypeError, ValueError, IndexError):
            continue
    m_opt = re.search(
        r"(?:Unexercised\s+Production\s+(\d+)|Toolkit\s*\([^)]*\)\s*\n\s*(\d+)\s*(?:\n\s*180\s*Days)?\s*\n\s*0003\b)",
        text or "",
        re.I,
    )
    if m_opt:
        try:
            option_qty = float((m_opt.group(1) or m_opt.group(2)).replace(",", ""))
        except (ValueError, TypeError, AttributeError):
            option_qty = None
    # Alternate: production qty then option qty both 40 in schedule block
    if option_qty is None:
        m_block = re.search(
            r"Toolkit\s*\([^)]*\)\s*\n\s*(\d+)\s*(?:\n\s*180\s*Days)?\s*\n\s*0003\b",
            text or "",
            re.I,
        )
        if m_block:
            try:
                option_qty = float(m_block.group(1).replace(",", ""))
            except ValueError:
                option_qty = None
    if not uom:
        # Prefer UOM near the quantity hit; avoid matching "Set-Aside" → SET/SE.
        window = ""
        if provenance:
            idx = (text or "").find(str(provenance)[:20])
            if idx >= 0:
                window = (text or "")[max(0, idx - 20) : idx + 80]
        mu = _UOM_RE.search(window) if window else None
        if mu and not re.search(r"set[\s-]?aside", window or "", re.I):
            uom = _normalize_uom(mu.group(1))
        elif qty is not None and re.search(r"\b(toolkit|tool\s*kit|\bkit\b)", text or "", re.I):
            uom = "EA"
        elif qty is not None and re.search(r"\b\d+(?:\.\d+)?\s*EA\b", text or "", re.I):
            uom = "EA"
    return {
        "quantity": qty,
        "uom": uom,
        "option_quantity": option_qty,
        "confirmed": qty is not None and uom is not None,
        "snippet": provenance,
        "status": "CONFIRMED" if (qty is not None and uom is not None) else "LIVE_QTY_UOM_UNCONFIRMED",
    }


def _strip_html(text: str) -> str:
    t = _HTML_STRIP.sub(" ", text or "")
    t = unescape(t)
    return _WS.sub(" ", t).strip()


def extract_public_umbrella_key(html: str) -> str | None:
    m = _UMBRELLA_RE.search(html or "")
    return m.group(1) if m else None


def is_spa_shell_html(html: str) -> bool:
    """True when page is Angular/SPA shell without opportunity facts."""
    t = (html or "").lower()
    if not t:
        return True
    if "captcha" in t or "cf-challenge" in t or "access denied" in t:
        return True
    # Shell pages rarely embed NSN / solicitation numbers
    has_app = "data-beasties-container" in t or 'id="root"' in t or "ng-version" in t
    has_facts = bool(
        re.search(r"\b\d{4}-\d{2}-\d{3}-\d{4}\b", html or "")
        or re.search(r"response\s*deadline|solicitation\s*number|quantity", t)
    )
    return has_app and not has_facts


def _public_headers(umbrella: str | None) -> dict[str, str]:
    h = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; GovTracker-M3/1.0; +https://sam.gov; research)"
        ),
        "Accept": "application/hal+json, application/json, text/html, */*",
    }
    if umbrella:
        h["x-api-key"] = umbrella
    return h


def fetch_sam_api_opportunity(
    notice_id: str,
    *,
    http_get: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """PREFERRED structured source: Opportunities v2 search with personal API key."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    key = (os.getenv("SAM_GOV_API_KEY") or "").strip()
    if not key:
        return {
            "ok": False,
            "api_failure": SAM_API_AUTH_FAILED,
            "error": "SAM_GOV_API_KEY_missing",
            "raw": None,
        }

    from sam_client import SAM_SEARCH_URL

    params = {"api_key": key, "noticeid": notice_id, "limit": 1, "offset": 0}
    try:
        if http_get is not None:
            resp = http_get(SAM_SEARCH_URL, params=params)
            status = getattr(resp, "status_code", None)
            body = getattr(resp, "text", "") or ""
            data = resp.json() if hasattr(resp, "json") and status == 200 else {}
        else:
            import httpx

            with httpx.Client(timeout=15.0) as client:
                resp = client.get(SAM_SEARCH_URL, params=params)
                status = resp.status_code
                body = resp.text or ""
                data = resp.json() if status == 200 else {}
                try:
                    from api_budget import record_sam_usage

                    record_sam_usage(1)
                except Exception:
                    pass
        if status != 200:
            return {
                "ok": False,
                "api_failure": classify_sam_api_failure(status_code=status, error=body[:200]),
                "status_code": status,
                "error": f"HTTP_{status}",
                "raw": None,
                "body_excerpt": body[:300],
            }
        rows = (data or {}).get("opportunitiesData") or []
        if not rows:
            return {
                "ok": True,
                "api_failure": SAM_API_OK,
                "found": False,
                "raw": None,
                "data": data,
            }
        return {
            "ok": True,
            "api_failure": SAM_API_OK,
            "found": True,
            "raw": rows[0],
            "data": data,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "api_failure": classify_sam_api_failure(exc=exc, error=str(exc)),
            "error": str(exc)[:200],
            "raw": None,
        }


def fetch_public_sam_opportunity(
    notice_id: str,
    *,
    umbrella_key: str | None = None,
    http_get: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Public SAM opportunity detail (browser SPA backend) — not personal API key."""
    url = _PUBLIC_OPP_URL.format(notice_id=notice_id)
    headers = _public_headers(umbrella_key)
    try:
        if http_get is not None:
            resp = http_get(url, headers=headers)
        else:
            import httpx

            with httpx.Client(timeout=40.0, follow_redirects=True) as client:
                resp = client.get(url, headers=headers)
        status = getattr(resp, "status_code", None)
        text = getattr(resp, "text", "") or ""
        if status == 403 or status == 429 or "captcha" in text.lower():
            return {
                "ok": False,
                "blocked": PUBLIC_WEB_BLOCKED,
                "status_code": status,
                "url": url,
            }
        if status != 200:
            return {"ok": False, "status_code": status, "url": url, "error": text[:200]}
        data = resp.json() if hasattr(resp, "json") else json.loads(text)
        return {
            "ok": True,
            "url": url,
            "data": data,
            "retrieved_at": _utc(),
            "source_type": "PUBLIC_SAM_DETAIL",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "url": url, "error": str(exc)[:200]}


def fetch_public_sam_page(
    notice_id: str,
    *,
    http_get: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Fetch public listing HTML; detect SPA/bot block."""
    url = listing_url(notice_id)
    headers = _public_headers(None)
    headers["Accept"] = "text/html,application/xhtml+xml"
    try:
        if http_get is not None:
            resp = http_get(url, headers=headers)
        else:
            import httpx

            with httpx.Client(timeout=30.0, follow_redirects=True) as client:
                resp = client.get(url, headers=headers)
        status = getattr(resp, "status_code", None)
        body = getattr(resp, "text", "") or ""
        umbrella = extract_public_umbrella_key(body)
        if status and status >= 400:
            return {
                "ok": False,
                "blocked": PUBLIC_WEB_BLOCKED if status in {401, 403, 429} else None,
                "status_code": status,
                "url": url,
                "umbrella_key": umbrella,
            }
        if is_spa_shell_html(body):
            return {
                "ok": False,
                "blocked": PUBLIC_WEB_BLOCKED,
                "reason": "spa_shell_no_opportunity_facts",
                "url": url,
                "umbrella_key": umbrella,
                "html_chars": len(body),
            }
        return {
            "ok": True,
            "url": url,
            "body": body,
            "umbrella_key": umbrella,
            "retrieved_at": _utc(),
            "source_type": "PUBLIC_SAM_PAGE",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "url": url, "error": str(exc)[:200]}


def fetch_public_attachment_list(
    notice_id: str,
    *,
    umbrella_key: str | None = None,
    http_get: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    url = _PUBLIC_RESOURCES_URL.format(notice_id=notice_id)
    headers = _public_headers(umbrella_key)
    try:
        if http_get is not None:
            resp = http_get(url, headers=headers)
        else:
            import httpx

            with httpx.Client(timeout=40.0, follow_redirects=True) as client:
                resp = client.get(url, headers=headers)
        status = getattr(resp, "status_code", None)
        if status != 200:
            return {"ok": False, "status_code": status, "url": url}
        data = resp.json() if hasattr(resp, "json") else json.loads(resp.text)
        attachments: list[dict[str, Any]] = []
        emb = (data or {}).get("_embedded") or {}
        for block in emb.get("opportunityAttachmentList") or []:
            for a in block.get("attachments") or []:
                if not isinstance(a, dict):
                    continue
                rid = a.get("resourceId") or a.get("resource_id")
                if not rid:
                    continue
                attachments.append(
                    {
                        "name": a.get("name"),
                        "resource_id": rid,
                        "posted_date": a.get("postedDate") or a.get("posted_date"),
                        "download_url": _PUBLIC_FILE_URL.format(resource_id=rid),
                        "type": a.get("type"),
                    }
                )
        return {
            "ok": True,
            "url": url,
            "attachments": attachments,
            "retrieved_at": _utc(),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "url": url, "error": str(exc)[:200]}


def download_public_attachment(
    resource_id: str,
    *,
    umbrella_key: str | None = None,
    http_get: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    url = _PUBLIC_FILE_URL.format(resource_id=resource_id)
    headers = _public_headers(umbrella_key)
    headers["Accept"] = "*/*"
    try:
        if http_get is not None:
            resp = http_get(url, headers=headers)
        else:
            import httpx

            with httpx.Client(timeout=90.0, follow_redirects=True) as client:
                resp = client.get(url, headers=headers)
        status = getattr(resp, "status_code", None)
        content = getattr(resp, "content", None) or b""
        if status != 200 or not content:
            return {"ok": False, "status_code": status, "url": url}
        return {
            "ok": True,
            "url": url,
            "content": content,
            "retrieved_at": _utc(),
            "source_type": "SOLICITATION_ATTACHMENT",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "url": url, "error": str(exc)[:200]}


def extract_text_from_attachment_bytes(data: bytes, *, filename: str) -> str:
    try:
        from document_ingestion import _extract_text_and_tables

        extracted = _extract_text_and_tables(data, filename=filename)
        return str(extracted.get("text") or "")
    except Exception:
        try:
            from pdf_text import extract_pdf_text

            if data.startswith(b"%PDF"):
                return extract_pdf_text(data) or ""
        except Exception:
            pass
    return ""


def normalize_public_opportunity(data: dict[str, Any]) -> dict[str, Any]:
    """Map public SPA opportunity JSON into verification fields."""
    d2 = data.get("data2") if isinstance(data.get("data2"), dict) else {}
    sol = d2.get("solicitation") if isinstance(d2.get("solicitation"), dict) else {}
    deadlines = sol.get("deadlines") if isinstance(sol.get("deadlines"), dict) else {}
    status_obj = data.get("status") if isinstance(data.get("status"), dict) else {}
    desc_parts: list[str] = []
    for item in data.get("description") or []:
        if isinstance(item, dict) and item.get("body"):
            desc_parts.append(_strip_html(str(item["body"])))
    description = "\n".join(desc_parts)
    archived = bool(data.get("archived"))
    cancelled = bool(data.get("cancelled"))
    code = str(status_obj.get("code") or "").lower()
    if cancelled:
        open_status = "CANCELLED"
    elif archived:
        open_status = "ARCHIVED"
    elif code in {"published", "active"} and not archived:
        open_status = "OPEN"
    else:
        open_status = "UNKNOWN"
    return {
        "title": d2.get("title") or data.get("title"),
        "solicitation_number": d2.get("solicitationNumber") or sol.get("number"),
        "set_aside": sol.get("setAside"),
        "response_deadline": deadlines.get("response"),
        "response_tz": deadlines.get("responseTz"),
        "archive_date": (d2.get("archive") or {}).get("date") if isinstance(d2.get("archive"), dict) else None,
        "posted_date": data.get("postedDate"),
        "modified_date": data.get("modifiedDate"),
        "latest": data.get("latest"),
        "version": d2.get("version"),
        "open_status": open_status,
        "description": description,
        "naics": d2.get("naics"),
        "point_of_contact": d2.get("pointOfContact"),
        "organization_id": d2.get("organizationId"),
        "classification_code": d2.get("classificationCode"),
    }


def discovery_coverage_after_api_failure(api_failure: str | None) -> str:
    """Hunt coverage label when structured API is unhealthy."""
    if api_failure in {SAM_API_AUTH_FAILED, SAM_API_RATE_LIMITED, SAM_API_TIMEOUT, SAM_API_OTHER_ERROR}:
        return DISCOVERY_DEGRADED
    return "DISCOVERY_NORMAL"


def retrieve_sam_live(
    *,
    notice_id: str,
    solicitation_number: str | None = None,
    agency_url: str | None = None,
    stale_cache: dict[str, Any] | None = None,
    fetch_attachments: bool = True,
    max_attachments: int = 2,
    http_get: Callable[..., Any] | None = None,
    api_fetcher: Callable[..., dict[str, Any]] | None = None,
    public_detail_fetcher: Callable[..., dict[str, Any]] | None = None,
    public_page_fetcher: Callable[..., dict[str, Any]] | None = None,
    attachment_list_fetcher: Callable[..., dict[str, Any]] | None = None,
    attachment_downloader: Callable[..., dict[str, Any]] | None = None,
    agency_fetcher: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Fallback chain:
      1. SAM Opportunities API
      2. Public SAM opportunity detail (SPA backend)
      3. Public SAM listing HTML page
      4. Public solicitation attachments
      5. Agency/buyer public source (if URL given)
      6. Stale cache (STALE_CACHE_ONLY — never as current open/qty alone)
    """
    attempts: list[dict[str, Any]] = []
    fields: dict[str, list[dict[str, Any]]] = {
        "open_status": [],
        "response_deadline": [],
        "quantity": [],
        "uom": [],
        "option_quantity": [],
        "solicitation_number": [],
        "set_aside": [],
        "title": [],
        "amendment": [],
        "description": [],
    }
    confirmation = LIVE_SOURCE_UNAVAILABLE
    api_failure = SAM_API_NOT_ATTEMPTED
    live_ok = False
    umbrella: str | None = None
    attachment_names: list[str] = []
    description_blob = ""

    # 1) API
    api_fn = api_fetcher or (lambda nid: fetch_sam_api_opportunity(nid, http_get=http_get))
    api = api_fn(notice_id)
    api_failure = api.get("api_failure") or (
        SAM_API_OK if api.get("ok") else SAM_API_OTHER_ERROR
    )
    attempts.append({"step": "SAM_OPPORTUNITIES_API", "ok": bool(api.get("ok") and api.get("found")), "api_failure": api_failure, "at": _utc()})
    if api.get("ok") and api.get("found") and isinstance(api.get("raw"), dict):
        raw = api["raw"]
        live_ok = True
        confirmation = LIVE_API_CONFIRMED
        url = listing_url(notice_id)
        rdue = raw.get("responseDeadLine") or raw.get("responseDeadline")
        fields["response_deadline"].append(
            field_provenance(rdue, source_type="SAM_OPPORTUNITIES_API", source_url=url)
        )
        fields["open_status"].append(
            field_provenance("OPEN", source_type="SAM_OPPORTUNITIES_API", source_url=url)
        )
        fields["solicitation_number"].append(
            field_provenance(
                raw.get("solicitationNumber") or solicitation_number,
                source_type="SAM_OPPORTUNITIES_API",
                source_url=url,
            )
        )
        fields["set_aside"].append(
            field_provenance(
                raw.get("typeOfSetAsideDescription") or raw.get("typeOfSetAside"),
                source_type="SAM_OPPORTUNITIES_API",
                source_url=url,
            )
        )
        fields["title"].append(
            field_provenance(raw.get("title"), source_type="SAM_OPPORTUNITIES_API", source_url=url)
        )
        blob = json.dumps(raw, default=str)
        q = parse_qty_uom_from_text(blob)
        if q.get("quantity") is not None:
            fields["quantity"].append(
                field_provenance(q["quantity"], source_type="SAM_OPPORTUNITIES_API", source_url=url, snippet=q.get("snippet"))
            )
        if q.get("uom"):
            fields["uom"].append(
                field_provenance(q["uom"], source_type="SAM_OPPORTUNITIES_API", source_url=url)
            )

    # 2) Public detail (always try when API did not fully confirm, or to enrich qty)
    need_public = confirmation != LIVE_API_CONFIRMED or not any(fields["quantity"])
    if need_public or api_failure != SAM_API_OK:
        # 3) page first only to obtain umbrella key if needed
        page_fn = public_page_fetcher or (lambda nid: fetch_public_sam_page(nid, http_get=http_get))
        page = page_fn(notice_id)
        attempts.append(
            {
                "step": "PUBLIC_SAM_PAGE",
                "ok": bool(page.get("ok")),
                "blocked": page.get("blocked"),
                "at": _utc(),
            }
        )
        umbrella = page.get("umbrella_key") or umbrella
        if page.get("ok") and page.get("body"):
            live_ok = True
            if confirmation == LIVE_SOURCE_UNAVAILABLE:
                confirmation = LIVE_PUBLIC_WEB_CONFIRMED
            q = parse_qty_uom_from_text(page["body"])
            url = page.get("url")
            if q.get("quantity") is not None:
                fields["quantity"].append(
                    field_provenance(q["quantity"], source_type="PUBLIC_SAM_PAGE", source_url=url, snippet=q.get("snippet"))
                )
            if q.get("uom"):
                fields["uom"].append(field_provenance(q["uom"], source_type="PUBLIC_SAM_PAGE", source_url=url))

        detail_fn = public_detail_fetcher or (
            lambda nid, key=None: fetch_public_sam_opportunity(nid, umbrella_key=key or umbrella, http_get=http_get)
        )
        try:
            detail = detail_fn(notice_id, umbrella)
        except TypeError:
            detail = detail_fn(notice_id)
        attempts.append(
            {
                "step": "PUBLIC_SAM_DETAIL",
                "ok": bool(detail.get("ok")),
                "blocked": detail.get("blocked"),
                "at": _utc(),
            }
        )
        if detail.get("ok") and isinstance(detail.get("data"), dict):
            live_ok = True
            if confirmation in {LIVE_SOURCE_UNAVAILABLE, STALE_CACHE_ONLY}:
                confirmation = LIVE_PUBLIC_WEB_CONFIRMED
            elif confirmation == LIVE_API_CONFIRMED:
                pass  # keep API as primary confirmation; public enriches
            else:
                confirmation = LIVE_PUBLIC_WEB_CONFIRMED
            norm = normalize_public_opportunity(detail["data"])
            url = detail.get("url")
            fields["open_status"].append(
                field_provenance(norm.get("open_status"), source_type="PUBLIC_SAM_DETAIL", source_url=url)
            )
            fields["response_deadline"].append(
                field_provenance(norm.get("response_deadline"), source_type="PUBLIC_SAM_DETAIL", source_url=url)
            )
            fields["solicitation_number"].append(
                field_provenance(
                    norm.get("solicitation_number") or solicitation_number,
                    source_type="PUBLIC_SAM_DETAIL",
                    source_url=url,
                )
            )
            fields["set_aside"].append(
                field_provenance(norm.get("set_aside"), source_type="PUBLIC_SAM_DETAIL", source_url=url)
            )
            fields["title"].append(
                field_provenance(norm.get("title"), source_type="PUBLIC_SAM_DETAIL", source_url=url)
            )
            fields["amendment"].append(
                field_provenance(
                    f"version={norm.get('version')}; latest={norm.get('latest')}; modified={norm.get('modified_date')}",
                    source_type="PUBLIC_SAM_DETAIL",
                    source_url=url,
                )
            )
            description_blob = str(norm.get("description") or "")
            fields["description"].append(
                field_provenance(description_blob[:2000], source_type="PUBLIC_SAM_DETAIL", source_url=url)
            )
            q = parse_qty_uom_from_text(description_blob)
            if q.get("quantity") is not None:
                fields["quantity"].append(
                    field_provenance(
                        q["quantity"],
                        source_type="PUBLIC_SAM_DETAIL",
                        source_url=url,
                        snippet=q.get("snippet"),
                    )
                )
            if q.get("uom"):
                fields["uom"].append(
                    field_provenance(q["uom"], source_type="PUBLIC_SAM_DETAIL", source_url=url)
                )
            if q.get("option_quantity") is not None:
                fields["option_quantity"].append(
                    field_provenance(q["option_quantity"], source_type="PUBLIC_SAM_DETAIL", source_url=url)
                )

            # 4) Attachments when qty/uom still missing or to confirm amendment
            if fetch_attachments and (
                not any(f.get("value") is not None for f in fields["quantity"])
                or not any(f.get("value") for f in fields["uom"])
            ):
                list_fn = attachment_list_fetcher or (
                    lambda nid, key=None: fetch_public_attachment_list(
                        nid, umbrella_key=key or umbrella, http_get=http_get
                    )
                )
                try:
                    alist = list_fn(notice_id, umbrella)
                except TypeError:
                    alist = list_fn(notice_id)
                attempts.append(
                    {
                        "step": "PUBLIC_ATTACHMENTS_LIST",
                        "ok": bool(alist.get("ok")),
                        "count": len(alist.get("attachments") or []),
                        "at": _utc(),
                    }
                )
                # Prefer amendment / base solicitation PDFs
                atts = list(alist.get("attachments") or [])
                atts.sort(
                    key=lambda a: (
                        0 if "0001" in str(a.get("name") or "") or "amend" in str(a.get("name") or "").lower() else 1,
                        0 if str(a.get("name") or "").lower().endswith(".pdf") else 1,
                    )
                )
                dl_fn = attachment_downloader or (
                    lambda rid, key=None: download_public_attachment(
                        rid, umbrella_key=key or umbrella, http_get=http_get
                    )
                )
                for att in atts[:max_attachments]:
                    name = str(att.get("name") or "attachment.pdf")
                    if not name.lower().endswith(".pdf"):
                        continue
                    try:
                        dl = dl_fn(str(att["resource_id"]), umbrella)
                    except TypeError:
                        dl = dl_fn(str(att["resource_id"]))
                    attempts.append(
                        {
                            "step": "PUBLIC_ATTACHMENT_DOWNLOAD",
                            "name": name,
                            "ok": bool(dl.get("ok")),
                            "at": _utc(),
                        }
                    )
                    if not dl.get("ok"):
                        continue
                    text = extract_text_from_attachment_bytes(dl["content"], filename=name)
                    if not text:
                        continue
                    attachment_names.append(name)
                    live_ok = True
                    confirmation = LIVE_ATTACHMENT_CONFIRMED
                    src_url = dl.get("url")
                    # Amendment PDFs preferred for deadline; base for qty
                    src_type = (
                        "LATEST_AMENDMENT"
                        if ("0001" in name or "amend" in name.lower())
                        else "SOLICITATION_ATTACHMENT"
                    )
                    q = parse_qty_uom_from_text(text)
                    if q.get("quantity") is not None:
                        fields["quantity"].append(
                            field_provenance(
                                q["quantity"],
                                source_type=src_type,
                                source_url=src_url,
                                snippet=q.get("snippet"),
                            )
                        )
                    if q.get("uom"):
                        fields["uom"].append(
                            field_provenance(q["uom"], source_type=src_type, source_url=src_url)
                        )
                    if q.get("option_quantity") is not None:
                        fields["option_quantity"].append(
                            field_provenance(
                                q["option_quantity"],
                                source_type=src_type,
                                source_url=src_url,
                            )
                        )
                    # Deadline from amendment text
                    m_due = re.search(
                        r"final bid submissions? is hereby extended to\s+"
                        r"([A-Za-z]+ \d{1,2}, \d{4}(?:,?\s+at\s+[\d:]+ ?[AP]M\s*[A-Z]{2,4})?)",
                        text,
                        re.I,
                    )
                    if m_due:
                        fields["response_deadline"].append(
                            field_provenance(
                                m_due.group(1),
                                source_type="LATEST_AMENDMENT",
                                source_url=src_url,
                                snippet=m_due.group(0)[:160],
                            )
                        )
                    fields["amendment"].append(
                        field_provenance(name, source_type=src_type, source_url=src_url)
                    )
                    description_blob = (description_blob + "\n" + text[:8000]).strip()
                    if q.get("confirmed"):
                        break

    # 5) Agency source
    if agency_url and confirmation == LIVE_SOURCE_UNAVAILABLE:
        ag_fn = agency_fetcher
        if ag_fn is not None:
            agency = ag_fn(agency_url)
            attempts.append({"step": "AGENCY_PUBLIC_SOURCE", "ok": bool(agency.get("ok")), "at": _utc()})
            if agency.get("ok"):
                live_ok = True
                confirmation = LIVE_AGENCY_SOURCE_CONFIRMED
                body = str(agency.get("body") or "")
                url = agency_url
                q = parse_qty_uom_from_text(body)
                if q.get("quantity") is not None:
                    fields["quantity"].append(
                        field_provenance(
                            q["quantity"],
                            source_type="AGENCY_PUBLIC_SOURCE",
                            source_url=url,
                            snippet=q.get("snippet"),
                        )
                    )
                if q.get("uom"):
                    fields["uom"].append(
                        field_provenance(q["uom"], source_type="AGENCY_PUBLIC_SOURCE", source_url=url)
                    )
                if agency.get("response_deadline"):
                    fields["response_deadline"].append(
                        field_provenance(
                            agency.get("response_deadline"),
                            source_type="AGENCY_PUBLIC_SOURCE",
                            source_url=url,
                        )
                    )
                if agency.get("open_status"):
                    fields["open_status"].append(
                        field_provenance(
                            agency.get("open_status"),
                            source_type="AGENCY_PUBLIC_SOURCE",
                            source_url=url,
                        )
                    )

    # 6) Stale cache — comparison only; never upgrades to live confirmation alone
    if stale_cache:
        attempts.append({"step": "STALE_CACHE", "ok": True, "at": _utc()})
        if confirmation == LIVE_SOURCE_UNAVAILABLE:
            confirmation = STALE_CACHE_ONLY
        for key, src_key in (
            ("open_status", "open_status"),
            ("response_deadline", "response_deadline"),
            ("quantity", "quantity"),
            ("uom", "uom"),
            ("solicitation_number", "solicitation_number"),
        ):
            if stale_cache.get(src_key) is not None:
                fields[key].append(
                    field_provenance(
                        stale_cache.get(src_key),
                        source_type="STALE_CACHE",
                        source_url=stale_cache.get("source_url"),
                        snippet="stale_cache_only",
                    )
                )

    resolved = {k: prefer_live_field(v) for k, v in fields.items()}
    conflicts = [k for k, v in resolved.items() if v.get("conflict")]

    # Stale cache alone cannot satisfy live verification
    live_verified = confirmation in {
        LIVE_API_CONFIRMED,
        LIVE_PUBLIC_WEB_CONFIRMED,
        LIVE_AGENCY_SOURCE_CONFIRMED,
        LIVE_ATTACHMENT_CONFIRMED,
    }
    if not live_verified:
        confirmation = LIVE_SOURCE_UNAVAILABLE if confirmation != STALE_CACHE_ONLY else STALE_CACHE_ONLY

    qty_val = resolved["quantity"].get("value")
    uom_val = resolved["uom"].get("value")
    qty_confirmed = qty_val is not None and uom_val is not None and live_verified

    return {
        "kind": "SamLiveRetrieval",
        "notice_id": notice_id,
        "listing_url": listing_url(notice_id),
        "confirmation": confirmation,
        "live_verified": live_verified,
        "api_failure": api_failure,
        "discovery_coverage": discovery_coverage_after_api_failure(
            None if api_failure == SAM_API_OK else api_failure
        ),
        "attempts": attempts,
        "fields": resolved,
        "conflicts": conflicts,
        "conflict_flag": SOURCE_CONFLICT if conflicts else None,
        "attachment_names": attachment_names,
        "description_excerpt": description_blob[:2500],
        "qty_uom": {
            "quantity": qty_val,
            "uom": uom_val,
            "option_quantity": resolved["option_quantity"].get("value"),
            "confirmed": bool(qty_confirmed),
            "status": "CONFIRMED" if qty_confirmed else "LIVE_QTY_UOM_UNCONFIRMED",
            "provenance": {
                "quantity": resolved["quantity"],
                "uom": resolved["uom"],
                "option_quantity": resolved["option_quantity"],
            },
        },
        "open_status": resolved["open_status"].get("value"),
        "response_deadline": resolved["response_deadline"].get("value"),
        "solicitation_number": resolved["solicitation_number"].get("value") or solicitation_number,
        "set_aside": resolved["set_aside"].get("value"),
        "title": resolved["title"].get("value"),
        "amendment": resolved["amendment"].get("value"),
        "retrieved_at": _utc(),
        "LIVE_OK": live_ok,
    }
