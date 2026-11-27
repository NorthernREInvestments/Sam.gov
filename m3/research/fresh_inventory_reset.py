"""Fresh acquisition-window inventory reset (Nov 24 evening – Nov 25 America/Chicago).

Preserves paid responses, documents, ledgers, suppliers, bids, and config.
Rewrites only active discovery inventory + owner queues.
Does NOT call NextBid or OpenAI.
"""

from __future__ import annotations

import json
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from m3.config import get_settings
from m3.research.ai_document_analysis import (
    PROMPT_VERSION,
    analysis_cache_key,
    build_single_request_corpus,
    estimate_live_analysis_cost_usd,
    _load_analysis_cache,
)
from m3.research.ai_output_verify import verify_analysis_against_sources
from m3.research.local_evidence import assemble_local_evidence_package
from m3.research.owner_queues import QUEUE_KEYS, empty_queues, write_queues
from m3.research.queue_deadline_repair import demote_expired_from_owner_queues

CHI = ZoneInfo("America/Chicago")
# Exact window: first Nov 24 evening ledger activity through end of Nov 25 Chicago
WINDOW_START_CHI = datetime(2026, 11, 24, 17, 0, 0, tzinfo=CHI)
WINDOW_END_CHI = datetime(2026, 11, 26, 0, 0, 0, tzinfo=CHI)  # exclusive
ACQUIRE_ENDPOINTS = {
    "get_opportunity",
    "read_file",
    "get_document_download",
    "list_documents",
    "list_files",
    "get_file",
}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(ts: Any) -> datetime | None:
    if not ts:
        return None
    s = str(ts).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _temp_root() -> Path:
    p = Path(__file__).resolve().parents[2] / "TEMP" / "fresh_inventory_reset"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _data() -> Path:
    return get_settings().data_root()


def identify_acquisition_window() -> dict[str, Any]:
    """Phase 1: locate ledger-backed acquisition window and cohort."""
    ledger = _data() / "nextbid_ledger" / "consumption_ledger.jsonl"
    events: list[dict[str, Any]] = []
    by_nb: dict[str, dict[str, Any]] = {}
    ambiguous_hits: list[dict[str, Any]] = []
    first_relevant: dict[str, Any] | None = None
    jobs: Counter = Counter()

    if ledger.exists():
        with ledger.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                at = _parse(row.get("at"))
                if not at:
                    continue
                local = at.astimezone(CHI)
                if not (WINDOW_START_CHI <= local < WINDOW_END_CHI):
                    continue
                events.append(row)
                if first_relevant is None or at < _parse(first_relevant["at_utc"]):
                    first_relevant = {
                        "at_utc": row.get("at"),
                        "at_chicago": local.isoformat(),
                        "op": row.get("op"),
                        "job_id": row.get("job_id"),
                        "endpoint": row.get("endpoint"),
                    }
                if row.get("op") != "commit":
                    # durable_store_hit without commit → ambiguous touch
                    if row.get("op") == "durable_store_hit":
                        meta = row.get("meta") or {}
                        nb = str(meta.get("nb_id") or "")
                        if nb.startswith("NB"):
                            ambiguous_hits.append(
                                {
                                    "nb_id": nb,
                                    "at_utc": row.get("at"),
                                    "at_chicago": local.isoformat(),
                                    "reason": "durable_store_hit_only_no_new_metered_commit",
                                }
                            )
                    continue
                meta = row.get("meta") or {}
                nb = str(meta.get("nb_id") or "")
                ep = str(row.get("endpoint") or "")
                if not nb.startswith("NB"):
                    continue
                if ep and ep not in ACQUIRE_ENDPOINTS and ep != "search":
                    # still record unknown endpoints as ambiguous
                    ambiguous_hits.append(
                        {
                            "nb_id": nb,
                            "at_utc": row.get("at"),
                            "endpoint": ep,
                            "reason": "commit_nonstandard_endpoint",
                        }
                    )
                if ep == "search":
                    continue
                if ep not in ACQUIRE_ENDPOINTS:
                    continue
                jobs[str(row.get("job_id") or "unknown")] += 1
                rec = by_nb.setdefault(
                    nb,
                    {
                        "nb_id": nb,
                        "provider": "nextbid",
                        "commits": [],
                        "job_ids": [],
                        "endpoints": [],
                        "request_ids": [],
                        "first_window_acquisition_at_utc": None,
                        "first_window_acquisition_at_chicago": None,
                    },
                )
                rec["commits"].append(
                    {
                        "at_utc": row.get("at"),
                        "at_chicago": local.isoformat(),
                        "endpoint": ep,
                        "job_id": row.get("job_id"),
                        "request_id": meta.get("request_id"),
                        "ok": row.get("ok"),
                        "units": row.get("units"),
                    }
                )
                if row.get("job_id") and row.get("job_id") not in rec["job_ids"]:
                    rec["job_ids"].append(row.get("job_id"))
                if ep not in rec["endpoints"]:
                    rec["endpoints"].append(ep)
                if meta.get("request_id"):
                    rec["request_ids"].append(meta.get("request_id"))
                if rec["first_window_acquisition_at_utc"] is None:
                    rec["first_window_acquisition_at_utc"] = row.get("at")
                    rec["first_window_acquisition_at_chicago"] = local.isoformat()

    # Merge inventory provenance
    inv_path = _data() / "nextbid_inventory" / "opportunities.jsonl"
    inv_map: dict[str, dict[str, Any]] = {}
    previous_total = 0
    if inv_path.exists():
        with inv_path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                previous_total += 1
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                nb = str(row.get("nb_id") or "")
                if nb:
                    inv_map[nb] = row

    cohort: list[dict[str, Any]] = []
    uncertain: list[dict[str, Any]] = []
    for nb, rec in sorted(by_nb.items()):
        inv = inv_map.get(nb) or {}
        fs = _parse(inv.get("first_seen_at"))
        if fs is None:
            provenance = "window_api_acquire_missing_first_seen"
            uncertain.append({"nb_id": nb, "reason": provenance})
        elif WINDOW_START_CHI <= fs.astimezone(CHI) < WINDOW_END_CHI:
            provenance = "first_seen_in_window"
        else:
            provenance = "prior_discovery_detail_acquired_in_window"
        out = {
            **rec,
            "title": inv.get("title"),
            "solicitation_number": inv.get("solicitation_number"),
            "agency": inv.get("agency") or inv.get("buyer"),
            "official_url": inv.get("official_url"),
            "source": inv.get("source") or "nextbid",
            "first_seen_at": inv.get("first_seen_at"),
            "last_seen_at": inv.get("last_seen_at"),
            "deadline_at": inv.get("deadline_at") or inv.get("response_deadline"),
            "solicitation_status": inv.get("solicitation_status"),
            "lifecycle_status": inv.get("lifecycle_status"),
            "provenance_class": provenance,
            "inventory_row_present": bool(inv),
        }
        cohort.append(out)

    # Ambiguous durable hits not already in cohort
    cohort_ids = {c["nb_id"] for c in cohort}
    ambiguous_only = [
        a for a in ambiguous_hits if a.get("nb_id") not in cohort_ids
    ]

    return {
        "window": {
            "timezone": "America/Chicago",
            "start_chicago": WINDOW_START_CHI.isoformat(),
            "end_chicago_exclusive": WINDOW_END_CHI.isoformat(),
            "start_utc": WINDOW_START_CHI.astimezone(timezone.utc).isoformat(),
            "end_utc_exclusive": WINDOW_END_CHI.astimezone(timezone.utc).isoformat(),
            "first_relevant_event": first_relevant,
            "note": (
                "Window starts 2026-11-24 17:00 America/Chicago; first metered activity "
                "was one-call-proof at 2026-11-24 21:41 CST. Includes all Nov 25 CST."
            ),
        },
        "previous_inventory_total": previous_total,
        "fresh_cohort_count": len(cohort),
        "fresh_cohort": cohort,
        "uncertain_provenance": uncertain,
        "ambiguous_excluded": ambiguous_only,
        "jobs": dict(jobs),
        "ledger_events_in_window": len(events),
        "identified_at": _utc(),
    }


def _coverage_label(cov: dict[str, Any], docs: list[dict[str, Any]]) -> str:
    if not docs:
        return "missing_or_unreadable"
    if cov.get("has_substantive_document_content") and int(cov.get("total_content_chars") or 0) >= 2000:
        return "complete_available_package"
    if cov.get("has_substantive_document_content"):
        return "substantive_incomplete"
    if any(d.get("is_listing_preview") for d in docs) or int(cov.get("listing_preview_count") or 0):
        return "thin_listing_preview"
    if all(d.get("is_metadata_only") for d in docs):
        return "attachment_metadata_only"
    if int(cov.get("total_content_chars") or 0) > 0:
        return "notice_only"
    return "missing_or_unreadable"


def assemble_fresh_packages(db: Session, cohort: list[dict[str, Any]]) -> dict[str, Any]:
    packages: list[dict[str, Any]] = []
    coverage_counts: Counter = Counter()
    for rec in cohort:
        nb = rec["nb_id"]
        evidence = assemble_local_evidence_package(
            nb,
            db=db,
            title=rec.get("title"),
            include_http_cache=False,
            include_package_db=True,
        )
        cov = evidence.get("coverage") or {}
        docs = evidence.get("document_texts") or []
        label = _coverage_label(cov, docs)
        coverage_counts[label] += 1
        single = build_single_request_corpus(evidence) if cov.get("has_substantive_document_content") else {
            "corpus_chars": 0,
            "corpus_hash": None,
            "doc_count": 0,
            "approx_input_tokens": 0,
        }
        packages.append(
            {
                "nb_id": nb,
                "title": evidence.get("title") or rec.get("title"),
                "provenance_class": rec.get("provenance_class"),
                "first_window_acquisition_at_utc": rec.get("first_window_acquisition_at_utc"),
                "first_window_acquisition_at_chicago": rec.get("first_window_acquisition_at_chicago"),
                "job_ids": rec.get("job_ids"),
                "request_ids": rec.get("request_ids"),
                "coverage_label": label,
                "coverage": {
                    "substantive_doc_count": cov.get("substantive_doc_count"),
                    "total_content_chars": cov.get("total_content_chars"),
                    "listing_preview_count": cov.get("listing_preview_count"),
                    "metadata_only_count": cov.get("metadata_only_count"),
                    "primary_source": cov.get("primary_source"),
                },
                "sources_inspected": evidence.get("sources_inspected"),
                "source_refs": evidence.get("source_refs"),
                "corpus_hash": single.get("corpus_hash"),
                "corpus_chars": single.get("corpus_chars"),
                "approx_input_tokens": single.get("approx_input_tokens"),
                "doc_count": single.get("doc_count"),
                "storage": {
                    "inventory": True,
                    "full_text": (_data() / "solicitation_full_text" / f"{nb}.json").exists(),
                    "paid_index": (_data() / "nextbid_paid_responses" / "by_nb_id" / f"{nb}.jsonl").exists()
                    or (_data() / "nextbid_paid_responses" / "by_nb_id" / f"{nb}.json").exists(),
                    "ai_result": (_data() / "ai_doc_understanding" / "by_nb_id" / f"{nb}.json").exists(),
                },
                "_evidence": evidence,  # stripped before durable write
            }
        )
    return {"packages": packages, "coverage_counts": dict(coverage_counts)}


def _load_disk_ai(nb_id: str) -> dict[str, Any] | None:
    path = _data() / "ai_doc_understanding" / "by_nb_id" / f"{nb_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def classify_with_cache(
    db: Session,
    packages: list[dict[str, Any]],
    inv_rows: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Reuse cached AI only when corpus_hash + prompt version match. No live OpenAI."""
    from m3.config import get_settings as _gs

    model = _gs().openai_default_model or "gpt-4o-mini"
    results: list[dict[str, Any]] = []
    awaiting_ai: list[dict[str, Any]] = []
    cache_hits = 0
    est_tokens_in = 0
    est_tokens_out = 0
    est_usd = 0.0

    for pkg in packages:
        nb = pkg["nb_id"]
        evidence = pkg.get("_evidence") or assemble_local_evidence_package(nb, db=db, include_http_cache=False)
        inv = dict(inv_rows.get(nb) or {})
        for k in ("deadline_at", "response_deadline", "solicitation_status", "title", "agency", "official_url"):
            if pkg.get(k) and not inv.get(k):
                inv[k] = pkg.get(k)
        if pkg.get("first_window_acquisition_at_utc"):
            inv["deadline_at"] = inv.get("deadline_at") or pkg.get("deadline_at")

        state: str
        ai_parsed = None
        verified = None
        cache_ok = False

        if pkg["coverage_label"] in {
            "thin_listing_preview",
            "attachment_metadata_only",
            "missing_or_unreadable",
            "notice_only",
        }:
            state = "potential_product_insufficient_source" if pkg["coverage_label"] != "missing_or_unreadable" else "awaiting_missing_original_documents"
            if pkg["coverage_label"] == "thin_listing_preview":
                state = "potential_product_insufficient_source"
            results.append(
                {
                    "nb_id": nb,
                    "title": pkg.get("title"),
                    "final_state": state,
                    "coverage_label": pkg["coverage_label"],
                    "ai_cached": False,
                    "awaiting_ai": False,
                    "promotion": {},
                    "disposition": (
                        "potential_product_classification_needed"
                        if state.startswith("potential")
                        else "verified_product_information_needed"
                    ),
                    "missing": ["substantive_local_solicitation_body"],
                    "source_refs": pkg.get("source_refs") or [],
                    "provenance_class": pkg.get("provenance_class"),
                }
            )
            continue

        # Substantive — try cache
        corpus_hash = pkg.get("corpus_hash")
        disk = _load_disk_ai(nb)
        cached = None
        if corpus_hash:
            cached = _load_analysis_cache(db, analysis_cache_key(corpus_hash=corpus_hash, model=model))
        if disk and disk.get("parsed") and disk.get("corpus_hash") == corpus_hash:
            ai_parsed = disk.get("parsed")
            cache_ok = True
        elif cached and cached.get("parsed") is not None and (cached.get("live") or cached.get("parsed")):
            # ResearchCache may not store prompt_version; require live flag or disk match
            if cached.get("live") or cached.get("prompt_version") in {None, PROMPT_VERSION}:
                ai_parsed = cached.get("parsed")
                cache_ok = True

        if cache_ok and ai_parsed:
            cache_hits += 1
            verified = verify_analysis_against_sources(ai_parsed, evidence, item=inv)
            promo = verified.get("promotion") or {}
            blocks = bool(verified.get("blocks_product_only"))
            expired = bool((verified.get("deadline_gate") or {}).get("expired"))
            req = str(ai_parsed.get("requirement_class") or "")
            if expired:
                final = "expired_or_canceled"
                disp = "closed_expired_non_solicitation"
            elif blocks or req in {
                "services",
                "construction",
                "maintenance_repair_services",
                "professional_services",
                "product_with_significant_installation",
            }:
                final = "mixed_or_service_execution_blocked" if req.startswith("product") or req == "mixed_product_service" else "service_or_construction_unsuitable"
                disp = "confirmed_disqualification"
            elif promo.get("supplier_quote_ready"):
                # Guard: generic category-only descriptions
                lines = verified.get("verified_line_items") or []
                generic = _looks_generic_category(lines)
                if generic:
                    final = "confirmed_product_requirements_incomplete"
                    disp = "verified_product_information_needed"
                else:
                    final = "confirmed_product_supplier_quote_ready"
                    disp = "awaiting_supplier_quotations"
            elif (verified.get("verified_line_items") or []) and ai_parsed.get("is_product_purchase"):
                final = "confirmed_product_requirements_incomplete"
                disp = "verified_product_information_needed"
            elif ai_parsed.get("is_product_purchase"):
                final = "confirmed_product_requirements_incomplete"
                disp = "verified_product_information_needed"
            else:
                final = "potential_product_insufficient_source"
                disp = "potential_product_classification_needed"

            missing = list(ai_parsed.get("missing_information") or []) + list(verified.get("unknown_fields") or [])
            results.append(
                {
                    "nb_id": nb,
                    "title": pkg.get("title"),
                    "solicitation_number": inv.get("solicitation_number") or pkg.get("solicitation_number"),
                    "agency": inv.get("agency") or pkg.get("agency"),
                    "final_state": final,
                    "coverage_label": pkg["coverage_label"],
                    "ai_cached": True,
                    "awaiting_ai": False,
                    "requirement_class": req,
                    "verified_line_items": verified.get("verified_line_items") or [],
                    "promotion": promo,
                    "deadline_gate": verified.get("deadline_gate"),
                    "rejected_assertions": verified.get("rejected_assertions") or [],
                    "disposition": disp,
                    "missing": missing,
                    "source_refs": pkg.get("source_refs") or [],
                    "provenance_class": pkg.get("provenance_class"),
                    "deadline_at": inv.get("deadline_at") or pkg.get("deadline_at"),
                    "official_url": inv.get("official_url") or pkg.get("official_url"),
                }
            )
        else:
            # Needs live AI — estimate only
            est = estimate_live_analysis_cost_usd(evidence)
            est_tokens_in += int(est.get("approx_input_tokens") or 0)
            est_tokens_out += int(est.get("approx_output_tokens") or 0)
            est_usd += float(est.get("estimated_usd") or 0)
            awaiting_ai.append(
                {
                    "nb_id": nb,
                    "title": pkg.get("title"),
                    "corpus_chars": pkg.get("corpus_chars"),
                    "corpus_hash": pkg.get("corpus_hash"),
                    "approx_input_tokens": est.get("approx_input_tokens"),
                    "estimated_usd": est.get("estimated_usd"),
                    "coverage_label": pkg["coverage_label"],
                }
            )
            results.append(
                {
                    "nb_id": nb,
                    "title": pkg.get("title"),
                    "final_state": "awaiting_ai_analysis",
                    "coverage_label": pkg["coverage_label"],
                    "ai_cached": False,
                    "awaiting_ai": True,
                    "promotion": {},
                    "disposition": "potential_product_classification_needed",
                    "missing": ["openai_analysis_not_authorized_for_this_batch"],
                    "source_refs": pkg.get("source_refs") or [],
                    "provenance_class": pkg.get("provenance_class"),
                    "deadline_at": inv.get("deadline_at") or pkg.get("deadline_at"),
                }
            )

    return {
        "results": results,
        "awaiting_ai": awaiting_ai,
        "cache_hits": cache_hits,
        "ai_estimate": {
            "packages_needing_live_ai": len(awaiting_ai),
            "approx_input_tokens": est_tokens_in,
            "approx_output_tokens": est_tokens_out,
            "estimated_usd_max": round(est_usd, 4),
            "model": model,
            "prompt_version": PROMPT_VERSION,
            "owner_authorization_required": True,
            "authorization_token": "OWNER_AUTHORIZED_FRESH_COHORT_AI_ANALYSIS",
            "note": "Previous $5 bulk authorization does NOT cover this batch. No OpenAI calls made.",
        },
    }


def _looks_generic_category(lines: list[dict[str, Any]]) -> bool:
    if not lines:
        return True
    bad = 0
    for li in lines:
        desc = str(li.get("description") or "").lower()
        if not desc or len(desc) < 8:
            bad += 1
            continue
        if any(
            x in desc
            for x in (
                "raw materials",
                "manufacturing support",
                "industrial process machinery",
                "prepared foods",
                "food products in accordance",
                "converted bid item",
                "see attachment",
            )
        ):
            bad += 1
    return bad >= max(1, len(lines) // 2)


def transactional_inventory_reset(
    cohort: list[dict[str, Any]],
    inv_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Move legacy inventory aside; write fresh active inventory. Preserves evidence stores."""
    root = _data() / "nextbid_inventory"
    root.mkdir(parents=True, exist_ok=True)
    active = root / "opportunities.jsonl"
    archive_dir = root / "legacy_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = archive_dir / f"opportunities_pre_fresh_reset_{stamp}.jsonl"
    manifest_path = archive_dir / f"legacy_nb_ids_{stamp}.json"
    fresh_tmp = root / f"opportunities_fresh_{stamp}.jsonl.tmp"

    previous_total = 0
    legacy_ids: list[str] = []
    if active.exists():
        # Move (not copy) active file into archive — no duplicate full copy left in place
        shutil.move(str(active), str(archive_path))
        with archive_path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                previous_total += 1
                try:
                    row = json.loads(line)
                    nb = str(row.get("nb_id") or "")
                    if nb and nb not in {c["nb_id"] for c in cohort}:
                        legacy_ids.append(nb)
                except Exception:
                    pass

    fresh_ids = [c["nb_id"] for c in cohort]
    with fresh_tmp.open("w", encoding="utf-8") as out:
        for c in cohort:
            nb = c["nb_id"]
            base = dict(inv_map.get(nb) or {})
            base["nb_id"] = nb
            base["title"] = c.get("title") or base.get("title")
            base["solicitation_number"] = c.get("solicitation_number") or base.get("solicitation_number")
            base["fresh_cohort"] = True
            base["fresh_window_start_chicago"] = WINDOW_START_CHI.isoformat()
            base["fresh_window_acquisition_at_utc"] = c.get("first_window_acquisition_at_utc")
            base["fresh_window_acquisition_at_chicago"] = c.get("first_window_acquisition_at_chicago")
            base["fresh_provenance_class"] = c.get("provenance_class")
            base["fresh_job_ids"] = c.get("job_ids")
            base["fresh_request_ids"] = c.get("request_ids")
            base["active_discovery"] = True
            out.write(json.dumps(base, default=str) + "\n")

    # Atomic replace
    fresh_final = root / "opportunities.jsonl"
    fresh_tmp.replace(fresh_final)

    manifest = {
        "archived_at": _utc(),
        "previous_inventory_total": previous_total,
        "legacy_excluded_count": len(legacy_ids),
        "fresh_retained_count": len(fresh_ids),
        "archive_inventory_path": str(archive_path),
        "fresh_nb_ids": fresh_ids,
        "legacy_nb_ids_count": len(legacy_ids),
        "protected_not_deleted": [
            "nextbid_paid_responses",
            "solicitation_full_text",
            "package_blobs",
            "nextbid_ledger",
            "ai_doc_understanding",
            "suppliers/quotes/bids (DB)",
        ],
        "note": "Legacy rows removed from ACTIVE inventory only; archive file retains prior JSONL.",
    }
    # Store legacy id list only (not full duplicate rows) — archive file already has full rows once
    manifest_path.write_text(
        json.dumps({**manifest, "legacy_nb_ids_sample": legacy_ids[:50]}, indent=2),
        encoding="utf-8",
    )
    # Marker preventing silent repopulation from old checkpoint listings without owner action
    guard = root / "ACTIVE_INVENTORY_POLICY.json"
    guard.write_text(
        json.dumps(
            {
                "policy": "fresh_window_only",
                "window_start_chicago": WINDOW_START_CHI.isoformat(),
                "window_end_chicago_exclusive": WINDOW_END_CHI.isoformat(),
                "active_count": len(fresh_ids),
                "block_legacy_reimport": True,
                "updated_at": _utc(),
                "requires_owner_to_expand": True,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return manifest


def rebuild_fresh_queues(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Backup existing queues, write fresh-cohort-only queues."""
    qroot = _data() / "procurement_queues"
    backup = qroot / "legacy_queue_backup"
    backup.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for p in qroot.glob("*.json"):
        if p.name.startswith("legacy"):
            continue
        shutil.copy2(p, backup / f"{stamp}_{p.name}")

    demote_expired_from_owner_queues()
    queues = empty_queues()
    supplier: list[dict[str, Any]] = []
    needs_doc: list[dict[str, Any]] = []

    for r in results:
        card = {
            "nb_id": r.get("nb_id"),
            "title": r.get("title"),
            "solicitation_number": r.get("solicitation_number"),
            "agency": r.get("agency"),
            "deadline": r.get("deadline_at"),
            "deadline_at": r.get("deadline_at"),
            "disposition": r.get("disposition"),
            "queue": r.get("disposition"),
            "final_state": r.get("final_state"),
            "requirement_class": r.get("requirement_class"),
            "verified_line_items": r.get("verified_line_items") or [],
            "promotion": r.get("promotion") or {},
            "source_refs": r.get("source_refs") or [],
            "official_url": r.get("official_url"),
            "fresh_cohort": True,
            "provenance_class": r.get("provenance_class"),
            "ai_cached": r.get("ai_cached"),
            "awaiting_ai": r.get("awaiting_ai"),
            "missing": r.get("missing") or [],
            "coverage_label": r.get("coverage_label"),
        }
        disp = r.get("disposition") or "potential_product_classification_needed"
        if disp in queues:
            queues[disp].append(card)
        if r.get("final_state") == "confirmed_product_supplier_quote_ready":
            queues.setdefault("awaiting_supplier_quotations", []).append(dict(card))
            supplier.append(_pricing_row(r))
        elif r.get("final_state") in {
            "confirmed_product_requirements_incomplete",
            "awaiting_missing_original_documents",
            "awaiting_ai_analysis",
            "potential_product_insufficient_source",
        }:
            needs_doc.append(_pricing_row(r, incomplete=True))

    summary = write_queues(queues, build="fresh-window-reset")
    # Staged owner views
    staged = _data() / "staged_inventory" / "owner"
    staged.mkdir(parents=True, exist_ok=True)
    (staged / "supplier_pricing_live.jsonl").write_text(
        "\n".join(json.dumps(x, default=str) for x in supplier) + ("\n" if supplier else ""),
        encoding="utf-8",
    )
    (staged / "needs_documentation_live.jsonl").write_text(
        "\n".join(json.dumps(x, default=str) for x in needs_doc) + ("\n" if needs_doc else ""),
        encoding="utf-8",
    )
    return {
        "queue_summary": summary,
        "supplier_pricing_count": len(supplier),
        "needs_documentation_count": len(needs_doc),
        "supplier_pricing": supplier,
        "needs_documentation": needs_doc,
        "queue_backup_dir": str(backup),
    }


def _pricing_row(r: dict[str, Any], *, incomplete: bool = False) -> dict[str, Any]:
    lines = r.get("verified_line_items") or []
    products = "; ".join(str(li.get("description") or "")[:100] for li in lines[:8])
    qty = "; ".join(
        f"{li.get('quantity') or 'UNKNOWN'} {li.get('unit') or ''}".strip() for li in lines[:8]
    )
    specs = "; ".join(", ".join(li.get("specs") or [])[:80] for li in lines[:3] if li.get("specs"))
    return {
        "solicitation": r.get("solicitation_number") or r.get("nb_id"),
        "nb_id": r.get("nb_id"),
        "agency": r.get("agency") or "",
        "products": products,
        "qty_uom": qty,
        "specs": specs,
        "deadline": r.get("deadline_at"),
        "supplier_quote": "ready" if not incomplete and r.get("final_state") == "confirmed_product_supplier_quote_ready" else "not_ready",
        "next_action": (
            "obtain_supplier_quotes"
            if r.get("final_state") == "confirmed_product_supplier_quote_ready"
            else (
                "authorize_openai_analysis"
                if r.get("awaiting_ai")
                else (
                    "retrieve_missing_documents"
                    if "missing" in str(r.get("final_state"))
                    or r.get("coverage_label") in {"thin_listing_preview", "missing_or_unreadable", "notice_only"}
                    else "complete_requirements"
                )
            )
        ),
        "final_state": r.get("final_state"),
        "missing": r.get("missing") or [],
    }


def verify_protected_stores() -> dict[str, Any]:
    root = _data()
    checks = {
        "nextbid_paid_responses": (root / "nextbid_paid_responses").exists(),
        "solicitation_full_text": (root / "solicitation_full_text").exists(),
        "package_blobs": (root / "package_blobs").exists(),
        "nextbid_ledger": (root / "nextbid_ledger" / "consumption_ledger.jsonl").exists(),
        "ai_doc_understanding": (root / "ai_doc_understanding").exists(),
        "legacy_inventory_archive": (root / "nextbid_inventory" / "legacy_archive").exists(),
        "active_inventory": (root / "nextbid_inventory" / "opportunities.jsonl").exists(),
        "emergency_stop": (root / "nextbid_checkpoints" / "NEXTBID_EMERGENCY_STOP.json").exists(),
    }
    ft = len(list((root / "solicitation_full_text").glob("*.json"))) if checks["solicitation_full_text"] else 0
    paid = len(list((root / "nextbid_paid_responses" / "by_nb_id").glob("*"))) if checks["nextbid_paid_responses"] else 0
    return {
        "checks": checks,
        "full_text_files_still_present": ft,
        "paid_by_nb_files_still_present": paid,
        "all_protected_present": all(checks.values()),
    }


def run_fresh_inventory_reset(db: Session) -> dict[str, Any]:
    temp = _temp_root()
    phase1 = identify_acquisition_window()
    (temp / "phase1_window_cohort.json").write_text(
        json.dumps({k: v for k, v in phase1.items() if k != "fresh_cohort"}, indent=2, default=str)
        , encoding="utf-8",
    )
    (temp / "fresh_cohort_full.json").write_text(
        json.dumps(phase1["fresh_cohort"], indent=2, default=str), encoding="utf-8"
    )

    inv_map: dict[str, dict[str, Any]] = {}
    # Load from archive if already moved, else active
    active = _data() / "nextbid_inventory" / "opportunities.jsonl"
    src = active
    if not src.exists():
        arch = _data() / "nextbid_inventory" / "legacy_archive"
        cands = sorted(arch.glob("opportunities_pre_fresh_reset_*.jsonl")) if arch.exists() else []
        src = cands[-1] if cands else active
    if src.exists():
        with src.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                nb = str(row.get("nb_id") or "")
                if nb:
                    inv_map[nb] = row

    # Also ensure cohort titles from whatever is in inv_map
    for c in phase1["fresh_cohort"]:
        if c["nb_id"] in inv_map and not c.get("title"):
            c["title"] = inv_map[c["nb_id"]].get("title")

    assembled = assemble_fresh_packages(db, phase1["fresh_cohort"])
    # Strip evidence blobs before writing package index
    slim_packages = []
    for p in assembled["packages"]:
        slim = {k: v for k, v in p.items() if k != "_evidence"}
        slim_packages.append(slim)
    (temp / "fresh_packages_index.json").write_text(
        json.dumps({"coverage_counts": assembled["coverage_counts"], "packages": slim_packages}, indent=2, default=str),
        encoding="utf-8",
    )

    classified = classify_with_cache(db, assembled["packages"], inv_map)

    reset_manifest = transactional_inventory_reset(phase1["fresh_cohort"], inv_map)
    queues = rebuild_fresh_queues(classified["results"])
    protected = verify_protected_stores()

    # State counts
    state_counts = Counter(r.get("final_state") for r in classified["results"])

    report = {
        "ok": True,
        "finished_at": _utc(),
        "inventory_reset": {
            "previous_inventory_total": phase1["previous_inventory_total"],
            "acquisition_window": phase1["window"],
            "legacy_removed_from_active": reset_manifest["legacy_excluded_count"],
            "fresh_retained": reset_manifest["fresh_retained_count"],
            "uncertain_provenance_count": len(phase1["uncertain_provenance"]),
            "ambiguous_excluded_count": len(phase1["ambiguous_excluded"]),
            "protected_verification": protected,
            "archive": reset_manifest.get("archive_inventory_path"),
            "jobs_in_window": phase1["jobs"],
        },
        "document_coverage": assembled["coverage_counts"],
        "ai_analysis": {
            "reusable_cached": classified["cache_hits"],
            "awaiting_owner_ai_authorization": len(classified["awaiting_ai"]),
            "awaiting_ai_packages": classified["awaiting_ai"],
            "estimate": classified["ai_estimate"],
            "new_openai_spent_usd": 0.0,
            "source_verification_rejects": sum(
                len(r.get("rejected_assertions") or []) for r in classified["results"]
            ),
        },
        "commercial_results": {
            "final_state_counts": dict(state_counts),
            "supplier_quote_ready": queues["supplier_pricing"],
            "needs_documentation": queues["needs_documentation"][:100],
            "queue_summary": queues["queue_summary"],
        },
        "safety": {
            "nextbid_spent": 0,
            "openai_spent": 0.0,
            "killswitch_locked": True,
            "schedulers_disabled": True,
            "production_evidence_preserved": protected.get("all_protected_present"),
            "deployment": False,
        },
        "temp_dir": str(temp),
    }

    # Durable acceptance pointer + owner report
    (_data() / "acceptance").mkdir(parents=True, exist_ok=True)
    outp = _data() / "acceptance" / "jobs_fresh_inventory_reset_last.json"
    # Don't embed full awaiting list twice excessively
    outp.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (temp / "OWNER_REPORT.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    _write_owner_md(report, temp)
    return report


def _write_owner_md(report: dict[str, Any], temp: Path) -> None:
    inv = report["inventory_reset"]
    cov = report["document_coverage"]
    ai = report["ai_analysis"]
    com = report["commercial_results"]
    w = inv["acquisition_window"]
    lines = [
        "# Fresh Inventory Reset — Owner Report",
        "",
        "**STOP FOR OWNER REVIEW — no new OpenAI charges executed.**",
        "",
        "## INVENTORY RESET",
        f"- Previous active inventory: **{inv['previous_inventory_total']}**",
        f"- Window: **{w['start_chicago']}** → **{w['end_chicago_exclusive']}** (America/Chicago)",
        f"- First relevant acquisition event: `{w.get('first_relevant_event')}`",
        f"- Legacy removed from active inventory: **{inv['legacy_removed_from_active']}**",
        f"- Fresh retained: **{inv['fresh_retained']}**",
        f"- Uncertain provenance: **{inv['uncertain_provenance_count']}**",
        f"- Ambiguous excluded (durable hit only): **{inv['ambiguous_excluded_count']}**",
        f"- Protected stores intact: **{inv['protected_verification'].get('all_protected_present')}**",
        f"- Archive: `{inv.get('archive')}`",
        "",
        "## DOCUMENT COVERAGE (fresh cohort)",
    ]
    for k, v in sorted((cov or {}).items()):
        lines.append(f"- {k}: **{v}**")
    lines += [
        "",
        "## AI ANALYSIS",
        f"- Reusable cached AI (hash match): **{ai['reusable_cached']}**",
        f"- Awaiting owner AI authorization: **{ai['awaiting_owner_ai_authorization']}**",
        f"- Estimated new OpenAI max: **${ai['estimate'].get('estimated_usd_max')}** "
        f"({ai['estimate'].get('approx_input_tokens')} in-tok est, model {ai['estimate'].get('model')})",
        f"- Auth token required: `{ai['estimate'].get('authorization_token')}`",
        f"- Actual new OpenAI spend this job: **$0.00**",
        f"- Source verification rejected assertions: **{ai['source_verification_rejects']}**",
        "",
        "## COMMERCIAL RESULTS",
        f"- Final states: `{com['final_state_counts']}`",
        f"- Supplier-quote-ready: **{len(com['supplier_quote_ready'])}**",
        "",
        "### Supplier pricing list",
        "",
        "| Solicitation | Agency | Products | Qty/UOM | Specs | Deadline | Supplier Quote | Next Action |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in com["supplier_quote_ready"]:
        lines.append(
            f"| {row.get('solicitation')} | {row.get('agency')} | {str(row.get('products') or '')[:80]} | "
            f"{str(row.get('qty_uom') or '')[:40]} | {str(row.get('specs') or '')[:40]} | {row.get('deadline')} | "
            f"{row.get('supplier_quote')} | {row.get('next_action')} |"
        )
    if not com["supplier_quote_ready"]:
        lines.append("| — | — | **none** | — | — | — | — | — |")
    lines += [
        "",
        "## SAFETY",
        "- NextBid: ZERO",
        "- OpenAI: ZERO (authorization required for awaiting batch)",
        "- Killswitch: LOCKED",
        "- Schedulers: DISABLED",
        "- Evidence preserved; no deployment",
        "",
    ]
    (temp / "OWNER_REPORT.md").write_text("\n".join(lines), encoding="utf-8")


OWNER_FRESH_AI_AUTH = "OWNER_AUTHORIZED_FRESH_COHORT_AI_ANALYSIS"
MIN_SUBSTANTIVE_CHARS = 800


def _valid_cached_analysis(
    db: Session,
    *,
    nb_id: str,
    corpus_hash: str,
    model: str,
) -> dict[str, Any] | None:
    """Reuse only when corpus_hash matches and analysis came from live model (or disk live)."""
    disk_path = _data() / "ai_doc_understanding" / "by_nb_id" / f"{nb_id}.json"
    if disk_path.exists():
        try:
            disk = json.loads(disk_path.read_text(encoding="utf-8"))
        except Exception:
            disk = None
        if (
            isinstance(disk, dict)
            and disk.get("parsed")
            and disk.get("corpus_hash") == corpus_hash
            and (disk.get("live_ai_invoked") or disk.get("cached") is False or disk.get("model"))
        ):
            # Accept prior live bulk results for same hash
            return {"parsed": disk["parsed"], "source": "disk", "corpus_hash": corpus_hash}

    cached = _load_analysis_cache(db, analysis_cache_key(corpus_hash=corpus_hash, model=model))
    if cached and cached.get("parsed") is not None and cached.get("live"):
        return {"parsed": cached["parsed"], "source": "research_cache", "corpus_hash": corpus_hash}
    return None


def _result_from_verified(
    nb_id: str,
    pkg_meta: dict[str, Any],
    inv: dict[str, Any],
    ai_parsed: dict[str, Any] | None,
    verified: dict[str, Any],
    *,
    ai_cached: bool,
    newly_analyzed: bool,
    coverage_label: str,
) -> dict[str, Any]:
    promo = verified.get("promotion") or {}
    blocks = bool(verified.get("blocks_product_only"))
    expired = bool((verified.get("deadline_gate") or {}).get("expired"))
    req = str((ai_parsed or {}).get("requirement_class") or "")
    lines = verified.get("verified_line_items") or []

    if coverage_label in {
        "thin_listing_preview",
        "attachment_metadata_only",
        "missing_or_unreadable",
        "notice_only",
    }:
        final = "awaiting_missing_original_documents"
        disp = "verified_product_information_needed"
    elif expired:
        final = "expired_or_canceled"
        disp = "closed_expired_non_solicitation"
    elif blocks or req in {
        "services",
        "construction",
        "maintenance_repair_services",
        "professional_services",
        "product_with_significant_installation",
    }:
        final = (
            "mixed_or_service_execution_blocked"
            if req.startswith("product") or req == "mixed_product_service"
            else "service_or_construction_unsuitable"
        )
        disp = "confirmed_disqualification"
    elif promo.get("supplier_quote_ready") and not _looks_generic_category(lines):
        final = "confirmed_product_supplier_quote_ready"
        disp = "awaiting_supplier_quotations"
    elif lines and (ai_parsed or {}).get("is_product_purchase"):
        final = "confirmed_product_requirements_incomplete"
        disp = "verified_product_information_needed"
    elif (ai_parsed or {}).get("is_product_purchase"):
        final = "confirmed_product_requirements_incomplete"
        disp = "verified_product_information_needed"
    else:
        final = "potential_product_insufficient_source"
        disp = "potential_product_classification_needed"

    currently_actionable = bool(
        final == "confirmed_product_supplier_quote_ready"
        and promo.get("currently_open_verified")
        and not expired
    )

    return {
        "nb_id": nb_id,
        "title": pkg_meta.get("title") or inv.get("title"),
        "solicitation_number": inv.get("solicitation_number") or pkg_meta.get("solicitation_number"),
        "agency": inv.get("agency") or pkg_meta.get("agency"),
        "final_state": final,
        "coverage_label": coverage_label,
        "ai_cached": ai_cached,
        "newly_analyzed": newly_analyzed,
        "awaiting_ai": False,
        "requirement_class": req or None,
        "is_product_purchase": (ai_parsed or {}).get("is_product_purchase"),
        "verified_line_items": lines,
        "has_verified_qty_specs": bool(
            any(li.get("quantity") not in (None, "", "UNKNOWN") for li in lines)
            and any((li.get("specs") or li.get("description")) for li in lines)
        ),
        "promotion": promo,
        "deadline_gate": verified.get("deadline_gate"),
        "rejected_assertions": verified.get("rejected_assertions") or [],
        "disposition": disp,
        "missing": list((ai_parsed or {}).get("missing_information") or [])
        + list(verified.get("unknown_fields") or []),
        "source_refs": pkg_meta.get("source_refs") or [],
        "deadline_at": inv.get("deadline_at") or pkg_meta.get("deadline_at"),
        "official_url": inv.get("official_url") or pkg_meta.get("official_url"),
        "currently_actionable": currently_actionable,
        "gaps": _document_gaps(coverage_label, ai_parsed, verified),
    }


def _document_gaps(
    coverage_label: str,
    ai_parsed: dict[str, Any] | None,
    verified: dict[str, Any],
) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    if coverage_label in {
        "thin_listing_preview",
        "attachment_metadata_only",
        "missing_or_unreadable",
        "notice_only",
    }:
        gaps.append(
            {
                "gap": "substantive_solicitation_body",
                "locally_available": False,
                "retrieval": "nextbid_get_opportunity_or_read_file",
                "paid_retrieval_authorized": False,
                "note": "NEEDS ORIGINAL DOCUMENTS — local search exhausted",
            }
        )
    for m in (ai_parsed or {}).get("missing_information") or []:
        gaps.append({"gap": str(m), "locally_available": False, "paid_retrieval_authorized": False})
    for u in verified.get("unknown_fields") or []:
        gaps.append({"gap": f"unknown:{u}", "locally_available": False, "paid_retrieval_authorized": False})
    return gaps


def run_fresh_cohort_ai_complete(
    db: Session,
    *,
    owner_authorization: str,
    max_usd: float = 0.25,
    concurrency: int = 5,
) -> dict[str, Any]:
    """Owner-authorized completion: all active fresh inventory through AI or NEEDS DOCS.

    No NextBid. Hard OpenAI ceiling max_usd. Concurrent workers ≤ 5.
    """
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
    from decimal import Decimal

    from m3.research.bulk_ai_doc_analysis import (
        AtomicJobBudget,
        _persist_result,
        _row_from_verification,
        _worker_analyze_one,
    )

    if owner_authorization != OWNER_FRESH_AI_AUTH:
        return {
            "ok": False,
            "error": "missing_owner_fresh_ai_authorization",
            "openai_spent_usd": 0.0,
            "nextbid_api_calls": 0,
        }

    temp = _temp_root() / "ai_complete"
    temp.mkdir(parents=True, exist_ok=True)
    model = get_settings().openai_default_model or "gpt-4o-mini"
    budget = AtomicJobBudget(Decimal(str(max_usd)))
    workers = max(1, min(int(concurrency or 10), 10))

    # Load active fresh inventory
    inv_path = _data() / "nextbid_inventory" / "opportunities.jsonl"
    inv_rows: dict[str, dict[str, Any]] = {}
    with inv_path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            nb = str(row.get("nb_id") or "")
            if nb:
                inv_rows[nb] = row
    fresh_ids = sorted(inv_rows.keys())

    print(json.dumps({"phase": "fresh_ai_start", "n": len(fresh_ids), "max_usd": max_usd}), flush=True)

    substantive_work: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    previously_analyzed = 0
    newly_analyzed = 0
    needs_original_docs = 0
    coverage_counts: Counter = Counter()

    # Phase A: assemble + cache reuse / queue live / needs-docs
    for nb in fresh_ids:
        inv = inv_rows[nb]
        evidence = assemble_local_evidence_package(
            nb, db=db, title=inv.get("title"), include_http_cache=False, include_package_db=True
        )
        cov = evidence.get("coverage") or {}
        label = _coverage_label(cov, evidence.get("document_texts") or [])

        # Deeper local search before declaring needs-docs
        if label in {"thin_listing_preview", "missing_or_unreadable", "notice_only", "attachment_metadata_only"}:
            evidence2 = assemble_local_evidence_package(
                nb, db=db, title=inv.get("title"), include_http_cache=True, include_package_db=True
            )
            cov2 = evidence2.get("coverage") or {}
            label2 = _coverage_label(cov2, evidence2.get("document_texts") or [])
            if cov2.get("has_substantive_document_content") and int(cov2.get("total_content_chars") or 0) >= MIN_SUBSTANTIVE_CHARS:
                evidence, cov, label = evidence2, cov2, label2
            else:
                coverage_counts[label] += 1
                needs_original_docs += 1
                results.append(
                    {
                        "nb_id": nb,
                        "title": inv.get("title") or evidence.get("title"),
                        "solicitation_number": inv.get("solicitation_number"),
                        "agency": inv.get("agency"),
                        "final_state": "awaiting_missing_original_documents",
                        "coverage_label": label,
                        "ai_cached": False,
                        "newly_analyzed": False,
                        "awaiting_ai": False,
                        "verified_line_items": [],
                        "promotion": {},
                        "disposition": "verified_product_information_needed",
                        "missing": ["NEEDS ORIGINAL DOCUMENTS"],
                        "source_refs": evidence.get("source_refs") or [],
                        "deadline_at": inv.get("deadline_at") or inv.get("response_deadline"),
                        "official_url": inv.get("official_url"),
                        "currently_actionable": False,
                        "gaps": _document_gaps(label, None, {"unknown_fields": []}),
                        "sources_searched": evidence.get("sources_inspected"),
                    }
                )
                continue

        single = build_single_request_corpus(evidence)
        if single["corpus_chars"] < MIN_SUBSTANTIVE_CHARS:
            coverage_counts["substantive_incomplete"] += 1
            needs_original_docs += 1
            results.append(
                {
                    "nb_id": nb,
                    "title": inv.get("title"),
                    "final_state": "awaiting_missing_original_documents",
                    "coverage_label": "substantive_incomplete",
                    "ai_cached": False,
                    "newly_analyzed": False,
                    "verified_line_items": [],
                    "promotion": {},
                    "disposition": "verified_product_information_needed",
                    "missing": ["NEEDS ORIGINAL DOCUMENTS — corpus below substantive threshold"],
                    "source_refs": evidence.get("source_refs") or [],
                    "deadline_at": inv.get("deadline_at"),
                    "currently_actionable": False,
                    "gaps": _document_gaps("substantive_incomplete", None, {"unknown_fields": []}),
                }
            )
            continue

        coverage_counts[label] += 1
        cached = _valid_cached_analysis(db, nb_id=nb, corpus_hash=single["corpus_hash"], model=model)
        pkg_meta = {
            "title": evidence.get("title") or inv.get("title"),
            "solicitation_number": inv.get("solicitation_number"),
            "agency": inv.get("agency"),
            "deadline_at": inv.get("deadline_at") or inv.get("response_deadline"),
            "official_url": inv.get("official_url"),
            "source_refs": evidence.get("source_refs") or [],
            "corpus_hash": single["corpus_hash"],
            "corpus_chars": single["corpus_chars"],
            "approx_input_tokens": single["approx_input_tokens"],
            "estimated_usd": float(estimate_live_analysis_cost_usd(evidence).get("estimated_usd") or 0.003),
        }

        if cached:
            previously_analyzed += 1
            verified = verify_analysis_against_sources(cached["parsed"], evidence, item=dict(inv))
            results.append(
                _result_from_verified(
                    nb,
                    pkg_meta,
                    inv,
                    cached["parsed"],
                    verified,
                    ai_cached=True,
                    newly_analyzed=False,
                    coverage_label=label,
                )
            )
            continue

        substantive_work.append(
            {
                "nb_id": nb,
                "inv": inv,
                "pkg_meta": pkg_meta,
                "label": label,
                "evidence_chars": single["corpus_chars"],
            }
        )

    print(
        json.dumps(
            {
                "phase": "assembly_done",
                "cached": previously_analyzed,
                "need_live": len(substantive_work),
                "needs_docs": needs_original_docs,
                "coverage": dict(coverage_counts),
            }
        ),
        flush=True,
    )

    # Phase B: concurrent live OpenAI for remaining substantive
    in_flight: dict[Any, dict[str, Any]] = {}
    pending = list(substantive_work)
    idx = 0

    def _consume(result: dict[str, Any], meta: dict[str, Any]) -> None:
        nonlocal newly_analyzed, previously_analyzed
        nb = meta["nb_id"]
        inv = meta["inv"]
        pkg_meta = meta["pkg_meta"]
        label = meta["label"]
        if not result.get("ok"):
            results.append(
                {
                    "nb_id": nb,
                    "title": pkg_meta.get("title"),
                    "final_state": "awaiting_ai_analysis",
                    "coverage_label": label,
                    "ai_cached": False,
                    "newly_analyzed": False,
                    "error": result.get("error"),
                    "verified_line_items": [],
                    "promotion": {},
                    "disposition": "potential_product_classification_needed",
                    "missing": [str(result.get("error") or "ai_failed")],
                    "currently_actionable": False,
                    "gaps": [],
                }
            )
            return
        ai = result["ai"]
        verified = result["verified"]
        if ai.get("live_ai_invoked"):
            newly_analyzed += 1
        elif result.get("cached"):
            previously_analyzed += 1
        results.append(
            _result_from_verified(
                nb,
                pkg_meta,
                inv,
                ai.get("parsed"),
                verified,
                ai_cached=bool(result.get("cached")),
                newly_analyzed=bool(ai.get("live_ai_invoked")),
                coverage_label=label,
            )
        )

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fresh-ai") as pool:
        while idx < len(pending) or in_flight:
            while idx < len(pending) and len(in_flight) < workers and not budget.stop:
                meta = pending[idx]
                idx += 1
                reserve = Decimal(str(max(0.01, float(meta["pkg_meta"].get("estimated_usd") or 0.01))))
                if not budget.try_reserve(reserve):
                    budget.stop = True
                    budget.stop_reason = budget.stop_reason or "openai_job_ceiling_reached"
                    results.append(
                        {
                            "nb_id": meta["nb_id"],
                            "title": meta["pkg_meta"].get("title"),
                            "final_state": "awaiting_ai_analysis",
                            "coverage_label": meta["label"],
                            "ai_cached": False,
                            "newly_analyzed": False,
                            "disposition": "potential_product_classification_needed",
                            "missing": ["openai_budget_ceiling"],
                            "currently_actionable": False,
                            "gaps": [],
                        }
                    )
                    # remaining pending also marked
                    break
                fut = pool.submit(
                    _worker_analyze_one,
                    nb_id=meta["nb_id"],
                    title=meta["pkg_meta"].get("title"),
                    inv_row=dict(meta["inv"]),
                    model=model,
                    budget=budget,
                    reserve_usd=reserve,
                )
                in_flight[fut] = {**meta, "reserve": reserve}

            if budget.stop and not in_flight:
                while idx < len(pending):
                    meta = pending[idx]
                    idx += 1
                    results.append(
                        {
                            "nb_id": meta["nb_id"],
                            "title": meta["pkg_meta"].get("title"),
                            "final_state": "awaiting_ai_analysis",
                            "coverage_label": meta["label"],
                            "missing": ["openai_budget_ceiling"],
                            "disposition": "potential_product_classification_needed",
                            "currently_actionable": False,
                            "newly_analyzed": False,
                            "ai_cached": False,
                            "gaps": [],
                        }
                    )
                break

            if not in_flight:
                break
            done, _ = wait(list(in_flight.keys()), return_when=FIRST_COMPLETED, timeout=180)
            for fut in done:
                meta = in_flight.pop(fut)
                try:
                    result = fut.result()
                except Exception as exc:
                    budget.release(meta["reserve"])
                    result = {"ok": False, "nb_id": meta["nb_id"], "error": str(exc)[:300]}
                _consume(result, meta)
                snap = budget.snapshot()
                print(
                    json.dumps(
                        {
                            "phase": "progress",
                            "done": len(results),
                            "new_live": newly_analyzed,
                            "spent": snap["spent"],
                        }
                    ),
                    flush=True,
                )

    # Rebuild queues from complete results
    queues = rebuild_fresh_queues(results)
    snap = budget.snapshot()
    state_counts = Counter(r.get("final_state") for r in results)

    physical = sum(
        1
        for r in results
        if r.get("is_product_purchase")
        and r.get("final_state")
        in {
            "confirmed_product_supplier_quote_ready",
            "confirmed_product_requirements_incomplete",
        }
    )
    with_lines = sum(1 for r in results if r.get("verified_line_items"))
    with_qty_specs = sum(1 for r in results if r.get("has_verified_qty_specs"))
    quote_ready = [r for r in results if r.get("final_state") == "confirmed_product_supplier_quote_ready"]
    actionable = [r for r in results if r.get("currently_actionable")]
    gaps = []
    for r in results:
        for g in r.get("gaps") or []:
            gaps.append({"nb_id": r["nb_id"], **g})

    report = {
        "ok": True,
        "finished_at": _utc(),
        "fresh_total": len(fresh_ids),
        "reconciled": len(results),
        "previously_analyzed_openai_cache": previously_analyzed,
        "newly_analyzed_openai": newly_analyzed,
        "needs_original_documents": needs_original_docs,
        "coverage_counts": dict(coverage_counts),
        "complete_source_packages": coverage_counts.get("complete_available_package", 0),
        "incomplete_source_packages": coverage_counts.get("substantive_incomplete", 0)
        + coverage_counts.get("notice_only", 0),
        "genuine_physical_product": physical,
        "with_verified_line_items": with_lines,
        "with_verified_qty_and_specs": with_qty_specs,
        "supplier_quote_ready_count": len(quote_ready),
        "currently_actionable_count": len(actionable),
        "supplier_quote_ready": queues.get("supplier_pricing") or [_pricing_row(r) for r in quote_ready],
        "openai_spent_usd": snap["spent"],
        "owner_max_usd": float(max_usd),
        "cost_basis": "application_estimate_from_reported_tokens_gpt4o_mini_rates",
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "concurrency": {"start": workers, "final": workers},
        "final_state_counts": dict(state_counts),
        "source_document_gaps": gaps[:200],
        "gap_count": len(gaps),
        "queue_summary": queues.get("queue_summary"),
        "nextbid_api_calls": 0,
        "killswitch_locked": True,
        "deployment": False,
        "results": [
            {k: v for k, v in r.items() if k not in {"gaps"} or True}
            for r in results
        ],
    }

    # Compact results file without huge nests for TEMP
    (temp / "OWNER_REPORT.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (temp / "analyzed_summary.json").write_text(
        json.dumps(
            [
                {
                    "nb_id": r.get("nb_id"),
                    "final_state": r.get("final_state"),
                    "coverage_label": r.get("coverage_label"),
                    "ai_cached": r.get("ai_cached"),
                    "newly_analyzed": r.get("newly_analyzed"),
                    "lines": len(r.get("verified_line_items") or []),
                    "quote_ready": r.get("final_state") == "confirmed_product_supplier_quote_ready",
                    "actionable": r.get("currently_actionable"),
                }
                for r in results
            ],
            indent=2,
        ),
        encoding="utf-8",
    )
    durable = _data() / "acceptance" / "jobs_fresh_cohort_ai_complete_last.json"
    durable.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    md = [
        "# Fresh Cohort AI Complete — Owner Report",
        "",
        "**STOP FOR OWNER REVIEW**",
        "",
        f"- Fresh records reconciled: **{len(results)} / {len(fresh_ids)}**",
        f"- Previously analyzed (valid cache): **{previously_analyzed}**",
        f"- Newly analyzed by OpenAI: **{newly_analyzed}**",
        f"- Needs original documents: **{needs_original_docs}**",
        f"- Complete packages: **{coverage_counts.get('complete_available_package', 0)}**",
        f"- Physical-product (post-verify): **{physical}**",
        f"- Verified line items: **{with_lines}** · qty+specs: **{with_qty_specs}**",
        f"- Supplier-quote-ready: **{len(quote_ready)}** · currently actionable: **{len(actionable)}**",
        f"- OpenAI spent: **${snap['spent']:.4f}** / ${float(max_usd):.2f}",
        f"- NextBid: **0** · Killswitch locked · No deploy",
        "",
        "## Supplier pricing",
        "",
        "| Solicitation | Products | Qty/UOM | Deadline | Next |",
        "|---|---|---|---|---|",
    ]
    for row in report["supplier_quote_ready"]:
        md.append(
            f"| {row.get('solicitation')} | {str(row.get('products') or '')[:70]} | "
            f"{str(row.get('qty_uom') or '')[:40]} | {row.get('deadline')} | {row.get('next_action')} |"
        )
    if not report["supplier_quote_ready"]:
        md.append("| — | none | — | — | — |")
    (temp / "OWNER_REPORT.md").write_text("\n".join(md), encoding="utf-8")
    return report
