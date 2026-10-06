"""Euna/Bonfire public-first discovery + optional authenticated enrichment."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any
from uuid import uuid4

import httpx

from application_clock import now_utc
from euna_auth.config import ACCOUNT_CATEGORY_RESTRICTION, M3_DISCOVERY_SCOPE, load_euna_auth_config
from euna_auth.states import AUTH_FAILED, DISABLED, SUCCESS_STATUSES, WORKING_AUTH, WORKING_PUBLIC
from euna_auth.telemetry import owner_connection_status, record_harvest_counters
from euna_discovery.portals import classify_bonfire_fetch, known_euna_portals

log = logging.getLogger("govtracker.euna_discovery.harvest")

REPORT = "euna_auth/last_discovery_report.json"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _to_record(raw: dict[str, Any], *, source_id: str, agency: str | None, list_url: str) -> dict[str, Any]:
    d = dict(raw)
    d["source_id"] = source_id
    d["platform"] = "live_bonfire"
    d["platform_family"] = "Bonfire"
    d["agency"] = d.get("agency") or agency
    d["status"] = d.get("status") or "OPEN"
    d["discovery_universe"] = "LIVE"
    d["discovery_scope"] = M3_DISCOVERY_SCOPE
    d["authoritative_url"] = d.get("detail_url") or list_url
    meta = dict(d.get("raw_metadata") or {})
    meta["discovery_scope"] = M3_DISCOVERY_SCOPE
    meta["platform_family"] = "EUNA_BONFIRE"
    meta["vendor_profile_codes_ignored"] = True
    d["raw_metadata"] = meta
    return d


def _normalize_portal_url(url: str) -> str:
    u = (url or "").strip()
    if "bonfirehub.com" in u.lower() and "tab=" not in u.lower():
        sep = "&" if "?" in u else "?"
        if "/portal" in u.lower():
            return f"{u}{sep}tab=openOpportunities"
        return u.rstrip("/") + "/portal/?tab=openOpportunities"
    return u


def harvest_bonfire_portal_public(
    portal: dict[str, Any],
    *,
    client: httpx.Client,
    max_pages: int = 6,
) -> dict[str, Any]:
    url = _normalize_portal_url(str(portal.get("portal_url") or ""))
    agency = (
        f"{portal.get('entity_name')} ({portal.get('state')})"
        if portal.get("state")
        else portal.get("entity_name")
    )
    sid = f"euna_{re.sub(r'[^a-z0-9]+', '_', str(portal.get('entity_name') or url).lower())[:48]}"
    result: dict[str, Any] = {
        "entity_name": portal.get("entity_name"),
        "state": portal.get("state"),
        "portal_url": url,
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
        result["status"] = "INVALID_PORTAL"
        return result

    all_rows: list[dict[str, Any]] = []
    pages = 0
    try:
        from discovery.live_fetchers import BonfireLiveFetcher

        fetcher = BonfireLiveFetcher()
        for page_num in range(1, max_pages + 1):
            page_url = url if page_num == 1 else (
                re.sub(r"([?&]page=)\d+", rf"\g<1>{page_num}", url, flags=re.I)
                if "page=" in url.lower()
                else f"{url}{'&' if '?' in url else '?'}page={page_num}"
            )
            try:
                resp = client.get(page_url)
            except Exception as exc:
                result["error"] = type(exc).__name__
                break
            pages += 1
            body = resp.text or ""
            st = classify_bonfire_fetch(html=body, n_opps=0, http_status=resp.status_code)
            if st == "ANTI_BOT":
                result["status"] = "ANTI_BOT"
                result["error"] = f"http_{resp.status_code}"
                break
            opps = fetcher.parse_listing(body, list_url=str(resp.url))
            if not opps:
                if page_num == 1:
                    result["status"] = st if st != "WORKING_PUBLIC" else "NO_OPEN_BIDS"
                break
            existing = {str(r.get("detail_url") or r.get("external_id")) for r in all_rows}
            added = 0
            for o in opps:
                rec = _to_record(
                    {
                        "external_id": o.external_id,
                        "title": o.title,
                        "solicitation_number": o.solicitation_number,
                        "agency": o.agency or agency,
                        "detail_url": o.detail_url,
                        "source_url": str(resp.url),
                        "deadline_raw": o.deadline_raw,
                        "status": o.status or "OPEN",
                        "description": getattr(o, "description", None),
                        "raw_metadata": dict(o.raw_metadata or {}),
                    },
                    source_id=sid,
                    agency=agency,
                    list_url=str(resp.url),
                )
                key = str(rec.get("detail_url") or rec.get("external_id"))
                if key in existing:
                    continue
                all_rows.append(rec)
                existing.add(key)
                added += 1
            if added == 0:
                break
        result["rows"] = all_rows
        result["retrieved_total"] = len(all_rows)
        result["pages_scanned"] = pages
        result["pagination_complete"] = True
        if all_rows:
            result["status"] = WORKING_PUBLIC
        elif not result.get("status"):
            result["status"] = "NO_OPEN_BIDS"
    except Exception as exc:
        result["status"] = "SOURCE_ERROR"
        result["error"] = type(exc).__name__
        log.exception("Bonfire portal harvest failed")
    return result


def run_euna_discovery(
    *,
    max_entities: int | None = None,
    max_pages: int = 6,
    persist: bool = True,
    run_id: str | None = None,
    use_auth: bool = True,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """Public Bonfire portal harvest + optional Euna auth session validation."""
    from m3_canonical_discovery_bridge import merge_discovery_into_canonical

    cfg = load_euna_auth_config()
    run_id = run_id or f"EU-{uuid4().hex[:12]}"
    started = now_utc().isoformat()
    portals = known_euna_portals()
    limit = int(max_entities if max_entities is not None else cfg.discovery_batch_size)
    selected = portals[: max(0, limit)]

    report: dict[str, Any] = {
        "kind": "EunaBonfireDiscovery",
        "run_id": run_id,
        "started_at": started,
        "discovery_scope": M3_DISCOVERY_SCOPE,
        "auth": None,
        "known_entities": len(portals),
        "entities_attempted": 0,
        "entities_successful": 0,
        "working_public": 0,
        "working_auth": 0,
        "portal_status_counts": {},
        "raw_opportunities": 0,
        "unique_records": 0,
        "reported_total": None,
        "pagination_complete": True,
        "per_entity": {},
        "canonical_merge": {},
        "account_category_restriction": False,
    }

    # Auth probe (does not drive public portal crawl)
    if use_auth and cfg.auth_enabled and cfg.credentials_present:
        try:
            from euna_auth.client import EunaAuthenticatedClient

            with EunaAuthenticatedClient() as client:
                auth = client.ensure_authenticated()
                report["auth"] = auth.to_dict()
                if auth.authenticated:
                    report["working_auth"] = 1
        except Exception as exc:
            report["auth"] = {"status": AUTH_FAILED, "authenticated": False, "message": type(exc).__name__}
    elif not cfg.auth_enabled:
        report["auth"] = {"status": DISABLED, "authenticated": False}
    else:
        report["auth"] = {
            "status": AUTH_FAILED,
            "authenticated": False,
            "message": "EUNA_USERNAME/EUNA_PASSWORD not configured",
        }

    status_counts: Counter = Counter()
    all_records: list[dict[str, Any]] = []
    with httpx.Client(timeout=35.0, follow_redirects=True, headers={"User-Agent": UA}) as client:
        for i, portal in enumerate(selected):
            ent = harvest_bonfire_portal_public(portal, client=client, max_pages=max_pages)
            report["entities_attempted"] += 1
            st = ent.get("status") or "SOURCE_ERROR"
            status_counts[st] += 1
            if st == WORKING_PUBLIC:
                report["working_public"] += 1
            report["per_entity"][ent.get("source_id") or portal.get("portal_url")] = {
                "ok": st in SUCCESS_STATUSES,
                "status": st,
                "raw": ent.get("retrieved_total") or 0,
                "pages_scanned": ent.get("pages_scanned"),
                "entity_name": ent.get("entity_name"),
                "state": ent.get("state"),
                "portal_url": ent.get("portal_url"),
                "error": ent.get("error"),
            }
            if ent.get("rows"):
                report["entities_successful"] += 1
                all_records.extend(ent["rows"])
            if not ent.get("pagination_complete"):
                report["pagination_complete"] = False
            if on_progress:
                try:
                    on_progress(
                        phase="EUNA_PUBLIC",
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
    report["reported_total"] = len(unique)

    succeeded = [k for k, v in report["per_entity"].items() if v.get("ok")]
    failed = [k for k, v in report["per_entity"].items() if not v.get("ok")]
    merge = merge_discovery_into_canonical(
        run_id=run_id,
        trigger="euna_bonfire_discovery",
        records=unique,
        sources_attempted=list(report["per_entity"].keys()),
        sources_succeeded=succeeded,
        sources_failed=failed,
        source_counts=report["per_entity"],
        raw_opportunities_found=len(all_records),
        records_normalized=len(unique),
        api_usage={"SAM": 0, "euna_bonfire": True},
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
        try:
            from m3_data_root import data_path

            path = data_path(REPORT)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        except Exception:
            log.exception("Failed saving Euna discovery report")
        record_harvest_counters(
            {
                "euna_reported_open": report.get("reported_total"),
                "euna_harvested": report.get("unique_records"),
                "euna_pagination_complete": report.get("pagination_complete"),
            }
        )
    return report
