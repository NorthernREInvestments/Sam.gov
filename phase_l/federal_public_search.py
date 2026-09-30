"""Phase L public SAM listing discovery (no personal API key required).

Uses the public SPA SGS search API (HAL+JSON) — same surface as sam.gov UI.
On failure: DISCOVERY_DEGRADED — never DISCOVERY_IMPOSSIBLE.
"""

from __future__ import annotations

from typing import Any

from application_clock import now_utc
from sam_live_fallback import DISCOVERY_DEGRADED, fetch_public_sam_page

_DEFAULT_KEYWORDS = [
    "NSN",
    "parts kit",
    "monitor",
    "printer",
    "UPS",
    "radio",
    "toolkit",
    "test set",
    "battery",
    "scanner",
    "copier",
    "LED",
    "pump",
    "valve",
    "bearing",
    "filter",
    "cable",
    "hose",
    "switch",
    "transformer",
    "laptop",
    "server",
    "furniture",
    "PPE",
]

_SGS_SEARCH = "https://sam.gov/api/prod/sgs/v1/search/"


def _utc() -> str:
    return now_utc().isoformat()


def _umbrella() -> str | None:
    page = fetch_public_sam_page("40c00954331b4d67953ad235dc2242b5")
    return page.get("umbrella_key")


def _headers(umbrella: str | None) -> dict[str, str]:
    h = {
        "User-Agent": "Mozilla/5.0 (compatible; GovTracker-M3/1.0)",
        "Accept": "application/hal+json",
    }
    if umbrella:
        h["x-api-key"] = umbrella
    return h


def _hit_to_row(h: dict[str, Any]) -> dict[str, Any]:
    nid = h.get("_id") or h.get("noticeId") or h.get("opportunityId")
    descs = h.get("descriptions") or []
    desc = ""
    if descs and isinstance(descs[0], dict):
        desc = str(descs[0].get("content") or "")
    org = h.get("organizationHierarchy") or []
    agency = None
    if org and isinstance(org[0], dict):
        agency = org[0].get("name")
    return {
        "external_id": str(nid or h.get("solicitationNumber") or h.get("title") or "")[:80],
        "notice_id": nid,
        "title": h.get("title") or "",
        "source_id": "fed_sam_public_search",
        "source_level": "FEDERAL",
        "buyer_type": "FEDERAL",
        "jurisdiction": "FEDERAL",
        "status": "OPEN" if h.get("isActive") and not h.get("isCanceled") else "UNKNOWN",
        "live_status": "OPEN" if h.get("isActive") and not h.get("isCanceled") else "UNKNOWN",
        "detail_url": f"https://sam.gov/opp/{nid}/view" if nid else None,
        "description": desc,
        "set_aside": h.get("typeOfSetAside") or h.get("setAside"),
        "response_deadline": h.get("responseDateActual") or h.get("responseDate"),
        "deadline_timezone": h.get("responseTimeZone"),
        "solicitation_number": h.get("solicitationNumber"),
        "agency": agency,
        "raw_metadata": {k: h.get(k) for k in ("type", "publishDate", "modifiedDate", "isActive")},
        "retrieved_at": _utc(),
    }


def public_sam_opportunity_search(
    *,
    keywords: list[str] | None = None,
    page_size: int = 100,
    max_pages_per_keyword: int = 2,
    max_total: int = 600,
    http_get: Any | None = None,
    umbrella_key: str | None = None,
) -> dict[str, Any]:
    """
    Best-effort public SAM opportunity search via SGS HAL API.
    """
    import httpx

    keywords = keywords or _DEFAULT_KEYWORDS
    umbrella = umbrella_key or _umbrella()
    headers = _headers(umbrella)
    rows: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    coverage = "DISCOVERY_NORMAL"
    page_size = max(1, min(int(page_size), 100))

    for kw in keywords:
        if len(rows) >= max_total:
            break
        for page in range(max_pages_per_keyword):
            if len(rows) >= max_total:
                break
            params = {
                "index": "opp",
                "page": page,
                "size": page_size,
                "mode": "search",
                "is_active": "true",
                "q": kw,
            }
            try:
                if http_get is not None:
                    resp = http_get(_SGS_SEARCH, headers=headers, params=params)
                else:
                    with httpx.Client(timeout=35.0, follow_redirects=True) as client:
                        resp = client.get(_SGS_SEARCH, headers=headers, params=params)
                status = getattr(resp, "status_code", None)
                attempts.append({"keyword": kw, "page": page, "status": status, "at": _utc()})
                if status != 200:
                    coverage = DISCOVERY_DEGRADED
                    break
                data = resp.json() if hasattr(resp, "json") else {}
                hits = ((data.get("_embedded") or {}).get("results")) or []
                if not hits:
                    break
                for h in hits:
                    if isinstance(h, dict):
                        rows.append(_hit_to_row(h))
                total_pages = int(((data.get("page") or {}).get("totalPages") or 1))
                if page + 1 >= total_pages:
                    break
            except Exception as exc:  # noqa: BLE001
                coverage = DISCOVERY_DEGRADED
                attempts.append({"keyword": kw, "page": page, "error": str(exc)[:160], "at": _utc()})
                break

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for r in rows:
        key = str(r.get("notice_id") or r.get("title") or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(r)
        if len(unique) >= max_total:
            break

    if not unique:
        coverage = DISCOVERY_DEGRADED

    return {
        "ok": bool(unique),
        "discovery_coverage": coverage,
        "rows": unique,
        "attempts": attempts,
        "count": len(unique),
        "retrieved_at": _utc(),
    }
