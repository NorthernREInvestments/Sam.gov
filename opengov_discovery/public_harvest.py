"""Public-first OpenGov discovery — HTTP project-list paths, no Playwright."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import httpx

from application_clock import now_utc
from opengov_auth.config import M3_DISCOVERY_SCOPE
from opengov_auth.states import (
    ANTI_BOT,
    BROKEN_PARSER,
    INVALID_PORTAL,
    MOVED,
    NO_OPEN_BIDS,
    PARTIAL,
    SOURCE_ERROR,
    SUCCESS_STATUSES,
    WORKING_PUBLIC,
)
from opengov_discovery.parse import parse_opengov_json_payload, parse_opengov_portal_html
from opengov_discovery.portals import classify_portal_fetch, known_opengov_portals

log = logging.getLogger("govtracker.opengov_discovery.public_harvest")

ENTITY_REGISTRY = "opengov_auth/entity_registry.json"
ROUTE_CACHE = "opengov_auth/route_cache.json"
REPORT = "opengov_auth/last_public_discovery_report.json"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Route cascade labels (first working route is cached per entity)
ROUTE_PUBLIC_STRUCTURED = "PUBLIC_STRUCTURED"
ROUTE_PUBLIC_PROJECT_LIST = "PUBLIC_PROJECT_LIST"
ROUTE_PUBLIC_PAGE_STATE = "PUBLIC_PAGE_STATE"
ROUTE_AUTH_SESSION = "AUTHENTICATED_SESSION_REQUEST"
ROUTE_AGENCY_SOURCE = "ORIGINAL_AGENCY_SOURCE"
ROUTE_BROWSER = "BROWSER_FALLBACK"


def _slug_from_portal(url: str) -> str | None:
    m = re.search(r"/portal/([^/?#]+)", url or "", re.I)
    if not m:
        return None
    slug = m.group(1)
    if slug.lower() == "embed":
        m2 = re.search(r"/portal/embed/([^/?#]+)", url or "", re.I)
        return m2.group(1) if m2 else None
    return slug


def _load_route_cache() -> dict[str, Any]:
    try:
        from m3_data_root import data_path

        path = data_path(ROUTE_CACHE)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {"routes": {}, "blocked": {}}


def _save_route_cache(cache: dict[str, Any]) -> None:
    try:
        from m3_data_root import data_path

        path = data_path(ROUTE_CACHE)
        path.parent.mkdir(parents=True, exist_ok=True)
        cache = dict(cache)
        cache["updated_at"] = now_utc().isoformat()
        path.write_text(json.dumps(cache, indent=2, default=str), encoding="utf-8")
    except Exception:
        log.exception("Failed saving OpenGov route cache")


def _entity_cache_key(portal_url: str, entity_name: str | None = None) -> str:
    slug = _slug_from_portal(portal_url) or ""
    name = re.sub(r"[^a-z0-9]+", "_", (entity_name or "").lower())[:48]
    return slug or name or (portal_url or "")[:120]


def remember_working_route(
    *,
    portal_url: str,
    entity_name: str | None,
    route_url: str,
    route_kind: str,
    method: str | None,
) -> None:
    cache = _load_route_cache()
    routes = cache.setdefault("routes", {})
    key = _entity_cache_key(portal_url, entity_name)
    routes[key] = {
        "route_url": route_url,
        "route_kind": route_kind,
        "method": method,
        "portal_url": portal_url,
        "entity_name": entity_name,
    }
    # Clear from blocked if it works now
    blocked = cache.setdefault("blocked", {})
    blocked.pop(route_url, None)
    _save_route_cache(cache)


def remember_blocked_route(route_url: str, *, reason: str = "CLOUDFLARE") -> None:
    if not route_url:
        return
    cache = _load_route_cache()
    blocked = cache.setdefault("blocked", {})
    blocked[route_url] = {"reason": reason, "at": now_utc().isoformat()}
    _save_route_cache(cache)


def candidate_public_urls(portal_url: str, *, entity_name: str | None = None) -> list[str]:
    """Generate public candidates in cascade order; prefer cached working route."""
    url = (portal_url or "").strip()
    if not url:
        return []
    out: list[str] = []
    seen: set[str] = set()
    cache = _load_route_cache()
    blocked = set((cache.get("blocked") or {}).keys())

    def add(u: str) -> None:
        u = (u or "").strip()
        if not u or u in seen:
            return
        if u in blocked:
            return  # do not repeatedly hit known Cloudflare-blocked routes
        seen.add(u)
        out.append(u)

    # 1) Cached first working route
    key = _entity_cache_key(url, entity_name)
    cached = (cache.get("routes") or {}).get(key) or {}
    if cached.get("route_url"):
        add(str(cached["route_url"]))

    slug = _slug_from_portal(url)
    if slug and "opengov.com" in url.lower():
        base = "https://procurement.opengov.com"
        # PUBLIC_STRUCTURED — JSON/XHR first
        add(f"https://api.procurement.opengov.com/api/v1/portal/{slug}/projects")
        add(f"https://api.procurement.opengov.com/api/procurated/portal/{slug}/projects")
        # PUBLIC_PROJECT_LIST — embed / project-list
        add(f"{base}/portal/embed/{slug}/project-list")
        add(f"{base}/portal/{slug}/project-list")
        add(f"{base}/portal/{slug}/projects")
        # PUBLIC_PAGE_STATE
        add(f"{base}/portal/{slug}?status=open")
        add(f"{base}/portal/{slug}")
    # ORIGINAL_AGENCY_SOURCE
    add(url)
    if "opengov.com" not in url.lower():
        add(url.rstrip("/") + "/bids")
        add(url.rstrip("/") + "/solicitations")
    return out


def _http_get(client: httpx.Client, url: str) -> tuple[int, str, str]:
    r = client.get(url)
    return r.status_code, str(r.url), r.text or ""


def _is_cf(html: str) -> bool:
    low = (html or "").lower()
    return ("just a moment" in low and "cloudflare" in low) or "cf-browser-verification" in low


def _to_record(raw: dict[str, Any], *, source_id: str, agency: str | None, list_url: str) -> dict[str, Any]:
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
    meta["discovery_method"] = "PUBLIC_PROJECT_LIST"
    meta["discovery_scope"] = M3_DISCOVERY_SCOPE
    meta["vendor_profile_codes_ignored"] = True
    d["raw_metadata"] = meta
    return d


def harvest_portal_public(
    portal: dict[str, Any],
    *,
    client: httpx.Client | None = None,
    max_pages: int = 8,
) -> dict[str, Any]:
    """Harvest one OpenGov entity via public HTTP paths."""
    url = str(portal.get("portal_url") or "")
    agency = (
        f"{portal.get('entity_name')} ({portal.get('state')})"
        if portal.get("state")
        else portal.get("entity_name")
    )
    sid = f"opengov_{re.sub(r'[^a-z0-9]+', '_', str(portal.get('entity_name') or url).lower())[:48]}"
    result: dict[str, Any] = {
        "entity_name": portal.get("entity_name"),
        "entity_type": portal.get("entity_type"),
        "state": portal.get("state"),
        "portal_url": url,
        "public_project_list_url": None,
        "platform_id": _slug_from_portal(url),
        "discovery_method": None,
        "platform_family": "OPENGOV",
        "source_id": sid,
        "status": None,
        "rows": [],
        "reported_total": None,
        "retrieved_total": 0,
        "pages_scanned": 0,
        "pagination_complete": False,
        "error": None,
    }
    if not url:
        result["status"] = INVALID_PORTAL
        result["error"] = "no_url"
        return result

    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            timeout=35.0,
            follow_redirects=True,
            headers={"User-Agent": UA, "Accept": "text/html,application/json,*/*"},
        )
    assert client is not None
    all_rows: list[dict[str, Any]] = []
    pages = 0
    saw_anti_bot = False
    try:
        for cand in candidate_public_urls(url, entity_name=portal.get("entity_name")):
            try:
                status, final_url, body = _http_get(client, cand)
            except Exception as exc:
                result["error"] = type(exc).__name__
                continue
            if _is_cf(body) or status in {403, 503}:
                # ANTI_BOT is not terminal — try remaining cascade routes
                saw_anti_bot = True
                remember_blocked_route(cand, reason=f"cf_or_blocked:{status}")
                result["error"] = f"cf_or_blocked:{status}"
                continue
            if status == 404:
                continue
            if status >= 400:
                result["error"] = f"http_{status}"
                continue

            rows: list[dict[str, Any]] = []
            method = None
            route_kind = ROUTE_PUBLIC_PAGE_STATE
            # JSON body → PUBLIC_STRUCTURED
            text = (body or "").strip()
            if text.startswith("{") or text.startswith("["):
                try:
                    data = json.loads(text)
                    rows = parse_opengov_json_payload(data, list_url=final_url, agency=agency)
                    method = "public_json"
                    route_kind = ROUTE_PUBLIC_STRUCTURED
                except Exception:
                    rows = []
            if not rows:
                rows = parse_opengov_portal_html(body, list_url=final_url, agency=agency)
                if rows:
                    method = "public_html"
                    if "embed" in (final_url or "").lower() or "project-list" in (final_url or "").lower():
                        route_kind = ROUTE_PUBLIC_PROJECT_LIST
                    elif "opengov.com" not in (final_url or "").lower():
                        route_kind = ROUTE_AGENCY_SOURCE
                    else:
                        route_kind = ROUTE_PUBLIC_PAGE_STATE
            if not rows:
                # Try OpenGovLiveFetcher parser for agency alternates
                try:
                    from discovery.live_fetchers import OpenGovLiveFetcher

                    fetcher = OpenGovLiveFetcher()
                    opps = fetcher.parse_listing(body, list_url=final_url)
                    for o in opps:
                        rows.append(
                            {
                                "external_id": o.external_id,
                                "title": o.title,
                                "solicitation_number": o.solicitation_number,
                                "agency": o.agency or agency,
                                "detail_url": o.detail_url,
                                "source_url": final_url,
                                "deadline_raw": o.deadline_raw,
                                "status": o.status or "OPEN",
                                "raw_metadata": dict(o.raw_metadata or {}),
                            }
                        )
                    if rows:
                        method = "public_live_fetcher"
                        route_kind = ROUTE_AGENCY_SOURCE
                except Exception:
                    pass

            pages += 1
            if not rows:
                st = classify_portal_fetch(
                    url=cand,
                    final_url=final_url,
                    html=body,
                    n_opps=0,
                    parse_signals=bool(re.search(r"project|solicitation|bid", body or "", re.I)),
                    http_status=status,
                )
                if st == NO_OPEN_BIDS:
                    result["status"] = NO_OPEN_BIDS
                    result["public_project_list_url"] = final_url
                    result["discovery_method"] = "public_empty"
                    result["pages_scanned"] = pages
                    result["pagination_complete"] = True
                    return result
                if st in {INVALID_PORTAL, MOVED}:
                    result["status"] = st
                    continue
                # keep trying other candidates
                if st == BROKEN_PARSER:
                    result["status"] = BROKEN_PARSER
                continue

            # Success on this candidate
            for r in rows:
                all_rows.append(
                    _to_record(r, source_id=sid, agency=agency, list_url=final_url)
                )
            result["public_project_list_url"] = final_url
            result["discovery_method"] = method
            result["pages_scanned"] = pages

            # Lightweight path pagination for ?page= / pageNumber=
            for p in range(2, max_pages + 1):
                next_url = None
                if "page=" in final_url.lower():
                    next_url = re.sub(r"([?&]page=)\d+", rf"\g<1>{p}", final_url, flags=re.I)
                elif "?" in final_url:
                    next_url = f"{final_url}&page={p}"
                else:
                    next_url = f"{final_url}?page={p}"
                try:
                    st2, fu2, body2 = _http_get(client, next_url)
                except Exception:
                    break
                if st2 != 200 or _is_cf(body2):
                    break
                more = parse_opengov_portal_html(body2, list_url=fu2, agency=agency)
                if not more and (body2 or "").strip()[:1] in "{[":
                    try:
                        more = parse_opengov_json_payload(json.loads(body2), list_url=fu2, agency=agency)
                    except Exception:
                        more = []
                if not more:
                    break
                existing = {str(x.get("detail_url") or x.get("external_id")) for x in all_rows}
                added = 0
                for r in more:
                    rec = _to_record(r, source_id=sid, agency=agency, list_url=fu2)
                    key = str(rec.get("detail_url") or rec.get("external_id"))
                    if key in existing:
                        continue
                    all_rows.append(rec)
                    existing.add(key)
                    added += 1
                pages += 1
                result["pages_scanned"] = pages
                if added == 0:
                    break

            result["rows"] = all_rows
            result["retrieved_total"] = len(all_rows)
            result["pagination_complete"] = True
            result["status"] = WORKING_PUBLIC if all_rows else NO_OPEN_BIDS
            result["route_kind"] = route_kind
            remember_working_route(
                portal_url=url,
                entity_name=portal.get("entity_name"),
                route_url=final_url,
                route_kind=route_kind,
                method=method,
            )
            return result

        if result.get("status") is None:
            # Prefer SOURCE_ERROR over terminal ANTI_BOT when we never found a path;
            # ANTI_BOT alone must not stop trying auth/other routes upstream.
            if saw_anti_bot and not all_rows:
                result["status"] = ANTI_BOT
                result["anti_bot_non_terminal"] = True
            else:
                result["status"] = result.get("error") and SOURCE_ERROR or ANTI_BOT
        result["rows"] = all_rows
        result["retrieved_total"] = len(all_rows)
        result["pages_scanned"] = pages
        result["pagination_complete"] = bool(all_rows)
        return result
    finally:
        if owns_client:
            try:
                client.close()
            except Exception:
                pass


def _save_registry(entities: list[dict[str, Any]]) -> None:
    try:
        from m3_data_root import data_path

        path = data_path(ENTITY_REGISTRY)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"entities": entities, "updated_at": now_utc().isoformat()}, indent=2, default=str), encoding="utf-8")
    except Exception:
        log.exception("Failed saving OpenGov entity registry")


def run_opengov_public_discovery(
    *,
    max_entities: int | None = 50,
    max_pages: int = 6,
    persist: bool = True,
    run_id: str | None = None,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """Public-first harvest across known OpenGov entities → canonical merge."""
    from m3_canonical_discovery_bridge import merge_discovery_into_canonical

    run_id = run_id or f"OGP-{uuid4().hex[:12]}"
    started = now_utc().isoformat()
    portals = known_opengov_portals()
    limit = len(portals) if max_entities is None else max(0, int(max_entities))
    selected = portals[:limit]

    report: dict[str, Any] = {
        "kind": "OpenGovPublicDiscovery",
        "run_id": run_id,
        "started_at": started,
        "discovery_scope": M3_DISCOVERY_SCOPE,
        "known_entities": len(portals),
        "entities_attempted": 0,
        "working_public": 0,
        "working_auth": 0,
        "partial": 0,
        "anti_bot": 0,
        "no_open_bids": 0,
        "invalid_portal": 0,
        "portal_status_counts": {},
        "raw_opportunities": 0,
        "unique_records": 0,
        "per_entity": {},
        "canonical_merge": {},
        "auth": {"status": "PUBLIC_ONLY", "authenticated": False},
    }

    status_counts: Counter = Counter()
    all_records: list[dict[str, Any]] = []
    registry: list[dict[str, Any]] = []

    with httpx.Client(
        timeout=35.0,
        follow_redirects=True,
        headers={"User-Agent": UA, "Accept": "text/html,application/json,*/*"},
    ) as client:
        for i, portal in enumerate(selected):
            ent = harvest_portal_public(portal, client=client, max_pages=max_pages)
            report["entities_attempted"] += 1
            st = ent.get("status") or SOURCE_ERROR
            status_counts[st] += 1
            if st == WORKING_PUBLIC:
                report["working_public"] += 1
            elif st == PARTIAL:
                report["partial"] += 1
            elif st == ANTI_BOT:
                report["anti_bot"] += 1
            elif st == NO_OPEN_BIDS:
                report["no_open_bids"] += 1
            elif st == INVALID_PORTAL:
                report["invalid_portal"] += 1
            report["per_entity"][ent.get("source_id") or portal.get("portal_url")] = {
                "ok": st in SUCCESS_STATUSES,
                "status": st,
                "raw": ent.get("retrieved_total") or 0,
                "pages_scanned": ent.get("pages_scanned"),
                "public_project_list_url": ent.get("public_project_list_url"),
                "discovery_method": ent.get("discovery_method"),
                "entity_name": ent.get("entity_name"),
                "state": ent.get("state"),
                "portal_url": ent.get("portal_url"),
                "error": ent.get("error"),
            }
            registry.append(
                {
                    "entity": ent.get("entity_name"),
                    "portal_url": ent.get("portal_url"),
                    "public_project_list_url": ent.get("public_project_list_url"),
                    "platform_id": ent.get("platform_id"),
                    "discovery_method": ent.get("discovery_method"),
                    "last_success": now_utc().isoformat() if st in SUCCESS_STATUSES else None,
                    "live_count": ent.get("retrieved_total") or 0,
                    "status": st,
                }
            )
            for row in ent.get("rows") or []:
                all_records.append(row)
            if on_progress:
                try:
                    on_progress(
                        phase="PUBLIC_HARVEST",
                        pct=min(90, int(10 + 80 * (i + 1) / max(1, len(selected)))),
                        entities=i + 1,
                        retrieved=len(all_records),
                    )
                except Exception:
                    pass

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for r in all_records:
        key = (
            str(r.get("detail_url") or "").strip().lower()
            or f"{r.get('source_id')}|{r.get('external_id')}|{(r.get('title') or '')[:80]}".lower()
        )
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(r)

    report["portal_status_counts"] = dict(status_counts)
    report["raw_opportunities"] = len(all_records)
    report["unique_records"] = len(unique)
    report["entities_successful"] = report["working_public"] + report["partial"]
    # Callers with persist=False still need rows for canonical merge
    report["records"] = unique

    succeeded = [k for k, v in report["per_entity"].items() if v.get("ok")]
    failed = [k for k, v in report["per_entity"].items() if not v.get("ok")]
    merge = merge_discovery_into_canonical(
        run_id=run_id,
        trigger="opengov_public",
        records=unique,
        sources_attempted=list(report["per_entity"].keys()),
        sources_succeeded=succeeded,
        sources_failed=failed,
        source_counts=report["per_entity"],
        raw_opportunities_found=len(all_records),
        records_normalized=len(unique),
        api_usage={"SAM": 0, "opengov_public": True},
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

    if persist:
        _save_registry(registry)
        try:
            from m3_data_root import data_path

            path = data_path(REPORT)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        except Exception:
            log.exception("Failed saving OpenGov public discovery report")
        try:
            from opengov_auth.telemetry import record_discovery_counters

            record_discovery_counters(
                {
                    "opengov_known_entities": report["known_entities"],
                    "opengov_entities_attempted": report["entities_attempted"],
                    "opengov_entities_successful": report["entities_successful"],
                    "opengov_raw_discovered": report["raw_opportunities"],
                    "opengov_canonical_live": report["canonical_merge"].get("available_after") or 0,
                }
            )
        except Exception:
            pass
    return report
