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
from m3.research.filters import apply_contract_filters
from m3.research.seeds import SEED_SOURCES
from m3.research.vocabulary import parse_search_terms


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
        "chicago_procurement",
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


def _mark_source(db: Session, source_key: str, *, ok: bool, error: str | None = None) -> None:
    row = db.query(SourceRegistry).filter_by(source_key=source_key).one_or_none()
    if not row:
        return
    now = datetime.now(timezone.utc)
    if ok:
        row.last_success_at = now
        row.experimentally_verified = True
    else:
        row.last_failure_at = now
        hist = list(row.failure_history or [])
        hist.append({"at": now.isoformat(), "error": (error or "")[:240]})
        row.failure_history = hist[-20:]
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


def _cache_put(db: Session, key: str, kind: str, payload: dict, *, hours: int = 24) -> None:
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


def _progress(run: ResearchRun, **fields: Any) -> None:
    p = dict(run.progress or {})
    p.update(fields)
    p["updated_at"] = datetime.now(timezone.utc).isoformat()
    run.progress = p


def _keywords_from_filters(filters: dict[str, Any]) -> list[str]:
    parsed = parse_search_terms(
        filters.get("keyword") or "",
        mode=(filters.get("search_mode") or "broad"),
        exclude=filters.get("exclude_keywords") or "",
    )
    keywords = list(parsed["include_terms"])
    for c in filters.get("product_categories") or []:
        keywords.append(str(c).replace("_", " "))
    if not keywords:
        keywords = ["equipment", "supplies", "furniture", "tools", "computer", "janitorial"]
    # unique
    seen: set[str] = set()
    out: list[str] = []
    for k in keywords:
        kl = k.lower().strip()
        if kl and kl not in seen:
            seen.add(kl)
            out.append(kl)
    return out


def run_contract_discovery(
    db: Session,
    *,
    user_id: uuid.UUID | None,
    filters: dict[str, Any] | None = None,
    limit: int | None = None,
    offset: int = 0,
    continue_run_id: uuid.UUID | None = None,
    enable_openai_sources: bool = False,
) -> dict[str, Any]:
    """Nationwide free-source discovery with pagination / continuation.

    `limit` is a display/slice size only — never the entire opportunity population.
    Pass limit=None to retrieve all pages within per-source max_pages.
    """
    settings = get_settings()
    ensure_source_registry(db)
    filters = filters or {}
    keywords = _keywords_from_filters(filters)
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
            params={"filters": filters, "keywords": keywords, "offset": offset},
            status="running",
            progress={"phase": "starting", "sources_evaluated": 0},
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
    sources_unavailable: list[str] = []
    more_available = False
    paid_cost = Decimal("0")
    openai_used = False

    _progress(
        run,
        phase="searching_sources",
        sources_total=len(sources),
        sources_evaluated=0,
        notices_examined=0,
        unique_so_far=len(all_items),
        incomplete=True,
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
                    for disc in result["items"][:30]:
                        dkey = f"discovered_{disc['domain'].replace('.', '_')}_{disc['dataset_id']}"
                        if db.query(SourceRegistry).filter_by(source_key=dkey).one_or_none():
                            continue
                        db.add(
                            SourceRegistry(
                                source_key=dkey,
                                name=f"{disc['name']} ({disc['domain']})",
                                base_url=disc["dataset_url"],
                                source_kind="socrata",
                                information_available=f"Discovered via catalog query: {disc.get('discovered_via')}",
                                adapter="socrata_contracts",
                                config={
                                    "dataset_url": disc["dataset_url"],
                                    "official_listing_url": disc.get("link"),
                                    "discovered": True,
                                },
                                access_cost="free",
                                documented_ok=True,
                                experimentally_verified=False,
                                is_enabled=True,
                            )
                        )
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
                )
                db.add(run)
                db.commit()
                continue

            if src.adapter == "socrata_contracts":
                # Full pagination within max_pages — slice limit does not cap a source
                result = search_socrata(
                    payload,
                    keywords=keywords,
                    limit=None,
                    offset=0,
                    page_size=settings.research_socrata_page_size,
                    max_pages=settings.research_max_pages_per_source,
                )
            else:
                result = search_html_links(payload, keywords=keywords, limit=200)
            pages_total += int(result.get("pages_fetched") or 1)
            rows_examined += int(result.get("rows_examined") or len(result.get("items") or []))
            if result.get("more_available"):
                more_available = True
            if result.get("ok") and result.get("items"):
                if src.source_key not in succeeded:
                    succeeded.append(src.source_key)
                _mark_source(db, src.source_key, ok=True)
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
            unique_so_far=len({i.get("external_key") for i in all_items if i.get("external_key")}),
            pages_fetched=pages_total,
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
        filtered_tmp = apply_contract_filters(uniq_tmp, filters)
        run.sources_tried = tried
        run.sources_succeeded = succeeded
        run.limitations = limitations[-80:]
        run.result_snapshot = filtered_tmp[:5000]
        run.result_count = len(filtered_tmp)
        db.add(run)
        db.commit()

    # Optional OpenAI source discovery (budget-gated)
    if enable_openai_sources and not run.cancel_requested:
        try:
            from m3.research.openai_client import web_research

            cache_key = "openai_source_discover:" + hashlib.sha256(
                ",".join(keywords[:6]).encode()
            ).hexdigest()[:16]
            cached = _cache_get(db, cache_key)
            if cached:
                discovered_sources.extend(cached.get("sources") or [])
                _progress(run, openai_cache_hit=True)
            else:
                schema = (
                    '{"sources":[{"entity":"","state":"","procurement_url":"","active_bids_url":"",'
                    '"award_archive_url":"","retrieval_method":"","notes":""}]}'
                )
                prompt = (
                    "Find free official U.S. state/local government procurement portals that publish "
                    "active product solicitations (goods/supplies/equipment). Prioritize Nebraska, then "
                    "other states. Prefer purchasing departments, cities, counties, school districts, "
                    "universities, utilities, transit, airports, hospitals, housing authorities. "
                    f"Product focus keywords: {', '.join(keywords[:8])}. "
                    "Return only portals with public bid listings (no login paywall). "
                    "Do not invent URLs — only cite real pages you found via web search."
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
                                },
                                access_cost="free",
                                documented_ok=True,
                                experimentally_verified=False,
                                is_enabled=True,
                            )
                        )
                    db.commit()
                elif ai.get("budget_denied"):
                    limitations.append(f"openai_source_discovery: {ai.get('error')}")
                else:
                    limitations.append(f"openai_source_discovery: {ai.get('error')}")
        except Exception as exc:
            limitations.append(f"openai_source_discovery: {type(exc).__name__}")

    # Deduplicate
    seen: set[str] = set()
    unique: list[dict] = []
    duplicates_excluded = 0
    for it in all_items:
        k = it.get("external_key") or it.get("official_url") or it.get("title")
        if not k:
            continue
        if k in seen:
            duplicates_excluded += 1
            continue
        seen.add(str(k))
        unique.append(it)

    filtered = apply_contract_filters(unique, filters)
    # Display slice only — full filtered set stays on the run for continuation
    page_items = filtered[offset : offset + display_page]
    incomplete = bool(more_available or run.cancel_requested or enable_openai_sources and not openai_used)

    if run.status != "cancelled":
        run.status = "partial" if incomplete and page_items else "completed"
        if not page_items and limitations and not filtered:
            run.status = "completed"
    run.result_count = len(filtered)
    run.sources_tried = tried
    run.sources_succeeded = succeeded
    run.limitations = limitations[-80:]
    run.result_snapshot = filtered[:5000]
    run.estimated_cost_usd = float(paid_cost)
    run.finished_at = datetime.now(timezone.utc)
    _progress(
        run,
        phase="done" if run.status == "completed" else run.status,
        sources_evaluated=len(tried),
        sources_succeeded=len(succeeded),
        sources_unavailable=len(sources_unavailable),
        notices_examined=rows_examined,
        unique_matching=len(filtered),
        unique_before_filters=len(unique),
        duplicates_excluded=duplicates_excluded,
        pages_fetched=pages_total,
        display_offset=offset,
        display_page_size=display_page,
        incomplete=incomplete,
        openai_used=openai_used,
        estimated_cost_usd=float(paid_cost),
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
        "limitations": limitations,
        "unfiltered_count": len(unique),
        "duplicates_excluded": duplicates_excluded,
        "notices_examined": rows_examined,
        "pages_fetched": pages_total,
        "discovered_sources": discovered_sources[:50],
        "incomplete": incomplete,
        "more_available": more_available,
        "offset": offset,
        "page_size": display_page,
        "progress": run.progress,
        "estimated_cost_usd": float(paid_cost),
        "openai_used": openai_used,
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
