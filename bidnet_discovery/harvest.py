"""Harvest BidNet from the authenticated supplier search/results interface."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from bidnet_auth.config import (
    ACCOUNT_CATEGORY_RESTRICTION,
    M3_DISCOVERY_SCOPE,
    load_bidnet_auth_config,
)
from bidnet_auth.states import AUTH_CHALLENGE, AUTH_FAILED, DISABLED
from bidnet_auth.telemetry import owner_connection_status, record_recovery_counters
from bidnet_discovery.parse import (
    SEARCH_RESULT_MARKERS,
    detect_category_restriction,
    next_page_urls_from_html,
    parse_json_search_payload,
    parse_search_results_html,
    reported_total_from_html,
)
from bidnet_discovery.detail import (
    DETAIL_OK,
    recover_detail,
)

log = logging.getLogger("govtracker.bidnet_discovery.harvest")

REPORT = "bidnet_auth/last_harvest_report.json"
DEFAULT_SEARCH_URL = "https://www.bidnetdirect.com/private/supplier/solicitations/search"
# Same open universe; used when private search reports a total but emits no parseable rows
FALLBACK_OPEN_BIDS_URL = "https://www.bidnetdirect.com/solicitations/open-bids"


def _page_diag(page: Any, html: str) -> dict[str, Any]:
    """Safe structural diagnostics for empty-result debugging."""
    low = (html or "").lower()
    diag: dict[str, Any] = {
        "html_len": len(html or ""),
        "has_mets_table_row": "mets-table-row" in low,
        "has_solicitation_link": "solicitation-link" in low,
        "has_simple_results": "simplesolresults" in low,
        "has_results_word": bool(re.search(r"\d[\d,]*\s+results", html or "", re.I)),
        "title": None,
        "locator_counts": {},
    }
    try:
        diag["title"] = (page.title() or "")[:120]
    except Exception:
        pass
    for sel in (
        "tr.mets-table-row",
        "a.solicitation-link",
        ".simpleSolResultsNumResults",
        "button:has-text('Search')",
        "input[type='submit']",
    ):
        try:
            diag["locator_counts"][sel] = page.locator(sel).count()
        except Exception:
            diag["locator_counts"][sel] = -1
    return diag


def _extract_rows_via_locators(page: Any, *, list_url: str, page_number: int) -> list[dict[str, Any]]:
    """Extract result rows using Playwright locators when HTML regex misses dynamic DOM."""
    from urllib.parse import urljoin

    from discovery.deadline import normalize_deadline

    out: list[dict[str, Any]] = []

    def _one_link(a: Any, i: int, mode: str) -> dict[str, Any] | None:
        href = a.get_attribute("href") or ""
        if not href or href.lower().startswith("javascript:") or href.strip() == "#":
            return None
        if "solicit" not in href.lower() and mode == "table":
            # still allow private detail paths
            if not re.search(r"/\d{6,}", href):
                return None
        title = None
        for sel in (".rowTitle", ".title", "[class*='Title']", "td >> nth=0"):
            try:
                title = a.locator(sel).inner_text(timeout=400).strip()
                if title:
                    break
            except Exception:
                continue
        if not title:
            try:
                title = (a.inner_text(timeout=800) or "").strip().split("\n")[0].strip()
            except Exception:
                title = None
        if not title or len(title) < 5:
            return None
        location = published = closing = internal_id = None
        for sel, attr in (
            (".location", "location"),
            (".publicationDate .dateValue", "published"),
            (".closingDate .dateValue", "closing"),
            (".accessibility-hidden", "hid"),
        ):
            try:
                txt = a.locator(sel).inner_text(timeout=300).strip()
                if attr == "location":
                    location = txt
                elif attr == "published":
                    published = txt
                elif attr == "closing":
                    closing = txt
                elif attr == "hid" and txt.isdigit():
                    internal_id = txt
            except Exception:
                pass
        if not internal_id:
            id_attr = a.get_attribute("id") or ""
            m = re.search(r"solicitation_(\d+)", id_attr)
            if m:
                internal_id = m.group(1)
        detail = urljoin(list_url, href)
        dl = normalize_deadline(closing) if closing else {}
        path_id = None
        pid = re.search(r"/(\d{7,})(?:\?|$)", href)
        if pid:
            path_id = pid.group(1)
        return {
            "external_id": str(internal_id or path_id or title)[:160],
            "source_id": "live_bidnet",
            "platform": "live_bidnet",
            "platform_family": "BidNet",
            "source_url": list_url,
            "detail_url": detail,
            "title": title[:300],
            "agency": location,
            "location": location,
            "jurisdiction": location,
            "status": "OPEN",
            "solicitation_number": internal_id,
            "solicitation_id": internal_id or path_id,
            "deadline_raw": closing,
            "deadline": dl.get("utc_deadline") or dl.get("parsed_local"),
            "issue_date_raw": published,
            "discovery_universe": "LIVE",
            "discovery_scope": "BROAD_PRODUCT_RESALE",
            "authoritative_url": detail,
            "raw_metadata": {
                "platform": "BidNet",
                "harvest_mode": f"authenticated_search_locators_{mode}",
                "bidnet_internal_id": internal_id,
                "bidnet_path_id": path_id,
                "result_href": href,
                "result_page": page_number,
                "result_position": i,
                "current_session_href": True,
                "vendor_profile_codes_ignored": True,
            },
        }

    # 1) Classic public open-bids links
    links = page.locator("a.solicitation-link")
    try:
        n = min(links.count(), 200)
    except Exception:
        n = 0
    for i in range(n):
        try:
            row = _one_link(links.nth(i), i, "solicitation_link")
            if row:
                out.append(row)
        except Exception:
            continue

    # 2) Private supplier search: mets-table-row without solicitation-link class
    if not out:
        rows = page.locator("tr.mets-table-row")
        try:
            n = min(rows.count(), 200)
        except Exception:
            n = 0
        for i in range(n):
            try:
                row = rows.nth(i)
                a = row.locator("a[href*='solicit'], a[href*='/private/'], a[href]").first
                if a.count() == 0:
                    continue
                item = _one_link(a, i, "table")
                if not item:
                    # Title/dates may live on the <tr>, link only has href
                    href = a.get_attribute("href") or ""
                    if not href:
                        continue
                    try:
                        title = (row.inner_text(timeout=800) or "").strip().split("\n")[0].strip()
                    except Exception:
                        title = None
                    if not title or len(title) < 5:
                        continue
                    detail = urljoin(list_url, href)
                    path_id = None
                    pid = re.search(r"/(\d{7,})(?:\?|$)", href)
                    if pid:
                        path_id = pid.group(1)
                    item = {
                        "external_id": str(path_id or title)[:160],
                        "source_id": "live_bidnet",
                        "platform": "live_bidnet",
                        "platform_family": "BidNet",
                        "source_url": list_url,
                        "detail_url": detail,
                        "title": title[:300],
                        "status": "OPEN",
                        "solicitation_id": path_id,
                        "discovery_universe": "LIVE",
                        "discovery_scope": "BROAD_PRODUCT_RESALE",
                        "authoritative_url": detail,
                        "raw_metadata": {
                            "platform": "BidNet",
                            "harvest_mode": "authenticated_search_locators_table_text",
                            "bidnet_path_id": path_id,
                            "result_href": href,
                            "result_page": page_number,
                            "result_position": i,
                            "current_session_href": True,
                            "vendor_profile_codes_ignored": True,
                        },
                    }
                # Prefer row-level date/location fields
                try:
                    loc = row.locator(".location, td").nth(1).inner_text(timeout=300).strip()
                    if loc and not item.get("location"):
                        item["location"] = loc
                        item["jurisdiction"] = loc
                except Exception:
                    pass
                out.append(item)
            except Exception:
                continue
    return out


def _dedupe_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    dupes = 0
    for r in rows:
        meta = r.get("raw_metadata") if isinstance(r.get("raw_metadata"), dict) else {}
        key = (
            str(meta.get("bidnet_internal_id") or "").strip()
            or str(r.get("solicitation_id") or "").strip()
            or str(r.get("detail_url") or "").split("?")[0].lower()
            or f"{(r.get('title') or '')[:80]}|{r.get('deadline_raw') or ''}".lower()
        )
        if not key or key in seen:
            dupes += 1
            continue
        seen.add(key)
        unique.append(r)
    return unique, dupes


def _enrich_from_detail(client: Any, row: dict[str, Any]) -> dict[str, Any]:
    """Open current authenticated result href and merge detail fields (classified)."""
    return recover_detail(client, row, retry=True)


def _row_fingerprint(rows: list[dict[str, Any]]) -> str:
    """Stable fingerprint of the first few rows — used to verify page advances."""
    parts: list[str] = []
    for r in (rows or [])[:3]:
        meta = r.get("raw_metadata") if isinstance(r.get("raw_metadata"), dict) else {}
        parts.append(
            str(meta.get("bidnet_internal_id") or "")
            or str(r.get("detail_url") or "").split("?")[0]
            or str(r.get("title") or "")[:60]
        )
    return "|".join(parts)


def _wait_results(page: Any, timeout_ms: int = 20_000) -> None:
    try:
        page.wait_for_selector("tr.mets-table-row, a.solicitation-link", timeout=timeout_ms)
    except Exception:
        pass
    try:
        page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 15_000))
    except Exception:
        pass


def _advance_search_page(
    page: Any,
    *,
    html: str,
    page_url: str,
    current_page: int,
    prev_fp: str,
    methods: set[str],
) -> tuple[bool, str]:
    """Advance BidNet search UI to the next page. Returns (ok, method).

    Verifies the first-row fingerprint changed so fake URL navigations do not count.
    Prefer real href navigations; avoid Search-button form posts that hang Playwright.
    """
    target = current_page + 1

    def _accept(method: str) -> tuple[bool, str] | None:
        _wait_results(page, 12_000)
        batch = _extract_rows_via_locators(page, list_url=page.url or page_url, page_number=target)
        if not batch:
            batch = parse_search_results_html(page.content(), list_url=page.url or page_url, page_number=target)
        fp = _row_fingerprint(batch)
        if batch and fp and fp != prev_fp:
            methods.add(method)
            return True, method
        return None

    # 1) Real data-href / rel=next / open-bids /pageN from HTML only
    for nu in next_page_urls_from_html(html, list_url=page_url, current_page=current_page):
        try:
            page.goto(nu, wait_until="domcontentloaded", timeout=35_000)
            got = _accept("page_url")
            if got:
                return got
        except Exception:
            continue

    # 2) option[data-href] only (no Search click — that path hangs on private search)
    try:
        opt = page.locator(
            f"option.mets-pagination-number[data-page-number='{target}'][data-href], "
            f"option[data-page-number='{target}'][data-href]"
        ).first
        if opt.count() > 0:
            href = opt.get_attribute("data-href") or ""
            if href and not str(href).lower().startswith("javascript"):
                page.goto(urljoin_safe(page_url, href), wait_until="domcontentloaded", timeout=35_000)
                got = _accept("page_option_href")
                if got:
                    return got
    except Exception:
        pass

    # 3) Next link (short timeouts)
    try:
        nxt = page.locator(
            "a.mets-pagination-next, a[rel='next'], .pagination a.next, a:has-text('Next')"
        ).first
        if nxt.count() > 0 and nxt.is_visible(timeout=800):
            with page.expect_navigation(timeout=20_000, wait_until="domcontentloaded"):
                nxt.click(timeout=5_000)
            got = _accept("next_click")
            if got:
                return got
    except Exception:
        pass

    return False, "none"


def urljoin_safe(base: str, href: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base, href)


def _paginate_open_bids(
    page: Any,
    *,
    all_rows: list[dict[str, Any]],
    max_results: int,
    max_pages: int,
    pages_already: int,
    methods: set[str],
    on_progress: Any | None = None,
) -> int:
    """Continue harvest via national open-bids /pageN over HTTP.

    Authenticated Playwright sessions often rewrite open-bids back into the
    private search DOM (same ~25 rows every page). Public HTTP path pagination
    is stable and returns distinct pages.
    """
    import httpx

    pages_extra = 0
    # If private search already contributed page-1 overlap, start at page 2
    fb_page = 2 if all_rows else 1
    empty_streak = 0
    page_budget = max(int(max_pages), int((max_results + 24) // 25) + 2)
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    try:
        client = httpx.Client(
            timeout=45.0,
            follow_redirects=True,
            headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml"},
        )
    except Exception:
        return 0
    try:
        while len(all_rows) < max_results and pages_extra < page_budget:
            url = FALLBACK_OPEN_BIDS_URL if fb_page == 1 else f"{FALLBACK_OPEN_BIDS_URL}/page{fb_page}"
            try:
                resp = client.get(url)
            except Exception:
                empty_streak += 1
                if empty_streak >= 3:
                    break
                fb_page += 1
                pages_extra += 1
                continue
            if resp.status_code != 200 or not (resp.text or "").strip():
                empty_streak += 1
                if empty_streak >= 3:
                    break
                fb_page += 1
                pages_extra += 1
                continue
            batch = parse_search_results_html(
                resp.text, list_url=str(resp.url), page_number=fb_page
            )
            if not batch:
                empty_streak += 1
                if empty_streak >= 3:
                    break
                fb_page += 1
                pages_extra += 1
                continue
            existing = {
                str((r.get("raw_metadata") or {}).get("bidnet_internal_id") or r.get("detail_url") or "")
                for r in all_rows
            }
            # ID/URL only — title collisions falsely stop national pagination before soft-cap
            new_batch = []
            for r in batch:
                key = str(
                    (r.get("raw_metadata") or {}).get("bidnet_internal_id") or r.get("detail_url") or ""
                )
                if key and key in existing:
                    continue
                if not key:
                    continue
                new_batch.append(r)
                existing.add(key)
            if new_batch:
                all_rows.extend(new_batch)
                methods.add("open_bids_http")
                empty_streak = 0
            else:
                empty_streak += 1
                if empty_streak >= 4:
                    break
            pages_extra += 1
            fb_page += 1
            if on_progress:
                try:
                    on_progress(
                        phase="OPEN_BIDS",
                        pct=min(90, 20 + pages_extra * 8),
                        pages=pages_already + pages_extra,
                        retrieved=len(all_rows),
                    )
                except Exception:
                    pass
    finally:
        try:
            client.close()
        except Exception:
            pass
    return pages_extra


def harvest_authenticated_search(
    client: Any,
    *,
    search_url: str,
    max_results: int = 100,
    max_pages: int = 20,
    open_details: bool = True,
    detail_limit: int | None = None,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """Navigate authenticated search, paginate, optionally open current result links."""

    def _progress(phase: str, pct: int, **extra: Any) -> None:
        if not on_progress:
            return
        try:
            on_progress(phase=phase, pct=pct, **extra)
        except Exception:
            pass
    result: dict[str, Any] = {
        "search_url": search_url,
        "search_reachable": False,
        "reported_total": None,
        "retrieved_total": 0,
        "unique_result_ids": 0,
        "pages_scanned": 0,
        "pagination_method": None,
        "pagination_complete": False,
        "DISCOVERY_TRUNCATED": False,
        "account_category_restriction": False,
        "rows": [],
        "network_endpoints": [],
        "error": None,
        "detail_stats": Counter(),
        "detail_failure_counts": Counter(),
    }

    assert getattr(client, "_page", None) is not None
    page = client._page
    captured: list[dict[str, Any]] = []

    def _on_response(response: Any) -> None:
        # Never read response bodies here — sync body reads deadlock Playwright
        # while the main thread is inside goto/click/wait.
        try:
            if response.status != 200:
                return
            u = str(response.url or "")
            ctype = (response.headers or {}).get("content-type", "")
            interesting = any(
                x in u.lower() for x in ("solicit", "search", "notice", "/api/", "ajax")
            )
            if interesting and len(result["network_endpoints"]) < 40:
                result["network_endpoints"].append(u[:220])
            if "application/json" in ctype and len(captured) < 20:
                captured.append({"url": u[:220], "data": None, "json_hint": True})
        except Exception:
            pass

    page.on("response", _on_response)
    html_fragments: list[str] = []
    all_rows: list[dict[str, Any]] = []
    try:
        page.goto(search_url, wait_until="domcontentloaded", timeout=60_000)
        _wait_results(page, 20_000)

        # Private search often needs an explicit Search click to populate results
        for name in (r"^\s*search\s*$", r"^\s*find\s*solicitations?\s*$", r"^\s*apply\s*$"):
            try:
                btn = page.get_by_role("button", name=re.compile(name, re.I)).first
                if btn.count() > 0 and btn.is_visible(timeout=1_500):
                    btn.click(timeout=10_000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=20_000)
                    except Exception:
                        pass
                    break
            except Exception:
                continue
        # Also try input[type=submit] search
        try:
            sub = page.locator(
                'input[type="submit"][value*="Search" i], button.search, #searchButton, '
                'button[id*="search" i], input[id*="search" i][type="submit"]'
            ).first
            if sub.count() > 0 and sub.is_visible(timeout=1_500):
                sub.click(timeout=10_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=20_000)
                except Exception:
                    pass
        except Exception:
            pass

        # Wait for result markers (AJAX)
        found_marker = False
        for sel in (
            "tr.mets-table-row",
            "a.solicitation-link",
            ".simpleSolResultsNumResults",
            ".simpleSolResultsTable",
            "text=/\\d[\\d,]*\\s+results/i",
        ):
            try:
                page.wait_for_selector(sel, timeout=25_000)
                found_marker = True
                break
            except Exception:
                continue
        if not found_marker:
            try:
                page.wait_for_timeout(5_000)
            except Exception:
                pass

        final_url = page.url or search_url
        html = page.content()
        if not SEARCH_RESULT_MARKERS.search(html or "") and "/private/" not in (final_url or "").lower():
            result["error"] = "search_page_not_recognized"
            result["final_url"] = final_url[:220]
            result["diag"] = _page_diag(page, html)
            return result

        result["search_reachable"] = True
        result["final_url"] = final_url[:220]
        result["account_category_restriction"] = detect_category_restriction(html)
        reported = reported_total_from_html(html)
        result["reported_total"] = reported
        result["diag"] = _page_diag(page, html)

        page_num = 1
        methods: set[str] = set()
        stagnant = 0
        private_search = "private" in (search_url or "").lower() or "private" in (page.url or "").lower()
        # Private supplier search pager is unreliable (form post hangs). After the
        # first page (+ reported_total), continue via open-bids /pageN.
        max_private_pages = 1 if private_search else max_pages
        while len(all_rows) < max_results and page_num <= max_private_pages:
            html = page.content()
            page_url = page.url or search_url
            batch = parse_search_results_html(html, list_url=page_url, page_number=page_num)
            if batch:
                methods.add("dom_table")
            # Always try Playwright locators for private search (often lacks solicitation-link class)
            loc_batch = _extract_rows_via_locators(page, list_url=page_url, page_number=page_num)
            if loc_batch:
                methods.add("playwright_locators")
                if not batch:
                    batch = loc_batch
                else:
                    seen_u = {str(r.get("detail_url") or "") for r in batch}
                    batch.extend([r for r in loc_batch if str(r.get("detail_url") or "") not in seen_u])
            if not batch:
                try:
                    row0 = page.locator("tr.mets-table-row").first
                    if row0.count() > 0:
                        result["diag_row0_html"] = (row0.inner_html(timeout=2_000) or "")[:1200]
                except Exception:
                    pass
            for frag in html_fragments:
                frows = parse_search_results_html(frag, list_url=page_url, page_number=page_num)
                if frows:
                    methods.add("xhr_html")
                    batch.extend(frows)
            html_fragments.clear()
            for blob in captured:
                jrows = parse_json_search_payload(blob.get("data"), list_url=page_url)
                if jrows:
                    methods.add("xhr_json")
                    batch.extend(jrows)
            captured.clear()

            existing = {
                str((r.get("raw_metadata") or {}).get("bidnet_internal_id") or r.get("detail_url") or "")
                for r in all_rows
            }
            new_batch = [
                r
                for r in batch
                if str((r.get("raw_metadata") or {}).get("bidnet_internal_id") or r.get("detail_url") or "")
                not in existing
            ]
            if not new_batch and page_num > 1:
                stagnant += 1
                if stagnant >= 2:
                    result["truncation_reason"] = "stagnant_pages"
                    break
            else:
                stagnant = 0
                all_rows.extend(new_batch)
            result["pages_scanned"] = page_num
            prev_fp = _row_fingerprint(batch) or _row_fingerprint(all_rows)
            _progress(
                "PAGINATING",
                min(90, 10 + page_num * 8),
                pages=page_num,
                retrieved=len(all_rows),
            )

            if len(all_rows) >= max_results:
                break
            if reported is not None and len(all_rows) >= int(reported):
                break

            if page_num >= max_private_pages:
                if private_search and len(all_rows) < max_results:
                    result["truncation_reason"] = "private_search_handoff_open_bids"
                break
            ok, method = _advance_search_page(
                page,
                html=html,
                page_url=page_url,
                current_page=page_num,
                prev_fp=prev_fp,
                methods=methods,
            )
            if not ok:
                result["truncation_reason"] = result.get("truncation_reason") or f"no_next_after_page_{page_num}"
                break
            result["last_pagination_method"] = method
            page_num += 1

        # Fallback / continuation via national open-bids path pagination
        need_more = len(all_rows) < max_results and (
            reported is None or len(all_rows) < int(reported)
        )
        if need_more:
            log.info(
                "BidNet continuing via open-bids path (%s rows so far, reason=%s)",
                len(all_rows),
                result.get("truncation_reason"),
            )
            try:
                if not all_rows:
                    page.goto(FALLBACK_OPEN_BIDS_URL, wait_until="domcontentloaded", timeout=90_000)
                    _wait_results(page, 25_000)
                    fb_html = page.content()
                    fb_url = page.url or FALLBACK_OPEN_BIDS_URL
                    fb_batch = parse_search_results_html(fb_html, list_url=fb_url, page_number=1)
                    if not fb_batch:
                        fb_batch = _extract_rows_via_locators(page, list_url=fb_url, page_number=1)
                    if fb_batch:
                        methods.add("open_bids_fallback")
                        all_rows.extend(fb_batch)
                        result["fallback_url"] = fb_url[:220]
                        result["diag"] = _page_diag(page, fb_html)
                extra = _paginate_open_bids(
                    page,
                    all_rows=all_rows,
                    max_results=max_results,
                    max_pages=max_pages,
                    pages_already=int(result.get("pages_scanned") or 0),
                    methods=methods,
                    on_progress=on_progress,
                )
                if extra:
                    result["pages_scanned"] = int(result.get("pages_scanned") or 0) + extra
                    result["fallback_url"] = result.get("fallback_url") or FALLBACK_OPEN_BIDS_URL
                    if len(all_rows) >= max_results or (
                        reported is not None and len(all_rows) >= int(reported)
                    ):
                        result.pop("truncation_reason", None)
            except Exception as exc:
                result["fallback_error"] = type(exc).__name__

        unique, _ = _dedupe_rows(all_rows)
        # Cap only when caller asked for a sample; full-universe uses high max_results
        unique = unique[:max_results]
        result["pagination_method"] = "+".join(sorted(methods)) or "none"
        hit_reported = reported is not None and len(unique) >= int(reported)
        sample_mode = reported is not None and max_results < int(reported)
        if sample_mode:
            result["sample_cap"] = max_results
            result["pagination_complete"] = len(unique) >= max_results
            result["DISCOVERY_TRUNCATED"] = len(unique) < max_results
        else:
            result["pagination_complete"] = bool(hit_reported)
            result["DISCOVERY_TRUNCATED"] = bool(reported is not None and not hit_reported)
        if result.get("DISCOVERY_TRUNCATED") and not result.get("truncation_reason"):
            result["truncation_reason"] = "below_reported_total"

        # Normalize harvest fields on every row
        for r in unique:
            meta = dict(r.get("raw_metadata") or {})
            meta.setdefault("current_detail_href", r.get("detail_url"))
            meta.setdefault("harvested_at", now_utc().isoformat())
            r["current_detail_href"] = r.get("detail_url")
            r["raw_metadata"] = meta

        # Detail recovery from CURRENT hrefs
        dlimit = detail_limit if detail_limit is not None else (len(unique) if open_details else 0)
        enriched: list[dict[str, Any]] = []
        stats: Counter = Counter()
        fail_counts: Counter = Counter()
        for i, row in enumerate(unique):
            if open_details and i < dlimit:
                try:
                    erow = _enrich_from_detail(client, row)
                except RuntimeError as exc:
                    msg = str(exc)
                    if "AUTH_CHALLENGE" in msg or "SESSION_LOST" in msg:
                        result["error"] = AUTH_CHALLENGE if "AUTH_CHALLENGE" in msg else "SESSION_LOST"
                        fail_counts["SESSION_LOST" if "SESSION" in msg else AUTH_CHALLENGE] += 1
                        enriched.append(row)
                        break
                    erow = row
                ds = erow.get("auth_detail") or {}
                st = ds.get("detail_status") or "OTHER"
                fail_counts[st] += 1
                for k in (
                    "detail_opened",
                    "detail_recovered",
                    "issuing_org",
                    "solicitation_number",
                    "source_url",
                    "description",
                ):
                    if ds.get(k):
                        stats[k] += 1
                stats["documents"] += int(ds.get("documents") or 0)
                stats["documents_downloaded"] += int(ds.get("documents_downloaded") or 0)
                if st == DETAIL_OK:
                    stats["DETAIL_OK"] += 1
                enriched.append(erow)
            else:
                enriched.append(row)

        result["rows"] = enriched
        result["retrieved_total"] = len(enriched)
        result["unique_result_ids"] = len(
            {
                str((r.get("raw_metadata") or {}).get("bidnet_internal_id") or r.get("solicitation_id") or r.get("detail_url"))
                for r in enriched
            }
        )
        result["detail_stats"] = dict(stats)
        result["detail_failure_counts"] = dict(fail_counts)
        if result.get("account_category_restriction"):
            result["ACCOUNT_CATEGORY_RESTRICTION"] = ACCOUNT_CATEGORY_RESTRICTION
    except Exception as exc:
        msg = str(exc)
        if "AUTH_CHALLENGE" in msg:
            result["error"] = AUTH_CHALLENGE
        else:
            result["error"] = type(exc).__name__
            log.exception("BidNet authenticated harvest failed")
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass
    return result


def run_bidnet_authenticated_harvest(
    *,
    max_results: int = 100,
    max_pages: int = 20,
    open_details: bool = True,
    detail_limit: int | None = None,
    persist: bool = True,
    run_id: str | None = None,
    use_auth: bool = True,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """Authenticate, harvest current search results, merge into canonical store."""
    from bidnet_auth import BidNetAuthenticatedClient
    from m3_canonical_discovery_bridge import merge_discovery_into_canonical

    cfg = load_bidnet_auth_config()
    run_id = run_id or f"BNH-{uuid4().hex[:12]}"
    started = now_utc().isoformat()
    search_url = cfg.search_url or DEFAULT_SEARCH_URL

    report: dict[str, Any] = {
        "kind": "BidNetAuthenticatedHarvest",
        "run_id": run_id,
        "started_at": started,
        "discovery_scope": M3_DISCOVERY_SCOPE,
        "vendor_profile_codes_ignored": True,
        "search_url": search_url,
        "auth": None,
        "harvest": None,
        "canonical_merge": {},
        "blocker": None,
        "SAM_API_CALLS": 0,
    }

    if not use_auth or not cfg.auth_enabled:
        report["auth"] = {"status": DISABLED, "authenticated": False}
        report["blocker"] = DISABLED
        return report
    if not cfg.credentials_present:
        report["auth"] = {
            "status": AUTH_FAILED,
            "authenticated": False,
            "message": "BIDNET_USERNAME/BIDNET_PASSWORD not configured",
        }
        report["blocker"] = AUTH_FAILED
        return report

    client = BidNetAuthenticatedClient()
    try:
        auth = client.ensure_authenticated()
        report["auth"] = auth.to_dict()
        if not auth.authenticated:
            report["blocker"] = auth.status
            if persist:
                _save_report(report)
            return report

        if on_progress:
            try:
                on_progress(phase="AUTH_OK", pct=15)
            except Exception:
                pass
        harvest = harvest_authenticated_search(
            client,
            search_url=search_url,
            max_results=max_results,
            max_pages=max_pages,
            open_details=open_details,
            detail_limit=detail_limit if detail_limit is not None else min(max_results, 100),
            on_progress=on_progress,
        )
        report["harvest"] = {
            "search_reachable": harvest.get("search_reachable"),
            "reported_total": harvest.get("reported_total"),
            "retrieved_total": harvest.get("retrieved_total"),
            "unique_result_ids": harvest.get("unique_result_ids"),
            "pages_scanned": harvest.get("pages_scanned"),
            "pagination_method": harvest.get("pagination_method"),
            "pagination_complete": harvest.get("pagination_complete"),
            "DISCOVERY_TRUNCATED": harvest.get("DISCOVERY_TRUNCATED"),
            "account_category_restriction": harvest.get("account_category_restriction"),
            "detail_stats": harvest.get("detail_stats"),
            "detail_failure_counts": harvest.get("detail_failure_counts"),
            "error": harvest.get("error"),
            "final_url": harvest.get("final_url"),
            "diag": harvest.get("diag"),
            "diag_row0_html": (harvest.get("diag_row0_html") or "")[:800],
            "fallback_url": harvest.get("fallback_url"),
            "fallback_error": harvest.get("fallback_error"),
            "truncation_reason": harvest.get("truncation_reason"),
            "last_pagination_method": harvest.get("last_pagination_method"),
            "network_endpoints_sample": (harvest.get("network_endpoints") or [])[:10],
        }
        if harvest.get("error") == AUTH_CHALLENGE:
            report["blocker"] = AUTH_CHALLENGE
            if persist:
                _save_report(report)
            return report

        rows = harvest.get("rows") or []
        merge = merge_discovery_into_canonical(
            run_id=run_id,
            trigger="bidnet_authenticated_harvest",
            records=rows,
            sources_attempted=["live_bidnet_authenticated_search"],
            sources_succeeded=["live_bidnet_authenticated_search"] if rows else [],
            sources_failed=[] if rows else ["live_bidnet_authenticated_search"],
            source_counts={
                "live_bidnet_authenticated_search": {
                    "ok": bool(rows),
                    "raw": len(rows),
                    "reported_total": harvest.get("reported_total"),
                }
            },
            raw_opportunities_found=len(rows),
            records_normalized=len(rows),
            error_summary=harvest.get("error"),
            api_usage={"SAM": 0, "bidnet_authenticated": True},
            started_at=started,
            persist=persist,
        )
        report["canonical_merge"] = {
            "raw": len(rows),
            "new": merge.get("new_canonical_opportunities_added"),
            "updated": merge.get("existing_opportunities_updated"),
            "duplicates_detected": merge.get("duplicates_detected"),
            "canonical_after": merge.get("canonical_total_after"),
            "available_after": merge.get("currently_available_after"),
            "available_before": merge.get("currently_available_before"),
            "run_status": merge.get("run_status"),
        }
        report["net_new"] = merge.get("new_canonical_opportunities_added")
        report["enriched_existing"] = merge.get("existing_opportunities_updated")
        ds = harvest.get("detail_stats") or {}
        from bidnet_auth.telemetry import record_harvest_counters

        record_harvest_counters(
            reported_total=harvest.get("reported_total"),
            retrieved_total=harvest.get("retrieved_total"),
            pages_scanned=harvest.get("pages_scanned"),
            pagination_complete=bool(harvest.get("pagination_complete")),
            discovery_truncated=bool(harvest.get("DISCOVERY_TRUNCATED")),
            detail_stats=ds,
            detail_failure_counts=harvest.get("detail_failure_counts") or {},
            net_new=merge.get("new_canonical_opportunities_added") or 0,
            enriched=merge.get("existing_opportunities_updated") or 0,
        )
        record_recovery_counters(
            detail_recovered=int(ds.get("detail_recovered") or 0),
            documents_recovered=int(ds.get("documents") or ds.get("documents_downloaded") or 0),
            economics_ready=0,
            authenticated_run=True,
        )
        report["records_sample"] = [
            {
                "title": (r.get("title") or "")[:80],
                "location": r.get("location"),
                "deadline_raw": r.get("deadline_raw"),
                "url": (r.get("detail_url") or "")[:120],
                "issuing_org": bool((r.get("auth_detail") or {}).get("issuing_org")),
            }
            for r in rows[:15]
        ]
    finally:
        try:
            client.close()
        except Exception:
            pass

    report["completed_at"] = now_utc().isoformat()
    report["connection"] = owner_connection_status()
    if persist:
        _save_report(report)
    return report


def _save_report(report: dict[str, Any]) -> None:
    from m3_data_root import data_path

    path = data_path(REPORT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
