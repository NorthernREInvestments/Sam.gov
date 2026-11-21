"""Orchestrate research runs against the source registry."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from m3.audit import record_audit
from m3.models import ResearchRun, SourceRegistry
from m3.research.adapters import (
    discover_socrata_catalog,
    probe_supplier_page,
    search_html_links,
    search_socrata,
)
from m3.research.filters import apply_contract_filters
from m3.research.seeds import SEED_SOURCES


def ensure_source_registry(db: Session) -> int:
    """Insert new seeds and refresh adapter/config for existing seed keys."""
    created = 0
    updated = 0
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
            updated += 1
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


def run_contract_discovery(
    db: Session,
    *,
    user_id: uuid.UUID | None,
    filters: dict[str, Any] | None = None,
    limit: int = 80,
) -> dict[str, Any]:
    ensure_source_registry(db)
    filters = filters or {}
    keywords = []
    if filters.get("keyword"):
        keywords.append(str(filters["keyword"]))
    for c in filters.get("product_categories") or []:
        keywords.append(str(c).replace("_", " "))
    if not keywords:
        keywords = ["equipment", "supplies", "furniture", "tools", "computer", "janitorial"]

    run = ResearchRun(
        objective="open_contracts",
        requested_by=user_id,
        params={"filters": filters, "keywords": keywords},
        status="running",
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
    # Prefer Nebraska-related, then live open-data boards
    sources = sorted(
        sources,
        key=lambda s: (
            0 if (s.config or {}).get("geo_state") == "NE" else 1,
            0 if s.adapter == "socrata_contracts" else 1,
            s.name,
        ),
    )

    all_items: list[dict] = []
    tried: list[str] = []
    succeeded: list[str] = []
    limitations: list[str] = []
    discovered_sources: list[dict] = []

    for src in sources:
        tried.append(src.source_key)
        payload = {"source_key": src.source_key, "base_url": src.base_url, "name": src.name, "config": src.config}
        try:
            if src.adapter == "socrata_catalog_discover":
                result = discover_socrata_catalog(payload)
                if result.get("ok") and result.get("items"):
                    succeeded.append(src.source_key)
                    _mark_source(db, src.source_key, ok=True)
                    discovered_sources.extend(result["items"])
                    # Auto-register newly discovered free datasets (documented until verified)
                    for disc in result["items"][:12]:
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
                                is_enabled=False,  # owner/ops enable after verification
                            )
                        )
                    db.commit()
                else:
                    err = ";".join(result.get("errors") or ["no_items"])
                    _mark_source(db, src.source_key, ok=False, error=err)
                    limitations.append(f"{src.source_key}: {err}")
                continue

            if src.adapter == "socrata_contracts":
                result = search_socrata(payload, keywords=keywords, limit=120)
            else:
                result = search_html_links(payload, keywords=keywords, limit=25)
            if result.get("ok") and result.get("items"):
                succeeded.append(src.source_key)
                _mark_source(db, src.source_key, ok=True)
                all_items.extend(result["items"])
            else:
                err = ";".join(result.get("errors") or ["no_items"])
                _mark_source(db, src.source_key, ok=False, error=err)
                limitations.append(f"{src.source_key}: {err}")
        except Exception as exc:
            _mark_source(db, src.source_key, ok=False, error=f"{type(exc).__name__}:{exc}")
            limitations.append(f"{src.source_key}: {type(exc).__name__}")

    # Deduplicate by external_key / url
    seen: set[str] = set()
    unique: list[dict] = []
    for it in all_items:
        k = it.get("external_key") or it.get("official_url") or it.get("title")
        if not k or k in seen:
            continue
        seen.add(str(k))
        unique.append(it)

    filtered = apply_contract_filters(unique, filters)
    filtered = filtered[:limit]

    run.status = "completed"
    run.result_count = len(filtered)
    run.sources_tried = tried
    run.sources_succeeded = succeeded
    run.limitations = limitations[:30]
    run.finished_at = datetime.now(timezone.utc)
    db.add(run)
    record_audit(
        db,
        action="research.contracts",
        entity_type="research_run",
        entity_id=str(run.id),
        actor_user_id=user_id,
        actor_label="system:research" if not user_id else "user",
        after={"count": len(filtered), "sources_succeeded": succeeded},
        origin="automated",
        research_run_id=run.id,
    )
    db.commit()

    return {
        "run_id": str(run.id),
        "count": len(filtered),
        "items": filtered,
        "sources_tried": tried,
        "sources_succeeded": succeeded,
        "limitations": limitations,
        "unfiltered_count": len(unique),
        "discovered_sources": discovered_sources[:20],
    }


def run_supplier_discovery(
    db: Session,
    *,
    user_id: uuid.UUID | None,
    filters: dict[str, Any] | None = None,
    limit: int = 40,
) -> dict[str, Any]:
    ensure_source_registry(db)
    filters = filters or {}
    categories = [c.lower() for c in (filters.get("product_categories") or [])]
    keyword = (filters.get("keyword") or "").lower()

    run = ResearchRun(
        objective="suppliers",
        requested_by=user_id,
        params={"filters": filters},
        status="running",
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

    for src in sources:
        tried.append(src.source_key)
        cfg = src.config or {}
        cats = [c.lower() for c in (cfg.get("categories") or [])]
        if categories and not any(c in cats for c in categories):
            continue
        if keyword and keyword not in (src.name or "").lower() and keyword not in " ".join(cats):
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
        if len(items) >= limit:
            break

    run.status = "completed"
    run.result_count = len(items)
    run.sources_tried = tried
    run.sources_succeeded = succeeded
    run.limitations = limitations
    run.finished_at = datetime.now(timezone.utc)
    db.add(run)
    record_audit(
        db,
        action="research.suppliers",
        entity_type="research_run",
        entity_id=str(run.id),
        actor_user_id=user_id,
        actor_label="system:research" if not user_id else "user",
        after={"count": len(items)},
        origin="automated",
        research_run_id=run.id,
    )
    db.commit()
    return {
        "run_id": str(run.id),
        "count": len(items),
        "items": items,
        "sources_tried": tried,
        "sources_succeeded": succeeded,
        "limitations": limitations,
    }
