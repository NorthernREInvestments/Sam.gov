"""EunaSupplierNetworkClient — session-bound central opportunity discovery."""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urljoin

from euna_auth.config import ACCOUNT_CATEGORY_RESTRICTION, M3_DISCOVERY_SCOPE

log = logging.getLogger("govtracker.euna_discovery.supplier_network")

CENTRAL_SEARCH_CANDIDATES = (
    "https://vendor.bonfirehub.com/opportunities",
    "https://vendor.bonfirehub.com/opportunities?status=Open",
    "https://vendor.bonfirehub.com/opportunities/open",
    "https://vendor.bonfirehub.com/browse",
    "https://vendor.bonfirehub.com/recommendations",
    "https://vendor.bonfirehub.com/dashboard",
    "https://vendor.bonfirehub.com/agencies/search",
)


def _to_record(raw: dict[str, Any], *, list_url: str) -> dict[str, Any]:
    d = dict(raw)
    d["source_id"] = "euna_supplier_network"
    d["platform"] = "live_bonfire"
    d["platform_family"] = "Bonfire"
    d["status"] = d.get("status") or "OPEN"
    d["discovery_universe"] = "LIVE"
    d["discovery_scope"] = M3_DISCOVERY_SCOPE
    d["authoritative_url"] = d.get("detail_url") or list_url
    meta = dict(d.get("raw_metadata") or {})
    meta["discovery_scope"] = M3_DISCOVERY_SCOPE
    meta["platform_family"] = "EUNA_SUPPLIER_NETWORK"
    meta["discovery_method"] = "CENTRAL_SUPPLIER_NETWORK"
    meta["vendor_profile_codes_ignored"] = True
    d["raw_metadata"] = meta
    return d


def _reported_total_from_html(html: str) -> int | None:
    for pat in (
        r"([\d,]+)\s+Total\s+Opportunit",
        r"of\s+([\d,]+)\s+(?:results?|opportunit)",
        r"Showing\s+\d+\s*[–\-]\s*\d+\s+of\s+([\d,]+)",
        r'"total(?:Count|Results|Elements)?"\s*:\s*(\d+)',
    ):
        m = re.search(pat, html or "", re.I)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


def _parse_cards(html: str, *, page_url: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    patterns = (
        r'href=["\']([^"\']*(?:/projects?/|/opportunity/|/opportunities/|/Portal/|/portal/)[^"\']+)["\']',
        r'href=["\']([^"\']*ProjectPublic[^"\']+)["\']',
        r'href=["\']([^"\']*(?:bonfirehub\.com)[^"\']*(?:project|opportunity)[^"\']+)["\']',
    )
    for pat in patterns:
        for m in re.finditer(pat, html or "", re.I):
            href = m.group(1)
            if href.startswith("/"):
                href = urljoin(page_url, href)
            if href in seen or href.endswith("#"):
                continue
            start = max(0, m.start() - 40)
            end = min(len(html), m.end() + 500)
            chunk = html[start:end]
            title_m = re.search(r">\s*([^<]{8,240})\s*<", chunk)
            title = re.sub(r"\s+", " ", (title_m.group(1) if title_m else "")).strip()
            if not title or title.lower() in {"view", "details", "learn more", "open", "view all"}:
                continue
            seen.add(href)
            rows.append(
                {
                    "external_id": href.rstrip("/").split("/")[-1][:160],
                    "title": title[:300],
                    "detail_url": href,
                    "source_url": page_url,
                    "agency": None,
                    "deadline_raw": None,
                    "status": "OPEN",
                    "raw_metadata": {"href_source": "central_card"},
                }
            )
    return rows


def _dom_extract_opportunities(page: Any) -> list[dict[str, Any]]:
    """Pull opportunity-like rows from the live DOM (SPA-friendly)."""
    try:
        data = page.evaluate(
            """() => {
              const out = [];
              const seen = new Set();
              const push = (title, href, agency, closeDate) => {
                if (!title || title.length < 8) return;
                const key = (href || title).toLowerCase();
                if (seen.has(key)) return;
                seen.add(key);
                out.push({title, href, agency, closeDate});
              };
              // Anchor cards
              for (const a of document.querySelectorAll('a[href]')) {
                const href = a.href || '';
                if (!/project|opportunit|portal|solicitation/i.test(href)) continue;
                const title = (a.innerText || a.textContent || '').trim().split('\\n')[0].trim();
                if (title) push(title.slice(0,300), href, null, null);
              }
              // Table rows
              for (const tr of document.querySelectorAll('table tr, [role=row]')) {
                const tds = [...tr.querySelectorAll('td, [role=cell]')].map(td => (td.innerText||'').trim());
                const a = tr.querySelector('a[href]');
                if (!a && tds.length < 2) continue;
                const title = (a && (a.innerText||'').trim()) || tds[0] || '';
                const href = a ? a.href : null;
                if (title.length >= 8) push(title.slice(0,300), href, tds[1] || null, tds.find(x => /\\d{4}/.test(x)) || null);
              }
              // List items / cards
              for (const card of document.querySelectorAll('[class*=opportunit i], [class*=project i], [data-testid*=opportunit i]')) {
                const a = card.querySelector('a[href]');
                const title = ((a && a.innerText) || card.innerText || '').trim().split('\\n')[0].trim();
                if (title) push(title.slice(0,300), a ? a.href : null, null, null);
              }
              return out.slice(0, 500);
            }"""
        )
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for item in data or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        href = item.get("href")
        if not title:
            continue
        rows.append(
            {
                "external_id": str(href or title).rstrip("/").split("/")[-1][:160],
                "title": title[:300],
                "detail_url": href,
                "source_url": None,
                "agency": item.get("agency"),
                "deadline_raw": item.get("closeDate"),
                "status": "OPEN",
                "raw_metadata": {"href_source": "dom_extract"},
            }
        )
    return rows


def _extract_from_payloads(payloads: list[Any], *, page_url: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for blob in payloads:
        data = blob.get("data") if isinstance(blob, dict) else blob
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                title = cur.get("title") or cur.get("name") or cur.get("projectName")
                href = (
                    cur.get("url")
                    or cur.get("href")
                    or cur.get("detailUrl")
                    or cur.get("projectUrl")
                    or cur.get("opportunityUrl")
                )
                oid = cur.get("id") or cur.get("projectId") or cur.get("opportunityId")
                if title and (href or oid):
                    key = str(href or oid)
                    if key not in seen:
                        seen.add(key)
                        rows.append(
                            {
                                "external_id": str(oid or key)[:160],
                                "title": str(title)[:300],
                                "detail_url": str(href) if href else None,
                                "source_url": page_url,
                                "agency": cur.get("agency")
                                or cur.get("organizationName")
                                or cur.get("agencyName"),
                                "deadline_raw": cur.get("closeDate")
                                or cur.get("dueDate")
                                or cur.get("closingDate"),
                                "status": cur.get("status") or "OPEN",
                                "raw_metadata": {"href_source": "xhr_json"},
                            }
                        )
                stack.extend(list(cur.values())[:80])
            elif isinstance(cur, list):
                stack.extend(cur[:200])
    return rows


_API_CANDIDATES = (
    "https://common-production-api-global.bonfirehub.com/v1.0/vendors/me/opportunities",
    "https://common-production-api-global.bonfirehub.com/v1.0/vendors/me/opportunities?status=Open",
    "https://common-production-api-global.bonfirehub.com/v1.0/vendors/me/opportunities?status=open",
    "https://common-production-api-global.bonfirehub.com/v1.0/vendors/me/projects",
    "https://common-production-api-global.bonfirehub.com/v1.0/vendors/me/recommendations",
    "https://common-production-api-global.bonfirehub.com/v1.0/opportunities",
    "https://common-production-api-global.bonfirehub.com/v1.0/opportunities/search",
    "https://common-production-api-global.bonfirehub.com/v1.0/search/opportunities",
    "https://common-production-api-us.bonfirehub.com/v1.0/vendors/me/opportunities",
    "https://common-production-api-us.bonfirehub.com/v1.0/vendors/me/opportunities?status=Open",
)


class EunaSupplierNetworkClient:
    """Harvest centralized Supplier Network opportunities using an authenticated page."""

    def __init__(self, auth_client: Any) -> None:
        self.auth_client = auth_client
        self.page = auth_client.page

    def _wait_out_cloudflare(self, *, timeout_ms: int = 25_000) -> bool:
        """Return True if page is usable (not stuck on CF interstitial)."""
        assert self.page is not None
        deadline = timeout_ms
        stepped = 0
        while stepped < deadline:
            try:
                text = (self.page.inner_text("body") or "").lower()
            except Exception:
                text = ""
            if "performing security verification" in text or (
                "just a moment" in text and "cloudflare" in text
            ):
                try:
                    self.page.wait_for_timeout(1500)
                except Exception:
                    pass
                stepped += 1500
                continue
            return True
        return False

    def harvest_via_api(self, *, max_results: int = 5000) -> dict[str, Any]:
        """Prefer structured vendor API over CF-prone SPA navigation."""
        assert self.page is not None
        out: dict[str, Any] = {
            "method": "api",
            "central_reachable": False,
            "search_url": None,
            "reported_total": None,
            "retrieved_total": 0,
            "pages_scanned": 0,
            "pagination_complete": False,
            "rows": [],
            "api_urls_seen": [],
            "error": None,
        }
        # Anchor session on a page that already works post-login (avoids CF wall).
        for anchor in (
            "https://vendor.bonfirehub.com/agencies",
            "https://vendor.bonfirehub.com/dashboard",
            "https://vendor.bonfirehub.com/",
        ):
            try:
                self.page.goto(anchor, wait_until="domcontentloaded", timeout=45_000)
                if self._wait_out_cloudflare(timeout_ms=12_000):
                    out["central_reachable"] = True
                    out["search_url"] = self.page.url
                    break
            except Exception as exc:
                out["error"] = type(exc).__name__
        if not out["central_reachable"]:
            out["error"] = out.get("error") or "api_anchor_unreachable"
            return out

        # Wait for SPA My Network copy, then detect ESN entitlement gate
        try:
            self.page.wait_for_timeout(2000)
            body_text = (self.page.inner_text("body") or "").lower()
            out["page_text_sample"] = body_text[:800]
            if re.search(
                r"sign\s+up\s+for\s+euna\s+supplier\s+network|no\s+agencies\s+available|"
                r"do\s+not\s+have\s+any\s+agencies",
                body_text,
            ):
                out["error"] = "SUPPLIER_NETWORK_NOT_ENABLED"
                out["account_entitlement"] = "SUPPLIER_NETWORK_NOT_ENABLED"
                out["pagination_complete"] = True
                return out
        except Exception:
            pass

        all_rows: list[dict[str, Any]] = []
        seen: set[str] = set()

        def _fetch_json(api_url: str) -> Any | None:
            # Prefer in-page fetch (cookies/CORS as browser) then Playwright request.
            try:
                payload = self.page.evaluate(
                    """async (url) => {
                      try {
                        const r = await fetch(url, {credentials:'include', headers:{'Accept':'application/json'}});
                        const text = await r.text();
                        return {status: r.status, text: text.slice(0, 500000)};
                      } catch (e) {
                        return {status: 0, error: String(e)};
                      }
                    }""",
                    api_url,
                )
                out["api_urls_seen"].append(f"{api_url}#fetch:{payload.get('status')}")
                if int(payload.get("status") or 0) >= 200 and int(payload.get("status") or 0) < 300:
                    return json.loads(payload.get("text") or "null")
            except Exception:
                pass
            try:
                resp = self.page.request.get(api_url, timeout=20_000)
                out["api_urls_seen"].append(f"{api_url}#req:{resp.status}")
                if resp.ok:
                    return resp.json()
            except Exception:
                return None
            return None

        # Probe me endpoint for clues (never store full PII-heavy body)
        me = _fetch_json("https://common-production-api-global.bonfirehub.com/v1.0/vendors/me")
        if isinstance(me, dict):
            out["vendor_me_keys"] = sorted(list(me.keys()))[:40]

        for api_url in _API_CANDIDATES:
            data = _fetch_json(api_url)
            if data is None:
                continue
            batch = _extract_from_payloads([{"url": api_url, "data": data}], page_url=api_url)
            if not batch and isinstance(data, dict):
                for key in ("total", "totalCount", "count", "totalElements"):
                    if data.get(key) is not None:
                        try:
                            out["reported_total"] = int(data[key])
                        except Exception:
                            pass
            for raw in batch:
                rec = _to_record(raw, list_url=api_url)
                key = (
                    str(rec.get("detail_url") or "").strip().lower()
                    or f"{rec.get('external_id')}|{(rec.get('title') or '')[:80]}".lower()
                )
                if not key or key in seen:
                    continue
                seen.add(key)
                all_rows.append(rec)
                if len(all_rows) >= max_results:
                    break
            if all_rows:
                out["search_url"] = api_url
                break
            if len(all_rows) >= max_results:
                break

        # Pagination via ?page= / pageNumber if first endpoint worked
        if all_rows and out.get("search_url"):
            base = out["search_url"].split("?")[0]
            for page_num in range(2, 40):
                if len(all_rows) >= max_results:
                    break
                page_url = f"{base}?page={page_num}&status=Open"
                try:
                    resp = self.page.request.get(page_url, timeout=20_000)
                    if not resp.ok:
                        break
                    data = resp.json()
                except Exception:
                    break
                batch = _extract_from_payloads([{"url": page_url, "data": data}], page_url=page_url)
                if not batch:
                    break
                new = 0
                for raw in batch:
                    rec = _to_record(raw, list_url=page_url)
                    key = (
                        str(rec.get("detail_url") or "").strip().lower()
                        or f"{rec.get('external_id')}|{(rec.get('title') or '')[:80]}".lower()
                    )
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    all_rows.append(rec)
                    new += 1
                    if len(all_rows) >= max_results:
                        break
                out["pages_scanned"] = page_num
                if new == 0:
                    break
            out["pagination_complete"] = True

        out["rows"] = all_rows
        out["retrieved_total"] = len(all_rows)
        out["pages_scanned"] = out.get("pages_scanned") or (1 if all_rows else 0)
        if not all_rows and not out.get("error"):
            out["error"] = "api_no_rows"
        return out

    def open_central_search(self) -> dict[str, Any]:
        assert self.page is not None
        out: dict[str, Any] = {
            "reachable": False,
            "search_url": None,
            "error": None,
            "page_text_sample": None,
            "nav_clicked": None,
        }
        # Force Opportunities route first — dashboard alone rarely lists open bids.
        for cand in CENTRAL_SEARCH_CANDIDATES:
            try:
                self.page.goto(cand, wait_until="domcontentloaded", timeout=60_000)
                try:
                    self.page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass
                try:
                    self.page.wait_for_timeout(1200)
                except Exception:
                    pass
            except Exception as exc:
                out["error"] = type(exc).__name__
                continue
            url = self.page.url or ""
            html = self.page.content() or ""
            if "login" in url.lower() and re.search(r'type=["\']password["\']', html, re.I):
                out["error"] = "session_expired"
                continue
            if "vendor.bonfirehub.com" not in url:
                continue
            out["reachable"] = True
            out["search_url"] = url
            # Prefer URL that still contains opportunities/browse/recommendations
            if re.search(r"opportunit|browse|recommend", url, re.I):
                break

        # Click Opportunities / Browse nav if still on dashboard/agencies
        url = out.get("search_url") or ""
        if out.get("reachable") and not re.search(r"opportunit|browse|recommend", url, re.I):
            for name in (r"^opportunit", r"browse", r"recommend", r"search"):
                try:
                    link = self.page.get_by_role("link", name=re.compile(name, re.I)).first
                    if link.count() == 0:
                        continue
                    link.click(timeout=8_000)
                    try:
                        self.page.wait_for_load_state("domcontentloaded", timeout=20_000)
                        self.page.wait_for_timeout(1200)
                    except Exception:
                        pass
                    out["search_url"] = self.page.url or out.get("search_url")
                    out["nav_clicked"] = name
                    if re.search(r"opportunit|browse|recommend", out.get("search_url") or "", re.I):
                        break
                except Exception:
                    continue
        try:
            out["page_text_sample"] = (self.page.inner_text("body") or "")[:800]
        except Exception:
            pass
        return out

    def harvest(
        self,
        *,
        max_results: int = 5000,
        max_pages: int = 40,
        on_progress: Any | None = None,
    ) -> dict[str, Any]:
        assert self.page is not None
        result: dict[str, Any] = {
            "central_reachable": False,
            "search_url": None,
            "reported_total": None,
            "retrieved_total": 0,
            "pages_scanned": 0,
            "pagination_complete": False,
            "account_category_restriction": False,
            "rows": [],
            "error": None,
            "api_urls_seen": [],
        }
        # Prefer API harvest — SPA opportunities route is Cloudflare-gated in headless.
        api_result = self.harvest_via_api(max_results=max_results)
        if api_result.get("rows"):
            if on_progress:
                try:
                    on_progress(
                        phase="EUNA_CENTRAL_API",
                        pct=80,
                        pages=api_result.get("pages_scanned"),
                        retrieved=api_result.get("retrieved_total"),
                        reported=api_result.get("reported_total"),
                    )
                except Exception:
                    pass
            return api_result

        # Do not hammer Cloudflare /opportunities when the account lacks ESN browse
        if api_result.get("account_entitlement") == "SUPPLIER_NETWORK_NOT_ENABLED" or (
            api_result.get("error") == "SUPPLIER_NETWORK_NOT_ENABLED"
        ):
            api_result["method"] = "api_entitlement_gate"
            return api_result

        result["api_fallback"] = {
            "error": api_result.get("error"),
            "api_urls_seen": api_result.get("api_urls_seen"),
            "anchor": api_result.get("search_url"),
        }

        # Attach listener before navigation so SPA XHR is captured.
        xhr_urls: list[str] = []

        def _on_response(response: Any) -> None:
            try:
                if response.status != 200:
                    return
                ct = (response.headers.get("content-type") or "").lower()
                u = response.url or ""
                if (
                    "json" not in ct
                    and "api" not in u.lower()
                    and "opportunit" not in u.lower()
                    and "project" not in u.lower()
                    and "graphql" not in u.lower()
                ):
                    return
                if u and u not in xhr_urls and len(xhr_urls) < 60:
                    xhr_urls.append(u)
            except Exception:
                return

        self.page.on("response", _on_response)

        opened = self.open_central_search()
        result["central_reachable"] = bool(opened.get("reachable"))
        result["search_url"] = opened.get("search_url")
        result["page_text_sample"] = opened.get("page_text_sample")
        result["nav_clicked"] = opened.get("nav_clicked")
        # Detect Cloudflare interstitial explicitly
        sample = (opened.get("page_text_sample") or "").lower()
        if "performing security verification" in sample or "verify you are not a bot" in sample:
            result["error"] = "CLOUDFLARE_CHALLENGE"
            result["blocker"] = "CLOUDFLARE_CHALLENGE"
            try:
                self.page.remove_listener("response", _on_response)
            except Exception:
                pass
            # Return API attempt details even if empty
            result["api_urls_seen"] = list(api_result.get("api_urls_seen") or [])
            return result
        if not opened.get("reachable"):
            result["error"] = opened.get("error") or "central_search_not_found"
            try:
                self.page.remove_listener("response", _on_response)
            except Exception:
                pass
            return result

        search_url = result["search_url"]
        all_rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        pages = 0
        reported = None
        empty_streak = 0

        try:
            for page_num in range(1, max_pages + 1):
                if page_num > 1:
                    advanced = False
                    for sel in (
                        self.page.get_by_role("button", name=re.compile(r"^next$", re.I)),
                        self.page.get_by_role("link", name=re.compile(r"^next$", re.I)),
                        self.page.locator("button[aria-label*='Next' i], a[aria-label*='Next' i]"),
                        self.page.locator(".pagination .next, [data-testid*='next' i]"),
                    ):
                        try:
                            if sel.count() == 0:
                                continue
                            target = sel.first
                            if not target.is_enabled():
                                continue
                            target.click(timeout=8_000)
                            advanced = True
                            try:
                                self.page.wait_for_load_state("domcontentloaded", timeout=20_000)
                            except Exception:
                                pass
                            break
                        except Exception:
                            continue
                    if not advanced:
                        cur = self.page.url or search_url or ""
                        if "page=" in cur.lower():
                            nxt = re.sub(r"([?&]page=)\d+", rf"\g<1>{page_num}", cur, flags=re.I)
                        else:
                            nxt = f"{cur}{'&' if '?' in cur else '?'}page={page_num}"
                        try:
                            self.page.goto(nxt, wait_until="domcontentloaded", timeout=45_000)
                        except Exception:
                            break
                try:
                    self.page.wait_for_timeout(700)
                except Exception:
                    pass
                html = self.page.content() or ""
                url = self.page.url or search_url or ""
                pages += 1
                if reported is None:
                    reported = _reported_total_from_html(html)
                    result["reported_total"] = reported

                payloads: list[Any] = []
                for xu in list(xhr_urls)[-12:]:
                    try:
                        resp = self.page.request.get(xu, timeout=15_000)
                        if resp.ok:
                            payloads.append({"url": xu, "data": resp.json()})
                            if xu not in result["api_urls_seen"]:
                                result["api_urls_seen"].append(xu[:200])
                    except Exception:
                        continue
                batch = _extract_from_payloads(payloads, page_url=url)
                if not batch:
                    batch = _dom_extract_opportunities(self.page)
                    for r in batch:
                        r["source_url"] = url
                if not batch:
                    batch = _parse_cards(html, page_url=url)
                # Keep xhr urls across pages (SPA may not refetch same endpoints)

                if re.search(
                    r"no\s+opportunit.*match.*profil|update\s+your\s+(commodity|category|profil)",
                    html,
                    re.I,
                ):
                    result["account_category_restriction"] = True
                    result["restriction_code"] = ACCOUNT_CATEGORY_RESTRICTION

                new = 0
                for raw in batch:
                    rec = _to_record(raw, list_url=url)
                    key = (
                        str(rec.get("detail_url") or "").strip().lower()
                        or f"{rec.get('external_id')}|{(rec.get('title') or '')[:80]}".lower()
                    )
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    all_rows.append(rec)
                    new += 1
                    if len(all_rows) >= max_results:
                        break

                if on_progress:
                    try:
                        on_progress(
                            phase="EUNA_CENTRAL",
                            pct=min(90, int(10 + 80 * page_num / max(1, max_pages))),
                            pages=pages,
                            retrieved=len(all_rows),
                            reported=reported,
                        )
                    except Exception:
                        pass

                if len(all_rows) >= max_results:
                    break
                if new == 0:
                    empty_streak += 1
                    if empty_streak >= 2:
                        result["pagination_complete"] = True
                        break
                else:
                    empty_streak = 0
                if reported is not None and len(all_rows) >= int(reported):
                    result["pagination_complete"] = True
                    break
            else:
                result["pagination_complete"] = bool(
                    reported is not None and len(all_rows) >= int(reported)
                )
        finally:
            try:
                self.page.remove_listener("response", _on_response)
            except Exception:
                pass

        result["rows"] = all_rows
        result["retrieved_total"] = len(all_rows)
        result["pages_scanned"] = pages
        if not all_rows and not result.get("error"):
            result["error"] = "no_rows_parsed"
        return result
