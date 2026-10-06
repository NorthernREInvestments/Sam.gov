"""Centralized Euna Supplier Network harvest — primary broad opportunity source.

Agency-specific *.bonfirehub.com portals are enrichment only, not discovery roots.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from euna_auth.config import ACCOUNT_CATEGORY_RESTRICTION, M3_DISCOVERY_SCOPE, load_euna_auth_config
from euna_auth.states import AUTH_FAILED, DISABLED, WORKING_AUTH

log = logging.getLogger("govtracker.euna_discovery.central")

REPORT = "euna_auth/last_discovery_report.json"
CHECKPOINT = "euna_auth/central_checkpoint.json"

# Post-login Supplier Network surfaces (tried in order)
CENTRAL_SEARCH_CANDIDATES = (
    "https://vendor.bonfirehub.com/opportunities",
    "https://vendor.bonfirehub.com/agencies/search",
    "https://vendor.bonfirehub.com/",
    "https://account.bonfirehub.com/Vendor/Opportunities",
    "https://account.bonfirehub.com/",
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


def _parse_opportunity_cards(html: str, *, page_url: str) -> list[dict[str, Any]]:
    """Best-effort parse of Supplier Network opportunity cards / table rows."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Project / opportunity detail links
    for m in re.finditer(
        r'href=["\']([^"\']*(?:/projects?/|/opportunity/|/Portal/|/portal/)[^"\']+)["\']',
        html or "",
        re.I,
    ):
        href = m.group(1)
        if href.startswith("/"):
            from urllib.parse import urljoin

            href = urljoin(page_url, href)
        if href in seen:
            continue
        # Nearby title text
        start = max(0, m.start() - 40)
        end = min(len(html), m.end() + 400)
        chunk = html[start:end]
        title_m = re.search(r">\s*([^<]{8,240})\s*<", chunk)
        title = re.sub(r"\s+", " ", (title_m.group(1) if title_m else "")).strip()
        if not title or title.lower() in {"view", "details", "learn more", "open"}:
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

    # JSON blobs embedded in page
    for jm in re.finditer(
        r'<script[^>]*type=["\']application/json["\'][^>]*>(\{.*?\})</script>',
        html or "",
        re.I | re.S,
    ):
        try:
            data = json.loads(jm.group(1))
        except Exception:
            continue
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
                )
                if title and href and str(href) not in seen:
                    seen.add(str(href))
                    rows.append(
                        {
                            "external_id": str(cur.get("id") or cur.get("projectId") or title)[:160],
                            "title": str(title)[:300],
                            "detail_url": str(href),
                            "source_url": page_url,
                            "agency": cur.get("agency") or cur.get("organizationName"),
                            "deadline_raw": cur.get("closeDate") or cur.get("dueDate"),
                            "status": cur.get("status") or "OPEN",
                            "raw_metadata": {"href_source": "embedded_json"},
                        }
                    )
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    return rows


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


def _extract_rows_from_xhr(payloads: list[Any], *, page_url: str) -> list[dict[str, Any]]:
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


def harvest_central_supplier_network(
    client: Any,
    *,
    max_results: int = 5000,
    max_pages: int = 40,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """Authenticate then paginate the centralized Supplier Network opportunity list."""
    page = client._page
    assert page is not None
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
        "xhr_hits": 0,
    }

    search_url = None
    for cand in CENTRAL_SEARCH_CANDIDATES:
        try:
            page.goto(cand, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:
                pass
        except Exception as exc:
            result["error"] = type(exc).__name__
            continue
        html = page.content() or ""
        url = page.url or ""
        if re.search(r'type=["\']password["\']', html, re.I) and "login" in url.lower():
            continue
        # Prefer pages that look like opportunity lists
        if re.search(r"opportunit|project|solicitation|bid", html, re.I):
            search_url = url
            result["central_reachable"] = True
            break
        if "vendor.bonfirehub.com" in url or "account.bonfirehub.com" in url:
            search_url = url
            result["central_reachable"] = True
            # keep looking for a better opportunities page
            if "opportunit" in url.lower():
                break

    if not search_url:
        result["error"] = result.get("error") or "central_search_not_found"
        return result

    result["search_url"] = search_url

    # Try clicking Opportunities nav if still on dashboard/agency search
    try:
        link = page.get_by_role("link", name=re.compile(r"opportunit", re.I)).first
        if link.count() > 0:
            link.click(timeout=8_000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=20_000)
            except Exception:
                pass
            search_url = page.url or search_url
            result["search_url"] = search_url
    except Exception:
        pass

    # Never call response.json()/text() inside Playwright handlers — deadlock risk.
    # Collect candidate API URLs only; fetch via page.request after settle.
    xhr_urls: list[str] = []

    def _on_response(response: Any) -> None:
        try:
            if response.status != 200:
                return
            ct = (response.headers.get("content-type") or "").lower()
            u = response.url or ""
            if "json" not in ct and "api" not in u.lower() and "opportunit" not in u.lower():
                return
            if u and u not in xhr_urls and len(xhr_urls) < 40:
                xhr_urls.append(u)
        except Exception:
            return

    page.on("response", _on_response)

    all_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    pages = 0
    reported = None
    empty_streak = 0

    try:
        for page_num in range(1, max_pages + 1):
            if page_num > 1:
                advanced = False
                # Prefer Next button
                for sel in (
                    page.get_by_role("button", name=re.compile(r"^next$", re.I)),
                    page.get_by_role("link", name=re.compile(r"^next$", re.I)),
                    page.locator("button[aria-label*='Next' i], a[aria-label*='Next' i]"),
                    page.locator(".pagination .next, [data-testid*='next' i]"),
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
                            page.wait_for_load_state("domcontentloaded", timeout=20_000)
                        except Exception:
                            pass
                        break
                    except Exception:
                        continue
                if not advanced:
                    # Query param fallback
                    cur = page.url or search_url
                    if "page=" in cur.lower():
                        nxt = re.sub(r"([?&]page=)\d+", rf"\g<1>{page_num}", cur, flags=re.I)
                    else:
                        nxt = f"{cur}{'&' if '?' in cur else '?'}page={page_num}"
                    try:
                        page.goto(nxt, wait_until="domcontentloaded", timeout=45_000)
                        advanced = True
                    except Exception:
                        break
                    if not advanced:
                        break

            try:
                page.wait_for_timeout(800)
            except Exception:
                pass
            html = page.content() or ""
            url = page.url or search_url
            pages += 1
            if reported is None:
                reported = _reported_total_from_html(html)
                result["reported_total"] = reported

            # Fetch captured XHR URLs outside the response handler
            payloads: list[Any] = []
            for xu in list(xhr_urls)[-12:]:
                try:
                    resp = page.request.get(xu, timeout=15_000)
                    if resp.ok:
                        payloads.append({"url": xu, "data": resp.json()})
                except Exception:
                    continue
            xhr_urls.clear()
            batch = _extract_rows_from_xhr(payloads, page_url=url)
            if not batch:
                batch = _parse_opportunity_cards(html, page_url=url)

            # Category restriction signal
            if re.search(
                r"no\s+opportunit.*match.*profil|update\s+your\s+(commodity|category|profil)",
                html,
                re.I,
            ):
                result["account_category_restriction"] = True

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
            page.remove_listener("response", _on_response)
        except Exception:
            pass

    result["rows"] = all_rows
    result["retrieved_total"] = len(all_rows)
    result["pages_scanned"] = pages
    result["xhr_hits"] = 0  # cleared during loop; presence indicated by row source
    if not all_rows and not result.get("error"):
        result["error"] = "no_rows_parsed"
    return result


def run_euna_central_discovery(
    *,
    max_results: int = 5000,
    max_pages: int = 40,
    persist: bool = True,
    run_id: str | None = None,
    use_auth: bool = True,
    enrich_portals: bool = False,
    max_enrich_entities: int = 0,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """Primary Euna discovery via centralized Supplier Network."""
    from m3_canonical_discovery_bridge import merge_discovery_into_canonical
    from euna_auth.client import EunaAuthenticatedClient
    from euna_auth.telemetry import owner_connection_status, record_harvest_counters

    cfg = load_euna_auth_config()
    run_id = run_id or f"EUC-{uuid4().hex[:12]}"
    started = now_utc().isoformat()
    report: dict[str, Any] = {
        "kind": "EunaCentralSupplierNetworkDiscovery",
        "run_id": run_id,
        "started_at": started,
        "discovery_scope": M3_DISCOVERY_SCOPE,
        "discovery_mode": "CENTRAL_SUPPLIER_NETWORK",
        "auth": None,
        "central_reachable": False,
        "reported_total": None,
        "retrieved_total": 0,
        "pages_scanned": 0,
        "pagination_complete": False,
        "raw_opportunities": 0,
        "unique_records": 0,
        "account_category_restriction": False,
        "canonical_merge": {},
        "enrichment": None,
        "blocker": None,
    }

    if not use_auth or not cfg.auth_enabled:
        report["auth"] = {"status": DISABLED, "authenticated": False}
        report["blocker"] = "EUNA_AUTH_DISABLED"
        return report
    if not cfg.credentials_present:
        report["auth"] = {
            "status": AUTH_FAILED,
            "authenticated": False,
            "message": "EUNA_USERNAME/EUNA_PASSWORD not configured",
        }
        report["blocker"] = "EUNA_CREDENTIALS_MISSING"
        return report

    all_rows: list[dict[str, Any]] = []
    with EunaAuthenticatedClient() as client:
        auth = client.ensure_authenticated()
        report["auth"] = auth.to_dict()
        report["failure_reason"] = getattr(auth, "failure_reason", None) or (
            auth.to_dict().get("failure_reason") if hasattr(auth, "to_dict") else None
        )
        report["account_state"] = getattr(auth, "account_state", None)
        report["flow_detected"] = getattr(auth, "flow_detected", None)
        report["auth_host"] = getattr(auth, "auth_host", None)
        if not auth.authenticated:
            report["blocker"] = report.get("failure_reason") or auth.status or AUTH_FAILED
            if persist:
                _save_report(report)
            return report

        report["working_auth"] = 1
        if on_progress:
            try:
                on_progress(phase="EUNA_AUTH", pct=15, retrieved=0)
            except Exception:
                pass

        try:
            from euna_discovery.supplier_network import EunaSupplierNetworkClient

            net = EunaSupplierNetworkClient(client)
            central = net.harvest(
                max_results=max_results,
                max_pages=max_pages,
                on_progress=on_progress,
            )
        except Exception:
            log.exception("EunaSupplierNetworkClient failed — falling back to legacy harvest")
            central = harvest_central_supplier_network(
                client,
                max_results=max_results,
                max_pages=max_pages,
                on_progress=on_progress,
            )
        report["central_reachable"] = bool(central.get("central_reachable"))
        report["search_url"] = central.get("search_url")
        report["reported_total"] = central.get("reported_total")
        report["retrieved_total"] = central.get("retrieved_total")
        report["pages_scanned"] = central.get("pages_scanned")
        report["pagination_complete"] = bool(central.get("pagination_complete"))
        report["account_category_restriction"] = bool(central.get("account_category_restriction"))
        report["page_text_sample"] = central.get("page_text_sample")
        report["nav_clicked"] = central.get("nav_clicked")
        report["api_urls_seen"] = central.get("api_urls_seen")
        report["api_fallback"] = central.get("api_fallback")
        report["vendor_me_keys"] = central.get("vendor_me_keys")
        report["method"] = central.get("method")
        if central.get("account_category_restriction"):
            report["blocker"] = ACCOUNT_CATEGORY_RESTRICTION
        all_rows.extend(central.get("rows") or [])
        if central.get("account_entitlement"):
            report["account_entitlement"] = central.get("account_entitlement")
            report["account_state"] = central.get("account_entitlement")
        if central.get("error") and not all_rows:
            report["blocker"] = report.get("blocker") or central.get("error")

        # Optional enrichment via a few agency hubs (not primary discovery)
        if enrich_portals and max_enrich_entities > 0:
            try:
                from euna_discovery.harvest import harvest_bonfire_portal_public
                from euna_discovery.portals import known_euna_portals
                import httpx

                portals = known_euna_portals()[:max_enrich_entities]
                enrich_rows: list[dict[str, Any]] = []
                with httpx.Client(timeout=30.0, follow_redirects=True) as http:
                    for p in portals:
                        ent = harvest_bonfire_portal_public(p, client=http, max_pages=2)
                        enrich_rows.extend(ent.get("rows") or [])
                report["enrichment"] = {
                    "entities": len(portals),
                    "rows": len(enrich_rows),
                }
                all_rows.extend(enrich_rows)
            except Exception as exc:
                report["enrichment"] = {"error": type(exc).__name__}

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for r in all_rows:
        key = (
            str(r.get("detail_url") or "").strip().lower()
            or f"{r.get('external_id')}|{(r.get('title') or '')[:80]}".lower()
        )
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(r)

    report["raw_opportunities"] = len(all_rows)
    report["unique_records"] = len(unique)
    report["working_public"] = 1 if unique else 0

    merge = merge_discovery_into_canonical(
        run_id=run_id,
        trigger="euna_central_supplier_network",
        records=unique,
        sources_attempted=["euna_supplier_network"],
        sources_succeeded=["euna_supplier_network"] if unique else [],
        sources_failed=[] if unique else ["euna_supplier_network"],
        source_counts={
            "euna_supplier_network": {
                "ok": bool(unique),
                "raw": len(unique),
                "status": WORKING_AUTH if unique else report.get("blocker") or "NO_OPEN_BIDS",
                "reported_total": report.get("reported_total"),
            }
        },
        raw_opportunities_found=len(all_rows),
        records_normalized=len(unique),
        error_summary=report.get("blocker"),
        api_usage={"SAM": 0, "euna_central": True, "auth": report.get("auth")},
        started_at=started,
        persist=persist,
    )
    report["canonical_merge"] = {
        "new": merge.get("new_canonical_opportunities_added"),
        "updated": merge.get("existing_opportunities_updated"),
        "duplicates_detected": merge.get("duplicates_detected"),
        "canonical_after": merge.get("canonical_total_after"),
        "available_after": merge.get("currently_available_after"),
        "available_before": merge.get("currently_available_before"),
        "run_status": merge.get("run_status"),
    }
    report["net_new"] = merge.get("new_canonical_opportunities_added")
    report["completed_at"] = now_utc().isoformat()
    report["connection"] = owner_connection_status()

    if persist:
        _save_report(report)
        try:
            from m3_data_root import data_path

            cp = data_path(CHECKPOINT)
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "updated_at": now_utc().isoformat(),
                        "retrieved": len(unique),
                        "reported": report.get("reported_total"),
                        "pages": report.get("pages_scanned"),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass
        record_harvest_counters(
            {
                "euna_reported_open": report.get("reported_total"),
                "euna_harvested": report.get("unique_records"),
                "euna_pagination_complete": report.get("pagination_complete"),
                "euna_central_reachable": report.get("central_reachable"),
                "euna_net_new": report.get("net_new"),
            }
        )
    return report


def _save_report(report: dict[str, Any]) -> None:
    try:
        from m3_data_root import data_path

        path = data_path(REPORT)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    except Exception:
        log.exception("Failed saving Euna central discovery report")
