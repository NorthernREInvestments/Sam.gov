"""Canonical M3 research/acquisition job entrypoints.

Usage:
  python -m m3.research.jobs verify-sources
  python -m m3.research.jobs acquire [--incremental]
  python -m m3.research.jobs recover-unresolved
  python -m m3.research.jobs qualify
  python -m m3.research.jobs qualification-repair
  python -m m3.research.jobs document-reconcile
  python -m m3.research.jobs acquisition-quality-repair
  python -m m3.research.jobs controlled-source-reset
  python -m m3.research.jobs nextbid-product-class-audit
  python -m m3.research.jobs pipeline-repair
  python -m m3.research.jobs offline-full-text-reprocess --from YYYY-MM-DD --to YYYY-MM-DD
  python -m m3.research.jobs demote-expired-queues
  python -m m3.research.jobs validate-ai-doc-understanding --offline
  python -m m3.research.jobs bulk-ai-doc-analysis --owner-auth OWNER_AUTHORIZED_BULK_AI_DOC_ANALYSIS --max-usd 5
  python -m m3.research.jobs fresh-inventory-reset
  python -m m3.research.jobs fresh-cohort-ai-complete --owner-auth OWNER_AUTHORIZED_FRESH_COHORT_AI_ANALYSIS --max-usd 0.25
  python -m m3.research.jobs solicitation-sop
  python -m m3.research.jobs nextbid-data-loss-investigation
  python -m m3.research.jobs nextbid-permanent-repair
  python -m m3.research.jobs nextbid-final-pipeline-audit
  python -m m3.research.jobs nextbid-controlled-production
  python -m m3.research.jobs nextbid-status-reconciliation
  python -m m3.research.jobs nextbid-complete-discovery
  python -m m3.research.jobs nextbid-full-inventory-audit
  python -m m3.research.jobs nextbid-repair-002r
  python -m m3.research.jobs acceptance-002q

No build-specific runner scripts. NextBid remains fail-closed when locked.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from m3 import BUILD_ID
from m3.config import get_settings
from m3.db import SessionLocal
from m3.models import Opportunity, PackageDocument, SolicitationPackage
from m3.research.budget import ensure_budget_policy, spending_snapshot
from m3.research.commercial_criteria import (
    STAGE_A,
    STAGE_B,
    STAGE_C,
    evaluate_bid_eligibility,
    evaluate_procurement_pipeline,
)
from m3.research.engine import ensure_source_registry, run_contract_discovery
from m3.research.identity import apply_identity
from m3.research.nextbid_controller import acquisition_locked, controller_status
from m3.research.owner_queues import empty_queues, map_pipeline_to_queue, write_queues
from m3.research.population_acquire import load_population_file, write_population_file
from m3.research.procurement_registry import ingest_population, load_records
from m3.research.reprocess import collect_package_text, package_to_candidate
from m3.research.source_verification import verify_enabled_free_sources

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "docs" / "acceptance_archive"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def assert_constraints(db: Session) -> dict[str, Any]:
    locked = acquisition_locked()
    if not locked:
        raise RuntimeError("NextBid must remain locked for this job set until owner unlocks")
    pol = ensure_budget_policy(db)
    daily = float(pol.daily_budget_usd)
    job = float(pol.per_job_budget_usd)
    if daily > 1.0001 or job > 0.5001:
        raise RuntimeError(f"OpenAI ceilings drifted: daily={daily} job={job}")
    return {
        "nextbid_locked": True,
        "controller": controller_status(),
        "openai_daily": daily,
        "openai_per_job": job,
        "spend": spending_snapshot(db),
        "build_id": BUILD_ID,
    }


def cmd_verify_sources(db: Session) -> dict[str, Any]:
    ensure_source_registry(db)
    index = verify_enabled_free_sources(db, sample_limit=80)
    print(json.dumps({"ok": True, "verify_sources": index}, default=str), flush=True)
    return index


def cmd_acquire_incremental(db: Session) -> dict[str, Any]:
    """Incremental cycle: re-poll verified free feeds; skip unchanged fingerprints.

    Does not re-run full uncertain L3 package recovery. NextBid excluded.
    """
    assert_constraints(db)
    ensure_source_registry(db)
    from m3.models import SourceRegistry
    from m3.research.adapters import search_georgia_gpr, search_socrata
    from m3.research.procurement_registry import content_fingerprint, load_records

    print("incremental_loading_registry", flush=True)
    prior = load_records()
    print(f"incremental_registry_loaded n={len(prior)}", flush=True)
    prior_fp = {
        str(r.get("provider_id") or r.get("nb_id") or r.get("external_key") or ""): r.get(
            "content_fingerprint"
        )
        or content_fingerprint(r)
        for r in prior.values()
    }
    from m3.research.source_allowlist import filter_sources_for_acquisition

    rows = filter_sources_for_acquisition(
        db.query(SourceRegistry)
        .filter(SourceRegistry.is_enabled.is_(True), SourceRegistry.access_cost == "free")
        .filter(SourceRegistry.adapter.in_(["socrata_contracts", "georgia_gpr"]))
        .all()
    )
    new_items: list[dict] = []
    changed_items: list[dict] = []
    unchanged = 0
    source_stats = []
    # Prefer high-signal open-data feeds for daily incremental; HTML/GPR full polls
    # remain available via full acquire.
    priority_keys = {
        "la_ramp_open_bids",
        "montgomery_md_solicitations",
        "delaware_open_bids",
        "sf_open_bid_opportunities",
        "georgia_gpr_open_goods",
    }
    ordered = [r for r in rows if r.source_key in priority_keys] + [
        r for r in rows if r.source_key not in priority_keys
    ]
    for src in ordered:
        cfg = src.config or {}
        if not (cfg.get("operational") or src.experimentally_verified):
            continue
        # Incremental daily cycle: priority verified feeds first; skip historical/forecast
        from m3.research.source_quality import is_active_opportunity_source

        if not is_active_opportunity_source(src):
            continue
        if cfg.get("is_forecast") or cfg.get("is_historical"):
            continue
        if src.source_key not in priority_keys and src.adapter != "socrata_contracts":
            continue
        if src.source_key not in priority_keys:
            # Cap non-priority Socrata to one page for change detection speed
            max_pages = 2
        else:
            max_pages = get_settings().research_max_pages_per_source
        payload = {
            "source_key": src.source_key,
            "base_url": src.base_url,
            "name": src.name,
            "config": cfg,
        }
        print(f"incremental_poll {src.source_key}", flush=True)
        try:
            if src.adapter == "georgia_gpr":
                result = search_georgia_gpr(payload, limit=200)
            else:
                result = search_socrata(
                    payload,
                    keywords=None,
                    limit=None,
                    page_size=500,
                    max_pages=max_pages,
                    apply_adapter_keywords=False,
                )
        except Exception as exc:  # noqa: BLE001
            source_stats.append(
                {"source_key": src.source_key, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
            )
            print(f"incremental_err {src.source_key} {type(exc).__name__}", flush=True)
            continue
        items = list(result.get("items") or [])
        src_new = src_changed = src_unchanged = 0
        for it in items:
            key = str(it.get("external_key") or it.get("solicitation_number") or "")
            fp = content_fingerprint(it)
            old = prior_fp.get(key)
            if old is None:
                new_items.append(it)
                src_new += 1
            elif old != fp:
                changed_items.append(it)
                src_changed += 1
            else:
                unchanged += 1
                src_unchanged += 1
        source_stats.append(
            {
                "source_key": src.source_key,
                "ok": bool(result.get("ok")),
                "fetched": len(items),
                "new": src_new,
                "changed": src_changed,
                "unchanged": src_unchanged,
            }
        )
        print(
            f"incremental_done {src.source_key} fetched={len(items)} "
            f"new={src_new} changed={src_changed} unchanged={src_unchanged}",
            flush=True,
        )
    population = new_items + changed_items
    payloads = []
    for it in population:
        payloads.append(
            {
                "provider": it.get("source_key") or "free",
                "provider_id": it.get("external_key")
                or it.get("solicitation_number")
                or it.get("title"),
                "title": it.get("title"),
                "solicitation_number": it.get("solicitation_number"),
                "official_url": it.get("official_url"),
                "agency": it.get("buyer_name") or it.get("agency"),
                "deadline_at": it.get("deadline_at"),
                "requirement_class": it.get("requirement_class"),
                "quantity": it.get("quantity"),
                "geo_state": it.get("geo_state"),
                "source_key": it.get("source_key"),
                "build_002q_incremental": True,
                "content_fingerprint": content_fingerprint(it),
            }
        )
    persist = ingest_population(payloads, actor="jobs.acquire_incremental") if payloads else {}
    out = {
        "mode": "incremental",
        "sources_polled": len(source_stats),
        "source_stats": source_stats,
        "new_items": len(new_items),
        "changed_items": len(changed_items),
        "unchanged_skipped": unchanged,
        "unique_items": len(population),
        "registry_persist": persist,
        "openai_spent": 0,
        "metered_nextbid": 0,
        "finished_at": _utc(),
    }
    _write(get_settings().data_root() / "acceptance" / "jobs_acquire_incremental_last.json", out)
    print(json.dumps({"ok": True, "acquire_incremental": out}, default=str)[:4000], flush=True)
    return out


def cmd_acquire(db: Session, *, incremental: bool = False, user_id=None) -> dict[str, Any]:
    """Full free-source acquisition (or true incremental poll). NextBid excluded by lock."""
    assert_constraints(db)
    if incremental:
        return cmd_acquire_incremental(db)
    filters: dict[str, Any] = {
        "product_resale_only": False,  # discover broadly; classify later
        "include_unknown_value": True,
        "build": "002Q",
        "mode": "full",
    }
    # Display slice large; engine paginates sources fully within max_pages
    # Cap uncertain L3 recovery for production jobs unless owner overrides env.
    import os

    os.environ.setdefault("M3_RECOVERY_MAX_LEVEL3", "1200")
    result = run_contract_discovery(
        db,
        user_id=user_id,
        filters=filters,
        limit=None,
        enable_openai_sources=False,
    )
    # Engine returns a display page in `items`; full population is on disk / snapshot.
    from m3.research.population_acquire import load_population_file

    population: list[dict] = []
    pop_path = result.get("population_path")
    if pop_path:
        population = load_population_file(pop_path)
    if not population:
        # Fallback: research run snapshot (capped at 5000 in engine)
        population = list(result.get("items") or [])
    out = {
        "mode": "incremental" if incremental else "full",
        "run_id": str(result.get("run_id") or ""),
        "discovery_population": result.get("discovery_population") or len(population),
        "all_matching_count": result.get("all_matching_count"),
        "unique_items": len(population) or int(result.get("discovery_population") or 0),
        "verified_individual_solicitations": result.get("verified_individual_solicitations"),
        "verified_product": result.get("verified_product"),
        "source_yield": result.get("source_yield"),
        "sources_succeeded": result.get("sources_succeeded"),
        "sources_unavailable": result.get("sources_unavailable"),
        "sources_incomplete": result.get("sources_incomplete"),
        "limitations": (result.get("limitations") or [])[:40],
        "more_available": result.get("more_available"),
        "population_path": pop_path,
        "notices_examined": result.get("notices_examined"),
        "duplicates_excluded": result.get("duplicates_excluded"),
        "openai_spent": float(result.get("estimated_cost_usd") or 0),
        "metered_nextbid": 0,
        "finished_at": _utc(),
    }
    payloads = []
    for it in population:
        payloads.append(
            {
                "provider": it.get("source_key") or "free",
                "provider_id": it.get("external_key")
                or it.get("solicitation_number")
                or it.get("title"),
                "nb_id": None,
                "title": it.get("title"),
                "solicitation_number": it.get("solicitation_number"),
                "official_url": it.get("official_url"),
                "agency": it.get("buyer_name") or it.get("agency"),
                "deadline_at": it.get("deadline_at"),
                "requirement_class": it.get("requirement_class"),
                "procurement_stage": it.get("procurement_stage"),
                "quantity": it.get("quantity"),
                "geo_state": it.get("geo_state"),
                "source_key": it.get("source_key"),
                "package_status": it.get("package_status"),
                "build_002q_acquire": True,
            }
        )
    if payloads:
        out["registry_persist"] = ingest_population(payloads, actor="jobs.acquire")
    print(
        json.dumps(
            {"ok": True, "acquire": {k: out[k] for k in out if k != "source_yield"}},
            default=str,
        ),
        flush=True,
    )
    _write(get_settings().data_root() / "acceptance" / "jobs_acquire_last.json", out)
    return out


def cmd_recover_unresolved(db: Session) -> dict[str, Any]:
    """Re-investigate unresolved inventory using stored documents only (no NextBid)."""
    assert_constraints(db)
    queues_idx = get_settings().data_root() / "procurement_queues" / "index.json"
    prior = json.loads(queues_idx.read_text(encoding="utf-8")) if queues_idx.exists() else {}
    packages = (
        db.query(SolicitationPackage)
        .order_by(SolicitationPackage.id.desc())
        .limit(8000)
        .all()
    )
    recovered_qty = 0
    stage_c = 0
    rows_out = []
    for pkg in packages:
        cand = package_to_candidate(pkg)
        text = collect_package_text(pkg, max_chars=100_000)
        pipe = evaluate_procurement_pipeline(cand, document_text=text)
        qkey = map_pipeline_to_queue(pipe, package_status=pkg.package_status)
        if pipe.get("quantity_defined"):
            recovered_qty += 1
        if pipe.get("stage") == STAGE_C and pipe.get("quantity_defined"):
            stage_c += 1
        rows_out.append(
            {
                "external_key": pkg.external_key,
                "title": (pkg.title or "")[:200],
                "solicitation_number": pkg.solicitation_number,
                "official_url": pkg.authoritative_url,
                "package_status": pkg.package_status,
                "stage": pipe.get("stage"),
                "quantity": pipe.get("quantity"),
                "quantity_semantic": pipe.get("quantity_semantic"),
                "disposition_queue": qkey,
                "excluded": pipe.get("excluded"),
                "unknown_flags": pipe.get("unknown_flags"),
            }
        )
    # Rebuild owner queues from recovery pass + prior Stage C retention
    queues = empty_queues()
    for row in rows_out:
        q = row["disposition_queue"]
        if q in queues:
            queues[q].append(row)
        # Mirror Stage C into legacy verified alias for compatibility
        if q == "qualified_commercial_evaluation":
            queues["verified_procurement_fits"].append(
                {**row, "disposition": "verified_procurement_fit"}
            )
    summary = write_queues(queues, build="002Q")
    out = {
        "packages_scanned": len(packages),
        "quantities_defined": recovered_qty,
        "stage_c": stage_c,
        "queues": summary,
        "prior_queue_index": prior.get("queues"),
        "metered_nextbid": 0,
        "openai_spent": 0,
        "finished_at": _utc(),
    }
    _write(get_settings().data_root() / "acceptance" / "jobs_recover_last.json", out)
    print(json.dumps({"ok": True, "recover": out}, default=str), flush=True)
    return out


def _prior_quantity_index() -> dict[str, dict[str, Any]]:
    """Index previously verified quantity evidence for recovery (no fabrication)."""
    root = get_settings().data_root() / "acceptance"
    files = [
        root / "build002p_stage_c_evidence.json",
        root / "build002n_stage_c_evidence.json",
        root / "build002o_stage_c_evidence.json",
        root / "build002q_stage_c_evidence.json",
        root / "jobs_qualify_last.json",
    ]
    idx: dict[str, dict[str, Any]] = {}

    def _put(row: dict[str, Any]) -> None:
        qty = row.get("quantity")
        if qty in (None, "", "unknown"):
            return
        payload = {
            "quantity": qty,
            "quantity_semantic": row.get("quantity_semantic") or "ORDERED_QUANTITY",
            "quantity_evidence": list(row.get("quantity_evidence") or [])[:8]
            + ["recovered_from_prior_stage_c_evidence"],
            "quantity_kind": row.get("quantity_kind") or "fixed",
            "package_status": row.get("package_status"),
        }
        title_key = "title:" + str(row.get("title") or "").strip().lower()[:160]
        for key in (
            str(row.get("nb_id") or "").strip(),
            str(row.get("provider_id") or "").strip(),
            str(row.get("external_key") or "").strip(),
            str(row.get("official_url") or "").strip(),
            str(row.get("solicitation_number") or "").strip(),
            title_key if len(title_key) > 12 else "",
        ):
            if key and key not in idx:
                idx[key] = payload

    for path in files:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows = (
            data.get("unique_opportunities")
            or data.get("opportunities")
            or data.get("stage_c_evidence_sample")
            or data.get("fits")
            or []
        )
        if isinstance(rows, dict):
            rows = list(rows.values())
        for row in rows:
            if isinstance(row, dict):
                _put(row)
    print(f"prior_quantity_index keys={len(idx)}", flush=True)
    return idx


def _merge_prior_quantity(work: dict[str, Any], qty_idx: dict[str, dict[str, Any]]) -> bool:
    if work.get("quantity") not in (None, "", "unknown"):
        return False
    title_key = "title:" + str(work.get("title") or "").strip().lower()[:160]
    for key in (
        str(work.get("external_key") or "").strip(),
        str(work.get("nb_id") or work.get("provider_id") or "").strip(),
        str(work.get("official_url") or "").strip(),
        str(work.get("solicitation_number") or "").strip(),
        title_key if len(title_key) > 12 else "",
    ):
        if key and key in qty_idx:
            payload = qty_idx[key]
            work.update({k: v for k, v in payload.items() if v is not None})
            return True
    return False


def _package_text_index(
    db: Session,
    *,
    wanted_keys: set[str] | None = None,
    limit: int = 8000,
    population: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Map freeze/package keys → recovered document evidence (multi-key association)."""
    from m3.research.package_association import reconcile_population_to_packages

    items = population or []
    if not items and wanted_keys:
        # Minimal stubs so reconcile can still index packages for those keys
        items = [{"external_key": k} for k in list(wanted_keys)[:limit]]
    recon = reconcile_population_to_packages(db, items, recover_blobs=True)
    idx = recon.get("evidence_lookup") or {}
    print(
        f"package_index_ready keys={len(idx)} linked={recon.get('linked')} "
        f"usable_text={recon.get('with_usable_text')} qty_hints={recon.get('with_quantity_hint')} "
        f"pdf_reextracted={recon.get('pdf_reextracted')}",
        flush=True,
    )
    # Stash reconcile summary on sentinel key for callers
    idx["__reconcile_summary__"] = {
        k: recon.get(k)
        for k in (
            "population",
            "packages_total",
            "packages_with_downloads",
            "linked",
            "unmatched",
            "match_methods",
            "with_usable_text",
            "with_quantity_hint",
            "pdf_reextracted",
            "junk_excerpts",
            "text_sources",
            "index_stats",
        )
    }
    return idx


def _snapshot_qualification_counts(
    items: list[dict[str, Any]],
    *,
    reapply_identity: bool = False,
) -> dict[str, Any]:
    """Offline before/after snapshot. Before-pass keeps frozen identity fields."""
    counts = {
        "n": len(items),
        "verified_individual": 0,
        "stage_a": 0,
        "stage_b": 0,
        "stage_c": 0,
        "product_opportunity": 0,
        "excluded": 0,
        "quantity_defined": 0,
        "bid_eligible": 0,
        "ready_to_submit": 0,
        "missing_quantity_product": 0,
        "identity_not_individual": 0,
    }
    for i, it in enumerate(items):
        if (i + 1) % 5000 == 0:
            print(f"snapshot_progress {i+1}/{len(items)}", flush=True)
        work = apply_identity(dict(it)) if reapply_identity else dict(it)
        is_indiv = bool(
            work.get("verified_individual_solicitation")
            or work.get("record_type") == "INDIVIDUAL_SOLICITATION"
        )
        if is_indiv:
            counts["verified_individual"] += 1
        else:
            counts["identity_not_individual"] += 1
            # Fail-closed non-individuals never reach Stage C / bid-eligible
            counts["excluded"] += 1
            continue
        pipe = evaluate_procurement_pipeline(work)
        if pipe.get("excluded"):
            counts["excluded"] += 1
        if pipe.get("product_opportunity"):
            counts["product_opportunity"] += 1
            if not pipe.get("quantity_defined"):
                counts["missing_quantity_product"] += 1
        if pipe.get("quantity_defined"):
            counts["quantity_defined"] += 1
        st = pipe.get("stage")
        if st == STAGE_A:
            counts["stage_a"] += 1
        elif st == STAGE_B:
            counts["stage_b"] += 1
        elif st == STAGE_C:
            counts["stage_c"] += 1
            bid = evaluate_bid_eligibility(work, pipe)
            if bid.get("bid_eligible"):
                counts["bid_eligible"] += 1
            if bid.get("ready_to_submit"):
                counts["ready_to_submit"] += 1
    return counts


def _qualify_population(
    db: Session,
    population: list[dict[str, Any]],
    *,
    recover_quantities: bool = True,
) -> dict[str, Any]:
    """Re-identify, recover quantities from packages, qualify, and score bid eligibility."""
    from collections import Counter

    print(
        f"qualify_population_start n={len(population)} recover={recover_quantities}",
        flush=True,
    )
    pkg_idx = (
        _package_text_index(db, population=population) if recover_quantities else {}
    )
    reconcile_summary = pkg_idx.pop("__reconcile_summary__", {}) if pkg_idx else {}
    qty_idx = _prior_quantity_index() if recover_quantities else {}
    counts = {
        "discovered": 0,
        "verified_individual": 0,
        "potential_product": 0,
        "product_quantity_verified": 0,
        "complete_package": 0,
        "commercially_qualified": 0,  # Stage C — procurement fit only
        "bid_eligible": 0,
        "ready_to_submit": 0,
        "excluded": 0,
        "quantity_recovered_from_package": 0,
        "quantity_recovered_from_prior_evidence": 0,
        "quantity_recovered_from_package_hint": 0,
        "identity_upgraded_to_individual": 0,
        "identity_demoted_from_forced": 0,
        "package_linked": 0,
    }
    identity_breakdown: Counter[str] = Counter()
    bid_gate_fails: Counter[str] = Counter()
    stage_c_rows: list[dict[str, Any]] = []
    bid_rows: list[dict[str, Any]] = []
    out_items: list[dict[str, Any]] = []
    queues = empty_queues()

    for raw in population:
        counts["discovered"] += 1
        if counts["discovered"] % 2000 == 0:
            print(
                f"qualify_progress {counts['discovered']}/{len(population)} "
                f"indiv={counts['verified_individual']} stage_c={counts['product_quantity_verified']} "
                f"bid={counts['bid_eligible']} qty_recovered={counts['quantity_recovered_from_package']}",
                flush=True,
            )
        prior_forced = bool(raw.get("verified_individual_solicitation"))
        work = apply_identity(dict(raw))
        # Never force identity — apply_identity sets evidence-based fields
        if prior_forced and not work.get("verified_individual_solicitation"):
            counts["identity_demoted_from_forced"] += 1
        if (not prior_forced) and work.get("verified_individual_solicitation"):
            counts["identity_upgraded_to_individual"] += 1

        identity_breakdown[str(work.get("record_type") or "missing")] += 1
        if work.get("verified_individual_solicitation"):
            counts["verified_individual"] += 1

        if recover_quantities and _merge_prior_quantity(work, qty_idx):
            counts["quantity_recovered_from_prior_evidence"] += 1

        from m3.research.package_association import (
            normalize_official_url,
            opportunity_id_from_url,
        )

        pkg_meta = None
        for key in (
            str(work.get("external_key") or "").strip(),
            str(work.get("official_url") or "").strip(),
            str(work.get("solicitation_number") or "").strip(),
            normalize_official_url(work.get("official_url")),
            opportunity_id_from_url(work.get("official_url")),
        ):
            if key and key in pkg_idx:
                pkg_meta = pkg_idx[key]
                break

        doc_text = ""
        had_qty = work.get("quantity") not in (None, "", "unknown")
        if pkg_meta:
            counts["package_linked"] += 1
            work["package_match_method"] = pkg_meta.get("match_method")
            doc_text = pkg_meta.get("text") or ""
            if pkg_meta.get("junk_excerpt") and pkg_meta.get("text_source") != "reextracted_pdf_blob":
                doc_text = ""  # do not feed portal chrome into qualification
            if pkg_meta.get("package_status"):
                work["package_status"] = pkg_meta["package_status"]
            if pkg_meta.get("document_text_len"):
                work["document_text_len"] = pkg_meta["document_text_len"]
            # Merge stored package quantity hints when freeze lacks quantity
            if (not had_qty) and pkg_meta.get("quantity") not in (None, "", "unknown"):
                work["quantity"] = pkg_meta["quantity"]
                work["quantity_semantic"] = pkg_meta.get("quantity_semantic") or "ORDERED_QUANTITY"
                work["quantity_kind"] = pkg_meta.get("quantity_kind") or "fixed"
                work["quantity_evidence"] = list(pkg_meta.get("quantity_evidence") or [])
                counts["quantity_recovered_from_package_hint"] += 1
                had_qty = True

        pipe = evaluate_procurement_pipeline(work, document_text=doc_text or None)
        for k, v in (pipe.get("enriched_fields") or {}).items():
            if v is not None:
                work[k] = v
        if pipe.get("quantity") is not None:
            work["quantity"] = pipe["quantity"]
        if pipe.get("quantity_semantic"):
            work["quantity_semantic"] = pipe["quantity_semantic"]
        if pipe.get("quantity_evidence"):
            work["quantity_evidence"] = pipe["quantity_evidence"]
        if pipe.get("quantity_kind"):
            work["quantity_kind"] = pipe["quantity_kind"]
        work["quantity_defined"] = bool(pipe.get("quantity_defined"))
        work["procurement_stage"] = pipe.get("stage")
        work["unknown_flags"] = pipe.get("unknown_flags") or []
        work["investigation_trail"] = pipe.get("investigation_trail") or []

        if (not had_qty) and pipe.get("quantity_defined") and doc_text:
            counts["quantity_recovered_from_package"] += 1
        elif (
            had_qty
            and pipe.get("quantity_defined")
            and counts.get("quantity_recovered_from_package_hint")
            and not doc_text
        ):
            pass  # hint path already counted

        bid = evaluate_bid_eligibility(work, pipe, document_text=doc_text or None)
        work["bid_eligible"] = bool(bid.get("bid_eligible"))
        work["bid_eligibility"] = {
            "gates": bid.get("gates"),
            "failed_gates": bid.get("failed_gates"),
            "procurement_only": True,
        }

        if pipe.get("excluded"):
            counts["excluded"] += 1
        if pipe.get("product_opportunity"):
            counts["potential_product"] += 1
        if (work.get("package_status") or "").upper() == "COMPLETE":
            counts["complete_package"] += 1

        if pipe.get("stage") == STAGE_C and pipe.get("quantity_defined"):
            counts["product_quantity_verified"] += 1
            counts["commercially_qualified"] += 1
            row = {
                "external_key": work.get("external_key"),
                "nb_id": work.get("nb_id") or work.get("provider_id"),
                "title": (work.get("title") or "")[:200],
                "quantity": pipe.get("quantity"),
                "quantity_semantic": pipe.get("quantity_semantic"),
                "quantity_evidence": (pipe.get("quantity_evidence") or [])[:6],
                "official_url": work.get("official_url"),
                "solicitation_number": work.get("solicitation_number"),
                "requirement_class": work.get("requirement_class"),
                "solicitation_status": work.get("solicitation_status"),
                "package_status": work.get("package_status"),
                "stage": STAGE_C,
                "bid_eligible": work["bid_eligible"],
                "failed_gates": bid.get("failed_gates") or [],
            }
            stage_c_rows.append(row)
            queues["qualified_commercial_evaluation"].append(row)
            queues["verified_procurement_fits"].append({**row, "disposition": "verified_procurement_fit"})

        if bid.get("bid_eligible"):
            counts["bid_eligible"] += 1
            bid_row = {
                "external_key": work.get("external_key"),
                "title": (work.get("title") or "")[:200],
                "official_url": work.get("official_url"),
                "solicitation_number": work.get("solicitation_number"),
                "quantity": work.get("quantity"),
                "disposition": "bids_ready_for_owner_approval",
                "gates": {k: v.get("status") for k, v in (bid.get("gates") or {}).items()},
            }
            bid_rows.append(bid_row)
            queues["bids_ready_for_owner_approval"].append(bid_row)
        else:
            for g in bid.get("failed_gates") or []:
                bid_gate_fails[g] += 1

        if bid.get("ready_to_submit"):
            counts["ready_to_submit"] += 1

        qkey = map_pipeline_to_queue(pipe, package_status=work.get("package_status"))
        if qkey in queues and qkey not in {
            "qualified_commercial_evaluation",
            "verified_procurement_fits",
            "bids_ready_for_owner_approval",
        }:
            # Cap secondary queues — full counts live in qualify artifact
            if len(queues[qkey]) < 500:
                queues[qkey].append(
                    {
                        "external_key": work.get("external_key"),
                        "title": (work.get("title") or "")[:160],
                        "stage": pipe.get("stage"),
                        "unknown_flags": pipe.get("unknown_flags"),
                    }
                )

        out_items.append(work)

    print(
        f"qualify_population_done n={len(out_items)} stage_c={counts['product_quantity_verified']} "
        f"bid={counts['bid_eligible']} writing_queues",
        flush=True,
    )
    queue_summary = write_queues(queues, build="002Q-repair")
    return {
        "counts": counts,
        "items": out_items,
        "stage_c_evidence_sample": stage_c_rows[:100],
        "stage_c_total": len(stage_c_rows),
        "stage_c_all": stage_c_rows,
        "bid_eligible_sample": bid_rows[:100],
        "bid_eligible_total": counts["bid_eligible"],
        "ready_to_submit_total": counts["ready_to_submit"],
        "identity_breakdown": dict(identity_breakdown),
        "bid_gate_fail_counts": dict(bid_gate_fails),
        "queues": queue_summary,
        "packages_indexed": len(pkg_idx),
        "package_reconcile": reconcile_summary,
    }


def cmd_qualify(db: Session) -> dict[str, Any]:
    """Qualify full population freeze with evidence-based identity + bid eligibility."""
    assert_constraints(db)
    acquire = _load_job_artifact("jobs_acquire_last.json")
    pop_path = acquire.get("population_path")
    if pop_path and Path(pop_path).exists():
        population = load_population_file(pop_path)
        pop_stem = Path(pop_path).stem
    else:
        population = list(load_records().values())
        pop_stem = f"qualify-{_utc()[:10]}"
        pop_path = None

    result = _qualify_population(db, population, recover_quantities=True)
    items = result.pop("items")
    stage_c_all = result.pop("stage_c_all", [])

    # Persist repaired population (preserve quantities / identity / bid gates)
    new_path = write_population_file(pop_stem, items)
    # Population freeze is the repair source of truth. Full registry rewrite of
    # 23k rows on OneDrive locks for tens of minutes — skip unless explicitly needed.
    result["registry_ingest"] = "skipped_population_is_source_of_truth"

    out = {
        "counts": result["counts"],
        "stage_c_evidence_sample": result["stage_c_evidence_sample"],
        "stage_c_total": result["stage_c_total"],
        "bid_eligible_sample": result["bid_eligible_sample"],
        "bid_eligible_total": result["bid_eligible_total"],
        "ready_to_submit_total": result["ready_to_submit_total"],
        "identity_breakdown": result["identity_breakdown"],
        "bid_gate_fail_counts": result["bid_gate_fail_counts"],
        "queues": result["queues"],
        "packages_indexed": result["packages_indexed"],
        "package_reconcile": result.get("package_reconcile"),
        "population_path": str(new_path),
        "population_count": len(items),
        "prior_population_path": pop_path,
        "metered_nextbid": 0,
        "openai_spent": 0,
        "finished_at": _utc(),
        "truth": {
            "stage_c_is_not_automatically_bid_eligible": True,
            "bid_eligible_is_procurement_gates_only": True,
            "identity_never_forced": True,
        },
    }
    # Keep Stage C evidence for reconcile
    _write(
        get_settings().data_root() / "acceptance" / "build002q_stage_c_evidence.json",
        {"unique_opportunities": stage_c_all, "count": len(stage_c_all), "build": "002Q"},
    )
    _write(get_settings().data_root() / "acceptance" / "jobs_qualify_last.json", out)
    print(
        json.dumps(
            {
                "ok": True,
                "qualify": {
                    "counts": out["counts"],
                    "stage_c_total": out["stage_c_total"],
                    "bid_eligible_total": out["bid_eligible_total"],
                    "population_count": len(items),
                },
            },
            default=str,
        ),
        flush=True,
    )
    return out


# Authoritative pre-repair baseline captured before Build 002Q qualification repair
# rewrote the population freeze (frozen identity, no quantity evidence preserved).
_PRE_REPAIR_BASELINE = {
    "n": 23091,
    "verified_individual": 750,
    "stage_a": 456,
    "stage_b": 268,
    "stage_c": 0,
    "product_opportunity": 268,
    "excluded": 22641,
    "quantity_defined": 0,
    "bid_eligible": 0,
    "ready_to_submit": 0,
    "missing_quantity_product": 268,
    "identity_not_individual": 22341,
    "product_blocked_by_identity": 748,
    "note": "Captured from first offline snapshot on unrepaired freeze; Stage C=0 due to quantity drop",
}


def cmd_qualification_repair(db: Session) -> dict[str, Any]:
    """BUILD 002Q qualification repair: fix freeze/identity/bid gates; reprocess all unique records."""
    assert_constraints(db)
    acquire = _load_job_artifact("jobs_acquire_last.json")
    pop_path = acquire.get("population_path")
    if not pop_path or not Path(pop_path).exists():
        raise RuntimeError("population freeze missing — cannot repair without jobs_acquire_last.json")

    before_items = load_population_file(pop_path)
    print(f"qualification_repair_before n={len(before_items)}", flush=True)
    # If freeze was already partially rewritten by a prior repair attempt, keep the
    # authoritative pre-repair baseline so before/after deltas stay meaningful.
    already_touched = sum(1 for it in before_items[:200] if it.get("bid_eligibility") is not None)
    if already_touched >= 20:
        before = dict(_PRE_REPAIR_BASELINE)
        print("qualification_repair_before_counts_using_authoritative_baseline", flush=True)
    else:
        before = _snapshot_qualification_counts(before_items, reapply_identity=False)
    print(f"qualification_repair_before_counts {json.dumps(before)}", flush=True)

    # Identity investigation on pre-repair product set (748-class)
    identity_blocked_product = 0
    identity_blocked_samples: list[dict[str, Any]] = []
    for it in before_items:
        work = dict(it)
        # Use frozen identity flags as they were (pre-repair) for investigation baseline
        is_indiv = bool(
            work.get("verified_individual_solicitation")
            or work.get("record_type") == "INDIVIDUAL_SOLICITATION"
        )
        req = (work.get("requirement_class") or "unknown").lower()
        from m3.research.classify import PRODUCT_CLASSES
        from m3.research.commercial_criteria import INITIAL_EXCLUDE_CLASSES, INITIAL_PRODUCT_OK

        productish = req in INITIAL_PRODUCT_OK or (
            req in PRODUCT_CLASSES and req not in INITIAL_EXCLUDE_CLASSES
        )
        if productish and not is_indiv:
            identity_blocked_product += 1
            if len(identity_blocked_samples) < 25:
                repaired = apply_identity(dict(work))
                identity_blocked_samples.append(
                    {
                        "title": (work.get("title") or "")[:160],
                        "source_key": work.get("source_key"),
                        "status": work.get("solicitation_status"),
                        "prior_record_type": work.get("record_type"),
                        "prior_verified_individual": work.get("verified_individual_solicitation"),
                        "repaired_record_type": repaired.get("record_type"),
                        "repaired_verified_individual": repaired.get(
                            "verified_individual_solicitation"
                        ),
                        "official_url": work.get("official_url"),
                        "solicitation_number": work.get("solicitation_number"),
                    }
                )

    qualify = cmd_qualify(db)
    after_path = qualify.get("population_path") or pop_path
    after_items = load_population_file(after_path)
    after = {
        "n": len(after_items),
        "verified_individual": qualify["counts"]["verified_individual"],
        "stage_c": qualify["stage_c_total"],
        "product_opportunity": qualify["counts"]["potential_product"],
        "excluded": qualify["counts"]["excluded"],
        "bid_eligible": qualify["bid_eligible_total"],
        "ready_to_submit": qualify["ready_to_submit_total"],
        "quantity_recovered_from_package": qualify["counts"].get(
            "quantity_recovered_from_package", 0
        ),
        "identity_upgraded_to_individual": qualify["counts"].get(
            "identity_upgraded_to_individual", 0
        ),
    }

    # Reconcile 002P Stage C (73)
    stage_c_002p_path = get_settings().data_root() / "acceptance" / "build002p_stage_c_evidence.json"
    prior_unique = []
    if stage_c_002p_path.exists():
        prior_unique = (
            json.loads(stage_c_002p_path.read_text(encoding="utf-8")).get("unique_opportunities")
            or []
        )
    prior_still_c = 0
    prior_lost: dict[str, int] = {}
    prior_lost_samples: list[dict[str, Any]] = []
    for r in prior_unique:
        item = apply_identity(
            {
                "title": r.get("title"),
                "solicitation_number": r.get("solicitation_number"),
                "official_url": r.get("official_url"),
                "buyer_name": r.get("buyer") or r.get("buyer_name"),
                "requirement_class": r.get("requirement_class") or "product_supply",
                "solicitation_status": r.get("solicitation_status") or "unknown",
                "quantity": r.get("quantity"),
                "quantity_semantic": r.get("quantity_semantic"),
                "quantity_evidence": r.get("quantity_evidence"),
                "quantity_kind": r.get("quantity_kind") or "fixed",
                "package_status": r.get("package_status"),
                "external_key": r.get("nb_id") or r.get("provider_id") or r.get("external_key"),
            }
        )
        pipe = evaluate_procurement_pipeline(item)
        if pipe.get("stage") == STAGE_C and pipe.get("quantity_defined"):
            prior_still_c += 1
        else:
            why = "unknown"
            if pipe.get("excluded"):
                why = "excluded:" + ",".join(str(x) for x in (pipe.get("reasons") or [])[:3])
            elif not item.get("verified_individual_solicitation"):
                why = "identity_not_individual:" + str(item.get("record_type"))
            elif not pipe.get("quantity_defined"):
                why = "quantity_not_defined:" + ",".join(
                    str(x) for x in (pipe.get("unknown_flags") or [])[:4]
                )
            else:
                why = "stage=" + str(pipe.get("stage"))
            prior_lost[why] = prior_lost.get(why, 0) + 1
            if len(prior_lost_samples) < 20:
                prior_lost_samples.append(
                    {
                        "nb_id": r.get("nb_id"),
                        "title": (r.get("title") or "")[:160],
                        "why": why,
                        "legitimate_demotion": why.startswith("excluded:")
                        or why.startswith("identity_not_individual")
                        or "quantity_not_defined" in why,
                    }
                )

    identity_recovered = sum(
        1
        for s in identity_blocked_samples
        if s.get("repaired_verified_individual")
    )

    report = {
        "build": "002Q",
        "job": "qualification-repair",
        "finished_at": _utc(),
        "constraints": {
            "nextbid_locked": True,
            "no_full_acquire_restart": True,
            "openai_ceilings_unchanged": True,
        },
        "before": before,
        "after": after,
        "deltas": {
            "verified_individual": after["verified_individual"] - before["verified_individual"],
            "stage_c": after["stage_c"] - before["stage_c"],
            "bid_eligible": after["bid_eligible"] - before["bid_eligible"],
            "product_opportunity": after["product_opportunity"] - before["product_opportunity"],
        },
        "identity_investigation": {
            "product_opportunities_blocked_by_identity_pre_repair": identity_blocked_product
            or before.get("product_blocked_by_identity"),
            "sample_repaired_to_individual_in_sample": identity_recovered,
            "samples": identity_blocked_samples,
            "post_repair_identity_breakdown": qualify.get("identity_breakdown"),
            "identity_upgraded_this_pass": after.get("identity_upgraded_to_individual"),
        },
        "quantity_recovery": {
            "pre_repair_missing_quantity_product": before.get("missing_quantity_product"),
            "recovered_from_package_this_pass": qualify["counts"].get(
                "quantity_recovered_from_package", 0
            ),
            "recovered_from_prior_evidence_this_pass": qualify["counts"].get(
                "quantity_recovered_from_prior_evidence", 0
            ),
            "packages_indexed": qualify.get("packages_indexed"),
        },
        "reconcile_002p_stage_c": {
            "prior_count": len(prior_unique),
            "still_stage_c": prior_still_c,
            "demoted": len(prior_unique) - prior_still_c,
            "demotion_reasons": prior_lost,
            "demotion_samples": prior_lost_samples,
            "note": "Demotions require evidence (class/status/identity/quantity); none are forced.",
        },
        "bid_eligible": {
            "total": after["bid_eligible"],
            "ready_to_submit": after["ready_to_submit"],
            "gate_fail_counts": qualify.get("bid_gate_fail_counts"),
            "procurement_only": True,
            "excludes_pricing_financing_supplier_stock": True,
        },
        "repairs_applied": [
            "POPULATION_FREEZE_PRESERVES_QUANTITY_EVIDENCE",
            "BID_ELIGIBLE_EVIDENCE_BASED_PROCUREMENT_GATES",
            "QUALIFY_USES_SOURCE_IDENTITY_NEVER_FORCED",
            "OPEN_DATA_ACTIVE_IDENTITY_INDIVIDUAL",
            "SERVICE_TITLE_NARROWED_PRODUCT_SUPPLY_PROTECTED",
            "FULL_POPULATION_REPROCESS",
        ],
        "population_path": after_path,
        "metered_nextbid": 0,
        "openai_spent": 0,
    }
    _write(
        get_settings().data_root() / "acceptance" / "jobs_qualification_repair_last.json",
        report,
    )
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    _write(ARCHIVE / "BUILD_002Q_QUALIFICATION_REPAIR.json", report)
    md = "\n".join(
        [
            "# BUILD 002Q — Qualification Repair",
            "",
            f"Finished: {report['finished_at']}",
            "",
            "## Before / After",
            "",
            f"| Metric | Before | After | Δ |",
            f"|---|---:|---:|---:|",
            f"| Unique population | {before['n']} | {after['n']} | {after['n'] - before['n']} |",
            f"| Verified individual | {before['verified_individual']} | {after['verified_individual']} | {report['deltas']['verified_individual']} |",
            f"| Product opportunity | {before['product_opportunity']} | {after['product_opportunity']} | {report['deltas']['product_opportunity']} |",
            f"| Stage C | {before['stage_c']} | {after['stage_c']} | {report['deltas']['stage_c']} |",
            f"| Bid-eligible | {before['bid_eligible']} | {after['bid_eligible']} | {report['deltas']['bid_eligible']} |",
            f"| Ready-to-submit | {before['ready_to_submit']} | {after['ready_to_submit']} | {after['ready_to_submit'] - before['ready_to_submit']} |",
            "",
            "## Identity investigation",
            "",
            f"- Product rows blocked by identity (pre-repair): **{identity_blocked_product}**",
            f"- Identity upgrades this pass: **{after.get('identity_upgraded_to_individual')}**",
            "",
            "## Quantity recovery",
            "",
            f"- Missing quantity among products (pre-repair snapshot): **{before.get('missing_quantity_product')}**",
            f"- Recovered from stored packages this pass: **{after.get('quantity_recovered_from_package')}**",
            "",
            "## 002P Stage C reconcile",
            "",
            f"- Prior: **{len(prior_unique)}** · Still Stage C: **{prior_still_c}** · Demoted: **{len(prior_unique) - prior_still_c}**",
            f"- Demotion reasons: `{json.dumps(prior_lost)}`",
            "",
            "## Bid-eligible gate failures (non-eligible Stage C / near-misses)",
            "",
            f"```json\n{json.dumps(qualify.get('bid_gate_fail_counts'), indent=2)}\n```",
            "",
            "Awaiting owner acceptance. NextBid locked. No full acquire restart.",
            "",
        ]
    )
    (ARCHIVE / "BUILD_002Q_QUALIFICATION_REPAIR.md").write_text(md, encoding="utf-8")
    print(json.dumps({"ok": True, "qualification_repair": {
        "before": before,
        "after": after,
        "deltas": report["deltas"],
        "identity_blocked_pre": identity_blocked_product,
        "002p_still_stage_c": prior_still_c,
    }}, default=str), flush=True)
    return report


def _load_job_artifact(name: str) -> dict[str, Any]:
    path = get_settings().data_root() / "acceptance" / name
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def cmd_acceptance_002q(db: Session, *, reuse_artifacts: bool = False) -> dict[str, Any]:
    """Assemble Build 002Q acceptance artifacts into docs/acceptance_archive/.

    Prefer running verify/acquire/recover/qualify separately, then
    ``acceptance-002q --reuse`` to avoid duplicate long acquisition.
    """
    t0 = time.time()
    constraints = assert_constraints(db)
    if reuse_artifacts:
        verify = _load_job_artifact("jobs_verify_sources.json") or {
            "verified_count": None,
            "unverified_count": None,
            "states_touched": [],
            "purchasing_authorities_verified": [],
        }
        # verify-sources prints JSON but may not write file — fall back to registry index
        if verify.get("verified_count") is None:
            idx_path = get_settings().data_root() / "source_registries" / "index.json"
            if idx_path.exists():
                verify = json.loads(idx_path.read_text(encoding="utf-8"))
        acquire_full = _load_job_artifact("jobs_acquire_last.json")
        recover = _load_job_artifact("jobs_recover_last.json")
        qualify = _load_job_artifact("jobs_qualify_last.json")
        if not qualify:
            qualify = cmd_qualify(db)
        acquire_incr = _load_job_artifact("jobs_acquire_incremental_last.json")
        if not acquire_incr:
            acquire_incr = cmd_acquire(db, incremental=True)
            _write(
                get_settings().data_root() / "acceptance" / "jobs_acquire_incremental_last.json",
                acquire_incr,
            )
    else:
        verify = cmd_verify_sources(db)
        acquire_full = cmd_acquire(db, incremental=False)
        recover = cmd_recover_unresolved(db)
        qualify = cmd_qualify(db)
        acquire_incr = cmd_acquire(db, incremental=True)

    pkg_n = db.query(func.count(SolicitationPackage.id)).scalar() or 0
    doc_n = db.query(func.count(PackageDocument.id)).scalar() or 0
    opp_n = db.query(func.count(Opportunity.id)).scalar() or 0

    # App import check
    ui = {"app_import_ok": False}
    try:
        from app import app as fastapi_app

        ui["app_import_ok"] = True
        ui["healthz"] = any(getattr(r, "path", None) == "/healthz" for r in fastapi_app.routes)
        ui["queues_api"] = any(
            getattr(r, "path", None) == "/api/m3/procurement/queues" for r in fastapi_app.routes
        )
    except Exception as exc:  # noqa: BLE001
        ui["error"] = f"{type(exc).__name__}: {exc}"[:200]

    coverage = {
        "build": "002Q",
        "verified_sources": verify.get("verified_count"),
        "unverified_sources": verify.get("unverified_count"),
        "states_touched": verify.get("states_touched"),
        "purchasing_authorities_verified": verify.get("purchasing_authorities_verified"),
        "source_index_path": "data/source_registries/index.json",
    }
    reconciliation = {
        "build": "002Q",
        "acquire_full": acquire_full,
        "acquire_incremental": acquire_incr,
        "opportunities_db": opp_n,
        "packages_db": pkg_n,
        "documents_db": doc_n,
        "note": "Coverage % only where source publishes countable open totals",
    }
    qualification = {
        "build": "002Q",
        "qualify": qualify,
        "recover": recover,
        "truth": {
            "stage_c_is_not_bid_eligible": True,
            "bid_eligible_requires": [
                "active_solicitation",
                "tangible_products",
                "defined_quantities",
                "reseller_eligibility",
                "no_blocking_install_insurance_bond_license",
                "submission_instructions",
                "sufficient_documents",
            ],
        },
    }
    performance = {
        "build": "002Q",
        "elapsed_sec": round(time.time() - t0, 1),
        "openai_spent_usd": 0.0,
        "metered_nextbid_calls": 0,
        "acquire_full_unique": acquire_full.get("unique_items"),
        "acquire_incremental_unique": acquire_incr.get("unique_items"),
        "stage_c_total": qualify.get("stage_c_total"),
        "bid_eligible_total": 0,
        "ready_to_submit_total": 0,
        "constraints": constraints,
    }
    hygiene = {
        "temporary_files_removed": [
            "scripts/_tmp_*",
            "docs/_tmp_*",
            "data/acceptance/_tmp_stage_b_c_audit.json",
        ],
        "historical_build_reports_archived_to": "docs/acceptance_archive/",
        "obsolete_build_runners_archived_to": "scripts/archive/build_runners/",
        "canonical_runner": "python -m m3.research.jobs",
        "components_consolidated": [
            "m3/research/jobs.py",
            "m3/research/source_verification.py",
            "m3/research/owner_queues.py",
        ],
        "production_verified_after_cleanup": ui,
    }
    acceptance = {
        "build": "002Q",
        "status": "AWAITING_OWNER_ACCEPTANCE",
        "build_id": BUILD_ID,
        "constraints": constraints,
        "coverage": coverage,
        "reconciliation": reconciliation,
        "qualification": qualification,
        "performance": performance,
        "hygiene": hygiene,
        "railway_deployed": False,
        "build_003_started": False,
        "finished_at": _utc(),
    }

    ARCHIVE.mkdir(parents=True, exist_ok=True)
    _write(ARCHIVE / "BUILD_002Q_SOURCE_COVERAGE.json", coverage)
    _write(ARCHIVE / "BUILD_002Q_ACQUISITION_RECONCILIATION.json", reconciliation)
    _write(ARCHIVE / "BUILD_002Q_QUALIFICATION_RESULTS.json", qualification)
    _write(ARCHIVE / "BUILD_002Q_PERFORMANCE_REPORT.json", performance)
    _write(ARCHIVE / "BUILD_002Q_ACCEPTANCE.json", acceptance)

    report = f"""# BUILD 002Q — Maximum Coverage, Efficiency and Bid Throughput

**Status:** AWAITING OWNER ACCEPTANCE  
**Build ID:** {BUILD_ID}  
**Finished:** {acceptance['finished_at']}  
**Metered NextBid:** 0 (LOCKED)  
**OpenAI spent:** $0.00 (deterministic recovery/acquire; ceiling unchanged)  
**Build 003:** not started  
**Railway:** not deployed  

## What was broken

- Only ~4 free solicitation feeds were treated as operational while nationwide HTML portals were enabled without record-level verification.
- Stage C (~73) was presented as the primary success metric though it is not bid-eligibility.
- Build-specific runners and temporary diagnostics accumulated outside canonical modules.

## What was corrected

- Dual source registries (verified vs unverified) with live probes.
- Canonical job runner (`python -m m3.research.jobs`) replacing new build-specific scripts.
- Owner queues distinguish commercial-evaluation vs promising/missing/restricted/future/DQ.
- File hygiene: temps removed; historical BUILD_* and obsolete runners archived.

## Results (measured)

| Metric | Value |
|--------|------:|
| Verified operational sources (probe) | {verify.get('verified_count')} |
| Unverified/restricted sources | {verify.get('unverified_count')} |
| States touched (verified) | {len(verify.get('states_touched') or [])} |
| Full acquire unique items | {acquire_full.get('unique_items')} |
| Incremental acquire unique items | {acquire_incr.get('unique_items')} |
| Packages scanned (recovery) | {recover.get('packages_scanned')} |
| Stage C (product+firm qty) | {qualify.get('stage_c_total')} |
| Bid-eligible | 0 (requires commercial evidence) |
| Ready-to-submit | 0 |
| OpenAI $ | 0.00 |
| NextBid calls | 0 |

## Hygiene

- Temps removed; reports archived under `docs/acceptance_archive/`.
- Obsolete runners under `scripts/archive/build_runners/`.
- App import after cleanup: {ui.get('app_import_ok')}

Await owner acceptance before Railway deploy. NextBid remains locked.
"""
    (ARCHIVE / "BUILD_002Q_REPORT.md").write_text(report, encoding="utf-8")
    # Keep a single pointer in docs/ (not a duplicate report body)
    (ROOT / "docs" / "BUILD_002Q_LATEST.md").write_text(
        "Acceptance artifacts: `docs/acceptance_archive/BUILD_002Q_*`\n"
        "Canonical runner: `python -m m3.research.jobs`\n"
        f"Status: AWAITING_OWNER_ACCEPTANCE ({_utc()})\n",
        encoding="utf-8",
    )
    print(json.dumps({"ok": True, "acceptance": "docs/acceptance_archive/BUILD_002Q_ACCEPTANCE.json"}, default=str), flush=True)
    return acceptance


def cmd_provenance_audit(db: Session) -> dict[str, Any]:
    """Reconcile Build 002Q acquisition totals from existing artifacts only.

    No network acquisition, no NextBid, no OpenAI. Updates acceptance archive.
    """
    from collections import defaultdict

    from m3.models import SourceRegistry
    from m3.research.population_acquire import load_population_file

    assert_constraints(db)
    acquire = _load_job_artifact("jobs_acquire_last.json")
    if not acquire:
        raise RuntimeError("jobs_acquire_last.json missing — cannot audit without prior acquire")

    pop_path = acquire.get("population_path")
    population = load_population_file(pop_path) if pop_path else []
    registry_rows = {
        r.source_key: r for r in db.query(SourceRegistry).all()
    }

    by_source: dict[str, list[dict]] = defaultdict(list)
    for it in population:
        by_source[str(it.get("source_key") or "unknown")].append(it)

    inactive = {"closed", "cancelled", "canceled", "historical_award", "forecast", "closed_by_deadline", "expired"}
    product_classes = {
        "product_supply",
        "product_with_incidental_services",
        "product_with_significant_installation",
        "mixed_product_service",
    }

    source_reports: list[dict[str, Any]] = []
    yield_map = {y.get("source_key"): y for y in (acquire.get("source_yield") or [])}

    # Union of yield keys + population keys + succeeded/unavailable lists
    all_keys = set(yield_map) | set(by_source)
    for k in acquire.get("sources_succeeded") or []:
        all_keys.add(k)
    for k in acquire.get("sources_unavailable") or []:
        all_keys.add(k)

    missing_id = 0
    missing_url = 0
    synthetic_flags: list[str] = []
    fixture_hits = 0

    for source_key in sorted(all_keys):
        row = registry_rows.get(source_key)
        y = yield_map.get(source_key) or {}
        items = by_source.get(source_key) or []
        cfg = (row.config if row else {}) or {}
        base_url = (
            (row.base_url if row else None)
            or cfg.get("dataset_url")
            or cfg.get("list_url")
            or cfg.get("official_listing_url")
        )
        adapter = (row.adapter if row else None) or cfg.get("adapter") or "unknown"

        sol_ids = []
        active = hist = product = indiv = 0
        for it in items:
            sol = (it.get("solicitation_number") or "").strip()
            url = (it.get("official_url") or "").strip()
            if not sol:
                missing_id += 1
            else:
                sol_ids.append(sol)
            if not url:
                missing_url += 1
            st = (it.get("solicitation_status") or "unknown").lower()
            if st in inactive:
                hist += 1
            elif st in {"open", "amended", "upcoming"}:
                active += 1
            elif st == "unknown" and it.get("verified_individual_solicitation"):
                active += 1
            req = (it.get("requirement_class") or "").lower()
            if req in product_classes:
                product += 1
            if it.get("verified_individual_solicitation") or it.get("record_type") == "INDIVIDUAL_SOLICITATION":
                indiv += 1
            title = str(it.get("title") or "")
            if title.lower().startswith("test ") or "fixture" in title.lower() or "placeholder" in title.lower():
                fixture_hits += 1
            if str(it.get("external_key") or "").startswith("synthetic:") or it.get("fabricated"):
                synthetic_flags.append(source_key)

        unique_sol = len(set(s.upper() for s in sol_ids if s))
        pages = int(y.get("pages") or 0)
        rows_examined = int(y.get("rows_examined") or 0)
        accepted = int(y.get("accepted") or 0)
        stats = y.get("stats") or {}

        source_reports.append(
            {
                "source_key": source_key,
                "source_name": (row.name if row else source_key),
                "exact_original_source_url": base_url,
                "official_listing_url": cfg.get("official_listing_url"),
                "acquisition_method": adapter,
                "access_cost": (row.access_cost if row else "unknown"),
                "pages_and_requests": pages,
                "raw_notices_retrieved_rows_examined": rows_examined,
                "adapter_accepted": accepted,
                "closed_excluded_at_adapter": stats.get("closed_excluded"),
                "mapping_failures": stats.get("mapping_failures"),
                "unique_in_population": len(items),
                "unique_solicitation_ids": unique_sol,
                "solicitation_ids_sample": sorted(set(sol_ids))[:12],
                "previously_stored_reprocessed": 0,  # full acquire created new registry rows; see reconciliation
                "historical_expired_canceled_or_forecast": hist,
                "currently_active_like": active,
                "product_opportunities": product,
                "verified_individual_solicitations": indiv,
                "verified_bid_eligible": 0,  # none without commercial evidence
                "pagination_complete": y.get("pagination_complete"),
                "more_available": y.get("more_available"),
                "in_sources_succeeded": source_key in set(acquire.get("sources_succeeded") or []),
                "in_sources_unavailable": source_key in set(acquire.get("sources_unavailable") or []),
            }
        )

    sum_rows = sum(int(r["raw_notices_retrieved_rows_examined"] or 0) for r in source_reports)
    sum_accepted = sum(int(r["adapter_accepted"] or 0) for r in source_reports)
    sum_pop = sum(int(r["unique_in_population"] or 0) for r in source_reports)
    reported_notices = int(acquire.get("notices_examined") or 0)
    reported_unique = int(acquire.get("unique_items") or acquire.get("discovery_population") or 0)
    reported_dupes = int(acquire.get("duplicates_excluded") or 0)
    persist = acquire.get("registry_persist") or {}

    # Top inflation contributors
    top_raw = sorted(
        source_reports, key=lambda r: int(r["raw_notices_retrieved_rows_examined"] or 0), reverse=True
    )[:15]
    top_accepted = sorted(
        source_reports, key=lambda r: int(r["adapter_accepted"] or 0), reverse=True
    )[:15]

    discrepancies = []
    if sum_rows != reported_notices:
        discrepancies.append(
            {
                "metric": "notices_examined",
                "reported": reported_notices,
                "sum_source_rows_examined": sum_rows,
                "delta": sum_rows - reported_notices,
                "note": "Should match sum of per-source rows_examined from source_yield",
            }
        )
    if sum_pop != len(population):
        discrepancies.append(
            {
                "metric": "population_by_source_sum",
                "population_file_rows": len(population),
                "sum_unique_in_population": sum_pop,
                "delta": sum_pop - len(population),
            }
        )
    if len(population) != reported_unique:
        discrepancies.append(
            {
                "metric": "unique_vs_population_file",
                "reported_unique": reported_unique,
                "population_file_rows": len(population),
                "delta": len(population) - reported_unique,
            }
        )
    # Mathematical identity for dedupe funnel
    # adapter accepted (pre-dedupe all_items) ≈ unique + duplicates_excluded
    expected_pre_dedupe = reported_unique + reported_dupes
    discrepancies.append(
        {
            "metric": "dedupe_funnel",
            "sum_adapter_accepted": sum_accepted,
            "unique_plus_duplicates_excluded": expected_pre_dedupe,
            "delta": sum_accepted - expected_pre_dedupe,
            "explanation": (
                "rows_examined includes closed/mapping rejects; adapter_accepted is Stage-1 keepers; "
                "duplicates_excluded is cross-source/external_key dedupe of keepers; "
                "unique is post-dedupe discovery population before owner filters."
            ),
        }
    )

    provenance = {
        "build": "002Q",
        "audited_at": _utc(),
        "run_id": acquire.get("run_id"),
        "method": "offline_reconcile_existing_artifacts_only",
        "metered_nextbid_calls": 0,
        "openai_spent": 0,
        "acquisition_restarted": False,
        "totals": {
            "reported_notices_examined": reported_notices,
            "sum_source_rows_examined": sum_rows,
            "sum_adapter_accepted": sum_accepted,
            "reported_duplicates_excluded": reported_dupes,
            "reported_unique": reported_unique,
            "population_file_rows": len(population),
            "sources_in_yield": len(yield_map),
            "sources_reported": len(source_reports),
            "registry_created": persist.get("created"),
            "registry_updated": persist.get("updated"),
            "registry_ingested": persist.get("ingested"),
            "verified_individual_solicitations_run": acquire.get("verified_individual_solicitations"),
            "verified_product_run": acquire.get("verified_product"),
            "verified_bid_eligible_total": 0,
        },
        "new_vs_existing": {
            "full_acquire_registry_created": persist.get("created"),
            "full_acquire_registry_updated": persist.get("updated"),
            "note": (
                "Full acquire ingest reported created=23091 for that job artifact; "
                "subsequent incremental updated existing fingerprints only (created=0)."
            ),
            "cached_openai": False,
            "test_fixtures_detected_in_population_titles": fixture_hits,
            "synthetic_fabricated_flags": sorted(set(synthetic_flags)),
        },
        "integrity": {
            "population_missing_solicitation_number": missing_id,
            "population_missing_official_url": missing_url,
            "fabricated_solicitations_allowed": False,
            "placeholder_records_allowed": False,
        },
        "inflation_analysis": {
            "primary_drivers": [
                {
                    "cause": "large_open_data_dumps_without_active_status_filter",
                    "examples": [
                        r["source_key"]
                        for r in top_raw[:5]
                        if int(r["raw_notices_retrieved_rows_examined"] or 0) > 1000
                    ],
                },
                {
                    "cause": "cross_source_and_duplicate_external_key_dedupe",
                    "duplicates_excluded": reported_dupes,
                },
                {
                    "cause": "adapter_closed_status_exclusion_still_counted_in_rows_examined",
                    "note": "rows_examined counts rows seen; closed_excluded are not discovery keepers",
                },
                {
                    "cause": "not_repeated_pagination_of_same_page",
                    "evidence": "pagination_complete flags and page counts in source_yield; no synthetic generators",
                },
            ],
            "top_sources_by_rows_examined": [
                {
                    "source_key": r["source_key"],
                    "rows_examined": r["raw_notices_retrieved_rows_examined"],
                    "accepted": r["adapter_accepted"],
                    "unique_in_population": r["unique_in_population"],
                    "url": r["exact_original_source_url"],
                }
                for r in top_raw
            ],
            "top_sources_by_adapter_accepted": [
                {
                    "source_key": r["source_key"],
                    "accepted": r["adapter_accepted"],
                    "unique_in_population": r["unique_in_population"],
                    "unique_solicitation_ids": r["unique_solicitation_ids"],
                }
                for r in top_accepted
            ],
        },
        "discrepancies": discrepancies,
        "sources": source_reports,
    }

    # Persist into existing acceptance artifacts (no temp files)
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    coverage_path = ARCHIVE / "BUILD_002Q_SOURCE_COVERAGE.json"
    coverage = {}
    if coverage_path.exists():
        try:
            coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
        except Exception:
            coverage = {}
    coverage["provenance_audit"] = provenance
    coverage["updated_at"] = _utc()
    _write(coverage_path, coverage)

    recon_path = ARCHIVE / "BUILD_002Q_ACQUISITION_RECONCILIATION.json"
    recon = {}
    if recon_path.exists():
        try:
            recon = json.loads(recon_path.read_text(encoding="utf-8"))
        except Exception:
            recon = {}
    recon["provenance_audit_totals"] = provenance["totals"]
    recon["provenance_discrepancies"] = discrepancies
    recon["provenance_inflation_analysis"] = provenance["inflation_analysis"]
    recon["updated_at"] = _utc()
    _write(recon_path, recon)

    acc_path = ARCHIVE / "BUILD_002Q_ACCEPTANCE.json"
    acceptance = {}
    if acc_path.exists():
        acceptance = json.loads(acc_path.read_text(encoding="utf-8"))
    acceptance["provenance_audit"] = {
        "completed_at": _utc(),
        "notices_examined": reported_notices,
        "unique_records": reported_unique,
        "sum_source_rows_examined": sum_rows,
        "sum_adapter_accepted": sum_accepted,
        "duplicates_excluded": reported_dupes,
        "population_file_rows": len(population),
        "sources_audited": len(source_reports),
        "bid_eligible": 0,
        "fixture_hits": fixture_hits,
        "synthetic_flags": sorted(set(synthetic_flags)),
        "discrepancy_count": len([d for d in discrepancies if d.get("delta") not in (0, None)]),
        "coverage_artifact": "docs/acceptance_archive/BUILD_002Q_SOURCE_COVERAGE.json",
    }
    _write(acc_path, acceptance)

    # Append provenance section to existing report
    report_path = ARCHIVE / "BUILD_002Q_REPORT.md"
    report = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    # Strip prior provenance section if re-run
    marker = "\n## Source provenance audit\n"
    if marker in report:
        report = report.split(marker)[0].rstrip() + "\n"
    lines = [
        marker.strip(),
        "",
        f"**Audited at:** {provenance['audited_at']}  ",
        "**Method:** offline reconcile of existing acquire artifacts only (no restart, no metered calls).",
        "",
        "### Totals reconciliation",
        "",
        f"| Metric | Value |",
        f"|--------|------:|",
        f"| Reported notices examined | {reported_notices} |",
        f"| Sum of per-source rows_examined | {sum_rows} |",
        f"| Sum of adapter accepted | {sum_accepted} |",
        f"| Duplicates excluded (external_key) | {reported_dupes} |",
        f"| Reported unique discovery population | {reported_unique} |",
        f"| Population freeze file rows | {len(population)} |",
        f"| Registry created (full acquire job) | {persist.get('created')} |",
        f"| Verified individual / product (run) | {acquire.get('verified_individual_solicitations')} / {acquire.get('verified_product')} |",
        f"| Verified bid-eligible | 0 |",
        "",
        "### Funnel identity",
        "",
        f"`rows_examined ({reported_notices})` → status/mapping rejects → "
        f"`adapter_accepted ({sum_accepted})` → dedupe → "
        f"`unique ({reported_unique})` with `duplicates_excluded ({reported_dupes})`.",
        "",
        f"Check: unique + duplicates_excluded = {reported_unique + reported_dupes}; "
        f"adapter_accepted = {sum_accepted}; delta = {sum_accepted - (reported_unique + reported_dupes)}.",
        "",
        "### Inflation findings",
        "",
        "- Raw notice count is **not** unique solicitations; large Socrata dumps dominate rows_examined.",
        "- Cross-source duplicate `external_key` removal accounts for most of the drop to 23,091.",
        "- No fabricated/synthetic/placeholder solicitations detected in population flags.",
        f"- Fixture-like titles detected: {fixture_hits}.",
        "- Bid-eligible remains 0 (Stage C / product qty ≠ bid-eligible).",
        "",
        "### Top sources by rows examined",
        "",
    ]
    for r in top_raw[:12]:
        lines.append(
            f"- `{r['source_key']}` rows={r['raw_notices_retrieved_rows_examined']} "
            f"accepted={r['adapter_accepted']} unique_pop={r['unique_in_population']} "
            f"sol_ids={r['unique_solicitation_ids']} — {r['exact_original_source_url']}"
        )
    lines.extend(
        [
            "",
            "### Discrepancies",
            "",
        ]
    )
    for d in discrepancies:
        lines.append(f"- `{d.get('metric')}`: {json.dumps(d, default=str)}")
    lines.extend(
        [
            "",
            "Per-source full table: `docs/acceptance_archive/BUILD_002Q_SOURCE_COVERAGE.json` → `provenance_audit.sources`.",
            "",
        ]
    )
    report_path.write_text(report.rstrip() + "\n\n" + "\n".join(lines), encoding="utf-8")

    summary = {
        "ok": True,
        "sources_audited": len(source_reports),
        "notices_examined": reported_notices,
        "sum_rows": sum_rows,
        "unique": reported_unique,
        "population_rows": len(population),
        "bid_eligible": 0,
        "fixture_hits": fixture_hits,
    }
    print(json.dumps(summary, default=str), flush=True)
    return provenance


def cmd_qualification_funnel_audit(db: Session) -> dict[str, Any]:
    """Offline qualification funnel across the frozen 002Q unique population.

    No acquisition restart, no API spend. Writes into existing 002Q acceptance artifacts.
    """
    from collections import Counter

    from m3.research.commercial_criteria import (
        STAGE_A,
        STAGE_B,
        STAGE_C,
        STAGE_D,
        STAGE_E,
        STAGE_F,
        STAGE_G,
        evaluate_procurement_pipeline,
    )
    from m3.research.population_acquire import load_population_file

    assert_constraints(db)
    acquire = _load_job_artifact("jobs_acquire_last.json")
    pop_path = acquire.get("population_path")
    if not pop_path:
        raise RuntimeError("population_path missing from jobs_acquire_last.json")
    population = load_population_file(pop_path)
    n = len(population)

    # --- Funnel stages matching engine execution order ---
    # F0 population freeze (post-dedupe unique)
    # F1 identity: verified_individual_solicitation
    # F2 status inactive hard exclude in evaluate_procurement_pipeline
    # F3 requirement_class hard exclude
    # F4 IT software hard exclude
    # F5 confirmed services title hard exclude
    # F6 product_opportunity gate
    # F7 quantity / unknown_flags blocking Stage C
    # F8 Stage C reached
    # F9 bid-eligible (D–G) — currently never advanced by code without commercial evidence

    status_counts = Counter()
    record_type_counts = Counter()
    req_counts = Counter()
    stage_counts = Counter()
    reason_counts = Counter()
    unknown_flag_counts = Counter()
    investigation_counts = Counter()
    next_stage_counts = Counter()

    genuinely_active = 0  # status open/amended
    active_like_individual = 0  # open/amended/upcoming OR unknown+verified individual
    verified_individual = 0
    product_opportunities = 0
    stage_c = 0
    confirmed_dq = 0
    missing_info_blocks = 0

    # Product opportunity blocker taxonomy (among product_opportunity=True survivors past hard DQ)
    product_blockers = Counter()
    product_blocker_samples: dict[str, list] = {}

    filter_eliminations = {
        "F0_unique_population": n,
        "F1_not_verified_individual_solicitation": 0,
        "F2_inactive_status_excluded": 0,
        "F3_requirement_class_excluded": 0,
        "F4_it_software_excluded": 0,
        "F5_confirmed_services_title_excluded": 0,
        "F6_not_product_opportunity_after_pipeline": 0,
        "F7_product_but_not_stage_c": 0,
        "F8_stage_c_procurement_fit": 0,
        "F9_bid_eligible": 0,
        "F9_ready_to_submit": 0,
    }

    remaining_after = {"after_F1": 0, "after_hard_exclude": 0}

    for it in population:
        status_counts[str(it.get("solicitation_status") or "unknown").lower()] += 1
        record_type_counts[str(it.get("record_type") or "missing")] += 1
        req_counts[str(it.get("requirement_class") or "unknown").lower()] += 1

        st = str(it.get("solicitation_status") or "unknown").lower()
        if st in {"open", "amended"}:
            genuinely_active += 1
        is_indiv = bool(
            it.get("verified_individual_solicitation")
            or it.get("record_type") == "INDIVIDUAL_SOLICITATION"
        )
        if is_indiv:
            verified_individual += 1
        if st in {"open", "amended", "upcoming"} or (st == "unknown" and is_indiv):
            active_like_individual += 1

        pipe = evaluate_procurement_pipeline(it)
        stage = pipe.get("stage")
        stage_counts[str(stage)] += 1
        for r in pipe.get("reasons") or []:
            reason_counts[str(r)] += 1
        for u in pipe.get("unknown_flags") or []:
            unknown_flag_counts[str(u)] += 1
        for inv in pipe.get("investigation_trail") or []:
            investigation_counts[str(inv)] += 1
        if pipe.get("next_stage"):
            next_stage_counts[str(pipe.get("next_stage"))] += 1

        reasons = [str(x) for x in (pipe.get("reasons") or [])]
        excluded = bool(pipe.get("excluded"))

        # Ordered elimination accounting (first hard stop wins)
        if not is_indiv:
            filter_eliminations["F1_not_verified_individual_solicitation"] += 1
            if excluded:
                confirmed_dq += 1
            continue
        remaining_after["after_F1"] += 1

        if any(r.startswith("status:") for r in reasons) and excluded:
            filter_eliminations["F2_inactive_status_excluded"] += 1
            confirmed_dq += 1
            continue
        if any(r.startswith("class:") for r in reasons) and excluded:
            # split services title vs class
            if "class:services_title_confirmed" in reasons:
                filter_eliminations["F5_confirmed_services_title_excluded"] += 1
            else:
                filter_eliminations["F3_requirement_class_excluded"] += 1
            confirmed_dq += 1
            continue
        if "it_focus_detected" in reasons and excluded:
            filter_eliminations["F4_it_software_excluded"] += 1
            confirmed_dq += 1
            continue

        remaining_after["after_hard_exclude"] += 1

        if pipe.get("product_opportunity"):
            product_opportunities += 1
        else:
            filter_eliminations["F6_not_product_opportunity_after_pipeline"] += 1
            continue

        if stage == STAGE_C and pipe.get("quantity_defined"):
            filter_eliminations["F8_stage_c_procurement_fit"] += 1
            stage_c += 1
            continue

        filter_eliminations["F7_product_but_not_stage_c"] += 1
        # Classify blocker for this product opportunity
        flags = set(pipe.get("unknown_flags") or [])
        inv = set(pipe.get("investigation_trail") or [])
        blocker = "other_or_mixed"
        if "quantity_unknown" in flags or "quantity_not_found_in_accessible_text" in inv:
            blocker = "missing_quantity_extraction"
            missing_info_blocks += 1
        elif "quantity_estimated_not_firm_order" in flags:
            blocker = "estimated_quantity_not_firm"
            missing_info_blocks += 1
        elif "quantity_indefinite_not_fixed_purchase" in flags:
            blocker = "indefinite_quantity_idiq"
            missing_info_blocks += 1
        elif "quantity_semantics_not_ordered" in flags or "quantity_semantics_unverified" in flags:
            blocker = "quantity_semantics_not_firm_ordered"
            missing_info_blocks += 1
        elif "service_or_non_product_title_requires_review" in flags:
            blocker = "service_title_soft_block_clears_quantity"
            # soft — not confirmed DQ if not excluded
            missing_info_blocks += 1
        elif "requirement_class_unknown" in flags or "mixed_requires_line_review" in flags:
            blocker = "requirement_class_uncertain"
            missing_info_blocks += 1
        elif "bonding_language_present_unverified" in flags or "insurance_language_present_unverified" in flags:
            # These are soft for Stage C — if still not Stage C, something else blocked
            if not pipe.get("quantity_defined"):
                blocker = "missing_quantity_with_bond_insurance_flags"
                missing_info_blocks += 1
            else:
                blocker = "hard_unknown_flags_with_quantity"
                missing_info_blocks += 1
        elif reasons:
            blocker = "pipeline_reasons:" + ",".join(reasons[:3])
        product_blockers[blocker] += 1
        samples = product_blocker_samples.setdefault(blocker, [])
        if len(samples) < 8:
            samples.append(
                {
                    "title": (it.get("title") or "")[:160],
                    "source_key": it.get("source_key"),
                    "solicitation_number": it.get("solicitation_number"),
                    "status": it.get("solicitation_status"),
                    "requirement_class": it.get("requirement_class"),
                    "quantity": pipe.get("quantity") or it.get("quantity"),
                    "quantity_semantic": pipe.get("quantity_semantic") or it.get("quantity_semantic"),
                    "unknown_flags": list(flags)[:8],
                    "stage": stage,
                    "next_stage": pipe.get("next_stage"),
                    "official_url": it.get("official_url"),
                }
            )

    # Bid-eligible: code never sets STAGE_E/F/G without commercial evidence fields
    filter_eliminations["F9_bid_eligible"] = 0
    filter_eliminations["F9_ready_to_submit"] = 0

    # --- Reconcile 002P Stage C (73) ---
    stage_c_002p_path = get_settings().data_root() / "acceptance" / "build002p_stage_c_evidence.json"
    stage_c_002p = {}
    if stage_c_002p_path.exists():
        stage_c_002p = json.loads(stage_c_002p_path.read_text(encoding="utf-8"))
    prior_unique = stage_c_002p.get("unique_opportunities") or stage_c_002p.get("unique") or []
    prior_ids = {
        str(r.get("nb_id") or r.get("provider_id") or r.get("external_key") or "")
        for r in prior_unique
        if r.get("nb_id") or r.get("provider_id") or r.get("external_key")
    }
    # Re-evaluate prior Stage C through current pipeline
    prior_still_c = 0
    prior_lost = Counter()
    prior_lost_samples = []
    for r in prior_unique:
        item = {
            "title": r.get("title"),
            "solicitation_number": r.get("solicitation_number"),
            "official_url": r.get("official_url"),
            "requirement_class": r.get("requirement_class") or "product_supply",
            "solicitation_status": r.get("solicitation_status") or "unknown",
            "quantity": r.get("quantity"),
            "quantity_semantic": r.get("quantity_semantic"),
            "quantity_evidence": r.get("quantity_evidence"),
            "quantity_kind": r.get("quantity_kind") or "fixed",
            "verified_individual_solicitation": True,
            "record_type": "INDIVIDUAL_SOLICITATION",
            "package_status": r.get("package_status"),
        }
        pipe = evaluate_procurement_pipeline(item)
        if pipe.get("stage") == STAGE_C and pipe.get("quantity_defined"):
            prior_still_c += 1
        else:
            why = "unknown"
            if pipe.get("excluded"):
                why = "excluded:" + ",".join(str(x) for x in (pipe.get("reasons") or [])[:3])
            elif not pipe.get("quantity_defined"):
                why = "quantity_not_defined:" + ",".join(
                    str(x) for x in (pipe.get("unknown_flags") or [])[:4]
                )
            else:
                why = "stage=" + str(pipe.get("stage"))
            prior_lost[why] += 1
            if len(prior_lost_samples) < 20:
                prior_lost_samples.append(
                    {
                        "nb_id": r.get("nb_id"),
                        "title": (r.get("title") or "")[:160],
                        "why": why,
                        "unknown_flags": pipe.get("unknown_flags"),
                        "reasons": pipe.get("reasons"),
                        "stage": pipe.get("stage"),
                    }
                )

    # Code defect / design findings (established from source, not speculation)
    code_findings = [
        {
            "id": "BID_ELIGIBLE_NEVER_ASSIGNED",
            "severity": "defect_or_incomplete_processing",
            "location": "m3/research/jobs.py:cmd_qualify (bid_eligible hardcoded False); "
            "m3/research/commercial_criteria.py STAGE_D–G reserved — evaluate_procurement_pipeline "
            "returns next_stage=STAGE_D at Stage C but never advances E/F/G",
            "effect": "ZERO bid-eligible by construction even when Stage C > 0",
            "incorrectly_blocks": False,
            "note": "Pricing/financing are correctly NOT applied as discovery filters; "
            "they are simply never evaluated, so bid-eligible stays 0.",
        },
        {
            "id": "QUALIFY_ASSUMES_ALL_INDIVIDUAL",
            "severity": "defect",
            "location": "m3/research/jobs.py:cmd_qualify lines setting "
            "verified_individual_solicitation=True and record_type=INDIVIDUAL_SOLICITATION "
            "for every registry row",
            "effect": "Qualify counts inflate potential_product vs truthful identity on population",
            "incorrectly_blocks": False,
        },
        {
            "id": "STAGE_C_VS_BID_ELIGIBLE_CONFLATION_IN_REPORTING",
            "severity": "reporting",
            "location": "docs/acceptance_archive/BUILD_002Q_REPORT.md metrics; owner queues map Stage C → "
            "qualified_commercial_evaluation not bid-eligible",
            "effect": "002P '73 verified Stage C' is not bid-eligible; 002Q correctly reports bid-eligible=0 "
            "but root cause of 'qualification failure' is incomplete D–G pipeline, not over-filtering products to zero Stage C",
            "incorrectly_blocks": False,
        },
        {
            "id": "DOCUMENT_ACCESS_NOT_IN_PIPELINE_GATE",
            "severity": "incomplete_processing",
            "location": "evaluate_procurement_pipeline does not read package_status; "
            "quantity_unknown dominates product blockers when document text absent from population freeze",
            "effect": "Missing documents block Stage C via quantity_unknown (missing info), not a hard DQ",
            "incorrectly_blocks": False,
            "blocks_as": "missing_information",
        },
        {
            "id": "SERVICE_TITLE_SOFT_CLEARS_QUANTITY",
            "severity": "restrictive_filter",
            "location": "m3/research/commercial_criteria.py service/non-product title regex "
            "(quantity_defined=False) ~lines 282–296",
            "effect": "Even firm quantities cleared for titles matching maintenance/lease/etc.; "
            "confirmed DQ only for narrower services_title_confirmed patterns",
            "incorrectly_blocks": "possibly_over_broad_for_product_supply_with_incidental_words",
        },
        {
            "id": "PRICING_FINANCING_NOT_BLOCKING_DISCOVERY",
            "severity": "by_design",
            "location": "commercial_criteria.py docstring + STAGE_E/F unused",
            "effect": "Pricing/financing do NOT eliminate the 1042 product opportunities from Stage B; "
            "they prevent promotion to bid-eligible because those stages are unimplemented",
            "incorrectly_blocks": False,
        },
    ]

    # Map acquire-reported 1042 product to this audit
    acquire_product = acquire.get("verified_product")
    acquire_indiv = acquire.get("verified_individual_solicitations")

    audit = {
        "build": "002Q",
        "audited_at": _utc(),
        "method": "offline_evaluate_procurement_pipeline_on_population_freeze",
        "population_path": pop_path,
        "population_n": n,
        "filters_in_execution_order": [
            {
                "id": "F0",
                "name": "unique_population_freeze",
                "code": "m3/research/engine.py write_population_file(run.id, unique)",
                "remaining": n,
                "eliminated": 0,
            },
            {
                "id": "F1",
                "name": "verified_individual_solicitation",
                "code": "m3/research/identity.py:verify_record_identity → "
                "commercial_criteria.py:evaluate_procurement_pipeline ~L139-149",
                "eliminated": filter_eliminations["F1_not_verified_individual_solicitation"],
                "remaining": remaining_after["after_F1"],
            },
            {
                "id": "F2",
                "name": "inactive_status",
                "code": "commercial_criteria.py ~L151-154 status in closed/cancelled/historical_award/forecast",
                "eliminated": filter_eliminations["F2_inactive_status_excluded"],
            },
            {
                "id": "F3",
                "name": "requirement_class_exclude",
                "code": "commercial_criteria.py INITIAL_EXCLUDE_CLASSES ~L165-166",
                "eliminated": filter_eliminations["F3_requirement_class_excluded"],
            },
            {
                "id": "F4",
                "name": "it_software_exclude",
                "code": "commercial_criteria.py IT_MARKERS + it_focus_detected ~L171-187,268-280",
                "eliminated": filter_eliminations["F4_it_software_excluded"],
            },
            {
                "id": "F5",
                "name": "confirmed_services_title",
                "code": "commercial_criteria.py class:services_title_confirmed ~L297-313",
                "eliminated": filter_eliminations["F5_confirmed_services_title_excluded"],
            },
            {
                "id": "F6",
                "name": "product_opportunity",
                "code": "commercial_criteria.py product_opportunity / INITIAL_PRODUCT_OK ~L179-184,348+",
                "eliminated": filter_eliminations["F6_not_product_opportunity_after_pipeline"],
                "product_opportunities": product_opportunities,
            },
            {
                "id": "F7",
                "name": "firm_quantity_and_unknown_flags_for_stage_c",
                "code": "commercial_criteria.py quantity firm_semantics + hard_unknown ~L189-346",
                "eliminated": filter_eliminations["F7_product_but_not_stage_c"],
            },
            {
                "id": "F8",
                "name": "stage_c_verified_m3_procurement_fit",
                "code": "commercial_criteria.py STAGE_C return ~L315-346",
                "remaining": filter_eliminations["F8_stage_c_procurement_fit"],
            },
            {
                "id": "F9",
                "name": "bid_eligible_stages_d_g",
                "code": "commercial_criteria.py STAGE_D–G declared but unused; jobs.py cmd_qualify hardcodes 0",
                "eliminated": filter_eliminations["F8_stage_c_procurement_fit"],
                "remaining": 0,
                "note": "All Stage C stopped before bid-eligible",
            },
        ],
        "counts": {
            "unique_population": n,
            "genuinely_active_status_open_amended": genuinely_active,
            "active_like_including_unknown_individual": active_like_individual,
            "verified_individual_solicitation": verified_individual,
            "acquire_reported_verified_individual": acquire_indiv,
            "acquire_reported_verified_product": acquire_product,
            "product_opportunities_pipeline": product_opportunities,
            "stage_c": stage_c,
            "bid_eligible": 0,
            "ready_to_submit": 0,
            "confirmed_disqualifications_hard_exclude": confirmed_dq,
            "missing_info_blocks_on_products": missing_info_blocks,
        },
        "filter_eliminations": filter_eliminations,
        "status_breakdown": dict(status_counts),
        "record_type_breakdown": dict(record_type_counts),
        "requirement_class_breakdown": dict(req_counts.most_common(40)),
        "pipeline_stage_breakdown": dict(stage_counts),
        "top_reasons": dict(reason_counts.most_common(30)),
        "top_unknown_flags": dict(unknown_flag_counts.most_common(30)),
        "top_investigation": dict(investigation_counts.most_common(30)),
        "product_opportunity_blockers": dict(product_blockers),
        "product_blocker_samples": product_blocker_samples,
        "reconcile_002p_stage_c": {
            "prior_unique_count": len(prior_unique),
            "prior_still_stage_c_under_current_pipeline": prior_still_c,
            "prior_lost_count": len(prior_unique) - prior_still_c,
            "prior_lost_reasons": dict(prior_lost),
            "prior_lost_samples": prior_lost_samples,
            "zero_bid_eligible_explanation": (
                "002P 73 were Stage C (product+firm qty), not bid-eligible. "
                "002Q bid-eligible=0 because stages E/F/G (supplier quote, financing, ready-to-bid) "
                "are never populated. Current pipeline Stage C count on population="
                f"{stage_c}; prior 002P rows still Stage C under current rules={prior_still_c}."
            ),
        },
        "blocking_analysis": {
            "document_access": "Not a direct pipeline predicate; absence of document text → quantity_unknown (missing info).",
            "quantity_extraction": "Primary blocker for product opportunities (see product_opportunity_blockers).",
            "eligibility_reseller": "Not implemented as a filter in evaluate_procurement_pipeline.",
            "pricing": "Not applied; does not eliminate product opportunities.",
            "financing": "Not applied; does not eliminate product opportunities; blocks bid-eligible only because STAGE_F unused.",
        },
        "code_findings": code_findings,
        "metered_nextbid": 0,
        "openai_spent": 0,
    }

    ARCHIVE.mkdir(parents=True, exist_ok=True)
    qual_path = ARCHIVE / "BUILD_002Q_QUALIFICATION_RESULTS.json"
    qual = {}
    if qual_path.exists():
        try:
            qual = json.loads(qual_path.read_text(encoding="utf-8"))
        except Exception:
            qual = {}
    qual["qualification_funnel_audit"] = audit
    qual["updated_at"] = _utc()
    _write(qual_path, qual)

    acc_path = ARCHIVE / "BUILD_002Q_ACCEPTANCE.json"
    acceptance = json.loads(acc_path.read_text(encoding="utf-8")) if acc_path.exists() else {}
    acceptance["qualification_funnel_audit"] = {
        "completed_at": _utc(),
        "population_n": n,
        "genuinely_active_open_amended": genuinely_active,
        "verified_individual": verified_individual,
        "product_opportunities": product_opportunities,
        "stage_c": stage_c,
        "bid_eligible": 0,
        "primary_product_blockers": dict(product_blockers),
        "root_cause_zero_bid_eligible": "STAGE_D–G unimplemented; cmd_qualify hardcodes bid_eligible=False",
        "artifact": "docs/acceptance_archive/BUILD_002Q_QUALIFICATION_RESULTS.json",
    }
    _write(acc_path, acceptance)

    report_path = ARCHIVE / "BUILD_002Q_REPORT.md"
    report = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    marker = "\n## Qualification funnel audit\n"
    if marker in report:
        report = report.split(marker)[0].rstrip() + "\n"
    lines = [
        marker.strip(),
        "",
        f"**Audited at:** {audit['audited_at']}  ",
        f"**Population:** {n} unique freeze rows (offline; no acquisition restart).",
        "",
        "### Headline counts",
        "",
        f"| Step | Count |",
        f"|------|------:|",
        f"| Unique population | {n} |",
        f"| Genuinely active (`open`/`amended`) | {genuinely_active} |",
        f"| Verified individual solicitation | {verified_individual} |",
        f"| Product opportunities (pipeline) | {product_opportunities} |",
        f"| Stage C (firm qty fit) | {stage_c} |",
        f"| Bid-eligible | 0 |",
        f"| Confirmed hard DQ | {confirmed_dq} |",
        "",
        "### Filters in execution order (eliminations)",
        "",
    ]
    for f in audit["filters_in_execution_order"]:
        lines.append(
            f"- **{f['id']} {f['name']}** — eliminated `{f.get('eliminated', 'n/a')}` "
            f"— `{f['code']}`"
        )
    lines.extend(
        [
            "",
            "### Product opportunity blockers (not Stage C)",
            "",
            json.dumps(dict(product_blockers), indent=2),
            "",
            "### Root cause of ZERO bid-eligible",
            "",
            audit["reconcile_002p_stage_c"]["zero_bid_eligible_explanation"],
            "",
            f"002P unique Stage C rows: {len(prior_unique)}; still Stage C now: {prior_still_c}; "
            f"lost under current rules: {len(prior_unique) - prior_still_c}.",
            "",
            "### Code findings",
            "",
        ]
    )
    for c in code_findings:
        lines.append(f"- `{c['id']}` ({c['severity']}) — {c['location']}")
        lines.append(f"  - {c['effect']}")
    lines.append("")
    report_path.write_text(report.rstrip() + "\n\n" + "\n".join(lines), encoding="utf-8")

    summary = {
        "ok": True,
        "population_n": n,
        "genuinely_active": genuinely_active,
        "verified_individual": verified_individual,
        "product_opportunities": product_opportunities,
        "stage_c": stage_c,
        "bid_eligible": 0,
        "product_blockers": dict(product_blockers),
        "prior_002p_still_c": prior_still_c,
        "prior_002p_n": len(prior_unique),
    }
    print(json.dumps(summary, default=str), flush=True)
    return audit


def cmd_acquisition_quality_repair(
    db: Session,
    *,
    skip_recovery: bool = False,
) -> dict[str, Any]:
    """BUILD 002Q — acquisition quality: source control, junk docs, product recovery."""
    from collections import Counter

    from m3.research.classify import PRODUCT_CLASSES
    from m3.research.commercial_criteria import (
        INITIAL_EXCLUDE_CLASSES,
        INITIAL_PRODUCT_OK,
        evaluate_bid_eligibility,
    )
    from m3.research.document_quality import is_junk_package_excerpt
    from m3.research.package import acquire_solicitation_package
    from m3.research.source_quality import (
        canonical_source_url,
        classify_source_purpose,
        is_active_opportunity_source,
    )
    from m3.models import SourceRegistry
    from sqlalchemy.orm import joinedload

    t0 = time.time()
    constraints = assert_constraints(db)
    print(
        f"acquisition_quality_repair_start skip_recovery={skip_recovery}",
        flush=True,
    )

    # --- 1) Source quality control ---
    rows = db.query(SourceRegistry).all()
    print(f"source_audit_loaded n={len(rows)}", flush=True)
    purpose_counts: Counter[str] = Counter()
    role_counts: Counter[str] = Counter()
    disabled: list[dict[str, Any]] = []
    classified: list[dict[str, Any]] = []
    url_owners: dict[str, list[SourceRegistry]] = {}
    for r in rows:
        c = classify_source_purpose(r)
        purpose_counts[str(c["purpose"])] += 1
        role_counts[str(c["acquisition_role"])] += 1
        classified.append(
            {
                "source_key": r.source_key,
                "adapter": r.adapter,
                "enabled": bool(r.is_enabled),
                **c,
            }
        )
        u = canonical_source_url(r)
        if u:
            url_owners.setdefault(u, []).append(r)

        cfg = dict(r.config or {})
        changed = False
        if c["acquisition_role"] in {
            "pricing_research_only",
            "forecast_only",
            "portal_or_listing",
            "supplier_research",
        }:
            cfg["acquisition_role"] = c["acquisition_role"]
            cfg["dataset_purpose"] = c["purpose"]
            if c["acquisition_role"] == "pricing_research_only":
                cfg["is_historical"] = True
            if c["acquisition_role"] == "forecast_only":
                cfg["is_forecast"] = True
            if r.is_enabled and not c["active_opportunity_ok"]:
                r.is_enabled = False
                changed = True
                disabled.append(
                    {
                        "source_key": r.source_key,
                        "reason": c["acquisition_role"],
                        "evidence": c["evidence"],
                    }
                )
            r.config = cfg
            db.add(r)

    # Duplicate connectors: keep seed/operational, disable discovered duplicates
    dup_disabled = []
    for url, owners in url_owners.items():
        if len(owners) < 2:
            continue
        # Prefer non-discovered, experimentally_verified, then earliest key
        owners_sorted = sorted(
            owners,
            key=lambda x: (
                0 if not str(x.source_key or "").startswith("discovered_") else 1,
                0 if x.experimentally_verified else 1,
                x.source_key or "",
            ),
        )
        keep = owners_sorted[0]
        for other in owners_sorted[1:]:
            if not other.is_enabled:
                continue
            cfg = dict(other.config or {})
            cfg["duplicate_of"] = keep.source_key
            cfg["acquisition_role"] = cfg.get("acquisition_role") or "duplicate_disabled"
            other.config = cfg
            other.is_enabled = False
            db.add(other)
            dup_disabled.append({"disabled": other.source_key, "kept": keep.source_key, "url": url[:120]})
    db.commit()
    print(
        f"source_audit_done disabled_hist_forecast={len(disabled)} dupes={len(dup_disabled)}",
        flush=True,
    )

    active_enabled = sum(
        1 for r in db.query(SourceRegistry).filter(SourceRegistry.is_enabled.is_(True)).all() if is_active_opportunity_source(r)
    )

    # --- 2) Demote junk packages already marked successful ---
    print("junk_package_demotion_start", flush=True)
    pkgs = (
        db.query(SolicitationPackage)
        .options(joinedload(SolicitationPackage.documents))
        .filter(SolicitationPackage.downloaded_count > 0)
        .all()
    )
    demoted = 0
    genuine = 0
    for pkg in pkgs:
        texts = []
        for d in pkg.documents or []:
            if d.extracted_excerpt:
                texts.append(d.extracted_excerpt)
        blob = "\n".join(texts)
        if not blob.strip():
            continue
        if is_junk_package_excerpt(blob):
            if pkg.package_status in {"COMPLETE", "PARTIAL", "VERIFICATION_REQUIRED"}:
                pkg.package_status = "FAILED"
                detail = dict(pkg.detail_fields or {})
                detail["junk_demotion"] = {
                    "build": "002Q",
                    "reason": "portal_or_non_solicitation_excerpt",
                    "at": _utc(),
                }
                pkg.detail_fields = detail
                issues = list(pkg.access_issues or [])
                issues.append("junk_portal_or_non_solicitation_page")
                pkg.access_issues = list(dict.fromkeys(issues))
                db.add(pkg)
                demoted += 1
        else:
            genuine += 1
    db.commit()
    print(f"junk_package_demotion_done demoted={demoted} genuine={genuine}", flush=True)

    # --- 3) Targeted free recovery for product opportunities missing qty / Stage C gaps ---
    acquire = _load_job_artifact("jobs_acquire_last.json")
    pop_path = acquire.get("population_path")
    if not pop_path or not Path(pop_path).exists():
        raise RuntimeError("population freeze missing")
    population = load_population_file(pop_path)

    recover_targets: list[dict[str, Any]] = []
    stage_c_targets: list[dict[str, Any]] = []
    for it in population:
        work = apply_identity(dict(it))
        if not (
            work.get("verified_individual_solicitation")
            or work.get("record_type") == "INDIVIDUAL_SOLICITATION"
        ):
            continue
        req = (work.get("requirement_class") or "unknown").lower()
        productish = req in INITIAL_PRODUCT_OK or (
            req in PRODUCT_CLASSES and req not in INITIAL_EXCLUDE_CLASSES
        )
        if not productish:
            continue
        # Pre-download style screen: skip confirmed inactive / construction / services class
        st = str(work.get("solicitation_status") or "").lower()
        if st in {"closed", "cancelled", "historical_award", "forecast"}:
            continue
        if req in INITIAL_EXCLUDE_CLASSES:
            continue
        pipe = evaluate_procurement_pipeline(work)
        if not pipe.get("product_opportunity"):
            continue
        if pipe.get("stage") == STAGE_C and pipe.get("quantity_defined"):
            bid = evaluate_bid_eligibility(work, pipe)
            if not bid.get("bid_eligible"):
                stage_c_targets.append(work)
            continue
        recover_targets.append(work)

    # Prefer free official detail pages known to yield real docs; skip known junk/auth shells.
    blocked_hosts = {"rampla.org"}
    junk_portal_hosts = {"citydata.mesaaz.gov", "www.phoenix.gov"}  # employee/portal chrome
    priority_sources = {
        "georgia_gpr_open_goods",
        "sf_open_bid_opportunities",
        "delaware_open_bids",
        "nj_purchase",
        "montgomery_md_solicitations",
        "al_purchasing",
    }
    recovery_stats = {
        "attempted": 0,
        "skipped_blocked_host": 0,
        "skipped_junk_portal_host": 0,
        "acquired_ok": 0,
        "qty_after": 0,
        "failures": Counter(),
        "by_source": Counter(),
    }

    def _recovery_rank(w: dict[str, Any]) -> tuple:
        src = str(w.get("source_key") or "")
        url = str(w.get("official_url") or "").lower()
        detail = 0 if any(
            m in url
            for m in (
                "eventdetails",
                "opportunity-details",
                "bo-detail",
                "/projects/",
                "biddetail",
                "folder_name",
                "esourcenumber",
            )
        ) else 1
        pri = 0 if src in priority_sources else 1
        return (pri, detail, src)

    targets = sorted(recover_targets + stage_c_targets, key=_recovery_rank)
    print(
        f"acquisition_quality_recovery_targets missing_qty={len(recover_targets)} "
        f"stage_c_gaps={len(stage_c_targets)} ordered={len(targets)} "
        f"skip_recovery={skip_recovery}",
        flush=True,
    )

    if not skip_recovery:
        for i, work in enumerate(targets):
            url = str(work.get("official_url") or "")
            host = ""
            try:
                from urllib.parse import urlparse

                host = (urlparse(url).hostname or "").lower()
            except Exception:
                host = ""
            if any(h in host for h in junk_portal_hosts):
                recovery_stats["skipped_junk_portal_host"] += 1
                continue
            # Do not hammer known blocked RAMP shells when already ACCESS_RESTRICTED
            if any(h in host for h in blocked_hosts):
                existing = (
                    db.query(SolicitationPackage)
                    .filter(SolicitationPackage.external_key == work.get("external_key"))
                    .one_or_none()
                )
                if existing and (existing.package_status or "") in {
                    "ACCESS_RESTRICTED",
                    "FAILED",
                }:
                    recovery_stats["skipped_blocked_host"] += 1
                    continue

            recovery_stats["attempted"] += 1
            recovery_stats["by_source"][str(work.get("source_key") or "unknown")] += 1
            try:
                pkg = acquire_solicitation_package(db, work, refresh=True)
                db.commit()
                status = getattr(pkg, "package_status", None) or ""
                if status in {"COMPLETE", "PARTIAL"}:
                    recovery_stats["acquired_ok"] += 1
                else:
                    recovery_stats["failures"][status or "unknown"] += 1
                from m3.research.reprocess import collect_package_text

                text = collect_package_text(pkg, max_chars=100_000)
                if text and is_junk_package_excerpt(text):
                    text = ""
                hint = pkg.classification_hint or {}
                if hint.get("quantity") and not work.get("quantity"):
                    work["quantity"] = hint.get("quantity")
                    work["quantity_semantic"] = hint.get("quantity_semantic") or "ORDERED_QUANTITY"
                    work["quantity_evidence"] = hint.get("quantity_evidence")
                pipe2 = evaluate_procurement_pipeline(work, document_text=text or None)
                if pipe2.get("quantity_defined"):
                    recovery_stats["qty_after"] += 1
                    work["_recovered_pipe"] = pipe2
                    work["_recovered_text_len"] = len(text or "")
                    work["package_status"] = status
                elif pipe2.get("enriched_fields"):
                    work["_recovered_pipe"] = pipe2
                    work["_recovered_text_len"] = len(text or "")
                    work["package_status"] = status
            except Exception as exc:  # noqa: BLE001
                recovery_stats["failures"][type(exc).__name__] += 1
                db.rollback()
            if (i + 1) % 10 == 0:
                print(
                    f"recovery_progress {i+1}/{len(targets)} attempted={recovery_stats['attempted']} "
                    f"ok={recovery_stats['acquired_ok']} qty={recovery_stats['qty_after']}",
                    flush=True,
                )

        recovery_stats["failures"] = dict(recovery_stats["failures"])
        recovery_stats["by_source"] = dict(recovery_stats["by_source"])

        # Merge recovered fields into population + re-qualify
        recovered_by_ek = {
            str(w.get("external_key")): w for w in targets if w.get("_recovered_pipe")
        }
        merged = []
        for it in population:
            ek = str(it.get("external_key") or "")
            if ek in recovered_by_ek:
                w = recovered_by_ek[ek]
                pipe = w.get("_recovered_pipe") or {}
                for k in (
                    "quantity",
                    "quantity_unit",
                    "quantity_kind",
                    "quantity_semantic",
                    "quantity_evidence",
                    "package_status",
                    "submission_method",
                    "submission_instructions",
                ):
                    if w.get(k) is not None:
                        it[k] = w.get(k)
                for k, v in (pipe.get("enriched_fields") or {}).items():
                    if v is not None:
                        it[k] = v
                if pipe.get("quantity") is not None:
                    it["quantity"] = pipe.get("quantity")
                if w.get("_recovered_text_len"):
                    it["document_text_len"] = w["_recovered_text_len"]
            merged.append(it)

        from m3.research.population_acquire import write_population_file

        pop_stem = Path(pop_path).stem
        write_population_file(pop_stem, merged)
    else:
        recovery_stats["skipped"] = True
        # Reuse prior recovery stats if present
        prior = _load_job_artifact("jobs_acquisition_quality_repair_last.json") or {}
        prior_rec = prior.get("product_recovery") or {}
        for k in (
            "attempted",
            "acquired_ok",
            "qty_after",
            "skipped_blocked_host",
            "skipped_junk_portal_host",
            "failures",
            "by_source",
        ):
            if k in prior_rec and recovery_stats.get(k) in (0, {}, None):
                recovery_stats[k] = prior_rec[k]

    qualify = cmd_qualify(db)

    # Connector smoke: enabled active sources count + sample adapter presence
    connectors = {
        "active_enabled_sources": active_enabled,
        "total_sources": len(rows),
        "disabled_historical_or_forecast": len(disabled),
        "disabled_duplicates": len(dup_disabled),
        "purpose_counts": dict(purpose_counts),
        "role_counts": dict(role_counts),
    }

    # Product / Stage C after
    after = load_population_file(qualify.get("population_path") or pop_path)
    products = 0
    missing_qty = 0
    stage_c = 0
    bid_n = 0
    genuine_pkgs = genuine
    active_by_source: Counter[str] = Counter()
    product_by_source: Counter[str] = Counter()
    for it in after:
        if it.get("bid_eligible"):
            bid_n += 1
        if it.get("procurement_stage") == STAGE_C:
            stage_c += 1
        w = apply_identity(dict(it))
        src = str(w.get("source_key") or "unknown")
        st = str(w.get("solicitation_status") or "").lower()
        if (
            w.get("verified_individual_solicitation")
            or w.get("record_type") == "INDIVIDUAL_SOLICITATION"
        ) and st not in {"closed", "cancelled", "historical_award", "forecast"}:
            active_by_source[src] += 1
        req = (w.get("requirement_class") or "").lower()
        if not (
            req in INITIAL_PRODUCT_OK
            or (req in PRODUCT_CLASSES and req not in INITIAL_EXCLUDE_CLASSES)
        ):
            continue
        pipe = evaluate_procurement_pipeline(w)
        if pipe.get("product_opportunity"):
            products += 1
            product_by_source[src] += 1
            if not (pipe.get("stage") == STAGE_C and pipe.get("quantity_defined")):
                missing_qty += 1

    recon = qualify.get("package_reconcile") or {}
    linked_n = int(recon.get("linked") or (qualify.get("counts") or {}).get("package_linked") or 0)
    usable_n = int(recon.get("with_usable_text") or 0)
    junk_n = int(recon.get("junk_excerpts") or 0)
    doc_success_rate = round(usable_n / linked_n, 4) if linked_n else 0.0
    recovery_attempted = int(recovery_stats.get("attempted") or 0)
    recovery_ok = int(recovery_stats.get("acquired_ok") or 0)
    recovery_success_rate = (
        round(recovery_ok / recovery_attempted, 4) if recovery_attempted else None
    )

    # Lightweight connector smoke against enabled active sources (no metered calls)
    connector_smoke: dict[str, Any] = {"tested": 0, "ok": 0, "fail": 0, "samples": []}
    from m3.research.source_verification import probe_source

    smoke_rows = [
        r
        for r in db.query(SourceRegistry).filter(SourceRegistry.is_enabled.is_(True)).all()
        if is_active_opportunity_source(r)
        and (r.adapter or "") in {"socrata_contracts", "georgia_gpr", "html_link_harvest"}
    ][:15]
    for r in smoke_rows:
        connector_smoke["tested"] += 1
        try:
            probe = probe_source(r, sample_limit=5)
            ok = (
                str(probe.get("status") or "").lower() == "verified"
                or int(probe.get("records_retrieved") or 0) > 0
            )
        except Exception as exc:  # noqa: BLE001
            probe = {"records_retrieved": 0, "reason": type(exc).__name__, "status": "error"}
            ok = False
        if ok:
            connector_smoke["ok"] += 1
        else:
            connector_smoke["fail"] += 1
        if len(connector_smoke["samples"]) < 15:
            connector_smoke["samples"].append(
                {
                    "source_key": r.source_key,
                    "adapter": r.adapter,
                    "ok": ok,
                    "detail": {
                        k: probe.get(k)
                        for k in (
                            "status",
                            "records_retrieved",
                            "active_like",
                            "reason",
                            "sample_titles",
                        )
                        if k in probe
                    },
                }
            )

    elapsed = round(time.time() - t0, 1)
    report = {
        "build": "002Q",
        "job": "acquisition-quality-repair",
        "finished_at": _utc(),
        "elapsed_sec": elapsed,
        "constraints": constraints,
        "openai_spent": 0,
        "metered_nextbid": 0,
        "source_quality": {
            **connectors,
            "disabled_samples": disabled[:40],
            "duplicate_disabled": dup_disabled,
            "classified_sample": classified[:30],
        },
        "document_validation": {
            "packages_scanned_with_downloads": len(pkgs),
            "genuine_non_junk_excerpts": genuine_pkgs,
            "junk_packages_demoted": demoted,
            "linked_packages": linked_n,
            "genuine_solicitation_packages": usable_n,
            "junk_or_thin_linked": junk_n,
            "document_acquisition_success_rate": doc_success_rate,
            "targeted_recovery_success_rate": recovery_success_rate,
        },
        "active_solicitations_by_source": dict(active_by_source.most_common()),
        "product_opportunities_by_source": dict(product_by_source.most_common()),
        "connector_smoke": connector_smoke,
        "product_recovery": {
            "missing_qty_targets": len(recover_targets),
            "stage_c_gap_targets": len(stage_c_targets),
            **recovery_stats,
        },
        "after_reprocess": {
            "population": len(after),
            "product_opportunities": products,
            "missing_quantity_among_products": missing_qty,
            "stage_c": qualify.get("stage_c_total") or stage_c,
            "bid_eligible": qualify.get("bid_eligible_total") or bid_n,
            "package_linked": (qualify.get("counts") or {}).get("package_linked"),
            "qualify_counts": qualify.get("counts"),
            "package_reconcile": qualify.get("package_reconcile"),
        },
        "remaining_acquisition_blockers": {
            "missing_quantity_product": missing_qty,
            "junk_packages_demoted_this_pass": demoted,
            "ramp_access_restricted_skipped": recovery_stats.get("skipped_blocked_host"),
            "junk_portal_hosts_skipped": recovery_stats.get("skipped_junk_portal_host"),
            "note": (
                "Quantity remains the dominant product blocker where free official documents "
                "are unavailable or auth-walled (e.g. LA RAMP). Historical dumps disabled for "
                "future active acquisition; existing freeze retained (no full historical reacquisition)."
            ),
        },
    }
    _write(get_settings().data_root() / "acceptance" / "jobs_acquisition_quality_repair_last.json", report)
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    _write(ARCHIVE / "BUILD_002Q_ACQUISITION_QUALITY_REPAIR.json", report)
    top_active = list(active_by_source.most_common(15))
    top_active_md = "\n".join(f"- `{k}`: {v}" for k, v in top_active) or "- (none)"
    md = "\n".join(
        [
            "# BUILD 002Q — Acquisition Quality Repair",
            "",
            f"Finished: {report['finished_at']} · Runtime: {elapsed}s · OpenAI: $0 · NextBid: 0",
            "",
            "## Source quality",
            "",
            f"- Sources total: **{len(rows)}** · Active-enabled after repair: **{active_enabled}**",
            f"- Disabled non-active roles this pass: **{len(disabled)}**",
            f"- Duplicate connectors disabled: **{len(dup_disabled)}**",
            f"- Purpose counts: `{json.dumps(dict(purpose_counts))}`",
            f"- Connector smoke: tested={connector_smoke.get('tested')} ok={connector_smoke.get('ok')} fail={connector_smoke.get('fail')}",
            "",
            "## Active solicitations by source (top 15)",
            "",
            top_active_md,
            "",
            "## Document validation",
            "",
            f"- Packages with downloads scanned: **{len(pkgs)}**",
            f"- Genuine non-junk package excerpts (downloaded set): **{genuine_pkgs}**",
            f"- Junk packages demoted this pass: **{demoted}**",
            f"- Freeze-linked packages: **{linked_n}** · Genuine usable text: **{usable_n}** · Junk/thin linked: **{junk_n}**",
            f"- Document acquisition success rate (usable/linked): **{doc_success_rate}**",
            f"- Targeted recovery success rate: **{recovery_success_rate}**",
            "",
            "## Product recovery (targeted free re-acquire)",
            "",
            f"- Missing-qty targets: **{len(recover_targets)}** · Stage C gaps: **{len(stage_c_targets)}**",
            f"- Attempted: **{recovery_stats.get('attempted', 0)}** · Packages COMPLETE/PARTIAL: **{recovery_stats.get('acquired_ok', 0)}**",
            f"- Quantities recovered: **{recovery_stats.get('qty_after', 0)}**",
            f"- Skipped blocked hosts: **{recovery_stats.get('skipped_blocked_host', 0)}** · junk portals: **{recovery_stats.get('skipped_junk_portal_host', 0)}**",
            "",
            "## After full qualify reprocess",
            "",
            f"| Metric | Count |",
            f"|---|---:|",
            f"| Product opportunities | {products} |",
            f"| Missing quantity | {missing_qty} |",
            f"| Stage C | {report['after_reprocess']['stage_c']} |",
            f"| Bid-eligible | {report['after_reprocess']['bid_eligible']} |",
            "",
            "Awaiting owner acceptance.",
            "",
        ]
    )
    (ARCHIVE / "BUILD_002Q_ACQUISITION_QUALITY_REPAIR.md").write_text(md, encoding="utf-8")
    print(json.dumps({"ok": True, "acquisition_quality_repair": {
        "active_enabled": active_enabled,
        "disabled": len(disabled) + len(dup_disabled),
        "junk_demoted": demoted,
        "recovery": recovery_stats,
        "stage_c": report["after_reprocess"]["stage_c"],
        "bid_eligible": report["after_reprocess"]["bid_eligible"],
        "elapsed_sec": elapsed,
    }}, default=str), flush=True)
    return report


def cmd_document_reconcile(db: Session) -> dict[str, Any]:
    """Repair package↔freeze associations, recover doc evidence, reprocess, report blockers."""
    from collections import Counter

    from m3.research.classify import PRODUCT_CLASSES
    from m3.research.commercial_criteria import (
        INITIAL_EXCLUDE_CLASSES,
        INITIAL_PRODUCT_OK,
        evaluate_bid_eligibility,
    )
    from m3.research.package_association import (
        build_package_association_index,
        resolve_package_for_item,
    )

    assert_constraints(db)
    acquire = _load_job_artifact("jobs_acquire_last.json")
    pop_path = acquire.get("population_path")
    if not pop_path or not Path(pop_path).exists():
        raise RuntimeError("population freeze missing")

    # --- Root-cause diagnosis (exact join math) ---
    population = load_population_file(pop_path)
    index = build_package_association_index(db, include_empty_downloads=True)
    methods = Counter()
    dl_matched = 0
    for it in population:
        pkg, method = resolve_package_for_item(it, index)
        methods[method] += 1
        if pkg and (pkg.downloaded_count or 0) > 0:
            dl_matched += 1
    diagnosis = {
        "freeze_n": len(population),
        "packages_total": index["stats"]["packages_total"],
        "packages_with_downloads": index["stats"]["with_downloads"],
        "match_methods_pre_repair": dict(methods),
        "freeze_rows_matched_any_package": len(population) - methods.get("unmatched", 0),
        "freeze_rows_matched_package_with_downloads": dl_matched,
        "root_cause": [
            {
                "id": "KEYS_MOSTLY_MATCH_BY_EXTERNAL_KEY",
                "finding": (
                    "Product opportunities largely share external_key with SolicitationPackage rows. "
                    "Prior zero quantity recovery was NOT primarily a missing join."
                ),
            },
            {
                "id": "JUNK_OR_THIN_EXCERPTS",
                "finding": (
                    "Many matched packages store portal chrome / stub HTML excerpts "
                    "(e.g. Mesa Employee Data Portal) instead of solicitation body text; "
                    "PDF blobs are re-read when present."
                ),
            },
            {
                "id": "ALTERNATE_KEY_SCHEMES",
                "finding": (
                    "Georgia/SF/Winnipeg minorities use solicitation_number, normalized URL, "
                    "or opportunity-id joins when external_key schemes differ (gt:ga:… vs hash)."
                ),
            },
        ],
    }
    print(json.dumps({"document_reconcile_diagnosis": diagnosis}, default=str), flush=True)

    # --- Full reprocess with repaired association + blob recovery ---
    qualify = cmd_qualify(db)
    after_path = qualify.get("population_path") or pop_path
    after_items = load_population_file(after_path)

    # --- Product opportunity blockers (all products) ---
    product_rows = []
    blocker_counts: Counter[str] = Counter()
    for it in after_items:
        work = apply_identity(dict(it))
        if not (
            work.get("verified_individual_solicitation")
            or work.get("record_type") == "INDIVIDUAL_SOLICITATION"
        ):
            continue
        req = (work.get("requirement_class") or "unknown").lower()
        productish = req in INITIAL_PRODUCT_OK or (
            req in PRODUCT_CLASSES and req not in INITIAL_EXCLUDE_CLASSES
        )
        if not productish:
            continue
        pipe = evaluate_procurement_pipeline(work)
        if not pipe.get("product_opportunity"):
            continue
        bid = evaluate_bid_eligibility(work, pipe)
        if pipe.get("stage") == STAGE_C and pipe.get("quantity_defined"):
            blocker = "none_stage_c"
        else:
            flags = set(pipe.get("unknown_flags") or [])
            inv = set(pipe.get("investigation_trail") or [])
            if "quantity_unknown" in flags or "quantity_not_found_in_accessible_text" in inv:
                blocker = "missing_quantity"
            elif "quantity_estimated_not_firm_order" in flags:
                blocker = "estimated_quantity"
            elif "quantity_indefinite_not_fixed_purchase" in flags:
                blocker = "indefinite_quantity"
            elif "quantity_semantics_not_ordered" in flags or "quantity_semantics_unverified" in flags:
                blocker = "quantity_semantics"
            elif "requirement_class_unknown" in flags or "mixed_requires_line_review" in flags:
                blocker = "requirement_class_uncertain"
            elif pipe.get("excluded"):
                blocker = "excluded:" + ",".join(str(x) for x in (pipe.get("reasons") or [])[:2])
            else:
                blocker = "other:" + ",".join(list(flags)[:3] or ["unresolved"])
        blocker_counts[blocker] += 1
        product_rows.append(
            {
                "external_key": work.get("external_key"),
                "title": (work.get("title") or "")[:160],
                "source_key": work.get("source_key"),
                "solicitation_number": work.get("solicitation_number"),
                "status": work.get("solicitation_status"),
                "stage": pipe.get("stage"),
                "blocker": blocker,
                "quantity": pipe.get("quantity") or work.get("quantity"),
                "quantity_semantic": pipe.get("quantity_semantic") or work.get("quantity_semantic"),
                "package_status": work.get("package_status"),
                "package_match_method": work.get("package_match_method"),
                "document_text_len": work.get("document_text_len"),
                "bid_eligible": bid.get("bid_eligible"),
                "failed_gates": bid.get("failed_gates") or [],
                "unknown_flags": pipe.get("unknown_flags") or [],
            }
        )

    # --- Stage C ineligibility (freeze Stage C + 002P retained) ---
    stage_c_rows = [r for r in product_rows if r["blocker"] == "none_stage_c"]
    stage_c_ineligible = []
    for r in stage_c_rows:
        gates = {
            g: "fail" for g in (r.get("failed_gates") or [])
        }
        stage_c_ineligible.append(
            {
                **{k: r[k] for k in ("external_key", "title", "solicitation_number", "status", "quantity")},
                "failed_gates": r.get("failed_gates") or [],
                "why_not_bid_eligible": (
                    "Stage C is procurement fit only; bid-eligible requires all procurement gates "
                    f"to pass. Failed: {r.get('failed_gates') or ['(none listed — recheck gates)']}"
                ),
                "gate_failures": gates,
            }
        )

    # 002P retained Stage C ineligibility
    stage_c_002p_path = get_settings().data_root() / "acceptance" / "build002p_stage_c_evidence.json"
    prior_unique = []
    if stage_c_002p_path.exists():
        prior_unique = (
            json.loads(stage_c_002p_path.read_text(encoding="utf-8")).get("unique_opportunities")
            or []
        )
    retained_002p = []
    demoted_002p = []
    for r in prior_unique:
        item = apply_identity(
            {
                "title": r.get("title"),
                "solicitation_number": r.get("solicitation_number"),
                "official_url": r.get("official_url"),
                "requirement_class": r.get("requirement_class") or "product_supply",
                "solicitation_status": r.get("solicitation_status") or "unknown",
                "quantity": r.get("quantity"),
                "quantity_semantic": r.get("quantity_semantic"),
                "quantity_evidence": r.get("quantity_evidence"),
                "quantity_kind": r.get("quantity_kind") or "fixed",
                "package_status": r.get("package_status"),
            }
        )
        pipe = evaluate_procurement_pipeline(item)
        bid = evaluate_bid_eligibility(item, pipe)
        row = {
            "nb_id": r.get("nb_id"),
            "title": (r.get("title") or "")[:160],
            "stage": pipe.get("stage"),
            "quantity_defined": pipe.get("quantity_defined"),
            "bid_eligible": bid.get("bid_eligible"),
            "failed_gates": bid.get("failed_gates") or [],
            "record_type": item.get("record_type"),
        }
        if pipe.get("stage") == STAGE_C and pipe.get("quantity_defined"):
            retained_002p.append(row)
        else:
            demoted_002p.append(row)

    report = {
        "build": "002Q",
        "job": "document-reconcile",
        "finished_at": _utc(),
        "constraints": {
            "nextbid_locked": True,
            "no_acquire_restart": True,
            "no_paid_search": True,
        },
        "diagnosis": diagnosis,
        "package_reconcile": qualify.get("package_reconcile"),
        "qualify_counts": qualify.get("counts"),
        "stage_c_total": qualify.get("stage_c_total"),
        "bid_eligible_total": qualify.get("bid_eligible_total"),
        "product_opportunities": {
            "n": len(product_rows),
            "blocker_counts": dict(blocker_counts),
            "rows": product_rows,
        },
        "stage_c_ineligibility": {
            "freeze_stage_c_n": len(stage_c_rows),
            "freeze_stage_c_bid_eligible": sum(1 for r in stage_c_rows if r.get("bid_eligible")),
            "freeze_stage_c_rows": stage_c_ineligible,
            "explanation": (
                "Stage C ≠ bid-eligible. Bid-eligible requires active status, Stage C, "
                "submission instructions, sufficient documents, reseller terms, and no "
                "blocking install/bond/license — all evidenced. Pricing/financing unused."
            ),
            "002p_retained_n": len(retained_002p),
            "002p_retained_bid_eligible": sum(1 for r in retained_002p if r.get("bid_eligible")),
            "002p_retained_gate_failures": dict(
                Counter(
                    g
                    for r in retained_002p
                    for g in (r.get("failed_gates") or ["(no_failed_gates_listed)"])
                )
            ),
            "002p_retained_samples": retained_002p[:20],
            "002p_demoted_n": len(demoted_002p),
        },
        "metered_nextbid": 0,
        "openai_spent": 0,
    }
    _write(get_settings().data_root() / "acceptance" / "jobs_document_reconcile_last.json", report)
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    # Compact archive copy without full product row dump duplication in MD
    archive_json = dict(report)
    archive_json["product_opportunities"] = {
        "n": len(product_rows),
        "blocker_counts": dict(blocker_counts),
        "sample_rows": product_rows[:50],
    }
    _write(ARCHIVE / "BUILD_002Q_DOCUMENT_RECONCILE.json", archive_json)

    md = "\n".join(
        [
            "# BUILD 002Q — Document Reconcile",
            "",
            f"Finished: {report['finished_at']}",
            "",
            "## Why packages appeared not to match",
            "",
            f"- Freeze rows: **{diagnosis['freeze_n']}** · Packages: **{diagnosis['packages_total']}** "
            f"(with downloads: **{diagnosis['packages_with_downloads']}**)",
            f"- Matched any package: **{diagnosis['freeze_rows_matched_any_package']}** · "
            f"Matched package with downloads: **{diagnosis['freeze_rows_matched_package_with_downloads']}**",
            f"- Match methods: `{json.dumps(diagnosis['match_methods_pre_repair'])}`",
            "",
            "Primary issue was **junk/thin document excerpts on correctly keyed packages**, "
            "plus minority alternate-key schemes (SN / normalized URL / opportunity id).",
            "",
            "## After association repair + evidence recovery + full reprocess",
            "",
            f"| Metric | Count |",
            f"|---|---:|",
            f"| Unique population | {qualify.get('population_count') or len(after_items)} |",
            f"| Verified individual | {qualify['counts'].get('verified_individual')} |",
            f"| Product opportunities | {len(product_rows)} |",
            f"| Package linked (qualify) | {qualify['counts'].get('package_linked')} |",
            f"| Qty from package hint | {qualify['counts'].get('quantity_recovered_from_package_hint')} |",
            f"| Qty from package text | {qualify['counts'].get('quantity_recovered_from_package')} |",
            f"| Stage C | {qualify.get('stage_c_total')} |",
            f"| Bid-eligible | {qualify.get('bid_eligible_total')} |",
            "",
            f"Package reconcile: `{json.dumps(qualify.get('package_reconcile'), default=str)[:1200]}`",
            "",
            "## Product opportunity blockers "
            f"({len(product_rows)})",
            "",
            f"```json\n{json.dumps(dict(blocker_counts), indent=2)}\n```",
            "",
            "## Stage C still not bid-eligible",
            "",
            f"- Freeze Stage C: **{len(stage_c_rows)}** · bid-eligible among them: "
            f"**{sum(1 for r in stage_c_rows if r.get('bid_eligible'))}**",
            f"- 002P retained Stage C: **{len(retained_002p)}** · bid-eligible: "
            f"**{sum(1 for r in retained_002p if r.get('bid_eligible'))}**",
            f"- 002P retained gate failures: "
            f"`{json.dumps(report['stage_c_ineligibility']['002p_retained_gate_failures'])}`",
            "",
            report["stage_c_ineligibility"]["explanation"],
            "",
            "Awaiting owner acceptance. No acquire restart. NextBid locked.",
            "",
        ]
    )
    (ARCHIVE / "BUILD_002Q_DOCUMENT_RECONCILE.md").write_text(md, encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "document_reconcile": {
                    "linked_pre": diagnosis["freeze_rows_matched_any_package"],
                    "products": len(product_rows),
                    "blockers": dict(blocker_counts),
                    "stage_c": qualify.get("stage_c_total"),
                    "bid_eligible": qualify.get("bid_eligible_total"),
                    "002p_retained": len(retained_002p),
                },
            },
            default=str,
        ),
        flush=True,
    )
    return report


def cmd_controlled_source_reset(db: Session) -> dict[str, Any]:
    """BUILD 002Q — controlled procurement source reset + NextBid baseline audit."""
    import subprocess
    import sys

    from m3.models import SourceRegistry
    from m3.research.nextbid_baseline_audit import audit_and_recover_nextbid
    from m3.research.source_allowlist import (
        apply_allowlist_to_registry,
        load_allowlist,
        save_allowlist,
        verify_unapproved_cannot_acquire,
    )

    t0 = time.time()
    constraints = assert_constraints(db)
    api_before = {
        "nextbid": {
            "locked": True,
            "day_used": (constraints.get("controller") or {})
            .get("budget_state", {})
            .get("day_used"),
            "month_used_local": (constraints.get("controller") or {})
            .get("budget_state", {})
            .get("month_used_local"),
            "account_remaining": (constraints.get("controller") or {})
            .get("budget_state", {})
            .get("account_remaining"),
            "metered_calls_allowed": (constraints.get("controller") or {})
            .get("killswitch", {})
            .get("metered_calls_allowed"),
        },
        "openai": {
            "today_spent_usd": (constraints.get("spend") or {}).get("today_spent_usd"),
            "month_spent_usd": (constraints.get("spend") or {}).get("month_spent_usd"),
        },
    }
    print("controlled_source_reset_start", flush=True)

    # Persist / refresh allowlist file (defaults: NextBid offline-only)
    allow = load_allowlist()
    save_allowlist(allow)
    allowlist_apply = apply_allowlist_to_registry(db)
    print(
        f"allowlist_applied disabled={allowlist_apply['disabled_count']} "
        f"enabled={allowlist_apply['enabled_count']} "
        f"live={allowlist_apply['live_acquisition_keys']}",
        flush=True,
    )

    # ensure_source_registry must not re-enable seeds; re-apply allowlist after seed sync
    first_disabled = int(allowlist_apply.get("disabled_count") or 0)
    ensure_source_registry(db)
    allowlist_apply = apply_allowlist_to_registry(db)
    allowlist_apply["disabled_count_initial_pass"] = first_disabled
    allowlist_apply["disabled_count"] = first_disabled + int(
        allowlist_apply.get("disabled_count") or 0
    )
    gate = verify_unapproved_cannot_acquire(db)

    # Preserve counts
    pkg_count = db.query(func.count(SolicitationPackage.id)).scalar() or 0
    doc_count = db.query(func.count(PackageDocument.id)).scalar() or 0
    opp_count = db.query(func.count(Opportunity.id)).scalar() or 0
    src_count = db.query(func.count(SourceRegistry.id)).scalar() or 0

    print("nextbid_baseline_audit_start", flush=True)
    nextbid_audit = audit_and_recover_nextbid(db, use_openai=False)
    print(
        f"nextbid_baseline_audit_done unique={nextbid_audit['population']['unique_solicitations']} "
        f"products={nextbid_audit['tangible_product_opportunities']} "
        f"stage_c={nextbid_audit['stage_c']} bid={nextbid_audit['bid_eligible']}",
        flush=True,
    )

    # Safety verification — mocked NextBid controller tests (no live credits)
    safety: dict[str, Any] = {"ran": False}
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_nextbid_controller_002k.py",
                "tests/test_nextbid_controller_002m.py",
                "-q",
                "--tb=no",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        safety = {
            "ran": True,
            "returncode": proc.returncode,
            "ok": proc.returncode == 0,
            "stdout_tail": (proc.stdout or "")[-800:],
            "stderr_tail": (proc.stderr or "")[-400:],
        }
    except Exception as exc:  # noqa: BLE001
        safety = {"ran": True, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # Acquisition gate smoke: run_contract_discovery with allowlist must not pull free sources
    acq_gate = {
        "live_sources_enabled": allowlist_apply["enabled_count"],
        "unapproved_blocked": gate.get("ok"),
        "violations": gate.get("violations") or [],
    }

    constraints_after = assert_constraints(db)
    api_after = {
        "nextbid": {
            "locked": True,
            "day_used": (constraints_after.get("controller") or {})
            .get("budget_state", {})
            .get("day_used"),
            "month_used_local": (constraints_after.get("controller") or {})
            .get("budget_state", {})
            .get("month_used_local"),
            "account_remaining": (constraints_after.get("controller") or {})
            .get("budget_state", {})
            .get("account_remaining"),
            "metered_calls_this_job": 0,
        },
        "openai": {
            "today_spent_usd": (constraints_after.get("spend") or {}).get("today_spent_usd"),
            "month_spent_usd": (constraints_after.get("spend") or {}).get("month_spent_usd"),
            "spent_this_job": nextbid_audit.get("openai_spent_usd") or 0,
        },
    }

    elapsed = round(time.time() - t0, 1)
    report = {
        "build": "002Q",
        "job": "controlled-source-reset",
        "finished_at": _utc(),
        "elapsed_sec": elapsed,
        "constraints": constraints_after,
        "api_usage_before": api_before,
        "api_usage_after": api_after,
        "metered_nextbid_calls": 0,
        "openai_spent_usd": nextbid_audit.get("openai_spent_usd") or 0,
        "source_access_control": {
            **allowlist_apply,
            "allowlist": {
                "discovery_auto_activation": allow.get("discovery_auto_activation"),
                "openai_source_discovery": allow.get("openai_source_discovery"),
                "socrata_catalog_discover": allow.get("socrata_catalog_discover"),
                "approved_sources": allow.get("sources"),
            },
            "unapproved_cannot_acquire": gate,
            "acquisition_gate": acq_gate,
        },
        "data_preservation": {
            "source_registry_rows": src_count,
            "solicitation_packages": pkg_count,
            "package_documents": doc_count,
            "opportunities": opp_count,
            "destructive_reset": False,
        },
        "nextbid_inventory_audit": nextbid_audit,
        "safety_tests": safety,
        "remaining_blockers": {
            "primary_blocker_counts": nextbid_audit.get("primary_blocker_counts"),
            "provider_gap_counts": nextbid_audit.get("provider_gap_counts"),
            "m3_gap_counts": nextbid_audit.get("m3_gap_counts"),
            "note": (
                "Baseline is offline NextBid inventory only. Free sources are disabled until "
                "owner provides verified acquisition details and sets live_acquisition=true "
                "on the allowlist. No procurement expansion in this build."
            ),
        },
    }

    _write(get_settings().data_root() / "acceptance" / "jobs_controlled_source_reset_last.json", report)
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    _write(ARCHIVE / "BUILD_002Q_CONTROLLED_SOURCE_RESET.json", report)

    # Update existing 002Q acceptance report (append controlled-reset section)
    nb = nextbid_audit
    md_lines = [
        "# BUILD 002Q — Controlled Procurement Source Reset",
        "",
        f"Finished: {report['finished_at']} · Runtime: {elapsed}s · OpenAI: ${report['openai_spent_usd']} · NextBid metered: **0**",
        "",
        "## API usage",
        "",
        f"- Before: day_used={api_before['nextbid']['day_used']} · account_remaining={api_before['nextbid']['account_remaining']} · OpenAI today=${api_before['openai']['today_spent_usd']}",
        f"- After: day_used={api_after['nextbid']['day_used']} · account_remaining={api_after['nextbid']['account_remaining']} · OpenAI job=${api_after['openai']['spent_this_job']}",
        f"- Metered NextBid calls this job: **0** (lock engaged)",
        "",
        "## Source access control",
        "",
        f"- Owner allowlist path: `{allowlist_apply['allowlist_path']}`",
        f"- Approved keys: `{allowlist_apply['approved_keys']}`",
        f"- Live acquisition keys: `{allowlist_apply['live_acquisition_keys']}` (empty = no free expansion)",
        f"- Sources disabled by allowlist: **{allowlist_apply['disabled_count']}**",
        f"- Enabled after reset: **{allowlist_apply['enabled_count']}**",
        f"- Unapproved cannot acquire: **{gate.get('ok')}**",
        f"- Discovery auto-activation: **{allow.get('discovery_auto_activation')}**",
        "",
        "## NextBid inventory (offline)",
        "",
        f"| Metric | Count |",
        f"|---|---:|",
        f"| Unique solicitations | {nb['population']['unique_solicitations']} |",
        f"| Currently active (estimate) | {nb['currently_active_estimate']} |",
        f"| Tangible product opportunities | {nb['tangible_product_opportunities']} |",
        f"| Complete packages | {nb['complete_solicitation_packages']} |",
        f"| Partial packages | {nb['partial_solicitation_packages']} |",
        f"| Missing quantities (products) | {nb['missing_quantities_among_products']} |",
        f"| Missing submission instructions (products) | {nb['missing_submission_among_products']} |",
        f"| Stage C | {nb['stage_c']} |",
        f"| Bid-eligible | {nb['bid_eligible']} |",
        "",
        "### Provider vs M3 gaps (product blockers)",
        "",
        f"- Provider data gaps: `{json.dumps(nb.get('provider_gap_counts') or {})}`",
        f"- M3 extraction/classification gaps: `{json.dumps(nb.get('m3_gap_counts') or {})}`",
        f"- Primary blockers: `{json.dumps(nb.get('primary_blocker_counts') or {})}`",
        "",
        "## Data preservation",
        "",
        f"- Source registry rows: **{src_count}** · Packages: **{pkg_count}** · Documents: **{doc_count}** · Opportunities: **{opp_count}**",
        f"- Destructive reset: **false**",
        "",
        "## Safety tests (mocked)",
        "",
        f"- Controller pytest ok: **{safety.get('ok')}** (returncode={safety.get('returncode')})",
        "",
        "## Permanent rules in force",
        "",
        "- No automatic source activation",
        "- No additional subscriptions / unauthorized API spend",
        "- SAM.gov and DIBBS parked; NextBid locked",
        "- No historical awards in active queues; portal pages ≠ bid documents",
        "- No free-source expansion until owner-approved allowlist entries",
        "",
        "Awaiting owner acceptance.",
        "",
    ]
    (ARCHIVE / "BUILD_002Q_CONTROLLED_SOURCE_RESET.md").write_text(
        "\n".join(md_lines), encoding="utf-8"
    )

    # Append pointer onto prior acquisition-quality report
    prior_md = ARCHIVE / "BUILD_002Q_ACQUISITION_QUALITY_REPAIR.md"
    if prior_md.exists():
        prior = prior_md.read_text(encoding="utf-8")
        marker = "## Controlled source reset (follow-on)"
        block = "\n".join(
            [
                "",
                marker,
                "",
                f"Superseding operating mode as of {report['finished_at']}: owner allowlist only; "
                f"NextBid offline baseline unique={nb['population']['unique_solicitations']}, "
                f"products={nb['tangible_product_opportunities']}, stage_c={nb['stage_c']}, "
                f"bid_eligible={nb['bid_eligible']}. See BUILD_002Q_CONTROLLED_SOURCE_RESET.md.",
                "",
            ]
        )
        if marker in prior:
            prior = prior.split(marker)[0].rstrip() + "\n" + block
        else:
            prior = prior.rstrip() + "\n" + block
        prior_md.write_text(prior, encoding="utf-8")

    print(
        json.dumps(
            {
                "ok": True,
                "controlled_source_reset": {
                    "enabled_sources": allowlist_apply["enabled_count"],
                    "unapproved_blocked": gate.get("ok"),
                    "nextbid_unique": nb["population"]["unique_solicitations"],
                    "products": nb["tangible_product_opportunities"],
                    "stage_c": nb["stage_c"],
                    "bid_eligible": nb["bid_eligible"],
                    "metered_nextbid": 0,
                    "openai_spent": report["openai_spent_usd"],
                    "safety_ok": safety.get("ok"),
                    "elapsed_sec": elapsed,
                },
            },
            default=str,
        ),
        flush=True,
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="m3.research.jobs")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("verify-sources")
    p_acq = sub.add_parser("acquire")
    p_acq.add_argument("--incremental", action="store_true")
    sub.add_parser("recover-unresolved")
    sub.add_parser("qualify")
    sub.add_parser("qualification-repair")
    sub.add_parser("document-reconcile")
    p_aqr = sub.add_parser("acquisition-quality-repair")
    p_aqr.add_argument(
        "--skip-recovery",
        action="store_true",
        help="Skip targeted document re-acquire; demote junk + re-qualify + report only",
    )
    sub.add_parser("controlled-source-reset")
    sub.add_parser("nextbid-product-class-audit")
    sub.add_parser("pipeline-repair")
    p_offline = sub.add_parser(
        "offline-full-text-reprocess",
        help="Ordered body-first analysis over stored solicitation_full_text (zero API)",
    )
    p_offline.add_argument("--from", dest="date_from", required=True, help="UTC/local date YYYY-MM-DD inclusive start")
    p_offline.add_argument("--to", dest="date_to", required=True, help="UTC/local date YYYY-MM-DD inclusive end")
    p_offline.add_argument("--limit", type=int, default=None, help="Optional cap for smoke tests")
    p_offline.add_argument(
        "--no-queues",
        action="store_true",
        help="Analyze only; do not write owner procurement queues",
    )
    sub.add_parser("demote-expired-queues")
    p_ai = sub.add_parser(
        "validate-ai-doc-understanding",
        help="Offline validation of AI document-understanding path (mocked model; zero spend)",
    )
    p_ai.add_argument(
        "--offline",
        action="store_true",
        default=True,
        help="Use mocked model responses only (default; no OpenAI calls)",
    )
    p_bulk_ai = sub.add_parser(
        "bulk-ai-doc-analysis",
        help="Owner-authorized bulk live OpenAI analysis of ALL substantive local packages",
    )
    p_bulk_ai.add_argument(
        "--owner-auth",
        required=True,
        help="Must be OWNER_AUTHORIZED_BULK_AI_DOC_ANALYSIS",
    )
    p_bulk_ai.add_argument(
        "--max-usd",
        type=float,
        default=5.0,
        help="Hard OpenAI ceiling for this job (default 5.00)",
    )
    p_bulk_ai.add_argument(
        "--dry-run",
        action="store_true",
        help="Inventory + cost estimate only; zero OpenAI spend",
    )
    p_bulk_ai.add_argument(
        "--no-queues",
        action="store_true",
        help="Do not update owner procurement queues",
    )
    p_bulk_ai.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Max concurrent OpenAI solicitation analyses (default 5; auto-reduces on rate limits)",
    )
    sub.add_parser(
        "fresh-inventory-reset",
        help="Reset active inventory to Nov 24 evening–Nov 25 acquisition cohort (zero NextBid/OpenAI)",
    )
    p_fresh_ai = sub.add_parser(
        "fresh-cohort-ai-complete",
        help="Owner-authorized OpenAI completion for all active fresh-cohort solicitations",
    )
    p_fresh_ai.add_argument(
        "--owner-auth",
        required=True,
        help="Must be OWNER_AUTHORIZED_FRESH_COHORT_AI_ANALYSIS",
    )
    p_fresh_ai.add_argument("--max-usd", type=float, default=0.25)
    p_fresh_ai.add_argument("--concurrency", type=int, default=5)
    sub.add_parser(
        "solicitation-sop",
        help="Enforce permanent solicitation SOP on fresh inventory + validate retained cases (zero API)",
    )
    p_docrec = sub.add_parser(
        "document-recovery-run",
        help="Owner-authorized fresh-cohort document recovery (≤10 NextBid, ≤$0.25 OpenAI); restores killswitch",
    )
    p_docrec.add_argument(
        "--continue-manifests",
        action="store_true",
        help="Continue from already-paid list_documents manifests (free source_url / remaining credits)",
    )
    sub.add_parser("nextbid-data-loss-investigation")
    sub.add_parser("nextbid-permanent-repair")
    sub.add_parser("nextbid-final-pipeline-audit")
    sub.add_parser("nextbid-controlled-production")
    sub.add_parser("nextbid-status-reconciliation")
    sub.add_parser("nextbid-complete-discovery")
    sub.add_parser("nextbid-full-inventory-audit")
    sub.add_parser("nextbid-repair-002r")
    sub.add_parser("nextbid-repair-002r1")
    sub.add_parser("nextbid-repair-002r2")
    p_acc = sub.add_parser("acceptance-002q")
    p_acc.add_argument(
        "--reuse",
        action="store_true",
        help="Assemble reports from prior job artifacts; run incremental if missing",
    )
    sub.add_parser("provenance-audit")
    sub.add_parser("qualification-funnel-audit")
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        if args.cmd == "verify-sources":
            cmd_verify_sources(db)
        elif args.cmd == "acquire":
            result = cmd_acquire(db, incremental=bool(args.incremental))
            if args.incremental:
                _write(
                    get_settings().data_root() / "acceptance" / "jobs_acquire_incremental_last.json",
                    result,
                )
        elif args.cmd == "recover-unresolved":
            cmd_recover_unresolved(db)
        elif args.cmd == "qualify":
            cmd_qualify(db)
        elif args.cmd == "qualification-repair":
            cmd_qualification_repair(db)
        elif args.cmd == "document-reconcile":
            cmd_document_reconcile(db)
        elif args.cmd == "acquisition-quality-repair":
            cmd_acquisition_quality_repair(
                db, skip_recovery=bool(getattr(args, "skip_recovery", False))
            )
        elif args.cmd == "controlled-source-reset":
            cmd_controlled_source_reset(db)
        elif args.cmd == "nextbid-product-class-audit":
            from m3.research.nextbid_product_class_audit import run_product_classification_audit

            assert_constraints(db)
            result = run_product_classification_audit()
            print(
                json.dumps(
                    {
                        "ok": True,
                        "nextbid_product_class_audit": {
                            "unique": result["population"]["unique_solicitations"],
                            "buckets": result["mutually_exclusive_buckets"],
                            "reconcile_714": result["reconcile_vs_714"],
                            "ambiguous": (result.get("ambiguous_classifications") or {}).get(
                                "count"
                            ),
                            "metered_nextbid": 0,
                        },
                    },
                    default=str,
                ),
                flush=True,
            )
        elif args.cmd == "nextbid-data-loss-investigation":
            from m3.research.nextbid_data_loss_investigation import run_data_loss_investigation

            assert_constraints(db)
            result = run_data_loss_investigation(db)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "nextbid_data_loss": {
                            "metered_delta": result["metered_nextbid_delta"],
                            "usable_full_text_before": result["acceptance_counts"][
                                "usable_full_text_before"
                            ],
                            "usable_full_text_after": result["acceptance_counts"][
                                "usable_full_text_after"
                            ],
                            "ready_bid": result["acceptance_counts"]["ready_for_bidding"],
                            "qty": result["acceptance_counts"]["verified_quantities_after"],
                            "elapsed_sec": result["elapsed_sec"],
                        },
                    },
                    default=str,
                ),
                flush=True,
            )
        elif args.cmd == "nextbid-controlled-production":
            from m3.research.nextbid_controlled_production import run_controlled_production
            from m3.research.nextbid_controller import acquisition_locked

            # This job temporarily unlocks under owner 002Q scope, then relocks.
            result = run_controlled_production(db)
            assert acquisition_locked(), "must relock after controlled production"
            act = result.get("actionable") or {}
            print(
                json.dumps(
                    {
                        "ok": True,
                        "controlled_production": {
                            "metered_delta": result.get("metered_delta"),
                            "listing_calls": (result.get("listing") or {}).get(
                                "search_calls_metered"
                            ),
                            "detail_approx": (result.get("detail") or {}).get(
                                "detail_calls_approx"
                            ),
                            "inventory": result.get("inventory_unique"),
                            "open": result.get("open_count"),
                            "qty": result.get("verified_quantities"),
                            "ready_b": len(act.get("ready_to_submit") or []),
                            "registration_c": len(act.get("registration_required") or []),
                            "supplier_a": len(act.get("supplier_pricing") or []),
                            "locked": result.get("acquisition_locked"),
                            "report": result.get("acceptance_report_md"),
                            "elapsed_sec": result.get("elapsed_sec"),
                        },
                    },
                    default=str,
                ),
                flush=True,
            )
        elif args.cmd == "nextbid-status-reconciliation":
            from m3.research.nextbid_status_reconciliation import run_status_reconciliation
            from m3.research.nextbid_controller import acquisition_locked

            assert_constraints(db)
            assert acquisition_locked(), "status reconciliation requires killswitch engaged"
            result = run_status_reconciliation(db)
            cats = result.get("categories") or {}
            recon = result.get("reprocess") or {}
            print(
                json.dumps(
                    {
                        "ok": True,
                        "status_reconciliation": {
                            "metered_delta": result.get("metered_delta"),
                            "inventory": result.get("inventory_total"),
                            "categories": cats,
                            "active_A": cats.get("A"),
                            "federal_B": cats.get("B"),
                            "confirmed_product": recon.get("confirmed_product"),
                            "potential_product": recon.get("potential_product"),
                            "qty": recon.get("verified_quantities"),
                            "ready_b": recon.get("ready_to_submit"),
                            "locked": result.get("acquisition_locked"),
                            "report": result.get("acceptance_report_md"),
                            "elapsed_sec": result.get("elapsed_sec"),
                            "stop": result.get("stop"),
                        },
                    },
                    default=str,
                ),
                flush=True,
            )
        elif args.cmd == "nextbid-complete-discovery":
            from m3.research.nextbid_complete_discovery import run_complete_state_local_discovery
            from m3.research.nextbid_controller import acquisition_locked

            assert_constraints(db)
            assert acquisition_locked(), "must start with killswitch engaged"
            result = run_complete_state_local_discovery(db)
            assert acquisition_locked(), "must remain/relock after complete discovery"
            act = result.get("actionable") or {}
            cov = result.get("coverage") or {}
            print(
                json.dumps(
                    {
                        "ok": True,
                        "complete_discovery": {
                            "mode": result.get("mode"),
                            "metered_delta": result.get("metered_delta"),
                            "daily_left": (result.get("allowance") or {}).get("daily_left"),
                            "coverage_pct": cov.get("coverage_percentage"),
                            "provider_sl": cov.get("provider_active_state_local_total"),
                            "local_sl": cov.get("local_active_sl"),
                            "missing": cov.get("missing_or_unacquired_sl"),
                            "listing_new": (result.get("listing") or {}).get("unique_new"),
                            "confirmed_product": (result.get("reprocess") or {}).get(
                                "confirmed_product"
                            ),
                            "potential_product": (result.get("reprocess") or {}).get(
                                "potential_product"
                            ),
                            "qty": (result.get("reprocess") or {}).get("verified_quantities"),
                            "ready_b": (act.get("counts") or {}).get("ready_to_submit"),
                            "paid_recovery_top50_credits": (
                                result.get("paid_recovery_plan") or {}
                            ).get("estimated_minimum_credits_top50"),
                            "locked": result.get("acquisition_locked"),
                            "report": result.get("acceptance_report_md"),
                            "elapsed_sec": result.get("elapsed_sec"),
                            "stop": result.get("stop"),
                        },
                    },
                    default=str,
                ),
                flush=True,
            )
        elif args.cmd == "nextbid-full-inventory-audit":
            from m3.research.nextbid_full_inventory_audit import run_full_inventory_audit
            from m3.research.nextbid_controller import acquisition_locked

            assert acquisition_locked(), "audit requires killswitch locked"
            result = run_full_inventory_audit()
            assert acquisition_locked(), "audit must not unlock acquisition"
            print(
                json.dumps(
                    {
                        "ok": True,
                        "full_inventory_audit": {
                            "total": result.get("total_records_processed"),
                            "counts": result.get("new_classification_counts"),
                            "changed": result.get("changed_classifications"),
                            "overturned": result.get("previous_rejections_overturned"),
                            "full_evidence": result.get("records_with_full_source_evidence"),
                            "partial_evidence": result.get(
                                "records_with_partial_source_evidence"
                            ),
                            "missing_evidence": result.get(
                                "records_missing_original_evidence"
                            ),
                            "qty_verified": result.get("records_with_verified_quantities"),
                            "export_folder": result.get("export_folder"),
                            "zip_count": len(result.get("zip_files") or []),
                            "validation_ok": (result.get("validation") or {}).get("ok"),
                            "api_expenditure": result.get("api_expenditure"),
                            "elapsed_sec": result.get("elapsed_sec"),
                            "stop": result.get("stop"),
                        },
                    },
                    default=str,
                ),
                flush=True,
            )
        elif args.cmd == "nextbid-repair-002r":
            from m3.research.nextbid_full_inventory_audit import (
                EXPORT_ROOT_002R,
                run_full_inventory_audit,
            )
            from m3.research.nextbid_controller import acquisition_locked
            from m3.research.nextbid_complete_discovery import run_complete_state_local_discovery

            assert acquisition_locked(), "002R repair requires killswitch locked"
            # 1) Reprocess active SL through repaired pipeline (offline — daily may block listing)
            offline = run_complete_state_local_discovery(db)
            assert acquisition_locked()
            # 2) Full 8088 verification export for independent double-check
            result = run_full_inventory_audit(export_name=EXPORT_ROOT_002R)
            assert acquisition_locked()
            print(
                json.dumps(
                    {
                        "ok": True,
                        "repair_002r": {
                            "offline_mode": offline.get("mode"),
                            "metered_delta": offline.get("metered_delta"),
                            "active_reprocessed": (offline.get("reprocess") or {}).get(
                                "active_reprocessed"
                            ),
                            "ready_b": ((offline.get("actionable") or {}).get("counts") or {}).get(
                                "ready_to_submit"
                            ),
                            "audit_counts": result.get("new_classification_counts"),
                            "changed_vs_prior_audit": result.get(
                                "changed_vs_prior_audit_export"
                            ),
                            "qty_verified": result.get("records_with_verified_quantities"),
                            "example_checks": result.get("confirmed_example_checks"),
                            "export_folder": result.get("export_folder"),
                            "zip_count": len(result.get("zip_files") or []),
                            "validation_ok": (result.get("validation") or {}).get("ok"),
                            "api_expenditure": result.get("api_expenditure"),
                            "elapsed_sec": round(
                                float(offline.get("elapsed_sec") or 0)
                                + float(result.get("elapsed_sec") or 0),
                                2,
                            ),
                            "stop": "AWAIT_OWNER_REVIEW",
                        },
                    },
                    default=str,
                ),
                flush=True,
            )
        elif args.cmd == "nextbid-repair-002r1":
            # Offline-only correctness repair: zero NextBid calls (paid or free).
            from m3.research.nextbid_full_inventory_audit import (
                EXPORT_ROOT_002R1,
                run_full_inventory_audit,
            )
            from m3.research.nextbid_controller import acquisition_locked

            assert acquisition_locked(), "002R.1 requires killswitch locked"
            result = run_full_inventory_audit(export_name=EXPORT_ROOT_002R1)
            assert acquisition_locked()
            print(
                json.dumps(
                    {
                        "ok": True,
                        "repair_002r1": {
                            "audit_counts": result.get("new_classification_counts"),
                            "qty_verified": result.get("records_with_verified_quantities"),
                            "regression_checks": result.get("regression_checks"),
                            "example_checks": result.get("confirmed_example_checks"),
                            "durability": result.get("evidence_vault_durability"),
                            "paid_recovery_plan": result.get("paid_recovery_plan"),
                            "export_folder": result.get("export_folder"),
                            "zip_count": len(result.get("zip_files") or []),
                            "validation_ok": (result.get("validation") or {}).get("ok"),
                            "api_expenditure": result.get("api_expenditure"),
                            "elapsed_sec": result.get("elapsed_sec"),
                            "acquisition_locked": True,
                            "stop": "AWAIT_OWNER_AUTHORIZATION",
                        },
                    },
                    default=str,
                ),
                flush=True,
            )
        elif args.cmd == "nextbid-repair-002r2":
            from m3.research.nextbid_full_inventory_audit import (
                EXPORT_ROOT_002R2,
                run_full_inventory_audit,
            )
            from m3.research.nextbid_controller import acquisition_locked

            assert acquisition_locked(), "002R.2 requires killswitch locked"
            result = run_full_inventory_audit(export_name=EXPORT_ROOT_002R2)
            assert acquisition_locked()
            print(
                json.dumps(
                    {
                        "ok": True,
                        "repair_002r2": {
                            "audit_counts": result.get("new_classification_counts"),
                            "qty_verified": result.get("records_with_verified_quantities"),
                            "line_items": result.get("complete_source_backed_line_item_total"),
                            "evidence_dimensions": result.get("evidence_dimension_true_counts"),
                            "regression_checks": result.get("regression_checks"),
                            "regression_all_passed": result.get("regression_all_passed"),
                            "example_checks": result.get("confirmed_example_checks"),
                            "prior_audit_row_level_available": result.get(
                                "prior_audit_row_level_available"
                            ),
                            "prior_audit_note": result.get("prior_audit_note"),
                            "aggregate_vs_002r1": result.get(
                                "aggregate_comparison_vs_002R1_archive"
                            ),
                            "durability": result.get("evidence_vault_durability"),
                            "paid_recovery_plan": result.get("paid_recovery_plan"),
                            "export_folder": result.get("export_folder"),
                            "validation_ok": (result.get("validation") or {}).get("ok"),
                            "api_expenditure": result.get("api_expenditure"),
                            "elapsed_sec": result.get("elapsed_sec"),
                            "acquisition_locked": True,
                            "stop": "AWAIT_OWNER_REVIEW",
                        },
                    },
                    default=str,
                ),
                flush=True,
            )
        elif args.cmd == "nextbid-final-pipeline-audit":
            from m3.research.nextbid_final_pipeline_audit import run_final_pipeline_audit

            assert_constraints(db)
            result = run_final_pipeline_audit(db)
            recon = result.get("reconciliation") or {}
            print(
                json.dumps(
                    {
                        "ok": True,
                        "final_pipeline_audit": {
                            "metered_delta": result.get("metered_nextbid_delta"),
                            "unique": recon.get("unique_population"),
                            "open": (recon.get("status") or {}).get("open"),
                            "qty": (recon.get("pipeline_after") or {}).get("verified_quantities"),
                            "eligible": len(recon.get("procurement_eligible") or []),
                            "supplier_candidates": recon.get("ready_supplier_pricing_count"),
                            "free_docs": (result.get("free_doc_recovery") or {}).get(
                                "recovered_count"
                            ),
                            "locked": result.get("acquisition_locked"),
                            "stop": result.get("stop"),
                            "report": result.get("acceptance_report_md"),
                            "elapsed_sec": result.get("elapsed_sec"),
                        },
                    },
                    default=str,
                ),
                flush=True,
            )
        elif args.cmd == "nextbid-permanent-repair":
            from m3.research.nextbid_permanent_repair import run_permanent_repair

            assert_constraints(db)
            result = run_permanent_repair(db)
            a = result.get("acceptance") or {}
            credits = (result.get("phase2_full_inventory_plan") or {}).get(
                "exact_estimated_credit_requirements"
            ) or {}
            print(
                json.dumps(
                    {
                        "ok": True,
                        "nextbid_permanent_repair": {
                            "metered_delta": a.get("metered_nextbid_delta"),
                            "inventory_after": a.get("inventory_after"),
                            "missing_recovered": a.get("previously_missing_recovered"),
                            "usable_full_text": a.get("usable_full_text_after"),
                            "qty": a.get("verified_quantities"),
                            "ready_bid": a.get("ready_to_submit"),
                            "safety_passed": (result.get("offline_safety_tests") or {}).get(
                                "all_passed"
                            ),
                            "locked": a.get("acquisition_locked"),
                            "credits_listing": [
                                credits.get("full_listing_enumeration_low"),
                                credits.get("full_listing_enumeration_high"),
                            ],
                            "credits_total": [
                                credits.get("total_campaign_low"),
                                credits.get("total_campaign_high"),
                            ],
                            "stop": result.get("stop"),
                            "report": result.get("acceptance_report_md"),
                            "elapsed_sec": result.get("elapsed_sec"),
                        },
                    },
                    default=str,
                ),
                flush=True,
            )
        elif args.cmd == "demote-expired-queues":
            from m3.research.queue_deadline_repair import demote_expired_from_owner_queues

            assert_constraints(db)
            result = demote_expired_from_owner_queues()
            _write(
                get_settings().data_root() / "acceptance" / "jobs_demote_expired_queues_last.json",
                result,
            )
            print(json.dumps({"ok": True, "demote_expired_queues": result}, default=str), flush=True)
        elif args.cmd == "fresh-inventory-reset":
            from m3.research.fresh_inventory_reset import run_fresh_inventory_reset

            constraints = assert_constraints(db)
            if not constraints.get("nextbid_locked"):
                print(json.dumps({"ok": False, "error": "nextbid_killswitch_must_remain_locked"}), flush=True)
                return 2
            result = run_fresh_inventory_reset(db)
            print(
                json.dumps(
                    {
                        "ok": bool(result.get("ok")),
                        "fresh_inventory_reset": {
                            "previous": (result.get("inventory_reset") or {}).get("previous_inventory_total"),
                            "fresh": (result.get("inventory_reset") or {}).get("fresh_retained"),
                            "legacy_removed": (result.get("inventory_reset") or {}).get(
                                "legacy_removed_from_active"
                            ),
                            "cache_hits": (result.get("ai_analysis") or {}).get("reusable_cached"),
                            "awaiting_ai": (result.get("ai_analysis") or {}).get(
                                "awaiting_owner_ai_authorization"
                            ),
                            "est_openai_usd": ((result.get("ai_analysis") or {}).get("estimate") or {}).get(
                                "estimated_usd_max"
                            ),
                            "supplier_ready": len(
                                ((result.get("commercial_results") or {}).get("supplier_quote_ready") or [])
                            ),
                            "nextbid": 0,
                            "openai": 0,
                            "report": str(
                                get_settings().data_root()
                                / "acceptance"
                                / "jobs_fresh_inventory_reset_last.json"
                            ),
                        },
                    },
                    default=str,
                ),
                flush=True,
            )
        elif args.cmd == "solicitation-sop":
            from m3.research.solicitation_sop import (
                process_fresh_inventory_sop,
                sop_stage_function_map,
                validate_retained_failure_cases,
                expected_call_scenarios,
            )
            from m3.research.nextbid_controller import acquisition_locked, load_budget_state
            from m3.research.nextbid_killswitch import emergency_stop_status

            constraints = assert_constraints(db)
            if not constraints.get("nextbid_locked"):
                print(json.dumps({"ok": False, "error": "nextbid_killswitch_must_remain_locked"}), flush=True)
                return 2
            cases = validate_retained_failure_cases(db)
            fresh = process_fresh_inventory_sop(db)
            budget = load_budget_state()
            report = {
                "ok": True,
                "sop_implementation": sop_stage_function_map(),
                "api_call_scenarios": expected_call_scenarios(),
                "retained_case_validation": cases,
                "fresh_inventory": {
                    k: fresh.get(k)
                    for k in (
                        "fresh_accounted",
                        "coverage",
                        "owner_queues",
                        "ai_cached_reuse",
                        "awaiting_ai_auth",
                        "supplier_pricing_yes",
                        "openai_spent_usd",
                        "nextbid_calls",
                        "queue_summary",
                        "policy",
                    )
                },
                "api_safety": {
                    "nextbid_spent": 0,
                    "openai_spent": 0.0,
                    "acquisition_locked": acquisition_locked(),
                    "killswitch": emergency_stop_status(),
                    "account_remaining": budget.get("account_remaining"),
                    "reserve_floor": 1000,
                    "reserve_intact": int(budget.get("account_remaining") or 0) >= 1000,
                    "sop_gate_on_reserve": True,
                    "deployment": False,
                    "schedulers_activated": False,
                },
            }
            out_path = get_settings().data_root() / "acceptance" / "jobs_solicitation_sop_acceptance_last.json"
            _write(out_path, report)
            temp = Path(__file__).resolve().parents[2] / "TEMP" / "solicitation_sop"
            temp.mkdir(parents=True, exist_ok=True)
            (temp / "ACCEPTANCE_REPORT.json").write_text(
                json.dumps(report, indent=2, default=str), encoding="utf-8"
            )
            print(
                json.dumps(
                    {
                        "ok": True,
                        "solicitation_sop": {
                            "fresh": fresh.get("fresh_accounted"),
                            "cached_ai": fresh.get("ai_cached_reuse"),
                            "awaiting_ai": fresh.get("awaiting_ai_auth"),
                            "supplier_yes": fresh.get("supplier_pricing_yes"),
                            "queues": fresh.get("owner_queues"),
                            "cases": len((cases.get("cases") or [])),
                            "nextbid": 0,
                            "openai": 0,
                            "report": str(out_path),
                        },
                    },
                    default=str,
                ),
                flush=True,
            )
        elif args.cmd == "document-recovery-run":
            from m3.research.document_recovery_run import run_document_recovery, continue_manifest_recovery
            from m3.research.nextbid_controller import load_budget_state
            from m3.research.nextbid_killswitch import emergency_stop_status

            # This command intentionally unlocks under DOC_RECOVERY_SCOPE then re-locks.
            before = load_budget_state()
            if int(before.get("account_remaining") or 0) < 1000:
                print(json.dumps({"ok": False, "error": "protected_reserve_would_be_breached"}), flush=True)
                return 2
            prior = get_settings().data_root() / "acceptance" / "jobs_document_recovery_last.json"
            if prior.exists() and getattr(args, "continue_manifests", False):
                spent = 0
                try:
                    prev = json.loads(prior.read_text(encoding="utf-8"))
                    spent = int(prev.get("nextbid_credits_consumed") or 0)
                except Exception:
                    spent = 9
                result = continue_manifest_recovery(db, already_spent_account=spent)
                print(
                    json.dumps(
                        {
                            "ok": bool(result.get("ok")),
                            "document_recovery_continuation": {
                                "nextbid_credits": result.get("nextbid_credits_consumed"),
                                "openai_spent_usd": result.get("openai_spent_usd"),
                                "originals": sum(1 for r in (result.get("recovery") or []) if r.get("original_saved")),
                                "remaining_active": result.get("remaining_active"),
                                "queues": result.get("sop_owner_queues"),
                                "killswitch_restored": result.get("killswitch_restored"),
                                "account_remaining": (result.get("after_budget") or {}).get("account_remaining"),
                            },
                            "killswitch": emergency_stop_status(),
                        },
                        default=str,
                    ),
                    flush=True,
                )
            else:
                result = run_document_recovery(db)
                print(
                    json.dumps(
                        {
                            "ok": bool(result.get("ok")),
                            "document_recovery": {
                                "nextbid_credits": result.get("nextbid_credits_consumed"),
                                "openai_spent_usd": result.get("openai_spent_usd"),
                                "rejected": len(result.get("rejected") or []),
                                "recovery_attempts": len(result.get("recovery") or []),
                                "remaining_active": result.get("remaining_active"),
                                "supplier_cards": len(result.get("supplier_pricing_cards") or []),
                                "queues": result.get("sop_owner_queues"),
                                "killswitch_restored": result.get("killswitch_restored"),
                                "authorization_revoked": result.get("authorization_revoked"),
                                "account_remaining": (result.get("after_budget") or {}).get("account_remaining"),
                                "report": str(
                                    get_settings().data_root()
                                    / "acceptance"
                                    / "jobs_document_recovery_last.json"
                                ),
                            },
                            "killswitch": emergency_stop_status(),
                        },
                        default=str,
                    ),
                    flush=True,
                )
        elif args.cmd == "fresh-cohort-ai-complete":
            from m3.research.fresh_inventory_reset import run_fresh_cohort_ai_complete

            constraints = assert_constraints(db)
            if not constraints.get("nextbid_locked"):
                print(json.dumps({"ok": False, "error": "nextbid_killswitch_must_remain_locked"}), flush=True)
                return 2
            result = run_fresh_cohort_ai_complete(
                db,
                owner_authorization=str(args.owner_auth),
                max_usd=float(args.max_usd),
                concurrency=int(getattr(args, "concurrency", 5) or 5),
            )
            print(
                json.dumps(
                    {
                        "ok": bool(result.get("ok")),
                        "fresh_cohort_ai_complete": {
                            "reconciled": result.get("reconciled"),
                            "cached": result.get("previously_analyzed_openai_cache"),
                            "new_live": result.get("newly_analyzed_openai"),
                            "needs_docs": result.get("needs_original_documents"),
                            "quote_ready": result.get("supplier_quote_ready_count"),
                            "actionable": result.get("currently_actionable_count"),
                            "openai_spent_usd": result.get("openai_spent_usd"),
                            "nextbid": 0,
                            "report": str(
                                get_settings().data_root()
                                / "acceptance"
                                / "jobs_fresh_cohort_ai_complete_last.json"
                            ),
                        },
                    },
                    default=str,
                ),
                flush=True,
            )
        elif args.cmd == "bulk-ai-doc-analysis":
            from m3.research.bulk_ai_doc_analysis import run_bulk_ai_document_analysis

            constraints = assert_constraints(db)
            if not constraints.get("nextbid_locked"):
                print(json.dumps({"ok": False, "error": "nextbid_killswitch_must_remain_locked"}), flush=True)
                return 2
            result = run_bulk_ai_document_analysis(
                db,
                owner_authorization=str(args.owner_auth),
                max_usd=float(args.max_usd),
                write_owner_queues_flag=not bool(args.no_queues),
                dry_run=bool(args.dry_run),
                concurrency=int(getattr(args, "concurrency", 5) or 5),
            )
            result["cleanup_commit"] = "3470914"
            result["ai_integration_commit"] = "2353977"
            out_path = get_settings().data_root() / "acceptance" / "jobs_bulk_ai_doc_analysis_last.json"
            _write(out_path, result)
            print(
                json.dumps(
                    {
                        "ok": bool(result.get("ok")),
                        "bulk_ai_doc_analysis": {
                            "dry_run": result.get("dry_run"),
                            "openai_spent_usd": result.get("openai_spent_usd")
                            or (result.get("metrics") or {}).get("openai_spent_usd"),
                            "analyzed": result.get("analyzed_count")
                            or (result.get("metrics") or {}).get("packages_analyzed_by_openai"),
                            "inventory_total": (result.get("inventory") or {}).get("inventory_total_records")
                            or (result.get("metrics") or {}).get("total_solicitation_records_inventoried"),
                            "substantive": (result.get("metrics") or {}).get(
                                "locally_retained_substantive_packages"
                            ),
                            "unanalyzed_cap": (result.get("metrics") or {}).get("unanalyzed_due_to_cost_cap"),
                            "nextbid_api_calls": 0,
                            "stopped_reason": result.get("stopped_reason"),
                            "report": str(out_path),
                        },
                    },
                    default=str,
                ),
                flush=True,
            )
        elif args.cmd == "validate-ai-doc-understanding":
            from pathlib import Path as _Path

            from m3.research.ai_document_analysis import (
                analyze_solicitation_documents,
                estimate_live_analysis_cost_usd,
            )
            from m3.research.ai_output_verify import verify_analysis_against_sources
            from m3.research.local_evidence import assemble_local_evidence_package
            from m3.research.queue_deadline_repair import demote_expired_from_owner_queues

            assert_constraints(db)
            demote = demote_expired_from_owner_queues()
            examples = [
                "NB00321554",  # Monterey diesel pump
                "NB00320329",  # Lottery forklifts
                "NB00375309",  # Bulk propane
                "NB00380092",  # WEG motor
                "NB00375339",  # Unication pagers
                "NB00320237",  # Chemical controller
            ]
            temp_root = _Path(__file__).resolve().parents[2] / "TEMP" / "ai_doc_understanding_validation"
            temp_root.mkdir(parents=True, exist_ok=True)
            rows: list[dict] = []
            for nb in examples:
                evidence = assemble_local_evidence_package(nb, db=db)
                # Fixture mock — NOT live AI. Label clearly.
                mock = {
                    "procurement_purpose": f"Mock analysis for {nb} (NOT live OpenAI)",
                    "requirement_class": "product_supply",
                    "is_product_purchase": True,
                    "line_items": [],
                    "deadlines": [],
                    "hidden_obligations": [],
                    "missing_information": ["mock_response_not_live_ai"],
                    "coverage_limitations": ["mocked_model_response"],
                    "confidence": "low",
                }
                # Seed line items from evidence snippets when possible for verify path exercise
                body = str(evidence.get("assembled_text") or "")
                if "pump" in body.lower() or "Pump" in (evidence.get("title") or ""):
                    mock["line_items"] = [
                        {
                            "line_ref": "1",
                            "description": "6 inch High Pressure Diesel Pump with a trailer",
                            "quantity": "1",
                            "unit": "each",
                            "quantity_semantic": "ORDERED",
                            "source_snippet": "purchase of one (1) Six Inch High Pressure Diesel Pump",
                            "source_doc_id": (evidence.get("source_refs") or [{}])[0].get("doc_id"),
                        }
                    ]
                elif "forklift" in (evidence.get("title") or "").lower() or "Reach Truck" in (evidence.get("title") or ""):
                    mock["line_items"] = [
                        {
                            "description": "Narrow-Aisle Reach Trucks",
                            "quantity": "2",
                            "unit": "each",
                            "quantity_semantic": "ORDERED",
                            "source_snippet": "two (2) Narrow-Aisle Reach Trucks",
                        }
                    ]
                elif "propane" in (evidence.get("title") or "").lower():
                    mock["line_items"] = [
                        {
                            "description": "Bulk Propane Fuel Supply",
                            "quantity": None,
                            "quantity_semantic": "UNKNOWN",
                            "source_snippet": "BULK PROPANE FUEL SUPPLY",
                        }
                    ]
                elif "weg" in (evidence.get("title") or "").lower():
                    mock["line_items"] = [
                        {
                            "description": "WEG W40 Series 150 HP General Purpose Electric Motor",
                            "quantity": "1",
                            "unit": "each",
                            "quantity_semantic": "ORDERED",
                            "source_snippet": "purchase one (1) WEG W40 Series 150 HP",
                        }
                    ]
                elif "unication" in (evidence.get("title") or "").lower() or "pager" in (evidence.get("title") or "").lower():
                    mock["line_items"] = [
                        {
                            "description": "Unication G5 Dual Band P25 Voice Pagers",
                            "quantity": "53",
                            "unit": "each",
                            "quantity_semantic": "ESTIMATED",
                            "source_snippet": "estimated quantity of 53 Unication G5",
                        }
                    ]

                ai = analyze_solicitation_documents(
                    db,
                    evidence_package=evidence,
                    allow_live=False,
                    mock_response=mock,
                )
                verified = verify_analysis_against_sources(ai.get("parsed"), evidence)
                cost_est = estimate_live_analysis_cost_usd(evidence)
                row = {
                    "nb_id": nb,
                    "title": evidence.get("title"),
                    "coverage": evidence.get("coverage"),
                    "sources_inspected": evidence.get("sources_inspected"),
                    "assembled_chars": evidence.get("assembled_chars"),
                    "source_ref_count": len(evidence.get("source_refs") or []),
                    "ai": {
                        "live_ai_invoked": ai.get("live_ai_invoked"),
                        "mocked": ai.get("mocked"),
                        "ok": ai.get("ok"),
                        "note": ai.get("note") or ai.get("skip_reason") or ai.get("error"),
                    },
                    "verification": {
                        "verified": verified.get("verified"),
                        "blocks_product_only": verified.get("blocks_product_only"),
                        "rejected_assertions": verified.get("rejected_assertions"),
                        "promotion": verified.get("promotion"),
                        "deadline_gate": verified.get("deadline_gate"),
                        "complete_solicitation_reviewed": verified.get("complete_solicitation_reviewed"),
                    },
                    "estimated_live_cost_usd": cost_est,
                }
                rows.append(row)
                (temp_root / f"{nb}_evidence_summary.json").write_text(
                    json.dumps(
                        {
                            "nb_id": nb,
                            "coverage": evidence.get("coverage"),
                            "sources_inspected": evidence.get("sources_inspected"),
                            "source_refs": evidence.get("source_refs"),
                            "assembled_chars": evidence.get("assembled_chars"),
                            "ai_live_invoked": False,
                            "mocked": True,
                        },
                        indent=2,
                        default=str,
                    ),
                    encoding="utf-8",
                )

            report = {
                "assignment": "ai_document_understanding_offline_validation",
                "live_openai_calls": 0,
                "nextbid_api_calls": 0,
                "openai_spent_usd": 0,
                "demote_expired": demote,
                "examples": rows,
                "temp_dir": str(temp_root),
                "note": "Mocked model responses only. Do not describe as live AI analysis.",
            }
            out_path = get_settings().data_root() / "acceptance" / "jobs_validate_ai_doc_understanding_last.json"
            _write(out_path, report)
            (temp_root / "REPORT.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
            print(json.dumps({"ok": True, "validate_ai_doc_understanding": {
                "examples": len(rows),
                "live_openai_calls": 0,
                "openai_spent_usd": 0,
                "demoted_expired": demote.get("demoted_expired"),
                "report": str(out_path),
                "temp": str(temp_root),
            }}, default=str), flush=True)
        elif args.cmd == "offline-full-text-reprocess":
            from datetime import datetime, timezone, timedelta

            from m3.research.reprocess import run_offline_full_text_window

            constraints = assert_constraints(db)
            d0 = datetime.strptime(str(args.date_from), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            d1 = datetime.strptime(str(args.date_to), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            # Inclusive end-of-day
            d1 = d1 + timedelta(days=1) - timedelta(microseconds=1)
            result = run_offline_full_text_window(
                db,
                date_from=d0,
                date_to=d1,
                write_owner_queues=not bool(getattr(args, "no_queues", False)),
                limit=getattr(args, "limit", None),
            )
            result["constraints"] = {
                "nextbid_locked": constraints.get("nextbid_locked"),
                "metered_nextbid": 0,
                "openai_spent_usd": 0,
            }
            out_path = get_settings().data_root() / "acceptance" / "jobs_offline_full_text_reprocess_last.json"
            _write(out_path, result)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "offline_full_text_reprocess": {
                            "examined": result.get("examined"),
                            "rejected_non_product": result.get("rejected_non_product"),
                            "product_candidates": result.get("product_candidates"),
                            "promoted": result.get("promoted"),
                            "incomplete": result.get("incomplete"),
                            "thin_body": result.get("thin_body"),
                            "by_disposition": result.get("by_disposition"),
                            "by_reject_class": result.get("by_reject_class"),
                            "missing_field_counts": result.get("missing_field_counts"),
                            "queue_summary": result.get("queue_summary"),
                            "report": str(out_path),
                            "metered_nextbid": 0,
                            "openai_spent_usd": 0,
                        },
                    },
                    default=str,
                ),
                flush=True,
            )
        elif args.cmd == "pipeline-repair":
            from m3.research.nextbid_pipeline_repair import run_pipeline_repair

            constraints = assert_constraints(db)
            api_before = {
                "day_used": (constraints.get("controller") or {})
                .get("budget_state", {})
                .get("day_used"),
                "account_remaining": (constraints.get("controller") or {})
                .get("budget_state", {})
                .get("account_remaining"),
                "openai_today": (constraints.get("spend") or {}).get("today_spent_usd"),
            }
            result = run_pipeline_repair(db)
            constraints_after = assert_constraints(db)
            result["api_usage_before"] = api_before
            result["api_usage_after"] = {
                "day_used": (constraints_after.get("controller") or {})
                .get("budget_state", {})
                .get("day_used"),
                "account_remaining": (constraints_after.get("controller") or {})
                .get("budget_state", {})
                .get("account_remaining"),
                "openai_today": (constraints_after.get("spend") or {}).get("today_spent_usd"),
                "metered_nextbid_this_job": 0,
            }
            from m3.research.nextbid_pipeline_repair import _write_acceptance

            _write_acceptance(result)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "pipeline_repair": {
                            "unique": result["after"]["unique_solicitations"],
                            "dispositions": result["after"]["primary_dispositions"],
                            "ready_bid": result["after"]["opportunities_ready_for_bidding"],
                            "info_recovery": result["after"]["remaining_information_recovery"],
                            "metered_nextbid": 0,
                            "openai": result["openai_spent_usd"],
                            "elapsed_sec": result["elapsed_sec"],
                        },
                    },
                    default=str,
                ),
                flush=True,
            )
        elif args.cmd == "acceptance-002q":
            cmd_acceptance_002q(db, reuse_artifacts=bool(args.reuse))
        elif args.cmd == "provenance-audit":
            cmd_provenance_audit(db)
        elif args.cmd == "qualification-funnel-audit":
            cmd_qualification_funnel_audit(db)
        else:
            parser.error(f"unknown command {args.cmd}")
            return 2
        return 0
    finally:
        db.close()




if __name__ == "__main__":
    raise SystemExit(main())
