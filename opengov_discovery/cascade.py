"""Full OpenGov route cascade — ANTI_BOT is never terminal while routes remain."""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from opengov_auth.config import M3_DISCOVERY_SCOPE
from opengov_auth.states import (
    ANTI_BOT,
    INVALID_PORTAL,
    MOVED,
    NO_OPEN_BIDS,
    RECOVERY_BLOCKED,
    SUCCESS_STATUSES,
    WORKING_AGENCY_FALLBACK,
    WORKING_AUTH_REQUEST,
    WORKING_BROWSER,
    WORKING_EMBED,
    WORKING_PAGE_STATE,
    WORKING_STRUCTURED,
)
from opengov_discovery.portals import classify_portal_fetch, known_opengov_portals
from opengov_discovery.public_data_client import OpenGovPublicDataClient
from opengov_discovery.route_resolver import (
    ROUTE_AGENCY_SOURCE,
    ROUTE_AUTH_SESSION,
    ROUTE_BROWSER,
    ROUTE_PUBLIC_EMBED,
    ROUTE_PUBLIC_PAGE_STATE,
    ROUTE_PUBLIC_STRUCTURED,
    STATUS_FOR_ROUTE,
    OpenGovRouteResolver,
    entity_key,
)

log = logging.getLogger("govtracker.opengov_discovery.cascade")

REPORT = "opengov_auth/last_cascade_discovery_report.json"
MAP_REPORT = "opengov_auth/last_route_map_report.json"


def _to_record(raw: dict[str, Any], *, source_id: str, agency: str | None, list_url: str, route_kind: str) -> dict[str, Any]:
    d = dict(raw)
    d["source_id"] = source_id
    d["platform"] = "live_opengov"
    d["platform_family"] = "OpenGov"
    d["agency"] = d.get("agency") or agency
    d["status"] = d.get("status") or "OPEN"
    d["discovery_universe"] = "LIVE"
    d["discovery_scope"] = M3_DISCOVERY_SCOPE
    d["authoritative_url"] = d.get("detail_url") or d.get("source_url") or list_url
    meta = dict(d.get("raw_metadata") or {})
    meta["discovery_method"] = route_kind
    meta["discovery_scope"] = M3_DISCOVERY_SCOPE
    meta["vendor_profile_codes_ignored"] = True
    meta["platform_family"] = "OpenGov"
    d["raw_metadata"] = meta
    return d


def _source_id(portal: dict[str, Any]) -> str:
    return f"opengov_{re.sub(r'[^a-z0-9]+', '_', str(portal.get('entity_name') or portal.get('portal_url') or '').lower())[:48]}"


def harvest_entity_cascade(
    portal: dict[str, Any],
    *,
    resolver: OpenGovRouteResolver | None = None,
    public_client: OpenGovPublicDataClient | None = None,
    auth_client: Any | None = None,
    max_pages: int = 8,
    allow_browser: bool = False,
) -> dict[str, Any]:
    """Try full route cascade for one entity. ANTI_BOT continues to next route."""
    resolver = resolver or OpenGovRouteResolver()
    owns_public = public_client is None
    public_client = public_client or OpenGovPublicDataClient()
    agency = (
        f"{portal.get('entity_name')} ({portal.get('state')})"
        if portal.get("state")
        else portal.get("entity_name")
    )
    sid = _source_id(portal)
    ent_meta = resolver.get_entity(portal)

    result: dict[str, Any] = {
        "entity_key": entity_key(portal),
        "entity_name": portal.get("entity_name"),
        "entity_type": portal.get("entity_type"),
        "state": portal.get("state"),
        "portal_url": portal.get("portal_url"),
        "source_id": sid,
        "status": None,
        "working_route": None,
        "routes_attempted": [],
        "failure_per_route": {},
        "rows": [],
        "reported_total": None,
        "retrieved_total": 0,
        "pages_scanned": 0,
        "pagination_complete": False,
        "anti_bot_primary": False,
        "recovered_via_fallback": False,
        "error": None,
    }

    try:
        candidates = resolver.candidate_urls(portal)
        saw_primary_cf = False

        for route_kind, url in candidates:
            # Browser only when explicitly allowed (route-map / last resort)
            if route_kind == ROUTE_BROWSER and not allow_browser:
                continue
            # Auth routes need session
            if route_kind == ROUTE_AUTH_SESSION and (
                auth_client is None or not getattr(auth_client, "is_authenticated", False)
            ):
                result["failure_per_route"][route_kind] = "auth_unavailable"
                if route_kind not in result["routes_attempted"]:
                    result["routes_attempted"].append(route_kind)
                continue

            if route_kind not in result["routes_attempted"]:
                result["routes_attempted"].append(route_kind)

            rows: list[dict[str, Any]] = []
            method = None
            final_url = url
            reported = None
            pages = 0

            if route_kind == ROUTE_AUTH_SESSION:
                try:
                    # Prefer session-bound API requests only (no full page navigation)
                    if "/api/" not in url.lower() and "api.procurement" not in url.lower():
                        result["failure_per_route"][route_kind] = "auth_html_skipped_use_api"
                        continue
                    # project/public is keyless POST — reuse public client pagination
                    if re.search(r"/government/[^/]+/project/public/?$", url, re.I):
                        fr = public_client.fetch_project_public_url(
                            url, page_size=50, max_pages=max_pages, open_only=True
                        )
                        if fr.get("cloudflare"):
                            resolver.mark_blocked(
                                portal, route_kind=route_kind, route_url=url, reason=ANTI_BOT
                            )
                            result["failure_per_route"][route_kind] = ANTI_BOT
                            continue
                        if not fr.get("ok") and not fr.get("rows"):
                            result["failure_per_route"][route_kind] = fr.get("error") or "auth_json_empty"
                            continue
                        parsed = public_client.parse_list(fr, agency=agency)
                        rows = parsed.get("rows") or []
                        method = "auth_project_public"
                        pages = int(fr.get("pages_scanned") or 1)
                        reported = fr.get("reported_total")
                        result["pagination_complete"] = bool(fr.get("pagination_complete"))
                        if not rows:
                            if (reported or 0) == 0 or fr.get("ok"):
                                resolver.mark_final_status(portal, NO_OPEN_BIDS)
                                result["status"] = NO_OPEN_BIDS
                                result["working_route"] = route_kind
                                result["pages_scanned"] = pages
                                result["reported_total"] = reported
                                result["pagination_complete"] = True
                                return result
                            result["failure_per_route"][route_kind] = "auth_json_no_rows"
                            continue
                    else:
                        data = auth_client.fetch_json(url)
                        if data is None:
                            resolver.mark_blocked(
                                portal, route_kind=route_kind, route_url=url, reason="auth_json_empty"
                            )
                            result["failure_per_route"][route_kind] = "auth_json_empty"
                            continue
                        from opengov_discovery.parse import parse_opengov_json_payload

                        rows = parse_opengov_json_payload(data, list_url=url, agency=agency)
                        method = "auth_json"
                        pages = 1
                        if not rows:
                            result["failure_per_route"][route_kind] = "auth_json_no_rows"
                            continue
                except Exception as exc:
                    result["failure_per_route"][route_kind] = type(exc).__name__
                    resolver.mark_blocked(
                        portal, route_kind=route_kind, route_url=url, reason=type(exc).__name__
                    )
                    continue

            elif route_kind == ROUTE_BROWSER and auth_client is not None:
                try:
                    body = auth_client.fetch_html(url) or ""
                    from opengov_discovery.public_data_client import is_cloudflare

                    if is_cloudflare(body):
                        resolver.mark_blocked(
                            portal, route_kind=route_kind, route_url=url, reason=ANTI_BOT
                        )
                        result["failure_per_route"][route_kind] = ANTI_BOT
                        continue
                    parsed = public_client.parse_list(
                        {"final_url": url, "body": body, "cloudflare": False},
                        agency=agency,
                    )
                    rows = parsed.get("rows") or []
                    method = "browser_html"
                    pages = 1
                except Exception as exc:
                    result["failure_per_route"][route_kind] = type(exc).__name__
                    continue

            else:
                # Public HTTP / agency — structured project/public uses POST pagination
                if re.search(r"/government/[^/]+/project/public/?$", url, re.I):
                    fr = public_client.fetch_project_public_url(
                        url, page_size=50, max_pages=max_pages, open_only=True
                    )
                else:
                    fr = public_client.fetch(url)
                final_url = fr.get("final_url") or url
                if fr.get("cloudflare") or fr.get("status_code") in {403, 503}:
                    resolver.mark_blocked(
                        portal, route_kind=route_kind, route_url=url, reason=ANTI_BOT
                    )
                    result["failure_per_route"][route_kind] = ANTI_BOT
                    if route_kind in {
                        ROUTE_PUBLIC_STRUCTURED,
                        ROUTE_PUBLIC_EMBED,
                        ROUTE_PUBLIC_PAGE_STATE,
                    }:
                        saw_primary_cf = True
                        result["anti_bot_primary"] = True
                    continue
                if fr.get("status_code") == 404:
                    result["failure_per_route"][route_kind] = "http_404"
                    continue
                if not fr.get("ok") and not fr.get("rows"):
                    result["failure_per_route"][route_kind] = fr.get("error") or f"http_{fr.get('status_code')}"
                    continue

                parsed = public_client.parse_list(fr, agency=agency)
                rows = parsed.get("rows") or []
                # Prefer pre-parsed rows from project/public fetch
                if not rows and fr.get("rows"):
                    from opengov_discovery.parse import parse_opengov_json_payload

                    rows = parse_opengov_json_payload(
                        {"count": fr.get("reported_total"), "rows": fr.get("rows")},
                        list_url=final_url,
                        agency=agency,
                    )
                method = parsed.get("method") or (
                    "project_public_post" if "project/public" in url else None
                )
                reported = fr.get("reported_total") if fr.get("reported_total") is not None else parsed.get("reported_total")
                pages = int(fr.get("pages_scanned") or 1)
                if fr.get("pagination_complete") is not None:
                    result["pagination_complete"] = bool(fr.get("pagination_complete"))

                if not rows:
                    # Empty open list from structured API = no open bids (not blocked)
                    if "project/public" in url and fr.get("status_code") == 200:
                        resolver.mark_final_status(portal, NO_OPEN_BIDS)
                        result["status"] = NO_OPEN_BIDS
                        result["working_route"] = route_kind
                        result["pagination_complete"] = True
                        result["pages_scanned"] = pages
                        result["reported_total"] = reported
                        return result
                    st = classify_portal_fetch(
                        url=url,
                        final_url=final_url,
                        html=fr.get("body") or "",
                        n_opps=0,
                        parse_signals=bool(
                            re.search(r"project|solicitation|bid", fr.get("body") or "", re.I)
                        ),
                        http_status=int(fr.get("status_code") or 0),
                    )
                    if st == NO_OPEN_BIDS:
                        resolver.mark_final_status(portal, NO_OPEN_BIDS)
                        result["status"] = NO_OPEN_BIDS
                        result["working_route"] = route_kind
                        result["pagination_complete"] = True
                        result["pages_scanned"] = pages
                        return result
                    if st in {INVALID_PORTAL, MOVED}:
                        result["failure_per_route"][route_kind] = st
                        continue
                    result["failure_per_route"][route_kind] = st or "no_rows"
                    continue

                # Lightweight pagination for non-structured public routes
                if "project/public" not in url:
                    for p in range(2, max_pages + 1):
                        if "page=" in final_url.lower():
                            next_url = re.sub(r"([?&]page=)\d+", rf"\g<1>{p}", final_url, flags=re.I)
                        elif "?" in final_url:
                            next_url = f"{final_url}&page={p}"
                        else:
                            next_url = f"{final_url}?page={p}"
                        fr2 = public_client.fetch(next_url)
                        if fr2.get("cloudflare") or not fr2.get("ok"):
                            break
                        more = public_client.parse_list(fr2, agency=agency).get("rows") or []
                        if not more:
                            break
                        existing = {str(x.get("detail_url") or x.get("external_id")) for x in rows}
                        added = 0
                        for r in more:
                            key = str(r.get("detail_url") or r.get("external_id"))
                            if key in existing:
                                continue
                            rows.append(r)
                            existing.add(key)
                            added += 1
                        pages += 1
                        if added == 0:
                            break

            if not rows:
                continue

            # Success
            status = STATUS_FOR_ROUTE.get(route_kind) or WORKING_PAGE_STATE
            if route_kind == ROUTE_PUBLIC_STRUCTURED:
                status = WORKING_STRUCTURED
            elif route_kind == ROUTE_PUBLIC_EMBED:
                status = WORKING_EMBED
            elif route_kind == ROUTE_PUBLIC_PAGE_STATE:
                status = WORKING_PAGE_STATE
            elif route_kind == ROUTE_AUTH_SESSION:
                status = WORKING_AUTH_REQUEST
            elif route_kind == ROUTE_AGENCY_SOURCE:
                status = WORKING_AGENCY_FALLBACK
            elif route_kind == ROUTE_BROWSER:
                status = WORKING_BROWSER

            records = [
                _to_record(r, source_id=sid, agency=agency, list_url=final_url, route_kind=route_kind)
                for r in rows
            ]
            resolver.mark_success(
                portal,
                route_kind=route_kind,
                route_url=final_url,
                status=status,
                retrieved=len(records),
            )
            result.update(
                {
                    "status": status,
                    "working_route": route_kind,
                    "working_route_url": final_url,
                    "discovery_method": method,
                    "rows": records,
                    "retrieved_total": len(records),
                    "reported_total": reported,
                    "pages_scanned": pages,
                    "pagination_complete": True,
                    "anti_bot_primary": saw_primary_cf or bool(ent_meta.get("anti_bot_primary")),
                    "recovered_via_fallback": bool(
                        (saw_primary_cf or ent_meta.get("anti_bot_primary"))
                        and route_kind
                        not in {
                            ROUTE_PUBLIC_STRUCTURED,
                            ROUTE_PUBLIC_EMBED,
                            ROUTE_PUBLIC_PAGE_STATE,
                        }
                    ),
                }
            )
            return result

        # Exhausted cascade
        if saw_primary_cf:
            result["anti_bot_primary"] = True
        # Prefer specific terminal states over generic ANTI_BOT
        final = RECOVERY_BLOCKED
        fails = result.get("failure_per_route") or {}
        if all(v == "http_404" for v in fails.values()) and fails:
            final = INVALID_PORTAL
        resolver.mark_final_status(portal, final)
        result["status"] = final
        result["error"] = "all_routes_exhausted"
        return result
    finally:
        if owns_public:
            public_client.close()


def run_opengov_cascade_discovery(
    *,
    max_entities: int | None = None,
    max_pages: int = 6,
    persist: bool = True,
    run_id: str | None = None,
    use_auth: bool = True,
    allow_browser: bool = False,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """Map + harvest all (or N) OpenGov entities via cascade; merge canonical."""
    from m3_canonical_discovery_bridge import merge_discovery_into_canonical
    from opengov_auth import OpenGovAuthenticatedClient
    from opengov_auth.config import load_opengov_auth_config
    from opengov_auth.telemetry import record_discovery_counters

    cfg = load_opengov_auth_config()
    run_id = run_id or f"OGC-{uuid4().hex[:12]}"
    started = now_utc().isoformat()
    portals = known_opengov_portals()
    # Enrich agency_source_url from catalog when available
    for p in portals:
        if not p.get("agency_source_url"):
            # keep portal_url as agency source when non-cdn
            url = str(p.get("portal_url") or "")
            if url and "opengov.com" not in url.lower():
                p["agency_source_url"] = url

    # Resolve government.code for known portals; append remaining API directory entities
    directory = None
    try:
        from opengov_discovery.government_directory import OpenGovGovernmentDirectory

        directory = OpenGovGovernmentDirectory()
        directory.ensure_loaded()
        seen_codes: set[str] = set()
        for p in portals:
            code = directory.resolve_code(p)
            if code:
                p["government_code"] = code
                seen_codes.add(code)
        # Full OpenGov public universe (same source family — structured directory)
        for dp in directory.as_portals():
            code = str(dp.get("government_code") or "")
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            portals.append(dp)
    except Exception as exc:
        log.warning("OpenGov government directory unavailable: %s", type(exc).__name__)

    # Prefer entities with a resolved government.code so staged runs exercise structured first
    portals.sort(key=lambda p: (0 if p.get("government_code") else 1, str(p.get("entity_name") or "")))
    limit = len(portals) if max_entities is None else max(0, int(max_entities))
    selected = portals[:limit]
    resolver = OpenGovRouteResolver()

    report: dict[str, Any] = {
        "kind": "OpenGovCascadeDiscovery",
        "run_id": run_id,
        "started_at": started,
        "discovery_scope": M3_DISCOVERY_SCOPE,
        "known_entities": len(portals),
        "catalog_entities": len(known_opengov_portals()),
        "directory_codes": len(directory.codes) if directory is not None else 0,
        "entities_attempted": 0,
        "entities_successful": 0,
        "portal_status_counts": {},
        "route_counts": {},
        "anti_bot_primary_failures": 0,
        "anti_bot_recovered_via_fallback": 0,
        "recovery_blocked": 0,
        "raw_opportunities": 0,
        "unique_records": 0,
        "pagination_complete_entities": 0,
        "per_entity": {},
        "blocked_entities": [],
        "canonical_merge": {},
        "auth": None,
        "vendor_global_search": None,
        "resolver_telemetry": {},
    }

    all_records: list[dict[str, Any]] = []
    status_counts: Counter = Counter()
    route_counts: Counter = Counter()

    auth_client = None
    if use_auth and cfg.auth_enabled and cfg.credentials_present:
        try:
            auth_client = OpenGovAuthenticatedClient()
            auth = auth_client.ensure_authenticated()
            report["auth"] = auth.to_dict()
            if auth.authenticated:
                try:
                    from opengov_discovery.vendor_search import harvest_vendor_search

                    vendor = harvest_vendor_search(
                        auth_client,
                        max_results=max(200, limit * 10),
                        max_pages=max_pages,
                    )
                    report["vendor_global_search"] = {
                        "search_reachable": vendor.get("search_reachable"),
                        "search_url": vendor.get("search_url"),
                        "reported_total": vendor.get("reported_total"),
                        "retrieved_total": vendor.get("retrieved_total"),
                        "pages_scanned": vendor.get("pages_scanned"),
                        "pagination_complete": vendor.get("pagination_complete"),
                        "data_endpoints": (vendor.get("data_endpoints") or [])[:20],
                        "error": vendor.get("error"),
                    }
                    for r in vendor.get("rows") or []:
                        r = dict(r)
                        r["source_id"] = r.get("source_id") or "opengov_vendor_global"
                        r["platform"] = "live_opengov"
                        r["platform_family"] = "OpenGov"
                        meta = dict(r.get("raw_metadata") or {})
                        meta["discovery_method"] = "WORKING_GLOBAL_SEARCH"
                        r["raw_metadata"] = meta
                        all_records.append(r)
                    if vendor.get("rows"):
                        status_counts["WORKING_GLOBAL_SEARCH"] += 1
                        route_counts["WORKING_GLOBAL_SEARCH"] += 1
                        report["entities_successful"] += 1
                except Exception as exc:
                    report["vendor_global_search"] = {"error": type(exc).__name__}
            else:
                auth_client = None
        except Exception:
            log.exception("OpenGov auth failed during cascade")
            auth_client = None
            report["auth"] = {"authenticated": False, "status": "AUTH_FAILED"}

    with OpenGovPublicDataClient() as public_client:
        # Browser fallback only when explicitly requested (too slow for 142× scheduled)
        for i, portal in enumerate(selected):
            ent = harvest_entity_cascade(
                portal,
                resolver=resolver,
                public_client=public_client,
                auth_client=auth_client,
                max_pages=max_pages,
                allow_browser=allow_browser,
            )
            report["entities_attempted"] += 1
            st = ent.get("status") or RECOVERY_BLOCKED
            status_counts[st] += 1
            if ent.get("working_route"):
                route_counts[ent["working_route"]] += 1
            if st in SUCCESS_STATUSES:
                report["entities_successful"] += 1
            if ent.get("anti_bot_primary"):
                report["anti_bot_primary_failures"] += 1
            if ent.get("recovered_via_fallback"):
                report["anti_bot_recovered_via_fallback"] += 1
            if st == RECOVERY_BLOCKED:
                report["recovery_blocked"] += 1
                report["blocked_entities"].append(
                    {
                        "entity_name": ent.get("entity_name"),
                        "portal_url": ent.get("portal_url"),
                        "routes_attempted": ent.get("routes_attempted"),
                        "failure_per_route": ent.get("failure_per_route"),
                    }
                )
            if ent.get("pagination_complete"):
                report["pagination_complete_entities"] += 1
            all_records.extend(ent.get("rows") or [])
            report["per_entity"][ent.get("source_id") or portal.get("portal_url")] = {
                "ok": st in SUCCESS_STATUSES,
                "status": st,
                "working_route": ent.get("working_route"),
                "raw": ent.get("retrieved_total") or 0,
                "pages_scanned": ent.get("pages_scanned"),
                "routes_attempted": ent.get("routes_attempted"),
                "failure_per_route": ent.get("failure_per_route"),
                "anti_bot_primary": ent.get("anti_bot_primary"),
                "recovered_via_fallback": ent.get("recovered_via_fallback"),
                "entity_name": ent.get("entity_name"),
                "state": ent.get("state"),
                "portal_url": ent.get("portal_url"),
            }
            if on_progress:
                try:
                    on_progress(
                        phase="OPENGOV_CASCADE",
                        pct=min(95, int(10 + 80 * (i + 1) / max(1, len(selected)))),
                        entities=i + 1,
                        retrieved=len(all_records),
                    )
                except Exception:
                    pass

    if auth_client is not None:
        try:
            auth_client.close()
        except Exception:
            pass

    # Dedupe within run
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for r in all_records:
        key = str(r.get("detail_url") or r.get("external_id") or r.get("title") or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(r)

    report["portal_status_counts"] = dict(status_counts)
    report["route_counts"] = dict(route_counts)
    report["raw_opportunities"] = len(all_records)
    report["unique_records"] = len(unique)
    report["records"] = unique
    report["resolver_telemetry"] = resolver.telemetry_summary()

    if persist and unique:
        succeeded = [k for k, v in report["per_entity"].items() if v.get("ok")]
        failed = [k for k, v in report["per_entity"].items() if not v.get("ok")]
        merge = merge_discovery_into_canonical(
            run_id=run_id,
            trigger="opengov_cascade",
            records=unique,
            sources_attempted=list(report["per_entity"].keys()),
            sources_succeeded=succeeded,
            sources_failed=failed,
            source_counts=report["per_entity"],
            raw_opportunities_found=len(all_records),
            records_normalized=len(unique),
            api_usage={"SAM": 0, "opengov_cascade": True},
            started_at=started,
            persist=True,
        )
        report["canonical_merge"] = merge
        report["net_new"] = merge.get("new_canonical_opportunities_added") or merge.get("new") or 0
        report["existing_enriched"] = merge.get("existing_enriched") or merge.get("updated") or 0
        report["within_source_duplicates"] = merge.get("duplicates_detected")
    elif persist:
        report["canonical_merge"] = {"new": 0, "updated": 0}
        report["net_new"] = 0

    try:
        record_discovery_counters(
            {
                "opengov_harvested": report["unique_records"],
                "opengov_entities_attempted": report["entities_attempted"],
                "opengov_entities_successful": report["entities_successful"],
                "opengov_net_new": report.get("net_new") or 0,
                "opengov_anti_bot_recovered": report["anti_bot_recovered_via_fallback"],
            }
        )
    except Exception:
        pass

    report["completed_at"] = now_utc().isoformat()
    try:
        from m3_data_root import data_path

        path = data_path(REPORT)
        path.parent.mkdir(parents=True, exist_ok=True)
        import json

        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        report["report_path"] = str(path)
    except Exception:
        pass
    return report


def run_opengov_route_map(
    *,
    max_entities: int | None = None,
    use_auth: bool = True,
    allow_browser: bool = False,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """Bounded route-mapping pass — classify each entity without requiring full pagination."""
    report = run_opengov_cascade_discovery(
        max_entities=max_entities,
        max_pages=2,
        persist=False,
        use_auth=use_auth,
        allow_browser=allow_browser,
        on_progress=on_progress,
        run_id=f"OGRM-{uuid4().hex[:12]}",
    )
    report["kind"] = "OpenGovRouteMap"
    try:
        from m3_data_root import data_path
        import json

        path = data_path(MAP_REPORT)
        path.parent.mkdir(parents=True, exist_ok=True)
        slim = {k: report.get(k) for k in report if k != "records"}
        path.write_text(json.dumps(slim, indent=2, default=str), encoding="utf-8")
        report["report_path"] = str(path)
    except Exception:
        pass
    return report
