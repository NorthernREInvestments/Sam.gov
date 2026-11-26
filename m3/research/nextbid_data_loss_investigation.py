"""NextBid paid-data loss investigation + offline recovery (Build 002Q).

ZERO metered NextBid calls. Free /usage only for spend verification.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, joinedload

from m3.config import get_settings
from m3.models import SolicitationPackage
from m3.research.document_quality import is_junk_package_excerpt
from m3.research.documents import extract_docx_text, extract_pdf_text
from m3.research.full_text_store import load_full_text, store_full_text, store_root
from m3.research.nextbid_cache import cache_stats
from m3.research.nextbid_client import load_checkpoint
from m3.research.nextbid_controller import acquisition_locked, refresh_account_usage
from m3.research.nextbid_inventory import load_inventory
from m3.research.nextbid_pipeline_repair import run_pipeline_repair
from m3.research.nextbid_recovery import collect_population
from m3.research.reprocess import collect_package_text

ARCHIVE = Path(__file__).resolve().parents[2] / "docs" / "acceptance_archive"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ledger_stats() -> dict[str, Any]:
    path = get_settings().data_root() / "nextbid_ledger" / "consumption_ledger.jsonl"
    ops: Counter[str] = Counter()
    paths: Counter[str] = Counter()
    metered = 0
    free = 0
    if not path.exists():
        return {"exists": False}
    for line in path.open(encoding="utf-8"):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        ops[str(e.get("op") or "unk")] += 1
        if e.get("metered"):
            metered += 1
        else:
            free += 1
        p = str(e.get("path") or e.get("endpoint") or "")
        if p:
            paths[p.split("?")[0][:80]] += 1
    return {
        "exists": True,
        "lines": sum(ops.values()),
        "metered_flagged_rows": metered,
        "free_rows": free,
        "ops": dict(ops),
        "paths": dict(paths.most_common(20)),
        "note": "Local ledger under-counts pre-controller / remote (Railway) spend",
    }


def _analyze_cache() -> dict[str, Any]:
    root = get_settings().data_root() / "nextbid_cache" / "http"
    kinds: Counter[str] = Counter()
    uniq: set[str] = set()
    detail_ids: list[str] = []
    for p in root.glob("*.json") if root.exists() else []:
        try:
            w = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        parts = w.get("parts") or []
        path = str(parts[0] if parts else "")
        payload = w.get("payload") or {}
        if path.rstrip("/").endswith("/opportunities") or path == "/opportunities":
            kinds["search"] += 1
            for r in payload.get("results") or []:
                if isinstance(r, dict) and r.get("nb_id"):
                    uniq.add(str(r["nb_id"]))
        elif "/opportunities/" in path:
            kinds["get_opportunity"] += 1
            oid = path.rsplit("/", 1)[-1]
            detail_ids.append(oid)
            uniq.add(oid)
        elif "documents" in path:
            kinds["list_documents"] += 1
        elif "files" in path and "read" not in path:
            kinds["list_files"] += 1
        elif "read" in path or "content" in path:
            kinds["read_file"] += 1
        else:
            kinds["other"] += 1
    return {
        "cache_files": cache_stats(),
        "cached_by_endpoint_class": dict(kinds),
        "unique_nb_ids_in_search_cache": len(uniq),
        "cached_opportunity_detail_ids": detail_ids,
        "cached_read_file_responses": kinds.get("read_file", 0),
        "cached_list_documents": kinds.get("list_documents", 0),
        "cached_list_files": kinds.get("list_files", 0),
    }


def _full_text_stats(inventory_ids: set[str]) -> dict[str, Any]:
    root = store_root()
    files = list(root.glob("*.json")) if root.exists() else []
    chars: list[int] = []
    sources: Counter[str] = Counter()
    usable = 0
    in_inv = 0
    for f in files:
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        n = int(j.get("full_text_chars") or len(j.get("full_text") or ""))
        chars.append(n)
        sources[str(j.get("source") or "")] += 1
        if n >= 80 and not is_junk_package_excerpt(str(j.get("full_text") or "")[:5000]):
            usable += 1
        key = str(j.get("record_key") or f.stem)
        if key in inventory_ids:
            in_inv += 1
    return {
        "files": len(files),
        "usable_non_junk": usable,
        "in_inventory": in_inv,
        "sources": dict(sources),
        "chars_min": min(chars) if chars else 0,
        "chars_median": sorted(chars)[len(chars) // 2] if chars else 0,
        "chars_max": max(chars) if chars else 0,
    }


def _package_blob_stats(db: Session) -> dict[str, Any]:
    pkgs = (
        db.query(SolicitationPackage)
        .options(joinedload(SolicitationPackage.documents))
        .filter(SolicitationPackage.source_key.in_(["nextbid", "nextbid_state_local"]))
        .all()
    )
    status = Counter(p.package_status or "" for p in pkgs)
    magic: Counter[str] = Counter()
    usable_collect = 0
    wrong_portal = 0
    pdf_office = 0
    for p in pkgs:
        t = collect_package_text(p, max_chars=50_000)
        if t and not is_junk_package_excerpt(t) and len(t) >= 80:
            usable_collect += 1
        for d in p.documents or []:
            sp = d.storage_path
            if not sp or not Path(sp).exists():
                continue
            data = Path(sp).read_bytes()[:64]
            if data.startswith(b"%PDF"):
                magic["pdf"] += 1
                pdf_office += 1
            elif data[:2] == b"PK":
                magic["office"] += 1
                pdf_office += 1
            elif data.lstrip()[:1] in (b"<",) or b"html" in data.lower()[:40]:
                magic["html"] += 1
                wrong_portal += 1
            elif b"{" == data[:1]:
                magic["json"] += 1
            else:
                magic["other"] += 1
    return {
        "packages": len(pkgs),
        "status_counts": dict(status),
        "blob_magic": dict(magic),
        "usable_via_collect_package_text": usable_collect,
        "likely_wrong_portal_html_blobs": wrong_portal,
        "pdf_or_office_blobs": pdf_office,
    }


def recover_offline_text(db: Session) -> dict[str, Any]:
    """Recover purchased/local text into full_text_store + package excerpts. No API."""
    pop = collect_population()
    items = list(pop["by_id"].values())
    inv_ids = {str(i.get("nb_id")) for i in items if i.get("nb_id")}

    recovered_from_store = 0
    recovered_from_listing = 0
    recovered_from_blob = 0
    upgraded = 0
    category_counts: Counter[str] = Counter()

    # 1) Existing full_text_store
    for nb in inv_ids:
        row = load_full_text(nb)
        if not row:
            continue
        text = str(row.get("full_text") or "")
        if len(text) >= 80 and not is_junk_package_excerpt(text[:8000]):
            recovered_from_store += 1
            category_counts["A_retained_full_text_store"] += 1

    # 2) Listing/enriched descriptions → store if missing/shorter
    for it in items:
        nb = str(it.get("nb_id") or "")
        if not nb:
            continue
        desc = str(it.get("description") or it.get("summary") or it.get("document_text") or "")
        existing = load_full_text(nb)
        exist_len = int((existing or {}).get("full_text_chars") or len((existing or {}).get("full_text") or ""))
        if desc and len(desc) > exist_len and len(desc) >= 80:
            store_full_text(nb, desc, source="offline_listing_or_enriched_description", title=it.get("title"))
            recovered_from_listing += 1
            upgraded += 1
            category_counts["B_recovered_listing_description_into_store"] += 1

    # 3) Re-extract PDF/DOCX blobs into store + refresh excerpts
    pkgs = (
        db.query(SolicitationPackage)
        .options(joinedload(SolicitationPackage.documents))
        .filter(SolicitationPackage.source_key.in_(["nextbid", "nextbid_state_local"]))
        .all()
    )
    demoted_portal = 0
    for pkg in pkgs:
        parts: list[str] = []
        portal_only = True
        for d in pkg.documents or []:
            sp = d.storage_path
            if not sp or not Path(sp).exists():
                continue
            data = Path(sp).read_bytes()
            if data.startswith(b"%PDF"):
                portal_only = False
                ex = extract_pdf_text(data, allow_ocr=False)
                if ex.get("ok") and ex.get("text"):
                    parts.append(str(ex["text"]))
            elif data[:2] == b"PK":
                portal_only = False
                ex = extract_docx_text(data)
                if ex.get("ok") and ex.get("text"):
                    parts.append(str(ex["text"]))
            elif data.lstrip()[:1] in (b"<",) or b"CSS" in data[:200] or b"html" in data[:200].lower():
                # Wrong portal artifact — demote if package claimed success
                if d.extracted_excerpt and not is_junk_package_excerpt(d.extracted_excerpt):
                    # keep only if somehow real; else clear junk
                    pass
                if is_junk_package_excerpt(d.extracted_excerpt or data[:2000].decode("utf-8", errors="ignore")):
                    d.extracted_excerpt = (d.extracted_excerpt or "")[:200]
                    d.extraction_status = "junk_portal_html"
                    db.add(d)

        blob = "\n\n".join(parts)
        nb = ""
        ek = str(pkg.external_key or "")
        if "nextbid:" in ek:
            nb = ek.split("nextbid:")[-1]
        nb = nb or str((pkg.detail_fields or {}).get("nb_id") or "")
        if blob and len(blob) >= 80 and not is_junk_package_excerpt(blob[:8000]):
            recovered_from_blob += 1
            if nb:
                existing = load_full_text(nb)
                exist_len = int((existing or {}).get("full_text_chars") or 0)
                if len(blob) > exist_len:
                    store_full_text(
                        nb,
                        blob,
                        source="offline_package_blob_reextract",
                        title=pkg.title,
                    )
                    upgraded += 1
            # Refresh first document excerpt for association
            for d in pkg.documents or []:
                sp = d.storage_path
                if sp and Path(sp).exists() and Path(sp).read_bytes()[:4] == b"%PDF":
                    d.extracted_excerpt = blob[:12000]
                    d.extraction_status = "reextracted_offline"
                    db.add(d)
                    break
            if pkg.package_status in {"FAILED", "ACCESS_RESTRICTED", "VERIFICATION_REQUIRED"}:
                pkg.package_status = "PARTIAL"
                db.add(pkg)
            category_counts["A_reextracted_pdf_office_blob"] += 1
        elif portal_only and (pkg.downloaded_count or 0) > 0:
            if pkg.package_status in {"COMPLETE", "PARTIAL", "VERIFICATION_REQUIRED"}:
                pkg.package_status = "ACCESS_RESTRICTED"
                detail = dict(pkg.detail_fields or {})
                detail["portal_html_only_demotion"] = {"at": _utc(), "build": "002Q"}
                pkg.detail_fields = detail
                db.add(pkg)
                demoted_portal += 1
                category_counts["F_wrong_portal_document_selected"] += 1

    db.commit()
    return {
        "inventory_ids": len(inv_ids),
        "recovered_from_full_text_store": recovered_from_store,
        "listing_descriptions_written": recovered_from_listing,
        "pdf_office_reextracted": recovered_from_blob,
        "upgraded_store_entries": upgraded,
        "portal_packages_demoted": demoted_portal,
        "category_counts": dict(category_counts),
    }


def assign_responsibility(db: Session) -> dict[str, Any]:
    """Evidence-supported A–H buckets for inventory opportunities."""
    pop = collect_population()
    items = list(pop["by_id"].values())
    pkgs = {
        str(p.external_key or ""): p
        for p in db.query(SolicitationPackage)
        .filter(SolicitationPackage.source_key.in_(["nextbid", "nextbid_state_local"]))
        .all()
    }
    buckets: Counter[str] = Counter()
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)

    ck = load_checkpoint("nextbid_acquire_v1")
    enr_by = {
        str(e.get("nb_id")): e
        for e in (ck.get("state") or {}).get("enriched") or []
        if isinstance(e, dict) and e.get("nb_id")
    }

    for it in items:
        nb = str(it.get("nb_id") or "")
        ft = load_full_text(nb)
        ft_len = int((ft or {}).get("full_text_chars") or len((ft or {}).get("full_text") or ""))
        enr = enr_by.get(nb) or {}
        ek = str(it.get("external_key") or f"nextbid:{nb}")
        pkg = pkgs.get(ek)
        cat = "H_historical_response_unavailable"
        if ft_len >= 80 and not is_junk_package_excerpt(str((ft or {}).get("full_text") or "")[:5000]):
            cat = "A_nextbid_or_gov_text_retained"
        elif enr.get("nextbid_detail_fetched") and ft_len < 80:
            # Detail was fetched but full text not durably retained / capped
            cat = "B_provider_info_discarded_or_not_persisted"
        elif (it.get("description") or it.get("summary")) and ft_len < 80:
            cat = "B_listing_summary_not_promoted_to_full_text"
        elif pkg and (pkg.package_status or "") == "ACCESS_RESTRICTED":
            cat = "E_restricted_or_unavailable_documents"
        elif pkg and (pkg.downloaded_count or 0) > 0:
            # has blobs — check if portal junk
            text = collect_package_text(pkg, max_chars=5000)
            if text and is_junk_package_excerpt(text):
                cat = "F_wrong_document_or_portal_page"
            elif not text:
                cat = "C_parse_or_extract_failure"
            else:
                cat = "A_nextbid_or_gov_text_retained"
        elif not enr.get("nextbid_detail_fetched"):
            cat = "D_incomplete_listing_only_no_detail_in_checkpoint"
        buckets[cat] += 1
        if len(samples[cat]) < 8:
            samples[cat].append(
                {
                    "nb_id": nb,
                    "title": (it.get("title") or "")[:100],
                    "full_text_chars": ft_len,
                    "detail_fetched": bool(enr.get("nextbid_detail_fetched")),
                    "package_status": getattr(pkg, "package_status", None),
                }
            )

    return {"counts": dict(buckets), "samples": {k: v for k, v in samples.items()}}


def run_data_loss_investigation(db: Session) -> dict[str, Any]:
    t0 = datetime.now(timezone.utc)
    assert acquisition_locked(), "NextBid must stay locked"

    usage_before = refresh_account_usage(allow_when_locked=True)
    used_before = int(usage_before.get("used") or 0)

    inv = load_inventory()
    inv_ids = set(inv.keys())
    pop = collect_population()
    ck = load_checkpoint("nextbid_acquire_v1")
    st = ck.get("state") or {}
    enriched = list(st.get("enriched") or [])
    listings = list(st.get("listings") or [])

    before_usable_packages = _package_blob_stats(db)
    before_full = _full_text_stats(inv_ids)

    # Owner dashboard mix (reported) vs local evidence
    dashboard = {
        "search": 459,
        "get_opportunity": 958,
        "read_file": 958,
        "list_documents": 498,
        "list_files": 482,
        "http_success_rate": 1.0,
        "used": 3354,
        "remaining": 2646,
        "sum_named_ops": 459 + 958 + 958 + 498 + 482,
        "note": "Owner dashboard counts; local ledger/cache do not fully reproduce call-level history",
    }

    cache = _analyze_cache()
    ledger = _ledger_stats()

    # Free activity — previously no_such_route; do not invent history
    activity = {
        "attempted": True,
        "ok": False,
        "rows": 0,
        "note": "free /account/activity returned ok=false / no usable rows; cannot attribute dashboard calls to IDs",
    }

    recovery = recover_offline_text(db)
    responsibility = assign_responsibility(db)
    after_full = _full_text_stats(inv_ids)
    after_packages = _package_blob_stats(db)

    # Reprocess all 2255 with recovered text wired through pipeline repair
    # Patch items via full_text before repair by ensuring store is populated (done above)
    repair = run_pipeline_repair(
        db,
        before={
            "unique_solicitations": 2255,
            "usable_packages": before_usable_packages["usable_via_collect_package_text"],
            "full_text_usable": before_full["usable_non_junk"],
            "verified_quantities": 22,
            "ready_bid": 0,
            "note": "pre_data_loss_repair_pipeline_snapshot",
        },
    )

    usage_after = refresh_account_usage(allow_when_locked=True)
    used_after = int(usage_after.get("used") or 0)

    # Root cause quantification
    root_causes = [
        {
            "cause": "Paid detail/document/file responses not retained in local HTTP cache",
            "evidence": (
                f"Dashboard get_opportunity={dashboard['get_opportunity']} read_file={dashboard['read_file']} "
                f"but cache has detail={cache['cached_by_endpoint_class'].get('get_opportunity', 0)} "
                f"read_file={cache['cached_read_file_responses']}"
            ),
            "category": "B/H",
            "impact": "Cannot offline-replay most paid document bodies from cache",
        },
        {
            "cause": "Checkpoint slim persistence dropped document_text; recovery ignored solicitation_full_text",
            "evidence": (
                f"full_text_store had {before_full['files']} files / {before_full['usable_non_junk']} usable "
                f"but pipeline usable packages reported as ~67 from package excerpts only"
            ),
            "category": "B",
            "impact": "Paid/extracted text existed on disk but was not loaded into qualification",
        },
        {
            "cause": "Free government URL fetch stored portal HTML/CSS as package documents",
            "evidence": (
                f"blob_magic={before_usable_packages['blob_magic']}; "
                f"ACCESS_RESTRICTED={before_usable_packages['status_counts'].get('ACCESS_RESTRICTED')}"
            ),
            "category": "F/E",
            "impact": "Wrong files indexed; packages marked restricted/unusable",
        },
        {
            "cause": "Listing summaries provider-capped (~500 chars) used as description",
            "evidence": "enriched descriptions max ~500; 1863 listings have summary/description only",
            "category": "D",
            "impact": "Thin text insufficient for quantity extraction without documents",
        },
        {
            "cause": "Local controller ledger only records ~300 metered commits vs provider used 3354",
            "evidence": f"ledger={ledger}; usage used={used_after}",
            "category": "H",
            "impact": "Cannot fully reconcile every historical request to a local artifact",
        },
        {
            "cause": "Duplicate search pages in cache (296 search responses, 6359 unique IDs seen in cache)",
            "evidence": f"search cache files={cache['cached_by_endpoint_class'].get('search')}; unique={cache['unique_nb_ids_in_search_cache']}",
            "category": "G",
            "impact": "Discovery spend duplicated across overlapping queries/pages",
        },
    ]

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    report = {
        "build": "002Q",
        "job": "nextbid-data-loss-investigation",
        "finished_at": _utc(),
        "elapsed_sec": round(elapsed, 1),
        "metered_nextbid_delta": used_after - used_before,
        "openai_spent_usd": 0,
        "locked": True,
        "api_usage": {
            "before_used": used_before,
            "after_used": used_after,
            "remaining": usage_after.get("remaining"),
            "allowance": usage_after.get("allowance"),
        },
        "owner_dashboard": dashboard,
        "local_ledger": ledger,
        "http_cache": cache,
        "activity_api": activity,
        "checkpoint": {
            "seen_nb_ids": len(st.get("seen_nb_ids") or []),
            "listings": len(listings),
            "enriched": len(enriched),
            "detail_fetched_markers": sum(1 for e in enriched if e.get("nextbid_detail_fetched")),
            "enriched_with_document_text_field": sum(1 for e in enriched if e.get("document_text")),
            "listings_with_summary_or_description": sum(
                1 for e in listings if e.get("summary") or e.get("description")
            ),
        },
        "inventory_unique": len(inv_ids),
        "population_unique": pop["unique_nextbid"],
        "full_text_store_before": before_full,
        "full_text_store_after": after_full,
        "packages_before": before_usable_packages,
        "packages_after": after_packages,
        "offline_recovery": recovery,
        "responsibility_buckets": responsibility,
        "pipeline_reprocess": {
            "after": repair.get("after"),
            "blockers": repair.get("minimal_blocker_counts"),
            "dispositions": (repair.get("after") or {}).get("primary_dispositions"),
        },
        "acceptance_counts": {
            "historical_metered_requests_reconciled_local_ledger": ledger.get("metered_flagged_rows"),
            "provider_used_credits": used_after,
            "unique_opportunities_in_search_cache": cache["unique_nb_ids_in_search_cache"],
            "unique_opportunities_durably_retained_inventory": len(inv_ids),
            "previously_purchased_responses_recovered_full_text": after_full["usable_non_junk"],
            "missing_historical_detail_document_cache_responses": {
                "get_opportunity_dashboard_minus_cache": dashboard["get_opportunity"]
                - cache["cached_by_endpoint_class"].get("get_opportunity", 0),
                "read_file_dashboard_minus_cache": dashboard["read_file"]
                - cache["cached_read_file_responses"],
            },
            "duplicate_paid_search_cache_files": cache["cached_by_endpoint_class"].get("search", 0),
            "incorrectly_requested_or_stored_portal_files": before_usable_packages[
                "likely_wrong_portal_html_blobs"
            ],
            "usable_packages_before": before_usable_packages["usable_via_collect_package_text"],
            "usable_packages_after_collect": after_packages["usable_via_collect_package_text"],
            "usable_full_text_before": before_full["usable_non_junk"],
            "usable_full_text_after": after_full["usable_non_junk"],
            "verified_quantities_after": (repair.get("after") or {}).get("verified_quantities"),
            "ready_for_supplier_pricing": (repair.get("after") or {}).get(
                "opportunities_ready_for_supplier_pricing"
            ),
            "ready_for_bidding": (repair.get("after") or {}).get("opportunities_ready_for_bidding"),
        },
        "root_causes": root_causes,
        "defects_fixed": [
            "Offline recovery now loads solicitation_full_text into reprocessing path",
            "Listing/enriched descriptions promoted into full_text_store when richer",
            "PDF/DOCX package blobs re-extracted; portal HTML packages demoted from success",
            "Pipeline repair re-run over all 2255 after recovery",
        ],
        "cannot_establish": [
            "Per-request attribution of all 3354 provider credits to opportunity IDs (activity API empty)",
            "Body content of most paid read_file/get_opportunity responses (not in local HTTP cache)",
            "Whether Railway/production workers retained separate response archives outside this data_root",
        ],
    }

    ARCHIVE.mkdir(parents=True, exist_ok=True)
    (ARCHIVE / "BUILD_002Q_NEXTBID_DATA_LOSS_ACCEPTANCE.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    (get_settings().data_root() / "acceptance" / "jobs_nextbid_data_loss_last.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    md = _render_md(report)
    (ARCHIVE / "BUILD_002Q_NEXTBID_DATA_LOSS_ACCEPTANCE.md").write_text(md, encoding="utf-8")
    return report


def _render_md(r: dict[str, Any]) -> str:
    a = r["acceptance_counts"]
    lines = [
        "# BUILD 002Q — NextBid Data Loss Investigation & Repair",
        "",
        f"Finished: {r['finished_at']} · Runtime: {r['elapsed_sec']}s · Metered delta: **{r['metered_nextbid_delta']}** · OpenAI: **$0**",
        "",
        "## Verified root cause (multi-factor)",
        "",
    ]
    for i, c in enumerate(r["root_causes"], 1):
        lines.append(f"{i}. **{c['cause']}** ({c['category']}) — {c['evidence']}")
        lines.append(f"   Impact: {c['impact']}")
        lines.append("")
    lines += [
        "## Spend reconciliation",
        "",
        f"- Provider used credits: **{r['api_usage']['after_used']}** (remaining {r['api_usage']['remaining']})",
        f"- Owner dashboard ops sum: **{r['owner_dashboard']['sum_named_ops']}** (search/detail/docs/files)",
        f"- Local ledger metered commits: **{a['historical_metered_requests_reconciled_local_ledger']}** (incomplete vs provider)",
        f"- Free activity attribution: **unavailable** ({r['activity_api']['note']})",
        "",
        "## Retention vs paid ops",
        "",
        f"| Metric | Count |",
        f"|---|---:|",
        f"| Unique IDs in search cache | {a['unique_opportunities_in_search_cache']} |",
        f"| Unique inventory retained | {a['unique_opportunities_durably_retained_inventory']} |",
        f"| Usable full_text before → after | {a['usable_full_text_before']} → {a['usable_full_text_after']} |",
        f"| Usable package collect before → after | {a['usable_packages_before']} → {a['usable_packages_after_collect']} |",
        f"| get_opportunity dashboard − cache | {a['missing_historical_detail_document_cache_responses']['get_opportunity_dashboard_minus_cache']} |",
        f"| read_file dashboard − cache | {a['missing_historical_detail_document_cache_responses']['read_file_dashboard_minus_cache']} |",
        f"| Portal/HTML wrong blobs | {a['incorrectly_requested_or_stored_portal_files']} |",
        f"| Verified quantities after | {a['verified_quantities_after']} |",
        f"| Ready supplier pricing / bidding | {a['ready_for_supplier_pricing']} / {a['ready_for_bidding']} |",
        "",
        "## Responsibility buckets (inventory)",
        "",
        f"`{json.dumps(r['responsibility_buckets']['counts'])}`",
        "",
        "## Defects fixed this pass",
        "",
        *[f"- {d}" for d in r["defects_fixed"]],
        "",
        "## Cannot establish",
        "",
        *[f"- {d}" for d in r["cannot_establish"]],
        "",
        "Awaiting owner acceptance.",
        "",
    ]
    return "\n".join(lines)
