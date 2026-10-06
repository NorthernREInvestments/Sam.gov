"""Expansion harvest — fetch high-gain free sources and merge ALL into canonical live universe.

Does NOT apply profit/product filtering. Downstream profit-first handles that.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from discovery_expansion.constants import DISCOVERY_TRUNCATED, FREE_ACCOUNT_REQUIRED

log = logging.getLogger("govtracker.discovery_expansion.harvest")

# Skip BidNet rows in catalog — covered by dedicated network harvest
_CATALOG_FAMILIES = (
    "OpenGov",
    "Bonfire",
    "PlanetBids",
    "IonWave",
    "Jaggaer",
    "PublicPurchase",
    "DemandStar",
)


def _opp_to_record(opp: Any, *, source_id: str, platform: str, agency: str | None = None) -> dict[str, Any]:
    if isinstance(opp, dict):
        d = dict(opp)
    elif hasattr(opp, "to_dict"):
        d = dict(opp.to_dict())
    else:
        d = {}
        for key in (
            "external_id",
            "title",
            "agency",
            "buyer",
            "detail_url",
            "source_url",
            "deadline",
            "response_deadline",
            "description",
            "status",
            "solicitation_number",
            "raw_metadata",
            "posted_at",
            "naics",
            "source_id",
            "document_links",
        ):
            if hasattr(opp, key):
                val = getattr(opp, key)
                if val is not None:
                    d[key] = val
    d["source_id"] = d.get("source_id") or source_id
    d["platform"] = platform
    d["agency"] = d.get("agency") or d.get("buyer") or agency
    d["status"] = d.get("status") or "OPEN"
    # Preserve BidNet/HTML closing dates into canonical deadline fields
    if not d.get("deadline") and not d.get("response_deadline"):
        raw = d.get("deadline_raw")
        if raw:
            try:
                from discovery.deadline import normalize_deadline

                nd = normalize_deadline(str(raw))
                d["deadline"] = nd.get("utc_deadline") or nd.get("parsed_local") or str(raw)
                d["response_deadline"] = d["deadline"]
            except Exception:
                d["deadline"] = str(raw)
                d["response_deadline"] = str(raw)
        elif d.get("response_deadline"):
            d["deadline"] = d["response_deadline"]
    d["discovery_universe"] = "LIVE"
    d["product_screen_survive"] = False
    d["inventory_freshness"] = "LIVE"
    d["live_status"] = "OPEN"
    d["jurisdiction"] = d.get("jurisdiction") or "LOCAL"
    meta = d.get("raw_metadata") if isinstance(d.get("raw_metadata"), dict) else {}
    docs = d.get("document_links") or meta.get("document_urls") or meta.get("attachments")
    if docs:
        d["attachments_metadata"] = docs
    return d


def _client() -> Any:
    from discovery.http_client import PublicProcurementHttpClient, RequestBudget

    budget = RequestBudget(
        max_total_requests=20000,
        max_requests_per_source=200,
        max_pages_per_source=120,
        max_records_per_source=10000,
        max_runtime_seconds=7200.0,
        max_retries=1,
        min_interval_seconds=0.35,
        timeout_seconds=30.0,
    )
    return PublicProcurementHttpClient(
        budget=budget,
        authorize_live=True,
        user_agent="M3DiscoveryExpansion/1.0 (public research)",
    )


def _source_id_for_portal(platform: str, name: str, url: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", f"{platform}_{name}".lower()).strip("_")[:80]
    if not slug:
        slug = re.sub(r"[^a-z0-9]+", "_", url.lower())[-60:]
    return f"exp_{slug}"


def _fetch_bidnet_networks(*, max_pages: int = 80, pagination_exhaust: bool = True) -> dict[str, Any]:
    from discovery.bidnet_network import BIDNET_STATE_NETWORKS, enrich_bidnet_network
    from discovery.live_fetchers import BidNetLiveFetcher

    client = _client()
    fetcher = BidNetLiveFetcher()
    records: list[dict[str, Any]] = []
    per_source: dict[str, Any] = {}
    truncated: list[str] = []

    for net in BIDNET_STATE_NETWORKS:
        row = enrich_bidnet_network(net)
        sid = row["source_id"]
        url = row["list_url"]
        try:
            result = fetcher.fetch_listing(
                client,
                list_url=url,
                source_id=sid,
                max_pages=max_pages,
                pagination_exhaust=pagination_exhaust,
                pagination_safety_max_pages=max(120, max_pages),
            )
            opps = result.get("opportunities") or []
            pages = int(result.get("pages_fetched") or 0)
            reported = result.get("source_reported_total")
            complete = bool(result.get("pagination_complete", True))
            stop = result.get("pagination_stop_reason")
            batch = [_opp_to_record(o, source_id=sid, platform="BidNet") for o in opps]
            records.extend(batch)
            ok = len(batch) > 0
            if reported is not None and len(batch) < int(reported):
                complete = False
                stop = DISCOVERY_TRUNCATED
                truncated.append(sid)
            if stop == "PAGINATION_INCOMPLETE":
                truncated.append(sid)
            per_source[sid] = {
                "ok": ok,
                "raw": len(batch),
                "unique": len(batch),
                "pages_scanned": pages,
                "source_reported_total": reported,
                "pagination_complete": complete,
                "source_stop_reason": stop or ("COMPLETED" if ok else "EMPTY"),
                "platform_family": "BidNet",
                "access_type": "PUBLIC_ANONYMOUS" if ok else FREE_ACCOUNT_REQUIRED,
            }
            log.info("BidNet %s raw=%s pages=%s complete=%s", sid, len(batch), pages, complete)
        except Exception as exc:
            log.exception("BidNet fetch failed %s", sid)
            per_source[sid] = {
                "ok": False,
                "raw": 0,
                "source_stop_reason": "SOURCE_EXCEPTION",
                "root_cause": type(exc).__name__,
                "platform_family": "BidNet",
            }
    return {"records": records, "per_source": per_source, "truncated": truncated}


def _fetch_structured_sources(*, max_pages: int = 5) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    per_source: dict[str, Any] = {}
    try:
        from discovery.live_fetchers import StructuredOpenDataLiveFetcher
        from discovery.structured_source_registry import all_structured_live_candidates

        client = _client()
        fetcher = StructuredOpenDataLiveFetcher()
        candidates = all_structured_live_candidates()
        for src in list(candidates or [])[:40]:
            if not isinstance(src, dict):
                continue
            sid = str(src.get("source_id") or src.get("id") or "structured")
            url = src.get("list_url") or src.get("url") or src.get("endpoint")
            if not url:
                continue
            try:
                result = fetcher.fetch_listing(
                    client,
                    list_url=url,
                    source_id=sid,
                    max_pages=max_pages,
                    pagination_exhaust=True,
                    pagination_safety_max_pages=20,
                )
                opps = result.get("opportunities") or []
                pages = int(result.get("pages_fetched") or 1)
                reported = result.get("source_reported_total")
                complete = bool(result.get("pagination_complete", True))
                batch = [_opp_to_record(o, source_id=sid, platform="Socrata") for o in opps]
                records.extend(batch)
                per_source[sid] = {
                    "ok": len(batch) > 0,
                    "raw": len(batch),
                    "pages_scanned": pages,
                    "source_reported_total": reported,
                    "pagination_complete": complete,
                    "platform_family": "Socrata",
                    "source_stop_reason": "COMPLETED" if batch else "EMPTY",
                }
            except Exception as exc:
                per_source[sid] = {
                    "ok": False,
                    "raw": 0,
                    "source_stop_reason": "SOURCE_EXCEPTION",
                    "root_cause": type(exc).__name__,
                    "platform_family": "Socrata",
                }
    except Exception:
        log.exception("Structured harvest skipped")
    return {"records": records, "per_source": per_source, "truncated": []}


def _fetch_platform_catalog(
    *,
    families: tuple[str, ...] | None = None,
    max_entities_per_family: int = 40,
    max_pages: int = 5,
) -> dict[str, Any]:
    """Cross-agency harvest via shared platform-family adapters + buyer catalog."""
    from discovery.live_fetchers import get_fetcher_for_platform
    from discovery.platform_buyer_catalog import platform_catalog_by_family

    client = _client()
    records: list[dict[str, Any]] = []
    per_source: dict[str, Any] = {}
    truncated: list[str] = []
    by_family = platform_catalog_by_family()
    target_families = families or _CATALOG_FAMILIES

    for fam in target_families:
        rows = by_family.get(fam) or []
        fetcher = get_fetcher_for_platform(fam)
        if fetcher is None:
            for row in rows[:3]:
                sid = _source_id_for_portal(fam, row["name"], row["portal_url"])
                per_source[sid] = {
                    "ok": False,
                    "raw": 0,
                    "platform_family": fam,
                    "source_stop_reason": "NOT_IMPLEMENTED",
                    "auth_required": bool(row.get("auth_required")),
                    "access_type": FREE_ACCOUNT_REQUIRED if row.get("auth_required") else "PUBLIC_ANONYMOUS",
                    "entity_name": row.get("name"),
                    "portal_url": row.get("portal_url"),
                }
            continue

        for row in rows[:max_entities_per_family]:
            url = str(row.get("portal_url") or "")
            if not url:
                continue
            # Prefer OpenGov CDN portals; skip non-opengov alternate agency pages
            # that are mis-tagged unless hostname matches family
            if fam == "OpenGov" and "opengov.com" not in url.lower() and "phoenix.gov" not in url.lower():
                # Still try agency alternate listings through OpenGov fetcher
                pass
            sid = _source_id_for_portal(fam, str(row.get("name") or ""), url)
            agency = f"{row.get('name')} ({row.get('state')})"
            try:
                result = fetcher.fetch_listing(
                    client,
                    list_url=url,
                    source_id=sid,
                    max_pages=max_pages,
                    pagination_exhaust=True,
                    pagination_safety_max_pages=max(20, max_pages),
                )
                opps = result.get("opportunities") or []
                pages = int(result.get("pages_fetched") or 0)
                reported = result.get("source_reported_total")
                complete = bool(result.get("pagination_complete", True))
                stop = result.get("pagination_stop_reason")
                batch = [
                    _opp_to_record(o, source_id=sid, platform=fam, agency=agency) for o in opps
                ]
                records.extend(batch)
                ok = len(batch) > 0
                auth_req = bool(row.get("auth_required"))
                if not ok and auth_req:
                    stop = stop or "AUTH_REQUIRED"
                if reported is not None and len(batch) < int(reported):
                    complete = False
                    stop = DISCOVERY_TRUNCATED
                    truncated.append(sid)
                per_source[sid] = {
                    "ok": ok,
                    "raw": len(batch),
                    "unique": len(batch),
                    "pages_scanned": pages,
                    "source_reported_total": reported,
                    "pagination_complete": complete,
                    "source_stop_reason": stop
                    or ("COMPLETED" if ok else ("AUTH_REQUIRED" if auth_req else "EMPTY")),
                    "platform_family": fam,
                    "auth_required": auth_req,
                    "access_type": (
                        FREE_ACCOUNT_REQUIRED
                        if (auth_req and not ok) or (stop and "AUTH" in str(stop).upper())
                        else "PUBLIC_ANONYMOUS"
                    ),
                    "entity_name": row.get("name"),
                    "entity_type": row.get("buyer_type"),
                    "state": row.get("state"),
                    "portal_url": url,
                }
                log.info("%s %s raw=%s pages=%s", fam, sid, len(batch), pages)
            except Exception as exc:
                log.warning("Catalog fetch failed %s %s: %s", fam, sid, type(exc).__name__)
                per_source[sid] = {
                    "ok": False,
                    "raw": 0,
                    "source_stop_reason": "SOURCE_EXCEPTION",
                    "root_cause": type(exc).__name__,
                    "platform_family": fam,
                    "auth_required": bool(row.get("auth_required")),
                    "access_type": FREE_ACCOUNT_REQUIRED if row.get("auth_required") else "PUBLIC_ANONYMOUS",
                    "entity_name": row.get("name"),
                    "portal_url": url,
                }

    return {"records": records, "per_source": per_source, "truncated": truncated}


def run_expansion_harvest(
    *,
    include_bidnet: bool = True,
    include_structured: bool = True,
    include_platform_catalog: bool = True,
    max_pages: int = 80,
    max_catalog_entities_per_family: int = 40,
    persist: bool = True,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Fetch free high-gain sources and merge every record into L23 canonical store."""
    from m3_canonical_discovery_bridge import merge_discovery_into_canonical

    started = now_utc().isoformat()
    run_id = run_id or f"EXP-{uuid4().hex[:12]}"
    all_records: list[dict[str, Any]] = []
    per_source: dict[str, Any] = {}
    truncated: list[str] = []
    platform_counts: dict[str, int] = {}

    if include_bidnet:
        bn = _fetch_bidnet_networks(max_pages=max_pages, pagination_exhaust=True)
        all_records.extend(bn["records"])
        per_source.update(bn["per_source"])
        truncated.extend(bn.get("truncated") or [])
        platform_counts["BidNet"] = len(bn["records"])

    if include_platform_catalog:
        cat = _fetch_platform_catalog(
            max_entities_per_family=max_catalog_entities_per_family,
            max_pages=min(8, max_pages),
        )
        all_records.extend(cat["records"])
        per_source.update(cat["per_source"])
        truncated.extend(cat.get("truncated") or [])
        for r in cat["records"]:
            plat = str(r.get("platform") or "Other")
            platform_counts[plat] = platform_counts.get(plat, 0) + 1

    if include_structured:
        st = _fetch_structured_sources()
        all_records.extend(st["records"])
        per_source.update(st["per_source"])
        platform_counts["Socrata"] = platform_counts.get("Socrata", 0) + len(st["records"])

    # Deduplicate within harvest by detail_url / external_id+title
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

    succeeded = [sid for sid, row in per_source.items() if row.get("ok")]
    failed = [sid for sid, row in per_source.items() if not row.get("ok")]

    merge = merge_discovery_into_canonical(
        run_id=run_id,
        trigger="manual",
        records=unique,
        sources_attempted=list(per_source.keys()),
        sources_succeeded=succeeded,
        sources_failed=failed,
        source_counts=per_source,
        raw_opportunities_found=len(all_records),
        records_normalized=len(unique),
        error_summary=(
            f"{DISCOVERY_TRUNCATED}:{','.join(truncated[:10])}" if truncated else None
        ),
        api_usage={"SAM": 0, "expansion_harvest": True},
        started_at=started,
        persist=persist,
    )

    report = {
        "kind": "DiscoveryExpansionHarvest",
        "run_id": run_id,
        "started_at": started,
        "completed_at": now_utc().isoformat(),
        "sources_attempted": len(per_source),
        "sources_successful": len(succeeded),
        "sources_failed": len(failed),
        "entities_attempted": len(per_source),
        "entities_successful": len(succeeded),
        "raw_opportunities": len(all_records),
        "raw_discovered": len(all_records),
        "duplicates_removed": dupes,
        "unique_records": len(unique),
        "canonical_live_opportunities": merge.get("currently_available_after"),
        "platform_family_counts": platform_counts,
        "pagination_truncated_sources": truncated,
        "canonical_merge": {
            "new": merge.get("new_canonical_opportunities_added"),
            "updated": merge.get("existing_opportunities_updated"),
            "duplicates_detected": merge.get("duplicates_detected"),
            "canonical_after": merge.get("canonical_total_after"),
            "available_after": merge.get("currently_available_after"),
            "run_status": merge.get("run_status"),
        },
        "TOTAL_LIVE_DISCOVERY_UNIVERSE": merge.get("currently_available_after"),
        "per_source": per_source,
    }
    try:
        from m3_data_root import data_path
        import json

        path = data_path("m3_discovery_expansion_last_harvest.json")
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        # Mirror summary into discovery run state for coverage matrix readers
        try:
            from m3_discovery_service import _load_state, _save_state

            state = _load_state()
            state["last_expansion_harvest"] = {
                "run_id": run_id,
                "completed_at": report["completed_at"],
                "per_source_summary": {
                    k: {
                        "ok": v.get("ok"),
                        "raw": v.get("raw"),
                        "unique": v.get("unique") or v.get("raw"),
                        "pages_scanned": v.get("pages_scanned"),
                        "source_reported_total": v.get("source_reported_total"),
                        "pagination_complete": v.get("pagination_complete"),
                        "source_stop_reason": v.get("source_stop_reason"),
                        "root_cause": v.get("root_cause"),
                        "platform_family": v.get("platform_family"),
                    }
                    for k, v in per_source.items()
                },
                "TOTAL_LIVE_DISCOVERY_UNIVERSE": report["TOTAL_LIVE_DISCOVERY_UNIVERSE"],
                "platform_family_counts": platform_counts,
            }
            _save_state(state)
        except Exception:
            log.exception("Could not mirror expansion into discovery run state")
    except Exception:
        log.exception("Failed writing expansion harvest report")

    return report
