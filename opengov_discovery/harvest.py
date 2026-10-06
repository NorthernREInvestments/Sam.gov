"""Authenticated OpenGov platform-family discovery with full pagination."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from opengov_auth.config import ACCOUNT_CATEGORY_RESTRICTION, M3_DISCOVERY_SCOPE, load_opengov_auth_config
from opengov_auth.states import AUTH_CHALLENGE, AUTH_FAILED, DISABLED, WORKING
from opengov_auth.telemetry import owner_connection_status, record_discovery_counters
from opengov_discovery.parse import (
    detect_category_restriction,
    parse_opengov_json_payload,
    parse_opengov_portal_html,
)
from opengov_discovery.portals import classify_portal_fetch, known_opengov_portals

log = logging.getLogger("govtracker.opengov_discovery.harvest")

REPORT = "opengov_auth/last_discovery_report.json"
PORTAL_STATUS = "opengov_auth/portal_status.json"


def _to_record(raw: dict[str, Any], *, source_id: str, agency: str | None = None) -> dict[str, Any]:
    d = dict(raw)
    d["source_id"] = source_id
    d["platform"] = "live_opengov"
    d["platform_family"] = "OpenGov"
    d["agency"] = d.get("agency") or agency
    d["status"] = d.get("status") or "OPEN"
    d["discovery_universe"] = "LIVE"
    d["discovery_scope"] = M3_DISCOVERY_SCOPE
    d["authoritative_url"] = d.get("detail_url") or d.get("source_url")
    meta = dict(d.get("raw_metadata") or {})
    meta["discovery_scope"] = M3_DISCOVERY_SCOPE
    meta["vendor_profile_codes_ignored"] = True
    d["raw_metadata"] = meta
    if d.get("deadline_raw") and not d.get("deadline"):
        try:
            from discovery.deadline import normalize_deadline

            nd = normalize_deadline(str(d["deadline_raw"]))
            d["deadline"] = nd.get("utc_deadline") or nd.get("parsed_local")
        except Exception:
            d["deadline"] = d["deadline_raw"]
    return d


def _extract_reported_total(html: str, captured: list[Any]) -> int | None:
    for blob in captured:
        data = blob.get("data") if isinstance(blob, dict) else blob
        if isinstance(data, dict):
            for key in ("total", "totalCount", "totalElements", "count", "reported_total"):
                if data.get(key) is not None:
                    try:
                        return int(data[key])
                    except (TypeError, ValueError):
                        pass
    m = re.search(r"(?:of|total)[:\s]+(\d{1,6})\s+(?:projects?|results?|bids?)", html or "", re.I)
    if m:
        return int(m.group(1))
    return None


def harvest_portal(
    client: Any,
    portal: dict[str, Any],
    *,
    max_pages: int = 10,
) -> dict[str, Any]:
    """Harvest one OpenGov portal with authenticated browser + pagination."""
    url = str(portal.get("portal_url") or "")
    agency = f"{portal.get('entity_name')} ({portal.get('state')})" if portal.get("state") else portal.get("entity_name")
    sid = f"opengov_{re.sub(r'[^a-z0-9]+', '_', str(portal.get('entity_name') or url).lower())[:48]}"
    result: dict[str, Any] = {
        "entity_name": portal.get("entity_name"),
        "entity_type": portal.get("entity_type"),
        "state": portal.get("state"),
        "portal_url": url,
        "platform_family": "OPENGOV",
        "source_id": sid,
        "status": None,
        "rows": [],
        "reported_total": None,
        "retrieved_total": 0,
        "pages_scanned": 0,
        "pagination_complete": True,
        "error": None,
    }
    if not url:
        result["status"] = "INVALID_PORTAL"
        result["error"] = "no_url"
        return result

    all_recs: list[dict[str, Any]] = []
    pages = 0
    reported = None
    final_url = url
    last_html = ""
    parse_signals = False
    category_restricted = False

    try:
        # Page 1
        # Soft-retry once if CDN interstitial ("just a moment") appears
        html, captured = client.navigate_and_collect_json(url)
        if re.search(r"just\s*a\s*moment|cf-browser-verification|challenge-platform", html or "", re.I):
            page = getattr(client, "_page", None)
            if page is not None:
                try:
                    page.wait_for_timeout(4_000)
                    page.reload(wait_until="domcontentloaded", timeout=60_000)
                    html, captured = client.navigate_and_collect_json(url)
                except Exception:
                    pass
        pages = 1
        final_url = getattr(client, "_page", None).url if getattr(client, "_page", None) else url
        last_html = html
        category_restricted = detect_category_restriction(html)
        parse_signals = bool(
            re.search(r"projectTitle|proposalDeadline|/projects/|__NEXT_DATA__", html or "", re.I)
        )

        for blob in captured:
            data = blob.get("data")
            recs = parse_opengov_json_payload(data, list_url=url, agency=agency)
            all_recs.extend(recs)
        html_recs = parse_opengov_portal_html(html, list_url=url, agency=agency)
        all_recs.extend(html_recs)
        reported = _extract_reported_total(html, captured)

        # Pagination: ?page=N or click Next
        for page_idx in range(2, max_pages + 1):
            if reported is not None and len(all_recs) >= int(reported):
                break
            next_url = None
            # Query param style
            if "page=" in url.lower():
                next_url = re.sub(r"([?&]page=)\d+", rf"\g<1>{page_idx}", url, flags=re.I)
            else:
                sep = "&" if "?" in url else "?"
                next_url = f"{url}{sep}page={page_idx}"

            progressed = False
            try:
                html2, cap2 = client.navigate_and_collect_json(next_url)
                pages += 1
                batch: list[dict[str, Any]] = []
                for blob in cap2:
                    batch.extend(parse_opengov_json_payload(blob.get("data"), list_url=url, agency=agency))
                batch.extend(parse_opengov_portal_html(html2, list_url=url, agency=agency))
                # Dedup within portal
                existing = {str(r.get("detail_url") or r.get("external_id")) for r in all_recs}
                new_batch = [r for r in batch if str(r.get("detail_url") or r.get("external_id")) not in existing]
                if not new_batch:
                    # Try clicking Next if available
                    page = getattr(client, "_page", None)
                    if page is not None:
                        try:
                            nxt = page.get_by_role("button", name=re.compile(r"next", re.I)).first
                            if nxt.count() > 0 and nxt.is_enabled():
                                nxt.click(timeout=8_000)
                                page.wait_for_load_state("domcontentloaded", timeout=20_000)
                                html3 = page.content()
                                pages += 1
                                batch3 = parse_opengov_portal_html(html3, list_url=url, agency=agency)
                                new_batch = [
                                    r
                                    for r in batch3
                                    if str(r.get("detail_url") or r.get("external_id")) not in existing
                                ]
                        except Exception:
                            pass
                if not new_batch:
                    break
                all_recs.extend(new_batch)
                progressed = True
            except Exception:
                break
            if not progressed:
                break

        # Dedup
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for r in all_recs:
            key = str(r.get("detail_url") or f"{r.get('external_id')}|{r.get('title')}").lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(_to_record(r, source_id=sid, agency=agency))

        status = classify_portal_fetch(
            url=url,
            final_url=final_url,
            html=last_html,
            n_opps=len(unique),
            parse_signals=parse_signals or bool(unique),
        )
        pagination_complete = True
        if reported is not None and len(unique) < int(reported):
            pagination_complete = False
            result["discovery_truncated"] = True

        result.update(
            {
                "status": status,
                "rows": unique,
                "reported_total": reported,
                "retrieved_total": len(unique),
                "pages_scanned": pages,
                "pagination_complete": pagination_complete,
                "account_category_restriction": category_restricted,
                "live_opportunity_count": len(unique),
                "raw_result_count": len(all_recs),
                "auth_required": status == "AUTH_REQUIRED",
                "documents_available": any(r.get("document_links") for r in unique),
            }
        )
    except Exception as exc:
        msg = str(exc)
        ename = type(exc).__name__
        if "AUTH_CHALLENGE" in msg and re.search(r"captcha|mfa|recaptcha|hcaptcha", msg, re.I):
            result["status"] = AUTH_CHALLENGE
            result["error"] = "AUTH_CHALLENGE"
        elif "Timeout" in ename or "timeout" in msg.lower():
            result["status"] = "NETWORK_TIMEOUT"
            result["error"] = ename
        elif "AUTH_CHALLENGE" in msg:
            # Soft challenge / CDN — do not hard-fail the portal family
            result["status"] = "NETWORK_TIMEOUT"
            result["error"] = "soft_challenge"
        else:
            result["status"] = "SOURCE_ERROR"
            result["error"] = ename
        result["pages_scanned"] = pages
    return result


def run_opengov_authenticated_discovery(
    *,
    max_entities: int | None = None,
    max_pages: int = 8,
    persist: bool = True,
    run_id: str | None = None,
    use_auth: bool = True,
    public_first: bool = True,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """OpenGov discovery: public project-list first, auth only for enrichment/search."""
    from m3_canonical_discovery_bridge import merge_discovery_into_canonical
    from opengov_auth import OpenGovAuthenticatedClient
    from opengov_auth.states import WORKING_AUTH, WORKING_PUBLIC

    cfg = load_opengov_auth_config()
    run_id = run_id or f"OG-{uuid4().hex[:12]}"
    started = now_utc().isoformat()
    portals = known_opengov_portals()
    limit = int(max_entities if max_entities is not None else cfg.discovery_batch_size)
    selected = portals[: max(0, limit)]

    report: dict[str, Any] = {
        "kind": "OpenGovDiscovery",
        "run_id": run_id,
        "started_at": started,
        "discovery_scope": M3_DISCOVERY_SCOPE,
        "vendor_profile_codes_ignored": True,
        "auth": None,
        "public_first": public_first,
        "known_entities": len(portals),
        "entities_attempted": 0,
        "entities_successful": 0,
        "working_public": 0,
        "working_auth": 0,
        "portal_status_counts": {},
        "raw_opportunities": 0,
        "unique_records": 0,
        "duplicates_removed": 0,
        "pagination_truncated": [],
        "account_category_restriction": False,
        "records_sample": [],
        "per_entity": {},
        "canonical_merge": {},
        "SAM_API_CALLS": 0,
    }

    all_records: list[dict[str, Any]] = []
    status_counts: Counter = Counter()
    category_flag = False

    # --- PUBLIC FIRST ---
    if public_first:
        try:
            from opengov_discovery.public_harvest import run_opengov_public_discovery

            pub = run_opengov_public_discovery(
                max_entities=limit,
                max_pages=min(max_pages, 6),
                persist=False,
                run_id=f"{run_id}-pub",
                on_progress=on_progress,
            )
            report["public_discovery"] = {
                "entities_attempted": pub.get("entities_attempted"),
                "working_public": pub.get("working_public"),
                "anti_bot": pub.get("anti_bot"),
                "no_open_bids": pub.get("no_open_bids"),
                "raw_opportunities": pub.get("raw_opportunities"),
                "unique_records": pub.get("unique_records"),
                "portal_status_counts": pub.get("portal_status_counts"),
            }
            report["working_public"] = int(pub.get("working_public") or 0)
            for k, v in (pub.get("per_entity") or {}).items():
                report["per_entity"][k] = v
                st = v.get("status")
                if st:
                    status_counts[st] += 1
            report["entities_attempted"] = int(pub.get("entities_attempted") or 0)
            report["entities_successful"] = int(pub.get("entities_successful") or 0)
            report["anti_bot_public"] = int(pub.get("anti_bot") or 0)
            # Rows are returned on the public report (persist=False still includes records)
            all_records.extend(pub.get("records") or [])
        except Exception:
            log.exception("OpenGov public-first harvest failed — continuing to auth path")
            report["public_discovery"] = {"error": "public_harvest_failed"}

    # --- AUTH SECOND (vendor search + optional auth portal crawl for AUTH_REQUIRED only) ---
    auth_client = None
    if use_auth and cfg.auth_enabled and cfg.credentials_present:
        auth_client = OpenGovAuthenticatedClient()
        try:
            auth = auth_client.ensure_authenticated()
            report["auth"] = auth.to_dict()
            if not auth.authenticated:
                report["blocker"] = auth.status
                log.warning("OpenGov auth unavailable status=%s — keeping public results", auth.status)
            else:
                try:
                    auth_client.enable_resource_blocking()
                except Exception:
                    pass
                try:
                    from opengov_discovery.vendor_search import harvest_vendor_search

                    vendor = harvest_vendor_search(
                        auth_client,
                        max_results=max(100, limit * 25),
                        max_pages=max_pages,
                    )
                    report["vendor_global_search"] = {
                        "search_reachable": vendor.get("search_reachable"),
                        "search_url": vendor.get("search_url"),
                        "reported_total": vendor.get("reported_total"),
                        "retrieved_total": vendor.get("retrieved_total"),
                        "pages_scanned": vendor.get("pages_scanned"),
                        "pagination_complete": vendor.get("pagination_complete"),
                        "data_endpoints": (vendor.get("data_endpoints") or [])[:15],
                        "error": vendor.get("error"),
                    }
                    vrows = vendor.get("rows") or []
                    if vrows:
                        all_records.extend(vrows)
                        status_counts[WORKING_AUTH] += 1
                        report["working_auth"] += 1
                        report["entities_successful"] += 1
                        report["per_entity"]["opengov_vendor_global_search"] = {
                            "ok": True,
                            "status": WORKING_AUTH,
                            "raw": len(vrows),
                            "reported_total": vendor.get("reported_total"),
                            "pagination_complete": vendor.get("pagination_complete"),
                            "entity_name": "OpenGov Vendor Global Search",
                            "platform_family": "OPENGOV",
                        }
                except Exception as exc:
                    report["vendor_global_search"] = {"error": type(exc).__name__}
                    log.warning("OpenGov vendor global search failed: %s", type(exc).__name__)
        except Exception:
            log.exception("OpenGov auth client failed")
            report["auth"] = {"status": AUTH_FAILED, "authenticated": False}
    elif not cfg.auth_enabled:
        report["auth"] = report.get("auth") or {"status": DISABLED, "authenticated": False}
    elif not cfg.credentials_present:
        report["auth"] = report.get("auth") or {
            "status": AUTH_FAILED,
            "authenticated": False,
            "message": "OPENGOV_USERNAME/OPENGOV_PASSWORD not configured",
        }

    # Auth per-portal only when public said AUTH_REQUIRED (not ANTI_BOT)
    auth_targets = [
        p
        for p in selected
        if (
            report["per_entity"].get(
                f"opengov_{re.sub(r'[^a-z0-9]+', '_', str(p.get('entity_name') or p.get('portal_url') or '').lower())[:48]}"
            )
            or {}
        ).get("status")
        == "AUTH_REQUIRED"
    ]
    try:
        if auth_client and getattr(auth_client, "is_authenticated", False) and (
            auth_targets or (not all_records and selected)
        ):
            crawl = auth_targets or selected[: min(5, len(selected))]
            for portal in crawl:
                try:
                    ent = harvest_portal(auth_client, portal, max_pages=min(max_pages, 4))
                except Exception as exc:
                    ent = {
                        "entity_name": portal.get("entity_name"),
                        "portal_url": portal.get("portal_url"),
                        "source_id": f"opengov_err_{(portal.get('entity_name') or 'x')}",
                        "status": "NETWORK_TIMEOUT" if "Timeout" in type(exc).__name__ else "SOURCE_ERROR",
                        "rows": [],
                        "error": type(exc).__name__,
                        "retrieved_total": 0,
                    }
                report["entities_attempted"] += 1
                st = ent.get("status") or "SOURCE_ERROR"
                if st == AUTH_CHALLENGE and (ent.get("error") or "") in {"AUTH_CHALLENGE", "challenge"}:
                    st = "NETWORK_TIMEOUT"
                    ent["status"] = st
                if st == WORKING:
                    st = WORKING_AUTH
                    ent["status"] = st
                status_counts[st] += 1
                report["per_entity"][ent.get("source_id") or portal.get("portal_url")] = {
                    "ok": st in {WORKING, WORKING_AUTH, WORKING_PUBLIC, "PARTIAL"},
                    "status": st,
                    "raw": ent.get("retrieved_total") or 0,
                    "pages_scanned": ent.get("pages_scanned"),
                    "reported_total": ent.get("reported_total"),
                    "pagination_complete": ent.get("pagination_complete"),
                    "entity_name": ent.get("entity_name"),
                    "state": ent.get("state"),
                    "portal_url": ent.get("portal_url"),
                    "error": ent.get("error"),
                    "platform_family": "OPENGOV",
                }
                if ent.get("account_category_restriction"):
                    category_flag = True
                if ent.get("discovery_truncated"):
                    report["pagination_truncated"].append(ent.get("source_id"))
                rows = ent.get("rows") or []
                if rows:
                    report["entities_successful"] += 1
                    report["working_auth"] = int(report.get("working_auth") or 0) + 1
                    all_records.extend(rows)
                if st == AUTH_CHALLENGE and "captcha" in str(ent.get("error") or "").lower():
                    report["blocker"] = AUTH_CHALLENGE
                    log.warning("OpenGov AUTH_CHALLENGE mid-discovery — stopping auth crawl")
                    break
    finally:
        if auth_client is not None:
            try:
                auth_client.close()
            except Exception:
                pass

    # Dedup + canonical merge (always)
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    dupes = 0
    for r in all_records:
        key = (
            str(r.get("detail_url") or "").strip().lower()
            or f"{r.get('source_id')}|{r.get('external_id')}|{(r.get('title') or '')[:80]}".lower()
        )
        if not key or key in seen:
            dupes += 1
            continue
        seen.add(key)
        unique.append(r)

    report["portal_status_counts"] = dict(status_counts)
    report["raw_opportunities"] = len(all_records)
    report["unique_records"] = len(unique)
    report["duplicates_removed"] = dupes
    report["account_category_restriction"] = category_flag
    if category_flag:
        report["ACCOUNT_CATEGORY_RESTRICTION"] = ACCOUNT_CATEGORY_RESTRICTION
    report["records_sample"] = [
        {"title": (r.get("title") or "")[:80], "agency": r.get("agency"), "url": r.get("detail_url")}
        for r in unique[:15]
    ]

    succeeded = [k for k, v in report["per_entity"].items() if v.get("ok")]
    failed = [k for k, v in report["per_entity"].items() if not v.get("ok")]
    merge = merge_discovery_into_canonical(
        run_id=run_id,
        trigger="opengov_discovery",
        records=unique,
        sources_attempted=list(report["per_entity"].keys()),
        sources_succeeded=succeeded,
        sources_failed=failed,
        source_counts=report["per_entity"],
        raw_opportunities_found=len(all_records),
        records_normalized=len(unique),
        error_summary=("DISCOVERY_TRUNCATED" if report["pagination_truncated"] else None),
        api_usage={"SAM": 0, "opengov_public_first": True, "opengov_auth": bool(report.get("auth"))},
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
    report["canonical_live"] = merge.get("currently_available_after")

    try:
        record_discovery_counters(
            {
                "opengov_known_entities": len(portals),
                "opengov_entities_attempted": report["entities_attempted"],
                "opengov_entities_successful": report["entities_successful"],
                "opengov_raw_discovered": len(all_records),
                "opengov_canonical_live": merge.get("currently_available_after") or 0,
                "account_category_restriction": category_flag,
            }
        )
    except Exception:
        pass

    report["completed_at"] = now_utc().isoformat()
    report["connection"] = owner_connection_status()
    if persist:
        _save_report(report)
        _save_portal_status(report)
    return report


def _save_report(report: dict[str, Any]) -> None:
    from m3_data_root import data_path

    path = data_path(REPORT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")


def _save_portal_status(report: dict[str, Any]) -> None:
    from m3_data_root import data_path

    path = data_path(PORTAL_STATUS)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": "OpenGovPortalStatus",
        "updated_at": now_utc().isoformat(),
        "status_counts": report.get("portal_status_counts"),
        "per_entity": report.get("per_entity"),
        "known_entities": report.get("known_entities"),
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
