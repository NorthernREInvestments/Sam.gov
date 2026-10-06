"""Post-auth OpenGov vendor opportunity search — prefer structured endpoints."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin

from opengov_auth.config import M3_DISCOVERY_SCOPE
from opengov_discovery.parse import parse_opengov_json_payload, parse_opengov_portal_html

log = logging.getLogger("govtracker.opengov_discovery.vendor_search")

# Candidate authenticated vendor surfaces (tried in order)
VENDOR_SEARCH_URLS = (
    "https://procurement.opengov.com/vendor",
    "https://procurement.opengov.com/",
    "https://procurement.opengov.com/network",
    "https://procurement.opengov.com/vendor/projects",
    "https://procurement.opengov.com/vendor/opportunities",
)

INTERESTING_API = re.compile(
    r"(project|opportunit|solicit|search|bid|proposal|/api/|/graphql|gateway)",
    re.I,
)


def harvest_vendor_search(
    client: Any,
    *,
    max_results: int = 100,
    max_pages: int = 10,
) -> dict[str, Any]:
    """After auth, find vendor-wide opportunity list via DOM + captured XHR/JSON."""
    out: dict[str, Any] = {
        "mode": "vendor_global_search",
        "search_url": None,
        "search_reachable": False,
        "data_endpoints": [],
        "reported_total": None,
        "retrieved_total": 0,
        "pages_scanned": 0,
        "pagination_complete": False,
        "rows": [],
        "error": None,
    }
    page = getattr(client, "_page", None)
    if page is None:
        out["error"] = "no_page"
        return out

    captured: list[dict[str, Any]] = []

    def _on_response(response: Any) -> None:
        try:
            if response.status != 200:
                return
            u = response.url or ""
            ctype = (response.headers or {}).get("content-type", "")
            if "application/json" not in ctype and not INTERESTING_API.search(u):
                return
            if "application/json" in ctype or "/api/" in u.lower() or "graphql" in u.lower():
                try:
                    data = response.json()
                    captured.append({"url": u[:220], "data": data})
                    if len(out["data_endpoints"]) < 30:
                        out["data_endpoints"].append(u[:220])
                except Exception:
                    pass
        except Exception:
            pass

    page.on("response", _on_response)
    all_rows: list[dict[str, Any]] = []
    try:
        # Block heavy assets for speed (safe for JSON/HTML content)
        try:
            if hasattr(client, "enable_resource_blocking"):
                client.enable_resource_blocking()
        except Exception:
            pass

        reached = False
        for url in VENDOR_SEARCH_URLS:
            try:
                # Prefer domcontentloaded — do not wait for full networkidle
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                try:
                    page.wait_for_timeout(2_500)
                except Exception:
                    pass
                html = page.content()
                final = page.url or url
                # Skip if bounced to login
                if "/login" in (final or "").lower() and "password" in (html or "").lower():
                    continue
                reached = True
                out["search_url"] = final[:220]
                out["search_reachable"] = True

                # Click Find bids / Opportunities if present
                for name in (
                    r"find\s*bids?",
                    r"opportunities",
                    r"open\s*projects?",
                    r"browse\s*bids?",
                    r"search",
                ):
                    try:
                        btn = page.get_by_role("link", name=re.compile(name, re.I)).first
                        if btn.count() == 0:
                            btn = page.get_by_role("button", name=re.compile(name, re.I)).first
                        if btn.count() > 0 and btn.is_visible(timeout=1_000):
                            btn.click(timeout=8_000)
                            page.wait_for_timeout(2_000)
                            break
                    except Exception:
                        continue

                html = page.content()
                page_url = page.url or final
                for blob in captured:
                    recs = parse_opengov_json_payload(
                        blob.get("data"), list_url=page_url, agency=None
                    )
                    all_rows.extend(recs)
                all_rows.extend(parse_opengov_portal_html(html, list_url=page_url, agency=None))

                # Extract reported total
                for blob in captured:
                    data = blob.get("data")
                    if isinstance(data, dict):
                        for key in ("total", "totalCount", "totalElements", "count"):
                            if data.get(key) is not None:
                                try:
                                    out["reported_total"] = int(data[key])
                                except (TypeError, ValueError):
                                    pass
                if out["reported_total"] is None:
                    m = re.search(r"([\d,]+)\s+(?:projects?|results?|opportunities|bids?)", html or "", re.I)
                    if m:
                        out["reported_total"] = int(m.group(1).replace(",", ""))

                if all_rows:
                    break
            except Exception as exc:
                out["error"] = type(exc).__name__
                continue

        if not reached:
            out["error"] = out.get("error") or "vendor_search_unreachable"
            return out

        # Pagination via ?page= or Next
        pages = 1
        while len(all_rows) < max_results and pages < max_pages:
            if out["reported_total"] is not None and len(all_rows) >= int(out["reported_total"]):
                break
            progressed = False
            try:
                nxt = page.get_by_role("button", name=re.compile(r"next", re.I)).first
                if nxt.count() == 0:
                    nxt = page.get_by_role("link", name=re.compile(r"next", re.I)).first
                if nxt.count() > 0 and nxt.is_enabled():
                    before = len(all_rows)
                    nxt.click(timeout=8_000)
                    page.wait_for_timeout(1_500)
                    html2 = page.content()
                    batch = parse_opengov_portal_html(html2, list_url=page.url or "", agency=None)
                    for blob in captured[-5:]:
                        batch.extend(
                            parse_opengov_json_payload(blob.get("data"), list_url=page.url or "", agency=None)
                        )
                    existing = {str(r.get("detail_url") or r.get("external_id")) for r in all_rows}
                    newb = [r for r in batch if str(r.get("detail_url") or r.get("external_id")) not in existing]
                    if newb:
                        all_rows.extend(newb)
                        progressed = True
                        pages += 1
                    elif len(all_rows) == before:
                        break
            except Exception:
                break
            if not progressed:
                # query page param
                cur = page.url or ""
                page_idx = pages + 1
                if "page=" in cur.lower():
                    nu = re.sub(r"([?&]page=)\d+", rf"\g<1>{page_idx}", cur, flags=re.I)
                else:
                    sep = "&" if "?" in cur else "?"
                    nu = f"{cur}{sep}page={page_idx}"
                try:
                    page.goto(nu, wait_until="domcontentloaded", timeout=40_000)
                    page.wait_for_timeout(1_500)
                    batch = parse_opengov_portal_html(page.content(), list_url=nu, agency=None)
                    existing = {str(r.get("detail_url") or r.get("external_id")) for r in all_rows}
                    newb = [r for r in batch if str(r.get("detail_url") or r.get("external_id")) not in existing]
                    if not newb:
                        break
                    all_rows.extend(newb)
                    pages += 1
                except Exception:
                    break

        # Normalize
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for r in all_rows:
            key = str(r.get("detail_url") or r.get("external_id") or r.get("title") or "").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            d = dict(r)
            d["source_id"] = d.get("source_id") or "live_opengov"
            d["platform"] = "live_opengov"
            d["platform_family"] = "OpenGov"
            d["discovery_scope"] = M3_DISCOVERY_SCOPE
            d["discovery_universe"] = "LIVE"
            meta = dict(d.get("raw_metadata") or {})
            meta["harvest_mode"] = "opengov_vendor_global_search"
            meta["vendor_profile_codes_ignored"] = True
            d["raw_metadata"] = meta
            unique.append(d)
            if len(unique) >= max_results:
                break

        out["rows"] = unique
        out["retrieved_total"] = len(unique)
        out["pages_scanned"] = pages
        out["pagination_complete"] = bool(
            out["reported_total"] is not None and len(unique) >= int(out["reported_total"])
        ) or (out["reported_total"] is None and pages < max_pages and len(unique) < max_results)
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass
    return out
