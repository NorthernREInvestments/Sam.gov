"""Orchestrate research runs against the source registry — one shared engine."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from m3.audit import record_audit
from m3.config import get_settings
from m3.models import ResearchCache, ResearchRun, SourceRegistry
from m3.research.adapters import (
    discover_socrata_catalog,
    probe_supplier_page,
    search_html_links,
    search_socrata,
)
from m3.research.classify import PRODUCT_CLASSES, classify_batch
from m3.research.filters import apply_contract_filters
from m3.research.schema_map import validate_socrata_source
from m3.research.seeds import SEED_SOURCES
from m3.research.recovery import recover_uncertain_population
from m3.research.vocabulary import parse_search_terms


def _register_validated_socrata(
    db: Session,
    *,
    source_key: str,
    name: str,
    dataset_url: str,
    official_listing_url: str | None = None,
    geo_state: str | None = None,
    discovery_meta: dict | None = None,
) -> dict[str, Any]:
    """Validate schema before enabling a discovered Socrata dataset as operational."""
    existing = db.query(SourceRegistry).filter_by(source_key=source_key).one_or_none()
    validation = validate_socrata_source(dataset_url, name=name)
    meta = discovery_meta or {}
    if not validation.get("operational"):
        cfg = {
            "dataset_url": dataset_url,
            "official_listing_url": official_listing_url,
            "discovered": True,
            "schema_validated": True,
            "operational": False,
            "quarantine_reason": (validation.get("decision") or {}).get("reason")
            or validation.get("reason")
            or "failed_schema_validation",
            "dataset_kind": (validation.get("decision") or {}).get("kind"),
            "inferred": validation.get("inferred") or {},
            **meta,
        }
        if existing:
            existing.config = {**(existing.config or {}), **cfg}
            existing.is_enabled = False
            existing.information_available = f"Quarantined: {cfg['quarantine_reason']}"
            db.add(existing)
        else:
            db.add(
                SourceRegistry(
                    source_key=source_key,
                    name=name[:255],
                    base_url=dataset_url,
                    source_kind="socrata",
                    information_available=f"Quarantined: {cfg['quarantine_reason']}",
                    adapter="socrata_contracts",
                    config=cfg,
                    access_cost="free",
                    documented_ok=True,
                    experimentally_verified=False,
                    is_enabled=False,
                )
            )
        return {"registered": True, "operational": False, "validation": validation}

    inferred = validation.get("inferred") or {}
    cfg = {
        "dataset_url": dataset_url,
        "official_listing_url": official_listing_url or dataset_url,
        "discovered": True,
        "schema_validated": True,
        "operational": True,
        "geo_state": geo_state,
        "is_forecast": bool((validation.get("decision") or {}).get("is_forecast")),
        **{k: v for k, v in inferred.items() if isinstance(v, str)},
        **meta,
    }
    if existing:
        existing.config = {**(existing.config or {}), **cfg}
        existing.is_enabled = True
        existing.name = name[:255]
        existing.information_available = (
            f"Validated solicitation dataset ({(validation.get('decision') or {}).get('kind')})"
        )
        db.add(existing)
    else:
        db.add(
            SourceRegistry(
                source_key=source_key,
                name=name[:255],
                base_url=dataset_url,
                source_kind="socrata",
                information_available=(
                    f"Validated solicitation dataset ({(validation.get('decision') or {}).get('kind')})"
                ),
                adapter="socrata_contracts",
                config=cfg,
                access_cost="free",
                documented_ok=True,
                experimentally_verified=False,
                is_enabled=True,
            )
        )
    return {"registered": True, "operational": True, "validation": validation}


def repair_discovered_sources(db: Session, *, limit: int = 80) -> dict[str, Any]:
    """Re-validate previously auto-enabled discovered Socrata sources (mapping repair)."""
    rows = (
        db.query(SourceRegistry)
        .filter(SourceRegistry.adapter == "socrata_contracts")
        .all()
    )
    examined = 0
    repaired = 0
    quarantined = 0
    skipped_ok = 0
    for row in rows:
        cfg = row.config or {}
        if not cfg.get("discovered"):
            continue
        # Re-check previously "operational" discovered sources once under Build 002A column rules
        needs_recheck = cfg.get("repair_pass") != "build_002a_colschema"
        if cfg.get("schema_validated") and cfg.get("operational") is False and not needs_recheck:
            skipped_ok += 1
            continue
        if (
            cfg.get("schema_validated")
            and cfg.get("title_field")
            and cfg.get("operational") is not False
            and not needs_recheck
        ):
            skipped_ok += 1
            continue
        if examined >= limit:
            break
        examined += 1
        url = cfg.get("dataset_url") or row.base_url
        result = _register_validated_socrata(
            db,
            source_key=row.source_key,
            name=row.name,
            dataset_url=url,
            official_listing_url=cfg.get("official_listing_url"),
            geo_state=cfg.get("geo_state"),
            discovery_meta={"repair_pass": "build_002a_colschema"},
        )
        if result.get("operational"):
            repaired += 1
        else:
            quarantined += 1
    db.commit()
    return {
        "examined": examined,
        "repaired_operational": repaired,
        "quarantined": quarantined,
        "skipped_already_validated": skipped_ok,
    }


def ensure_source_registry(db: Session) -> int:
    """Insert new seeds and refresh adapter/config for existing seed keys."""
    created = 0
    for seed in SEED_SOURCES:
        existing = db.query(SourceRegistry).filter_by(source_key=seed["source_key"]).one_or_none()
        if existing:
            existing.name = seed["name"]
            existing.base_url = seed["base_url"]
            existing.source_kind = seed.get("source_kind") or existing.source_kind
            existing.information_available = seed.get("information_available") or existing.information_available
            existing.search_capabilities = seed.get("search_capabilities") or existing.search_capabilities
            existing.adapter = seed.get("adapter") or existing.adapter
            existing.config = seed.get("config") or {}
            existing.access_cost = "free"
            existing.is_enabled = True
            db.add(existing)
            continue
        db.add(
            SourceRegistry(
                source_key=seed["source_key"],
                name=seed["name"],
                base_url=seed["base_url"],
                source_kind=seed.get("source_kind") or "other",
                information_available=seed.get("information_available") or "",
                search_capabilities=seed.get("search_capabilities") or {},
                adapter=seed.get("adapter") or "http_listing",
                config=seed.get("config") or {},
                access_cost="free",
                documented_ok=True,
                experimentally_verified=False,
            )
        )
        created += 1
    seed_keys = {s["source_key"] for s in SEED_SOURCES}
    obsolete = {
        "nyc_checkbook_solicitations",
        "maryland_procure",
        "colorado_vss_public",
        "supplier_grainger_gov",
        "supplier_works",
    }
    for row in db.query(SourceRegistry).all():
        if row.source_key in seed_keys:
            continue
        cfg = row.config or {}
        if cfg.get("discovered"):
            continue
        if row.source_key in obsolete and row.is_enabled:
            row.is_enabled = False
            db.add(row)

    db.commit()
    return created


def _mark_source(
    db: Session,
    source_key: str,
    *,
    ok: bool,
    error: str | None = None,
    yield_info: dict | None = None,
) -> None:
    row = db.query(SourceRegistry).filter_by(source_key=source_key).one_or_none()
    if not row:
        return
    now = datetime.now(timezone.utc)
    cfg = dict(row.config or {})
    if ok:
        row.last_success_at = now
        row.experimentally_verified = True
        if yield_info:
            cfg["last_yield"] = {
                "at": now.isoformat(),
                "accepted": yield_info.get("accepted"),
                "rows_examined": yield_info.get("rows_examined"),
                "pages": yield_info.get("pages"),
                "pagination_complete": yield_info.get("pagination_complete"),
                "more_available": yield_info.get("more_available"),
                "next_offset": yield_info.get("next_offset"),
            }
            hist = list(cfg.get("yield_history") or [])
            hist.append(cfg["last_yield"])
            cfg["yield_history"] = hist[-30:]
            cfg["operational"] = True
            cfg["last_verification_date"] = now.isoformat()
            if yield_info.get("detail_capability"):
                cfg["detail_page_retrieval"] = yield_info["detail_capability"]
            if yield_info.get("attachment_capability"):
                cfg["attachment_retrieval"] = yield_info["attachment_capability"]
            row.document_access = yield_info.get("document_access") or row.document_access
            row.config = cfg
    else:
        row.last_failure_at = now
        hist = list(row.failure_history or [])
        hist.append({"at": now.isoformat(), "error": (error or "")[:240]})
        row.failure_history = hist[-20:]
        cfg["last_failure"] = {"at": now.isoformat(), "error": (error or "")[:240]}
        row.config = cfg
    db.add(row)


def _cache_get(db: Session, key: str) -> dict | None:
    row = db.query(ResearchCache).filter_by(cache_key=key).one_or_none()
    if not row:
        return None
    if row.expires_at and row.expires_at < datetime.now(timezone.utc):
        return None
    row.hit_count = int(row.hit_count or 0) + 1
    db.add(row)
    db.commit()
    return row.payload


def _sanitize_for_json(obj: Any) -> Any:
    """Strip NUL / control junk so Postgres JSONB accepts research snapshots."""
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return obj.replace("\x00", "").replace("\u0000", "")
    if isinstance(obj, dict):
        return {str(k): _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    return obj


def _cache_put(db: Session, key: str, kind: str, payload: dict, *, hours: int = 24) -> None:
    payload = _sanitize_for_json(payload) if isinstance(payload, dict) else payload
    try:
        row = db.query(ResearchCache).filter_by(cache_key=key).one_or_none()
        exp = datetime.now(timezone.utc) + timedelta(hours=hours)
        if row:
            row.payload = payload
            row.kind = kind
            row.expires_at = exp
            db.add(row)
        else:
            db.add(ResearchCache(cache_key=key, kind=kind, payload=payload, expires_at=exp))
        db.commit()
    except Exception:
        db.rollback()
        # Do not fail the research run because a cache write failed
        return


def _progress(run: ResearchRun, **fields: Any) -> None:
    p = dict(run.progress or {})
    p.update(fields)
    p["updated_at"] = datetime.now(timezone.utc).isoformat()
    run.progress = _sanitize_for_json(p)


def _operator_keywords(filters: dict[str, Any]) -> list[str]:
    """Keywords the operator explicitly requested — used ONLY by the filter layer."""
    raw = (filters.get("keyword") or "").strip()
    if not raw:
        return []
    parsed = parse_search_terms(
        raw,
        mode=(filters.get("search_mode") or "broad"),
        exclude=filters.get("exclude_keywords") or "",
    )
    return list(parsed["include_terms"])


def _openai_portal_focus(_filters: dict[str, Any] | None = None) -> list[str]:
    """Entity-type focus for OpenAI *portal* discovery — never product categories."""
    return [
        "state procurement portal",
        "county purchasing",
        "city purchasing department",
        "school district procurement",
        "university purchasing",
        "public utility bids",
        "transit authority procurement",
        "housing authority bids",
        "airport authority purchasing",
        "cooperative purchasing",
    ]


def run_contract_discovery(
    db: Session,
    *,
    user_id: uuid.UUID | None,
    filters: dict[str, Any] | None = None,
    limit: int | None = None,
    offset: int = 0,
    continue_run_id: uuid.UUID | None = None,
    enable_openai_sources: bool | None = None,
) -> dict[str, Any]:
    """Nationwide free-source discovery with pagination / continuation.

    `limit` is a display/slice size only — never the entire opportunity population.
    Pass limit=None to retrieve all pages within per-source max_pages.
    Keyword matching is applied once via ``apply_contract_filters`` — not at adapters.
    """
    settings = get_settings()
    ensure_source_registry(db)
    # Repair previously auto-enabled discovered datasets with bad/missing title maps
    repair_summary = repair_discovered_sources(db, limit=60)
    filters = filters or {}
    operator_keywords = _operator_keywords(filters)
    openai_portal_focus = _openai_portal_focus(filters)
    # Default ON for production research workflow (budget still gates actual spend)
    if enable_openai_sources is None:
        enable_openai_sources = True
    display_page = limit if limit is not None else settings.research_display_page_size

    if continue_run_id:
        run = db.get(ResearchRun, continue_run_id)
        if not run or run.objective != "open_contracts":
            continue_run_id = None
            run = None
    else:
        run = None

    prior_items: list[dict] = []
    if run:
        prior_items = list(run.result_snapshot or [])
        run.status = "running"
        run.cancel_requested = False
        db.add(run)
        db.commit()
        db.refresh(run)
    else:
        run = ResearchRun(
            objective="open_contracts",
            requested_by=user_id,
            params={
                "filters": filters,
                "keywords": operator_keywords,
                "openai_portal_focus": openai_portal_focus,
                "offset": offset,
                "source_repair": repair_summary,
                "pipeline": [
                    "unrestricted_discovery",
                    "normalize_dedupe",
                    "central_classification",
                    "evidence_verification",
                    "user_filters",
                ],
            },
            status="running",
            progress={"phase": "starting", "sources_evaluated": 0, "source_repair": repair_summary},
        )
        db.add(run)
        db.commit()
        db.refresh(run)

    sources = (
        db.query(SourceRegistry)
        .filter(SourceRegistry.is_enabled.is_(True), SourceRegistry.access_cost == "free")
        .filter(
            SourceRegistry.adapter.in_(
                ["socrata_contracts", "html_link_harvest", "socrata_catalog_discover"]
            )
        )
        .all()
    )
    sources = sorted(
        sources,
        key=lambda s: (
            0 if (s.config or {}).get("geo_state") == "NE" else 1,
            0 if s.adapter == "socrata_contracts" else 1,
            s.name,
        ),
    )

    all_items: list[dict] = list(prior_items)
    tried: list[str] = list(run.sources_tried or [])
    succeeded: list[str] = list(run.sources_succeeded or [])
    limitations: list[str] = list(run.limitations or [])
    discovered_sources: list[dict] = []
    pages_total = 0
    rows_examined = 0
    mapping_failures_total = 0
    sources_unavailable: list[str] = []
    sources_fully_paginated: list[str] = []
    sources_incomplete: list[str] = []
    source_yield: list[dict] = []
    more_available = False
    paid_cost = Decimal("0")
    openai_used = False
    openai_cache_hit = False
    new_operational_sources = 0
    quarantined_sources = 0

    _progress(
        run,
        phase="searching_sources",
        sources_total=len(sources),
        sources_evaluated=0,
        notices_examined=0,
        unique_so_far=len(all_items),
        incomplete=True,
        source_repair=repair_summary,
    )
    db.add(run)
    db.commit()

    for idx, src in enumerate(sources):
        db.refresh(run)
        if run.cancel_requested:
            run.status = "cancelled"
            break
        if src.source_key not in tried:
            tried.append(src.source_key)
        payload = {"source_key": src.source_key, "base_url": src.base_url, "name": src.name, "config": src.config}
        try:
            if src.adapter == "socrata_catalog_discover":
                result = discover_socrata_catalog(payload)
                if result.get("ok") and result.get("items"):
                    if src.source_key not in succeeded:
                        succeeded.append(src.source_key)
                    _mark_source(db, src.source_key, ok=True)
                    discovered_sources.extend(result["items"])
                    for disc in result["items"][:40]:
                        dkey = f"discovered_{disc['domain'].replace('.', '_')}_{disc['dataset_id']}"
                        reg = _register_validated_socrata(
                            db,
                            source_key=dkey,
                            name=f"{disc['name']} ({disc['domain']})",
                            dataset_url=disc["dataset_url"],
                            official_listing_url=disc.get("link"),
                            discovery_meta={
                                "discovered_via": disc.get("discovered_via"),
                                "discovery_method": "socrata_catalog",
                            },
                        )
                        if reg.get("operational"):
                            new_operational_sources += 1
                        else:
                            quarantined_sources += 1
                    db.commit()
                else:
                    err = ";".join(result.get("errors") or ["no_items"])
                    _mark_source(db, src.source_key, ok=False, error=err)
                    limitations.append(f"{src.source_key}: {err}")
                    sources_unavailable.append(src.source_key)
                _progress(
                    run,
                    sources_evaluated=idx + 1,
                    unique_so_far=len({i.get("external_key") for i in all_items}),
                    new_operational_sources=new_operational_sources,
                    quarantined_sources=quarantined_sources,
                )
                db.add(run)
                db.commit()
                continue

            if src.adapter == "socrata_contracts":
                # Full pagination — no adapter keyword gate; filter layer is authoritative
                result = search_socrata(
                    payload,
                    keywords=None,
                    limit=None,
                    offset=0,
                    page_size=settings.research_socrata_page_size,
                    max_pages=settings.research_max_pages_per_source,
                    apply_adapter_keywords=False,
                )
            else:
                result = search_html_links(
                    payload,
                    keywords=None,
                    limit=500,
                    apply_adapter_keywords=False,
                )
            pages_total += int(result.get("pages_fetched") or 1)
            rows_examined += int(result.get("rows_examined") or len(result.get("items") or []))
            stats = result.get("stats") or {}
            mapping_failures_total += int(stats.get("mapping_failures") or 0)
            pag_complete = bool(result.get("pagination_complete"))
            if result.get("more_available"):
                more_available = True
                if src.source_key not in sources_incomplete:
                    sources_incomplete.append(src.source_key)
            elif pag_complete or result.get("ok"):
                if src.source_key not in sources_fully_paginated:
                    sources_fully_paginated.append(src.source_key)
            source_yield.append(
                {
                    "source_key": src.source_key,
                    "pages": result.get("pages_fetched"),
                    "rows_examined": result.get("rows_examined"),
                    "accepted": len(result.get("items") or []),
                    "pagination_complete": pag_complete,
                    "more_available": bool(result.get("more_available")),
                    "stats": stats,
                }
            )
            if result.get("ok") and result.get("items"):
                if src.source_key not in succeeded:
                    succeeded.append(src.source_key)
                _mark_source(
                    db,
                    src.source_key,
                    ok=True,
                    yield_info={
                        "accepted": len(result.get("items") or []),
                        "rows_examined": result.get("rows_examined"),
                        "pages": result.get("pages_fetched"),
                        "pagination_complete": result.get("pagination_complete"),
                        "more_available": result.get("more_available"),
                        "next_offset": result.get("next_offset"),
                        "document_access": "url_field" if src.adapter == "socrata_contracts" else "link_harvest",
                        "detail_capability": "official_url",
                        "attachment_capability": "detail_page_scan",
                    },
                )
                all_items.extend(result["items"])
            else:
                err = ";".join(result.get("errors") or ["no_items"])
                _mark_source(db, src.source_key, ok=False, error=err)
                limitations.append(f"{src.source_key}: {err}")
                sources_unavailable.append(src.source_key)
        except Exception as exc:
            _mark_source(db, src.source_key, ok=False, error=f"{type(exc).__name__}:{exc}")
            limitations.append(f"{src.source_key}: {type(exc).__name__}")
            sources_unavailable.append(src.source_key)

        _progress(
            run,
            sources_evaluated=idx + 1,
            sources_succeeded=len(succeeded),
            sources_unavailable=len(sources_unavailable),
            notices_examined=rows_examined,
            mapping_failures=mapping_failures_total,
            unique_so_far=len({i.get("external_key") for i in all_items if i.get("external_key")}),
            pages_fetched=pages_total,
            sources_fully_paginated=len(sources_fully_paginated),
            sources_incomplete=len(sources_incomplete),
            incomplete=True,
        )
        # Persist incremental snapshot so UI can poll
        seen_tmp: set[str] = set()
        uniq_tmp: list[dict] = []
        for it in all_items:
            k = it.get("external_key") or it.get("official_url") or it.get("title")
            if not k or k in seen_tmp:
                continue
            seen_tmp.add(str(k))
            uniq_tmp.append(it)
        # Mid-run: persist raw discovery population (Stage 1). Classification/filters are Stage 3–5.
        run.sources_tried = tried
        run.sources_succeeded = succeeded
        run.limitations = limitations[-80:]
        run.result_snapshot = _sanitize_for_json(uniq_tmp[:5000])
        run.result_count = len(uniq_tmp)
        db.add(run)
        db.commit()

    # OpenAI source discovery — available in production workflow; budget-gated
    if enable_openai_sources and not run.cancel_requested:
        try:
            from m3.research.openai_client import web_research

            cache_key = "openai_portal_discover_v2:" + hashlib.sha256(
                ",".join(openai_portal_focus[:4]).encode()
            ).hexdigest()[:16]
            cached = _cache_get(db, cache_key)
            if cached:
                discovered_sources.extend(cached.get("sources") or [])
                openai_cache_hit = True
                _progress(run, openai_cache_hit=True)
            else:
                schema = (
                    '{"sources":[{"entity":"","state":"","government_level":"","procurement_url":"",'
                    '"active_bids_url":"","award_archive_url":"","retrieval_method":"","notes":""}]}'
                )
                prompt = (
                    "Find free official U.S. government procurement portals that publish active "
                    "solicitation / bid listings (any goods or services — do not filter by product type). "
                    "Prioritize Nebraska coverage gaps, then nationwide. Include state, county, city, "
                    "school district, university/community college, public hospital, utility, water/"
                    "wastewater, transit, airport, housing authority, port, and special district portals. "
                    f"Entity focus: {', '.join(openai_portal_focus[:8])}. "
                    "Return only portals with publicly accessible bid listings (no paid login). "
                    "Do not invent URLs — only cite real pages found via web search. "
                    "Do not prefer product categories; locate where governments publish solicitations."
                )
                ai = web_research(
                    db,
                    user_id=user_id,
                    objective="source_discovery",
                    prompt=prompt,
                    research_run_id=run.id,
                    json_schema_hint=schema,
                    job_spent_usd=paid_cost,
                )
                if ai.get("ok"):
                    openai_used = True
                    paid_cost += Decimal(str(ai.get("estimated_cost_usd") or 0))
                    parsed = ai.get("parsed") or {}
                    srcs = parsed.get("sources") if isinstance(parsed, dict) else []
                    pack = {"sources": srcs or [], "citations": ai.get("citations") or []}
                    _cache_put(db, cache_key, "source_discovery", pack, hours=72)
                    discovered_sources.extend(srcs or [])
                    for s in srcs or []:
                        url = (s.get("active_bids_url") or s.get("procurement_url") or "").strip()
                        if not url.startswith("http"):
                            continue
                        entity = (s.get("entity") or "Discovered portal")[:200]
                        dkey = "ai_" + hashlib.sha256(url.encode()).hexdigest()[:20]
                        if db.query(SourceRegistry).filter_by(source_key=dkey).one_or_none():
                            continue
                        # HTML portals: enable but mark pending experimental verification
                        db.add(
                            SourceRegistry(
                                source_key=dkey,
                                name=entity,
                                base_url=url,
                                source_kind="state_portal",
                                information_available=s.get("notes") or "OpenAI web research discovery",
                                adapter="html_link_harvest",
                                config={
                                    "list_url": url,
                                    "geo_state": (s.get("state") or "")[:2].upper() or None,
                                    "discovered": True,
                                    "discovery_method": "openai_web_search",
                                    "award_archive_url": s.get("award_archive_url"),
                                    "citations": ai.get("citations") or [],
                                    "link_hints": [
                                        "bid",
                                        "rfp",
                                        "rfq",
                                        "ifb",
                                        "solicitation",
                                        "opportunity",
                                    ],
                                },
                                access_cost="free",
                                documented_ok=True,
                                experimentally_verified=False,
                                is_enabled=True,
                            )
                        )
                        new_operational_sources += 1
                    db.commit()
                elif ai.get("budget_denied"):
                    limitations.append(f"openai_source_discovery: {ai.get('error')}")
                else:
                    limitations.append(f"openai_source_discovery: {ai.get('error')}")
        except Exception as exc:
            limitations.append(f"openai_source_discovery: {type(exc).__name__}")

    # Stage 2 — Deduplicate (identity only; do not merge on similar titles alone)
    seen: set[str] = set()
    unique: list[dict] = []
    duplicates_excluded = 0
    for it in all_items:
        k = it.get("external_key") or (
            f"{it.get('source_key')}|{it.get('solicitation_number')}"
            if it.get("solicitation_number")
            else None
        ) or it.get("official_url") or it.get("title")
        if not k:
            continue
        if k in seen:
            duplicates_excluded += 1
            continue
        seen.add(str(k))
        unique.append(it)
    discovery_population = len(unique)

    # Stage 2b — Record identity verification (Build 002G; not product filtering)
    _progress(run, phase="record_identity", unique_before_filters=discovery_population)
    db.add(run)
    db.commit()
    from m3.research.identity import apply_identity_batch

    source_cfgs = {
        r.source_key: (r.config or {})
        for r in db.query(SourceRegistry).all()
    }
    identity_pack = apply_identity_batch(unique, source_configs=source_cfgs)
    unique = identity_pack["items"]
    identity_breakdown = identity_pack.get("identity_breakdown") or {}

    # Stage 3 — Centralized classification (independent of owner filters)
    # Skip requirement classification for confirmed non-solicitation artifacts.
    _progress(run, phase="central_classification", unique_before_filters=discovery_population)
    db.add(run)
    db.commit()
    classified_pack = classify_batch(unique)
    unique = classified_pack["items"]
    classification_breakdown = classified_pack.get("classification_breakdown") or {}
    status_breakdown = classified_pack.get("status_breakdown") or {}

    # Stage 4 — Progressive classification recovery (full uncertain population)
    import threading

    _cache_lock = threading.Lock()

    def _cg(key: str):
        with _cache_lock:
            return _cache_get(db, key)

    def _cp(key: str, kind: str, payload: dict, hours: int = 24):
        with _cache_lock:
            _cache_put(db, key, kind, payload, hours=hours)

    # L4 AI recovery uses budget independently of portal discovery checkbox
    recovery_ai = bool(settings.paid_research_enabled and settings.openai_enabled)
    recovery = recover_uncertain_population(
        db,
        unique,
        user_id=user_id,
        research_run_id=run.id,
        enable_openai=recovery_ai,
        max_level2=None,  # process all remaining uncertain at L2
        max_level3=None,
        max_level4=40,
        job_spent_usd=paid_cost,
        cache_get=_cg,
        cache_put=_cp,
    )
    unique = recovery["items"]
    paid_cost = Decimal(str(recovery.get("job_spent_usd") or paid_cost))
    uncertain_report = recovery.get("stats") or {}
    # Recompute breakdown after recovery
    classification_breakdown = {}
    for i in unique:
        k = i.get("requirement_class") or "unknown_mixed"
        classification_breakdown[k] = classification_breakdown.get(k, 0) + 1

    # Stage 5 — User-selected filters only (does not alter discovery_population)
    filtered = apply_contract_filters(unique, filters)
    page_items = filtered[offset : offset + display_page]
    incomplete = bool(
        more_available
        or run.cancel_requested
        or (enable_openai_sources and not openai_used and not openai_cache_hit and not any(
            "openai_source_discovery" in (x or "") for x in limitations
        ))
    )
    if openai_used or openai_cache_hit or any("openai_source_discovery" in (x or "") for x in limitations):
        incomplete = bool(more_available or run.cancel_requested)

    inactive_statuses = {"closed", "closed_by_deadline", "cancelled", "historical_award", "forecast"}
    product_verified = sum(
        1
        for i in unique
        if (i.get("requirement_class") or "") in PRODUCT_CLASSES
        and (i.get("solicitation_status") or "unknown") not in inactive_statuses
    )
    product_incidental = sum(
        1 for i in unique if (i.get("requirement_class") or "") == "product_with_incidental_services"
    )
    non_product = sum(
        1
        for i in unique
        if (i.get("requirement_class") or "")
        in {"services", "professional_services", "maintenance_repair_services", "construction", "software_only"}
    )
    uncertain_left = sum(
        1
        for i in unique
        if (i.get("requirement_class") or "") in {"unknown_mixed", "unknown", "pending_classification"}
        or (i.get("uncertain_resolution") or "") == "requires_further_verification"
    )
    verified_active = sum(
        1
        for i in unique
        if (i.get("solicitation_status") or "unknown") not in inactive_statuses
    )

    if run.status != "cancelled":
        run.status = "partial" if incomplete and (page_items or filtered) else "completed"
        if not page_items and limitations and not filtered:
            run.status = "completed"
    run.result_count = len(filtered)
    run.sources_tried = tried
    run.sources_succeeded = succeeded
    run.limitations = limitations[-80:]
    run.result_snapshot = _sanitize_for_json(filtered[:5000])
    # Full unique population on disk for freeze / full-scale acquisition (002E)
    population_path = None
    try:
        from m3.research.population_acquire import write_population_file

        population_path = str(write_population_file(run.id, unique))
    except Exception as pop_exc:
        limitations.append(f"population_freeze_write_failed:{type(pop_exc).__name__}")
    run.estimated_cost_usd = float(paid_cost)
    run.finished_at = datetime.now(timezone.utc)
    _progress(
        run,
        phase="done" if run.status == "completed" else run.status,
        sources_evaluated=len(tried),
        sources_succeeded=len(succeeded),
        sources_unavailable=len(sources_unavailable),
        notices_examined=rows_examined,
        mapping_failures=mapping_failures_total,
        unique_matching=len(filtered),
        unique_before_filters=discovery_population,
        discovery_population=discovery_population,
        verified_active=verified_active,
        verified_product=product_verified,
        product_with_incidental_services=product_incidental,
        confirmed_non_product=non_product,
        classification_breakdown=classification_breakdown,
        status_breakdown=status_breakdown,
        identity_breakdown=identity_breakdown,
        verified_individual_solicitations=identity_pack.get("verified_individual_solicitations"),
        uncertain_remaining=uncertain_left,
        uncertain_investigated=uncertain_report.get("investigated"),
        uncertain_recovery=uncertain_report,
        uncertain_newly_product=(
            int(uncertain_report.get("resolved_product") or 0)
            + int(uncertain_report.get("resolved_product_incidental") or 0)
            + int(uncertain_report.get("resolved_product_installation") or 0)
        ),
        duplicates_excluded=duplicates_excluded,
        pages_fetched=pages_total,
        sources_fully_paginated=sources_fully_paginated,
        sources_incomplete=sources_incomplete,
        source_yield=source_yield[:80],
        new_operational_sources=new_operational_sources,
        quarantined_sources=quarantined_sources,
        display_offset=offset,
        display_page_size=display_page,
        incomplete=incomplete,
        openai_used=openai_used,
        openai_cache_hit=openai_cache_hit,
        estimated_cost_usd=float(paid_cost),
        population_path=population_path,
        last_refresh=datetime.now(timezone.utc).isoformat(),
    )
    db.add(run)
    actor_label = "system:research"
    if user_id:
        from m3.models import User

        u = db.get(User, user_id)
        actor_label = (u.email if u else None) or "user"
    record_audit(
        db,
        action="research.contracts",
        entity_type="research_run",
        entity_id=str(run.id),
        actor_user_id=user_id,
        actor_label=actor_label,
        after={
            "count": len(filtered),
            "sources_succeeded": succeeded,
            "status": run.status,
            "estimated_cost_usd": float(paid_cost),
        },
        origin="automated",
        research_run_id=run.id,
    )
    db.commit()

    try:
        from m3.research.coverage import build_coverage_matrix

        coverage_matrix = build_coverage_matrix(db, source_yield)
    except Exception:
        coverage_matrix = None

    return {
        "run_id": str(run.id),
        "status": run.status,
        "count": len(filtered),
        "page_count": len(page_items),
        "items": page_items,
        "all_matching_count": len(filtered),
        "sources_tried": tried,
        "sources_succeeded": succeeded,
        "sources_unavailable": sources_unavailable,
        "sources_fully_paginated": sources_fully_paginated,
        "sources_incomplete": sources_incomplete,
        "source_yield": source_yield,
        "limitations": limitations,
        "unfiltered_count": discovery_population,
        "discovery_population": discovery_population,
        "verified_active": verified_active,
        "verified_product": product_verified,
        "product_with_incidental_services": product_incidental,
        "confirmed_non_product": non_product,
        "classification_breakdown": classification_breakdown,
        "status_breakdown": status_breakdown,
        "identity_breakdown": identity_breakdown,
        "verified_individual_solicitations": identity_pack.get("verified_individual_solicitations"),
        "uncertain_remaining": uncertain_left,
        "uncertain_investigated": uncertain_report.get("investigated"),
        "uncertain_recovery": uncertain_report,
        "uncertain_newly_product": (
            int(uncertain_report.get("resolved_product") or 0)
            + int(uncertain_report.get("resolved_product_incidental") or 0)
            + int(uncertain_report.get("resolved_product_installation") or 0)
        ),
        "duplicates_excluded": duplicates_excluded,
        "notices_examined": rows_examined,
        "mapping_failures": mapping_failures_total,
        "pages_fetched": pages_total,
        "discovered_sources": discovered_sources[:50],
        "new_operational_sources": new_operational_sources,
        "quarantined_sources": quarantined_sources,
        "source_repair": repair_summary,
        "incomplete": incomplete,
        "more_available": more_available,
        "offset": offset,
        "page_size": display_page,
        "progress": run.progress,
        "estimated_cost_usd": float(paid_cost),
        "openai_used": openai_used,
        "openai_cache_hit": openai_cache_hit,
        "population_path": population_path,
        "population_count": discovery_population,
        "coverage_matrix": coverage_matrix,
    }


def run_supplier_discovery(
    db: Session,
    *,
    user_id: uuid.UUID | None,
    filters: dict[str, Any] | None = None,
    limit: int | None = None,
    enable_openai: bool = True,
) -> dict[str, Any]:
    ensure_source_registry(db)
    filters = filters or {}
    categories = [c.lower() for c in (filters.get("product_categories") or [])]
    keyword = (filters.get("keyword") or "").lower()
    settings = get_settings()
    soft_limit = limit if limit is not None else 200

    run = ResearchRun(
        objective="suppliers",
        requested_by=user_id,
        params={"filters": filters},
        status="running",
        progress={"phase": "seed_probe"},
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    sources = (
        db.query(SourceRegistry)
        .filter(SourceRegistry.is_enabled.is_(True), SourceRegistry.adapter == "supplier_page")
        .all()
    )
    items: list[dict] = []
    tried: list[str] = []
    succeeded: list[str] = []
    limitations: list[str] = []
    paid_cost = Decimal("0")

    for src in sources:
        tried.append(src.source_key)
        cfg = src.config or {}
        cats = [c.lower() for c in (cfg.get("categories") or [])]
        # Soft category filter only — natural-language keyword searches rely on OpenAI expansion
        if categories and cats and not any(c in cats for c in categories):
            continue
        payload = {"source_key": src.source_key, "base_url": src.base_url, "name": src.name, "config": cfg}
        result = probe_supplier_page(payload)
        if result.get("ok") and result.get("item"):
            succeeded.append(src.source_key)
            _mark_source(db, src.source_key, ok=True)
            items.append(result["item"])
        else:
            err = ";".join(result.get("errors") or ["fail"])
            _mark_source(db, src.source_key, ok=False, error=err)
            limitations.append(f"{src.source_key}: {err}")

    # OpenAI-assisted nationwide recruitment when enabled and under budget
    openai_leads: list[dict] = []
    if enable_openai and (keyword or categories):
        from m3.research.openai_client import web_research

        focus = keyword or ", ".join(categories) or "commercial wholesale distributors"
        cache_key = "supplier_recruit:" + hashlib.sha256(focus.encode()).hexdigest()[:20]
        cached = _cache_get(db, cache_key)
        if cached:
            openai_leads = list(cached.get("leads") or [])
        else:
            schema = (
                '{"leads":[{"company":"","website":"","address":"","phone":"","email":"",'
                '"categories":[],"brands":[],"coverage":"","classification":"",'
                '"reseller_program":"","account_application_url":"","credit_application_url":"",'
                '"payment_terms":"","catalog_url":"","dropship":"","min_order":"",'
                '"evidence_urls":[],"next_action":""}]}'
            )
            prompt = (
                f"Find legitimate U.S. wholesale distributors / authorized distributors / "
                f"commercial dealers with reseller or new-account programs for: {focus}. "
                "Prefer broad-line and industrial wholesalers, not retail storefronts. "
                "Include website, general phone/email if published, account/credit application "
                "URLs, catalog links, dropshipping notes, and geographic coverage. "
                "Only include companies with evidence URLs from web search. Return up to 25 leads."
            )
            ai = web_research(
                db,
                user_id=user_id,
                objective="supplier_recruitment",
                prompt=prompt,
                research_run_id=run.id,
                json_schema_hint=schema,
                job_spent_usd=paid_cost,
            )
            if ai.get("ok"):
                paid_cost += Decimal(str(ai.get("estimated_cost_usd") or 0))
                parsed = ai.get("parsed") or {}
                if isinstance(parsed, list):
                    openai_leads = parsed
                elif isinstance(parsed, dict):
                    openai_leads = list(parsed.get("leads") or parsed.get("suppliers") or [])
                else:
                    openai_leads = []
                _cache_put(
                    db,
                    cache_key,
                    "supplier_recruitment",
                    {"leads": openai_leads, "citations": ai.get("citations") or [], "raw": (ai.get("text") or "")[:2000]},
                    hours=48,
                )
                for lead in openai_leads:
                    if not isinstance(lead, dict):
                        continue
                    items.append(
                        {
                            "name": lead.get("company") or "Unknown supplier",
                            "website": lead.get("website"),
                            "business_address": lead.get("address"),
                            "general_phone": lead.get("phone"),
                            "general_email": lead.get("email"),
                            "categories": lead.get("categories") or [],
                            "brands": lead.get("brands") or [],
                            "coverage": lead.get("coverage"),
                            "supplier_classification": lead.get("classification") or "distributor_lead",
                            "reseller_program": lead.get("reseller_program"),
                            "account_application_url": lead.get("account_application_url"),
                            "credit_application_url": lead.get("credit_application_url"),
                            "payment_terms_published": lead.get("payment_terms"),
                            "catalog_url": lead.get("catalog_url"),
                            "dropshipping": lead.get("dropship"),
                            "min_order": lead.get("min_order"),
                            "evidence_urls": lead.get("evidence_urls") or [
                                c.get("url") for c in (ai.get("citations") or [])
                            ],
                            "next_action": lead.get("next_action") or "Review and decide whether to pursue",
                            "source_key": "openai_web_research",
                            "lead_status": "unverified_prospect",
                            "not_an_approved_account": True,
                        }
                    )
            elif ai.get("error"):
                limitations.append(f"openai_supplier_recruitment: {ai.get('error')}")

    # Deduplicate by website/name
    seen: set[str] = set()
    unique: list[dict] = []
    for it in items:
        k = (it.get("website") or it.get("name") or "").lower().strip()
        if not k or k in seen:
            continue
        seen.add(k)
        unique.append(it)

    out = unique[:soft_limit]
    run.status = "completed"
    run.result_count = len(out)
    run.sources_tried = tried
    run.sources_succeeded = succeeded
    run.limitations = limitations
    run.result_snapshot = out
    run.estimated_cost_usd = float(paid_cost)
    run.finished_at = datetime.now(timezone.utc)
    _progress(
        run,
        phase="done",
        seed_hits=len([i for i in out if i.get("source_key") != "openai_web_research"]),
        openai_leads=len([i for i in out if i.get("source_key") == "openai_web_research"]),
        estimated_cost_usd=float(paid_cost),
    )
    db.add(run)
    actor_label = "system:research"
    if user_id:
        from m3.models import User

        u = db.get(User, user_id)
        actor_label = (u.email if u else None) or "user"
    record_audit(
        db,
        action="research.suppliers",
        entity_type="research_run",
        entity_id=str(run.id),
        actor_user_id=user_id,
        actor_label=actor_label,
        after={"count": len(out), "estimated_cost_usd": float(paid_cost)},
        origin="automated",
        research_run_id=run.id,
    )
    db.commit()
    return {
        "run_id": str(run.id),
        "count": len(out),
        "items": out,
        "sources_tried": tried,
        "sources_succeeded": succeeded,
        "limitations": limitations,
        "estimated_cost_usd": float(paid_cost),
        "progress": run.progress,
    }


def run_historical_award_research(
    db: Session,
    *,
    user_id: uuid.UUID | None,
    query: str,
    product: str | None = None,
    buyer: str | None = None,
    part_number: str | None = None,
) -> dict[str, Any]:
    """On-demand historical award pricing via OpenAI web research (not a permanent warehouse)."""
    run = ResearchRun(
        objective="historical_awards",
        requested_by=user_id,
        params={"query": query, "product": product, "buyer": buyer, "part_number": part_number},
        status="running",
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    from m3.research.openai_client import web_research

    schema = (
        '{"awards":[{"buyer":"","product":"","part_number":"","match_type":"exact|approximate",'
        '"award_date":"","awardee":"","quantity":"","unit_price":"","total_award_value":"",'
        '"competing_bids":"","bidder_count":"","source_url":"","notes":""}]}'
    )
    prompt = (
        "Find official U.S. government historical award / bid tabulation / purchase order evidence "
        f"for: {query}. "
        + (f"Product: {product}. " if product else "")
        + (f"Buyer: {buyer}. " if buyer else "")
        + (f"Part number: {part_number}. " if part_number else "")
        + "Prefer bid tabs, council/board packets, award notices, and public contract documents. "
        "Distinguish exact product matches from approximate. Never treat a multi-award ceiling as "
        "unit price. Cite source URLs. Return JSON only."
    )
    ai = web_research(
        db,
        user_id=user_id,
        objective="historical_awards",
        prompt=prompt,
        research_run_id=run.id,
        json_schema_hint=schema,
        use_capable_model=False,
    )
    awards = []
    cost = 0.0
    if ai.get("ok"):
        cost = float(ai.get("estimated_cost_usd") or 0)
        parsed = ai.get("parsed") or {}
        awards = list(parsed.get("awards") or []) if isinstance(parsed, dict) else []
        for a in awards:
            a["citations"] = ai.get("citations") or []
            a["evidence_class"] = "historical_benchmark"
            a["not_guaranteed_future_price"] = True
    run.status = "completed" if ai.get("ok") else "failed"
    run.error = None if ai.get("ok") else str(ai.get("error") or "failed")
    run.result_count = len(awards)
    run.result_snapshot = awards
    run.estimated_cost_usd = cost
    run.finished_at = datetime.now(timezone.utc)
    run.limitations = [] if ai.get("ok") else [str(ai.get("error"))]
    db.add(run)
    db.commit()
    return {
        "run_id": str(run.id),
        "ok": bool(ai.get("ok")),
        "awards": awards,
        "citations": ai.get("citations") or [],
        "estimated_cost_usd": cost,
        "error": ai.get("error"),
        "web_search_calls": ai.get("web_search_calls"),
    }


def cancel_research_run(db: Session, run_id: uuid.UUID) -> bool:
    run = db.get(ResearchRun, run_id)
    if not run or run.status not in {"running", "partial"}:
        return False
    run.cancel_requested = True
    db.add(run)
    db.commit()
    return True
