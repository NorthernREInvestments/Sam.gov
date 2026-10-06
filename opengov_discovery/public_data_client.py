"""OpenGovPublicDataClient — structured/public JSON + hydration extraction (no API key)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from opengov_discovery.parse import parse_opengov_json_payload, parse_opengov_portal_html

log = logging.getLogger("govtracker.opengov_discovery.public_data_client")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_HYDRATION_PATTERNS = (
    r'<script[^>]*type=["\']application/json["\'][^>]*>([\s\S]*?)</script>',
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>([\s\S]*?)</script>',
    r'window\.__INITIAL_STATE__\s*=\s*(\{[\s\S]*?\});?\s*</script>',
    r'window\.__PRELOADED_STATE__\s*=\s*(\{[\s\S]*?\});?\s*</script>',
    r'window\.__APOLLO_STATE__\s*=\s*(\{[\s\S]*?\});?\s*</script>',
    r'"projects"\s*:\s*(\[[\s\S]{20,500000}?\])\s*[,}]',
)


def is_cloudflare(body: str) -> bool:
    low = (body or "").lower()
    return ("just a moment" in low and "cloudflare" in low) or "cf-browser-verification" in low


def extract_hydration_payloads(html: str) -> list[Any]:
    """Pull JSON blobs from HTML/JS without Playwright."""
    out: list[Any] = []
    body = html or ""
    for pat in _HYDRATION_PATTERNS:
        for m in re.finditer(pat, body, re.I):
            raw = (m.group(1) or "").strip()
            if not raw:
                continue
            # Trim trailing junk for window.* assignments
            if raw.endswith(";"):
                raw = raw[:-1]
            try:
                out.append(json.loads(raw))
            except Exception:
                # Try to find nested object
                start = raw.find("{")
                end = raw.rfind("}")
                if start >= 0 and end > start:
                    try:
                        out.append(json.loads(raw[start : end + 1]))
                    except Exception:
                        pass
    return out


class OpenGovPublicDataClient:
    """HTTP client for public OpenGov list/detail structured data."""

    def __init__(self, *, timeout: float = 35.0, client: httpx.Client | None = None) -> None:
        self._owns = client is None
        self._client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": UA, "Accept": "text/html,application/json,*/*"},
        )

    def close(self) -> None:
        if self._owns:
            try:
                self._client.close()
            except Exception:
                pass

    def __enter__(self) -> "OpenGovPublicDataClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def fetch(self, url: str) -> dict[str, Any]:
        # Structured government project list is POST-only
        if re.search(r"/government/[^/]+/project/public/?$", url or "", re.I):
            return self.fetch_project_public_url(url, max_pages=1)
        try:
            r = self._client.get(url)
            body = r.text or ""
            return {
                "ok": 200 <= r.status_code < 400,
                "status_code": r.status_code,
                "final_url": str(r.url),
                "body": body,
                "cloudflare": is_cloudflare(body),
                "content_type": (r.headers.get("content-type") or "").lower(),
            }
        except Exception as exc:
            return {
                "ok": False,
                "status_code": 0,
                "final_url": url,
                "body": "",
                "cloudflare": False,
                "error": type(exc).__name__,
            }

    def fetch_project_public(
        self,
        code: str,
        *,
        page_size: int = 50,
        max_pages: int = 40,
        open_only: bool = False,
    ) -> dict[str, Any]:
        """POST government/{code}/project/public with offset pagination."""
        from opengov_discovery.government_directory import PROJECT_PUBLIC_TMPL

        url = PROJECT_PUBLIC_TMPL.format(code=str(code).strip().lower())
        return self.fetch_project_public_url(
            url, page_size=page_size, max_pages=max_pages, open_only=open_only
        )

    def fetch_project_public_url(
        self,
        url: str,
        *,
        page_size: int = 50,
        max_pages: int = 40,
        open_only: bool = False,
    ) -> dict[str, Any]:
        all_rows: list[dict[str, Any]] = []
        reported = None
        pages = 0
        offset = 0
        last_status = 0
        error = None
        try:
            for _ in range(max(1, max_pages)):
                r = self._client.post(
                    url,
                    json={"limit": page_size, "offset": offset},
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "Origin": "https://procurement.opengov.com",
                        "Referer": "https://procurement.opengov.com/",
                    },
                )
                last_status = r.status_code
                body = r.text or ""
                if is_cloudflare(body):
                    return {
                        "ok": False,
                        "status_code": last_status,
                        "final_url": url,
                        "body": body,
                        "cloudflare": True,
                        "content_type": (r.headers.get("content-type") or "").lower(),
                        "rows": [],
                        "reported_total": None,
                        "pages_scanned": pages,
                        "pagination_complete": False,
                    }
                if r.status_code == 404:
                    return {
                        "ok": False,
                        "status_code": 404,
                        "final_url": url,
                        "body": body,
                        "cloudflare": False,
                        "content_type": (r.headers.get("content-type") or "").lower(),
                        "rows": [],
                        "reported_total": None,
                        "pages_scanned": pages,
                        "pagination_complete": True,
                        "error": "http_404",
                    }
                if not (200 <= r.status_code < 400):
                    error = f"http_{r.status_code}"
                    break
                try:
                    data = r.json()
                except Exception:
                    error = "json_decode"
                    break
                if isinstance(data, dict) and data.get("count") is not None:
                    try:
                        reported = int(data["count"])
                    except (TypeError, ValueError):
                        pass
                batch = data.get("rows") if isinstance(data, dict) else None
                if not isinstance(batch, list):
                    batch = []
                pages += 1
                if not batch:
                    break
                all_rows.extend([x for x in batch if isinstance(x, dict)])
                offset += len(batch)
                if reported is not None and offset >= reported:
                    break
                if len(batch) < page_size:
                    break
        except Exception as exc:
            error = type(exc).__name__

        if open_only:
            all_rows = [x for x in all_rows if str(x.get("status") or "").lower() == "open"]

        complete = False
        if error:
            complete = False
        elif reported is not None:
            complete = offset >= int(reported)
        elif pages > 0:
            # Unknown total — complete only when a short page ends the walk
            complete = True

        # Serialize combined payload so parse_list can reuse JSON path
        payload = json.dumps({"count": reported, "rows": all_rows})
        return {
            # HTTP 200 with zero open rows is still a successful structured read
            "ok": (last_status == 200 and error is None) or bool(all_rows),
            "status_code": last_status or (200 if all_rows else 0),
            "final_url": url,
            "body": payload,
            "cloudflare": False,
            "content_type": "application/json",
            "rows": all_rows,
            "reported_total": reported,
            "pages_scanned": pages,
            "pagination_complete": complete,
            "retrieved_total": len(all_rows),
            "error": error,
        }

    def parse_list(
        self,
        fetch_result: dict[str, Any],
        *,
        agency: str | None = None,
    ) -> dict[str, Any]:
        """Parse JSON body, hydration, or HTML into opportunity rows."""
        url = fetch_result.get("final_url") or ""
        body = fetch_result.get("body") or ""
        rows: list[dict[str, Any]] = []
        method = None
        if fetch_result.get("cloudflare"):
            return {"rows": [], "method": None, "cloudflare": True}

        text = body.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                data = json.loads(text)
                rows = parse_opengov_json_payload(data, list_url=url, agency=agency)
                method = "public_json"
            except Exception:
                rows = []

        if not rows:
            for payload in extract_hydration_payloads(body):
                batch = parse_opengov_json_payload(payload, list_url=url, agency=agency)
                if batch:
                    rows.extend(batch)
                    method = "hydration_json"
                    break

        if not rows:
            rows = parse_opengov_portal_html(body, list_url=url, agency=agency)
            if rows:
                method = "public_html"

        # Dedupe
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for r in rows:
            key = str(r.get("detail_url") or r.get("external_id") or r.get("title") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(r)

        reported = None
        if text.startswith("{"):
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    for k in ("total", "totalCount", "count", "totalElements"):
                        if data.get(k) is not None:
                            reported = int(data[k])
                            break
            except Exception:
                pass

        return {
            "rows": deduped,
            "method": method,
            "reported_total": reported,
            "cloudflare": False,
        }
