"""BUILD 002Q — Owner-authorized controlled NextBid production acquisition.

Campaign ceilings: listing≤400, detail≤1246, total≤1646.
Hard limits still bind: daily≤100, job≤50, reserve≥1000.
Federal (SAM/DIBBS) parked. OpenAI/SAM/DIBBS spend = 0.
Killswitch re-engaged on completion or safety failure.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from m3.config import get_settings
from m3.research.nextbid_acquire import (
    EXCLUDED_SOURCES,
    _normalize_listing,
    enrich_with_detail,
    maybe_nextbid_documents,
    try_original_gov_documents,
)
from m3.research.nextbid_client import NextBidClient, load_checkpoint, save_checkpoint
from m3.research.nextbid_controller import (
    BUILD_002Q_SCOPE,
    NextBidBudgetExhausted,
    NextBidUnauthorized,
    acquisition_locked,
    append_ledger,
    controller_status,
    load_budget_state,
    new_job_id,
    refresh_account_usage,
    revoke_owner_authorization,
    set_usage_reconcile_baseline,
    write_owner_authorization,
)
from m3.research.nextbid_discovery_backfill import SOURCE_PLAN, WAVE3_PLAN, _search_key
from m3.research.nextbid_free_doc_recovery import recover_free_documents
from m3.research.nextbid_inventory import load_inventory, upsert_inventory
from m3.research.nextbid_killswitch import clear_emergency_stop, engage_emergency_stop
from m3.research.nextbid_pipeline_repair import run_pipeline_repair

ARCHIVE = Path(__file__).resolve().parents[2] / "docs" / "acceptance_archive"
PAGE_SIZE = 25
CHECKPOINT = "nextbid_acquire_v1"
COVERAGE = "build002q_controlled_coverage.json"
PROGRESS_EVERY_SEC = 60


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str) -> None:
    print(f"[002q-controlled] {msg}", flush=True)


def _coverage_path() -> Path:
    p = get_settings().data_root() / "nextbid_checkpoints" / COVERAGE
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_coverage() -> dict[str, Any]:
    path = _coverage_path()
    if not path.exists():
        return {"version": 1, "build": "002Q", "searches": {}, "totals": {}, "updated_at": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "build": "002Q", "searches": {}, "totals": {}, "corrupt": True}


def _save_coverage(cov: dict[str, Any]) -> None:
    cov["updated_at"] = _utc()
    path = _coverage_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cov, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _authorize_and_unlock() -> dict[str, Any]:
    write_owner_authorization(
        authorized=True,
        max_additional_calls=1646,
        note="002Q_owner_authorized_controlled_production",
        scope=BUILD_002Q_SCOPE,
        test_call_cap=1646,
        allowed_endpoints=[
            "search",
            "get_opportunity",
            "list_documents",
            "list_files",
            "read_file",
        ],
        daily_limit_override=100,
        reset_test_used=True,
    )
    os.environ["NEXTBID_ENABLED"] = "true"
    os.environ["NEXTBID_EMERGENCY_STOP"] = "false"
    get_settings.cache_clear()
    clear_emergency_stop(owner_authorized=True)
    # Campaign buckets: never zero mid-campaign (owner: do not reset counters between jobs)
    st = load_budget_state()
    st.setdefault("campaign_listing_used", 0)
    st.setdefault("campaign_detail_used", 0)
    st.setdefault("by_endpoint", {})
    if not st.get("campaign_002q_started_at"):
        st["campaign_002q_started_at"] = _utc()
    st["campaign_listing_reserved"] = 0
    st["campaign_detail_reserved"] = 0
    from m3.research.nextbid_controller import save_budget_state

    save_budget_state(st)
    assert acquisition_locked() is False
    return controller_status()


def _relock(*, reason: str) -> None:
    engage_emergency_stop(reason=reason)
    revoke_owner_authorization()
    os.environ["NEXTBID_ENABLED"] = "false"
    os.environ["NEXTBID_EMERGENCY_STOP"] = "true"
    get_settings.cache_clear()


def _persist_listings(seen_ids: set[str], items_by_id: dict[str, dict], *, meta: dict) -> None:
    ck = load_checkpoint(CHECKPOINT)
    state = ck.get("state") or {}
    state["seen_nb_ids"] = sorted(seen_ids)
    # Merge listings without dropping enriched
    prior_listings = {
        str(i.get("nb_id")): i for i in (state.get("listings") or []) if i.get("nb_id")
    }
    prior_listings.update(items_by_id)
    state["listings"] = list(prior_listings.values())
    state["controlled_002q"] = meta
    save_checkpoint(state, CHECKPOINT)
    upsert_inventory(list(items_by_id.values()))


def run_listing_discovery(*, max_calls: int) -> dict[str, Any]:
    """Non-overlapping state/local listing enumeration. Federal sources excluded."""
    if max_calls <= 0:
        return {"ok": True, "search_calls": 0, "unique_new": 0, "stopped_reason": "no_budget"}

    plans = list(SOURCE_PLAN) + list(WAVE3_PLAN)
    # Untagged/unclassified open listings per source (no tradeline) already in WAVE3
    # Add combined commodities pass last (non-overlapping via coverage tracker)
    plans.append(
        {
            "source": "priority_commodities_combined",
            "params": {
                "tradelines": "commodities",
                "sources": "opengov,bonfire,planetbids,calprocure,publicpurchase,instantmarkets",
                "open": "true",
            },
        }
    )

    ck = load_checkpoint(CHECKPOINT)
    state = ck.get("state") or {}
    seen_ids: set[str] = set(str(x) for x in (state.get("seen_nb_ids") or []))
    for nb in load_inventory().keys():
        seen_ids.add(str(nb))
    items_by_id: dict[str, dict] = {
        str(i["nb_id"]): i for i in (state.get("listings") or []) if i.get("nb_id")
    }

    cov = _load_coverage()
    searches: dict[str, Any] = dict(cov.get("searches") or {})
    for plan in plans:
        sk = _search_key(plan["source"], plan["params"])
        if sk not in searches:
            searches[sk] = {
                "source": plan["source"],
                "query_parameters": dict(plan["params"]),
                "pages_requested": 0,
                "last_successful_page": 0,
                "provider_total_pages": None,
                "unique_records": 0,
                "duplicate_records": 0,
                "completion_status": "pending",
                "errors": [],
            }

    calls = 0
    unique_new = 0
    duplicates = 0
    cache_hits = 0
    by_endpoint: Counter[str] = Counter()
    errors: list[str] = []
    stopped: str | None = None
    job_id = new_job_id("002q-list")
    job_calls = 0
    t0 = time.time()
    last_prog = t0

    _log(f"listing discovery start max_calls={max_calls} plans={len(plans)}")

    active = True
    while active and calls < max_calls:
        progressed = False
        for plan in plans:
            if calls >= max_calls:
                break
            sk = _search_key(plan["source"], plan["params"])
            meta = searches[sk]
            if meta.get("completion_status") in {"complete", "budget_exhausted", "error_stop"}:
                continue
            next_page = int(meta.get("last_successful_page") or 0) + 1
            tp = meta.get("provider_total_pages")
            if tp is not None and next_page > int(tp):
                meta["completion_status"] = "complete"
                continue

            if job_calls >= 50:
                job_id = new_job_id("002q-list")
                job_calls = 0

            client = NextBidClient(job_id=job_id)
            params = {**plan["params"], "page": next_page, "page_size": PAGE_SIZE, "sort": "deadline"}
            try:
                res = client.search(params)
            except (NextBidBudgetExhausted, NextBidUnauthorized) as exc:
                stopped = f"{type(exc).__name__}:{exc}"
                meta["completion_status"] = "budget_exhausted"
                errors.append(stopped)
                active = False
                break
            except Exception as exc:
                errors.append(f"{sk}:page{next_page}:{type(exc).__name__}")
                meta["completion_status"] = "error_stop"
                continue

            if res.get("_cache_hit") or res.get("_durable_store_hit"):
                cache_hits += 1
            else:
                calls += 1
                job_calls += 1
                by_endpoint["search"] += 1
            progressed = True

            if meta.get("provider_total_pages") is None and res.get("total_pages") is not None:
                meta["provider_total_pages"] = int(res["total_pages"])
            if meta.get("provider_total") is None:
                meta["provider_total"] = res.get("total") or res.get("total_count")

            page_rows = 0
            page_new = 0
            for row in res.get("results") or []:
                if not isinstance(row, dict):
                    continue
                page_rows += 1
                src = str(row.get("source") or "").lower()
                if src in EXCLUDED_SOURCES:
                    continue
                # Park federal even if mis-tagged source
                url = str(row.get("source_url") or row.get("url") or "").lower()
                if "sam.gov" in url or "dibbs" in url:
                    continue
                nb = str(row.get("nb_id") or "")
                if not nb:
                    continue
                if nb in seen_ids:
                    duplicates += 1
                    meta["duplicate_records"] = int(meta.get("duplicate_records") or 0) + 1
                    continue
                seen_ids.add(nb)
                # Retain open=true search membership for status mapping
                if str(plan["params"].get("open") or "").lower() in {"true", "1", "yes"}:
                    row = {**row, "provider_open_filter": True}
                item = _normalize_listing(row)
                items_by_id[nb] = item
                unique_new += 1
                page_new += 1
                meta["unique_records"] = int(meta.get("unique_records") or 0) + 1

            meta["pages_requested"] = int(meta.get("pages_requested") or 0) + 1
            meta["last_successful_page"] = next_page
            if meta.get("provider_total_pages") is not None:
                if next_page >= int(meta["provider_total_pages"]):
                    meta["completion_status"] = "complete"
                else:
                    meta["completion_status"] = "partial"
            elif page_rows == 0:
                meta["completion_status"] = "complete"
            else:
                meta["completion_status"] = "partial"

            _persist_listings(
                seen_ids,
                items_by_id,
                meta={
                    "listing_calls": calls,
                    "unique_new": unique_new,
                    "duplicates": duplicates,
                    "at": _utc(),
                },
            )
            cov["searches"] = searches
            cov["totals"] = {
                "search_calls": calls,
                "unique_new": unique_new,
                "duplicates": duplicates,
                "cache_hits": cache_hits,
            }
            _save_coverage(cov)

            now = time.time()
            if now - last_prog >= PROGRESS_EVERY_SEC:
                _log(
                    f"listing progress calls={calls}/{max_calls} new={unique_new} "
                    f"dup={duplicates} cache_hits={cache_hits} page={sk}#{next_page}"
                )
                last_prog = now

        if not progressed:
            stopped = stopped or "all_searches_complete_or_blocked"
            break

    return {
        "ok": True,
        "search_calls_metered": calls,
        "cache_hits": cache_hits,
        "unique_new": unique_new,
        "duplicates": duplicates,
        "inventory_total": len(seen_ids),
        "by_endpoint": dict(by_endpoint),
        "stopped_reason": stopped,
        "errors": errors[-20:],
        "elapsed_sec": round(time.time() - t0, 2),
    }


def run_targeted_detail(*, max_calls: int) -> dict[str, Any]:
    """Least-cost detail for promising open product/ambiguous records."""
    if max_calls <= 0:
        return {"ok": True, "detail_calls": 0, "enriched": 0, "stopped_reason": "no_budget"}

    from m3.research.nextbid_recovery import collect_population
    from m3.research.classify import classify_solicitation_item

    pop = collect_population()
    items = list(pop["by_id"].values())
    # Rank: open/amended productish without firm qty or thin text
    ranked: list[tuple[int, dict]] = []
    for it in items:
        src = str(it.get("nextbid_source") or it.get("source") or "").lower()
        if src in EXCLUDED_SOURCES:
            continue
        url = str(it.get("official_url") or "").lower()
        if "sam.gov" in url or "dibbs" in url:
            continue  # federal parked
        st = str(it.get("solicitation_status") or it.get("lifecycle_status") or "").lower()
        if st in {"closed", "cancelled", "historical_award", "closed_by_deadline", "forecast"}:
            continue
        if it.get("nextbid_detail_fetched") and len(str(it.get("description") or it.get("document_text") or "")) >= 800:
            continue
        score = 0
        if st in {"open", "amended", "active"}:
            score += 40
        title = str(it.get("title") or "")
        if re_product(title):
            score += 30
        if not it.get("quantity"):
            score += 20
        doclen = len(str(it.get("document_text") or it.get("description") or ""))
        if doclen < 400:
            score += 15
        if it.get("document_set_status") in {"COMPLETE", "PARTIAL", "AVAILABLE"}:
            score += 5
        ranked.append((score, it))
    ranked.sort(key=lambda x: -x[0])
    targets = [it for _, it in ranked[: max(1, max_calls)]]  # at most 1 detail each

    job_id = new_job_id("002q-detail")
    job_calls = 0
    client = NextBidClient(job_id=job_id)
    calls = 0
    cache_hits = 0
    enriched_n = 0
    gov_docs = 0
    nb_docs = 0
    errors: list[str] = []
    stopped: str | None = None
    by_endpoint: Counter[str] = Counter()
    slim: list[dict] = []
    t0 = time.time()
    last_prog = t0

    _log(f"targeted detail start targets={len(targets)} max_calls={max_calls}")

    ck = load_checkpoint(CHECKPOINT)
    state = ck.get("state") or {}
    prior_enriched = {
        str(e.get("nb_id")): e for e in (state.get("enriched") or []) if e.get("nb_id")
    }

    for i, it in enumerate(targets, 1):
        if calls >= max_calls:
            stopped = "detail_budget"
            break
        if job_calls >= 50:
            job_id = new_job_id("002q-detail")
            job_calls = 0
            client = NextBidClient(job_id=job_id)

        row = dict(it)
        nb = str(row.get("nb_id") or "")
        try:
            before_used = int((load_budget_state().get("by_endpoint") or {}).get("get_opportunity") or 0)
            row = enrich_with_detail(client, row)
            after_state = load_budget_state()
            after_used = int((after_state.get("by_endpoint") or {}).get("get_opportunity") or 0)
            if after_used > before_used:
                calls += after_used - before_used
                job_calls += after_used - before_used
                by_endpoint["get_opportunity"] += after_used - before_used
            else:
                cache_hits += 1

            # Free official docs first
            row = try_original_gov_documents(row)
            if row.get("gov_downloaded"):
                gov_docs += len(row["gov_downloaded"])

            need = len(str(row.get("document_text") or "")) < 400 and re_product(
                str(row.get("title") or "")
            )
            if need and calls + 2 <= max_calls and job_calls + 2 <= 50:
                before_docs = int((load_budget_state().get("by_endpoint") or {}).get("read_file") or 0)
                before_list = int((load_budget_state().get("by_endpoint") or {}).get("list_files") or 0)
                row = maybe_nextbid_documents(client, row)
                st2 = load_budget_state()
                d_read = int((st2.get("by_endpoint") or {}).get("read_file") or 0) - before_docs
                d_list = int((st2.get("by_endpoint") or {}).get("list_files") or 0) - before_list
                d_ld = int((st2.get("by_endpoint") or {}).get("list_documents") or 0) - int(
                    (after_state.get("by_endpoint") or {}).get("list_documents") or 0
                )
                spent = max(0, d_read) + max(0, d_list) + max(0, d_ld)
                if spent:
                    calls += spent
                    job_calls += spent
                    nb_docs += spent
                    if d_read > 0:
                        by_endpoint["read_file"] += d_read
                    if d_list > 0:
                        by_endpoint["list_files"] += d_list
        except (NextBidBudgetExhausted, NextBidUnauthorized) as exc:
            stopped = f"{type(exc).__name__}:{exc}"
            errors.append(stopped)
            break
        except Exception as exc:
            errors.append(f"{nb}:{type(exc).__name__}")
            continue

        classified = classify_solicitation_item(row)
        prior_enriched[nb] = {
            k: classified.get(k)
            for k in (
                "nb_id",
                "external_key",
                "title",
                "solicitation_number",
                "buyer_name",
                "official_url",
                "source_url_original",
                "nextbid_source",
                "deadline_at",
                "solicitation_status",
                "lifecycle_status",
                "requirement_class",
                "quantity",
                "quantity_semantic",
                "quantity_evidence",
                "description",
                "document_text",
                "document_set_status",
                "package_status",
                "nextbid_detail_fetched",
                "submission_method",
                "submission_instructions",
                "record_type",
                "verified_individual_solicitation",
            )
        }
        enriched_n += 1
        slim.append(prior_enriched[nb])

        if i % 5 == 0:
            state["enriched"] = list(prior_enriched.values())
            save_checkpoint(state, CHECKPOINT)
            upsert_inventory(slim[-5:])

        now = time.time()
        if now - last_prog >= PROGRESS_EVERY_SEC:
            _log(
                f"detail progress {i}/{len(targets)} metered~={calls}/{max_calls} "
                f"enriched={enriched_n} gov_docs={gov_docs} cache_hits={cache_hits}"
            )
            last_prog = now

    state["enriched"] = list(prior_enriched.values())
    save_checkpoint(state, CHECKPOINT)
    upsert_inventory(slim)

    return {
        "ok": True,
        "detail_calls_approx": calls,
        "cache_hits": cache_hits,
        "enriched": enriched_n,
        "gov_docs": gov_docs,
        "nextbid_doc_calls": nb_docs,
        "by_endpoint": dict(by_endpoint),
        "stopped_reason": stopped,
        "errors": errors[-20:],
        "elapsed_sec": round(time.time() - t0, 2),
    }


def re_product(title: str) -> bool:
    import re

    return bool(
        re.search(
            r"(?i)\b(purchase|furnish|supply|equipment|supplies|materials?|goods|"
            r"vehicle|pump|generator|trailer|furniture|hardware|commodit|battery|"
            r"meter|radio|pager|lift|truck|tools?)\b",
            title or "",
        )
    )


def _actionable_lists(pipeline: dict[str, Any]) -> dict[str, Any]:
    after = (pipeline or {}).get("after") or {}
    pop_path = get_settings().data_root() / "acceptance" / "nextbid_pipeline_repair_population.jsonl"
    ready = []
    reg = []
    supplier = []
    for line in pop_path.open(encoding="utf-8") if pop_path.exists() else []:
        r = json.loads(line)
        url = str(r.get("official_url") or "").lower()
        if "sam.gov" in url or "dibbs" in url:
            continue  # federal parked from actionable state/local lists
        d = r.get("primary_disposition")
        row = {
            k: r.get(k)
            for k in (
                "nb_id",
                "title",
                "solicitation_number",
                "buyer_name",
                "deadline_at",
                "quantity",
                "quantity_semantic",
                "official_url",
                "requirement_class",
                "solicitation_status",
                "primary_disposition",
            )
        }
        if d == "verified_bid_opportunities":
            ready.append(row)
        elif d == "easy_vendor_registration":
            reg.append(row)
        elif (r.get("procurement_pipeline") or {}).get("quantity_defined") and (
            r.get("procurement_pipeline") or {}
        ).get("product_opportunity"):
            supplier.append(row)
    return {
        "ready_to_submit": ready,
        "registration_required": reg,
        "supplier_pricing": supplier,
        "pipeline_after": after,
    }


def _write_report(payload: dict[str, Any]) -> Path:
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    jpath = ARCHIVE / "BUILD_002Q_CONTROLLED_PRODUCTION_ACCEPTANCE.json"
    mpath = ARCHIVE / "BUILD_002Q_CONTROLLED_PRODUCTION_ACCEPTANCE.md"
    jpath.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    act = payload.get("actionable") or {}
    lines = [
        "# BUILD 002Q — Controlled NextBid Production Acquisition",
        "",
        f"**Generated:** {payload.get('generated_at')}",
        f"**Runtime:** {payload.get('elapsed_sec')}s",
        "",
        "## Usage",
        "",
        f"- Before: `{json.dumps(payload.get('usage_before'), default=str)}`",
        f"- After: `{json.dumps(payload.get('usage_after'), default=str)}`",
        f"- Metered delta: **{payload.get('metered_delta')}**",
        f"- By endpoint: `{json.dumps(payload.get('by_endpoint'), default=str)}`",
        f"- Cache/durable hits: {payload.get('cache_hits_total')}",
        f"- Campaign listing used: {payload.get('campaign_listing_used')}",
        f"- Campaign detail used: {payload.get('campaign_detail_used')}",
        f"- Killswitch re-engaged: **{payload.get('killswitch_engaged')}**",
        "",
        "## Inventory results (state/local; federal parked)",
        "",
        f"| Metric | Count |",
        f"|---|---|",
        f"| Unique inventory | {payload.get('inventory_unique')} |",
        f"| Open | {payload.get('open_count')} |",
        f"| Confirmed product | {payload.get('confirmed_product')} |",
        f"| Potential product | {payload.get('potential_product')} |",
        f"| Verified quantities | {payload.get('verified_quantities')} |",
        f"| Ready to submit (B) | {len(act.get('ready_to_submit') or [])} |",
        f"| Registration required (C) | {len(act.get('registration_required') or [])} |",
        f"| Supplier pricing (A) | {len(act.get('supplier_pricing') or [])} |",
        "",
        "## Actual ready-to-submit (B)",
        "",
    ]
    ready = act.get("ready_to_submit") or []
    if not ready:
        lines.append("_None — not inflated._")
    else:
        lines.append("| nb_id | qty | deadline | title | URL |")
        lines.append("|---|---|---|---|---|")
        for r in ready:
            lines.append(
                f"| {r.get('nb_id')} | {r.get('quantity')} | {r.get('deadline_at')} | "
                f"{str(r.get('title') or '')[:70].replace('|','/')} | {r.get('official_url')} |"
            )
    lines += ["", "## Registration required (C)", ""]
    for r in (act.get("registration_required") or [])[:30]:
        lines.append(
            f"- {r.get('nb_id')} qty={r.get('quantity')} — {str(r.get('title') or '')[:80]}"
        )
    lines += [
        "",
        "## Supplier-pricing candidates (A) sample",
        "",
    ]
    for r in (act.get("supplier_pricing") or [])[:25]:
        lines.append(
            f"- {r.get('nb_id')} qty={r.get('quantity')} — {str(r.get('title') or '')[:80]}"
        )
    lines += [
        "",
        "## Discovery / detail",
        "",
        f"- Listing: `{json.dumps(payload.get('listing'), default=str)[:800]}`",
        f"- Detail: `{json.dumps(payload.get('detail'), default=str)[:800]}`",
        f"- Free docs: `{json.dumps((payload.get('free_docs') or {}).get('stats'), default=str)}`",
        "",
        "## Confirmation",
        "",
        "- No Railway deploy. No Build 003. No OpenAI/SAM/DIBBS.",
        "- **STOP — AWAITING OWNER REVIEW.**",
        "",
    ]
    mpath.write_text("\n".join(lines), encoding="utf-8")
    return mpath


def run_detail_continuation(db: Session) -> dict[str, Any]:
    """Continue detail/docs within remaining daily/job/campaign caps, then relock."""
    t0 = time.time()
    usage_before = refresh_account_usage(allow_when_locked=True)
    if usage_before.get("used") is not None:
        set_usage_reconcile_baseline(
            account_used=int(usage_before["used"]),
            note="002Q_detail_continuation_baseline",
        )
    st = load_budget_state()
    daily_left = max(0, 100 - int(st.get("day_used") or 0))
    detail_left = max(0, 1246 - int(st.get("campaign_detail_used") or 0))
    budget = min(daily_left, detail_left, 50)
    _log(f"detail continuation budget={budget} daily_left={daily_left}")
    detail_result: dict[str, Any] = {}
    free_docs: dict[str, Any] = {}
    try:
        _authorize_and_unlock()
        detail_result = run_targeted_detail(max_calls=budget)
        _log(f"detail continuation done {detail_result}")
        try:
            free_docs = recover_free_documents(db, limit=80)
        except Exception as exc:
            free_docs = {"ok": False, "error": type(exc).__name__}
    finally:
        _relock(reason="002Q_controlled_production_detail_continuation_complete")
    assert acquisition_locked()
    pipeline = run_pipeline_repair(db)
    actionable = _actionable_lists(pipeline)
    usage_after = refresh_account_usage(allow_when_locked=True)
    st = load_budget_state()
    after = (pipeline or {}).get("after") or {}
    open_n = confirmed = potential = 0
    pop_path = get_settings().data_root() / "acceptance" / "nextbid_pipeline_repair_population.jsonl"
    if pop_path.exists():
        for line in pop_path.open(encoding="utf-8"):
            r = json.loads(line)
            if "sam.gov" in str(r.get("official_url") or "").lower():
                continue
            if str(r.get("solicitation_status") or "").lower() in {"open", "amended", "active"}:
                open_n += 1
            req = str(r.get("requirement_class") or "")
            if req in {"product_supply", "product_with_incidental_services"}:
                confirmed += 1
            elif req in {"unknown_mixed", "unknown", "pending_classification", ""}:
                potential += 1
    metered_delta = 0
    if usage_before.get("used") is not None and usage_after.get("used") is not None:
        metered_delta = int(usage_after["used"]) - int(usage_before["used"])
    payload = {
        "build": "002Q",
        "directive": "CONTROLLED_NEXTBID_PRODUCTION_ACQUISITION",
        "phase": "detail_continuation",
        "generated_at": _utc(),
        "elapsed_sec": round(time.time() - t0, 2),
        "usage_before": usage_before,
        "usage_after": usage_after,
        "metered_delta": metered_delta,
        "by_endpoint": st.get("by_endpoint"),
        "cache_hits_total": detail_result.get("cache_hits"),
        "campaign_listing_used": st.get("campaign_listing_used"),
        "campaign_detail_used": st.get("campaign_detail_used"),
        "day_used": st.get("day_used"),
        "killswitch_engaged": True,
        "acquisition_locked": True,
        "listing": {
            "note": "prior_session",
            "search_calls_metered": st.get("campaign_listing_used"),
        },
        "detail": detail_result,
        "free_docs": free_docs,
        "inventory_unique": len(load_inventory()),
        "open_count": open_n,
        "confirmed_product": confirmed,
        "potential_product": potential,
        "verified_quantities": after.get("verified_quantities"),
        "actionable": actionable,
        "dispositions": after.get("primary_dispositions"),
        "stop": "AWAITING_OWNER_REVIEW",
    }
    report = _write_report(payload)
    payload["acceptance_report_md"] = str(report)
    _log(f"DONE continuation report={report} metered_delta={metered_delta}")
    return payload


def run_controlled_production(db: Session) -> dict[str, Any]:
    t0 = time.time()
    _log("START controlled production acquisition")
    usage_before = refresh_account_usage(allow_when_locked=True)
    if usage_before.get("used") is not None:
        set_usage_reconcile_baseline(
            account_used=int(usage_before["used"]),
            note="002Q_controlled_production_baseline",
        )

    listing_result: dict[str, Any] = {}
    detail_result: dict[str, Any] = {}
    free_docs: dict[str, Any] = {}
    pipeline: dict[str, Any] = {}
    safety_stop: str | None = None
    tls: dict[str, Any] = {}

    try:
        status = _authorize_and_unlock()
        _log(f"unlocked scope={BUILD_002Q_SCOPE} limits={status.get('limits')}")

        # Free tradelines snapshot (non-metered)
        try:
            tls = NextBidClient(job_id=new_job_id("002q-free")).tradelines()
        except Exception as exc:
            tls = {"ok": False, "error": type(exc).__name__}

        # Allocate today's remaining daily budget: prefer listing first, then detail
        st = load_budget_state()
        day_used = int(st.get("day_used") or 0)
        daily_left = max(0, 100 - day_used)
        listing_used = int(st.get("campaign_listing_used") or 0)
        detail_used = int(st.get("campaign_detail_used") or 0)
        listing_left = max(0, 400 - listing_used)
        detail_left = max(0, 1246 - detail_used)
        campaign_left = max(0, 1646 - listing_used - detail_used)

        # Session plan within daily: up to 2 jobs × 50, but only daily_left total
        session_budget = min(daily_left, campaign_left, 100)
        # Prefer ~70% listing / 30% detail for this session when both available
        list_budget = min(listing_left, int(session_budget * 0.7) if detail_left else session_budget)
        # Round to job chunks handled inside runners via job rotation
        list_budget = min(list_budget, session_budget)
        detail_budget = min(detail_left, session_budget - list_budget)

        _log(
            f"session_budget={session_budget} list_budget={list_budget} "
            f"detail_budget={detail_budget} daily_left={daily_left}"
        )

        listing_result = run_listing_discovery(max_calls=list_budget)
        _log(f"listing done {listing_result}")

        # Refresh remaining after listing
        st = load_budget_state()
        day_used = int(st.get("day_used") or 0)
        daily_left = max(0, 100 - day_used)
        detail_budget = min(detail_budget, daily_left, max(0, 1246 - int(st.get("campaign_detail_used") or 0)))
        detail_result = run_targeted_detail(max_calls=detail_budget)
        _log(f"detail done {detail_result}")

        # Free official URL recovery (no NextBid credits)
        try:
            free_docs = recover_free_documents(db, limit=120)
        except Exception as exc:
            free_docs = {"ok": False, "error": type(exc).__name__}

    except Exception as exc:
        safety_stop = f"{type(exc).__name__}:{exc}"
        _log(f"SAFETY STOP {safety_stop}")
    finally:
        _relock(reason="002Q_controlled_production_complete")
        _log("killswitch re-engaged")

    assert acquisition_locked(), "must be locked after controlled run"

    # Offline reprocess full inventory
    _log("offline pipeline reprocess")
    pipeline = run_pipeline_repair(db)
    actionable = _actionable_lists(pipeline)

    usage_after = refresh_account_usage(allow_when_locked=True)
    metered_delta = 0
    if usage_before.get("used") is not None and usage_after.get("used") is not None:
        metered_delta = int(usage_after["used"]) - int(usage_before["used"])

    st = load_budget_state()
    after = (pipeline or {}).get("after") or {}
    disp = after.get("primary_dispositions") or {}

    # Status counts from population
    open_n = confirmed = potential = 0
    pop_path = get_settings().data_root() / "acceptance" / "nextbid_pipeline_repair_population.jsonl"
    if pop_path.exists():
        for line in pop_path.open(encoding="utf-8"):
            r = json.loads(line)
            url = str(r.get("official_url") or "").lower()
            if "sam.gov" in url:
                continue
            stv = str(r.get("solicitation_status") or "").lower()
            if stv in {"open", "amended", "active"}:
                open_n += 1
            req = str(r.get("requirement_class") or "")
            if req in {"product_supply", "product_with_incidental_services"}:
                confirmed += 1
            elif req in {"unknown_mixed", "unknown", "pending_classification", ""}:
                potential += 1

    by_ep = dict(st.get("by_endpoint") or {})
    cache_hits = int((listing_result or {}).get("cache_hits") or 0) + int(
        (detail_result or {}).get("cache_hits") or 0
    )

    payload = {
        "build": "002Q",
        "directive": "CONTROLLED_NEXTBID_PRODUCTION_ACQUISITION",
        "generated_at": _utc(),
        "elapsed_sec": round(time.time() - t0, 2),
        "usage_before": usage_before,
        "usage_after": usage_after,
        "metered_delta": metered_delta,
        "by_endpoint": by_ep,
        "cache_hits_total": cache_hits,
        "campaign_listing_used": st.get("campaign_listing_used"),
        "campaign_detail_used": st.get("campaign_detail_used"),
        "day_used": st.get("day_used"),
        "killswitch_engaged": True,
        "acquisition_locked": acquisition_locked(),
        "safety_stop": safety_stop,
        "listing": listing_result,
        "detail": detail_result,
        "free_docs": free_docs,
        "inventory_unique": len(load_inventory()),
        "open_count": open_n,
        "confirmed_product": confirmed,
        "potential_product": potential,
        "verified_quantities": after.get("verified_quantities"),
        "actionable": actionable,
        "dispositions": disp,
        "tradelines_free": {
            "ok": (tls or {}).get("ok") if isinstance(tls, dict) else None
        },
        "stop": "AWAITING_OWNER_REVIEW",
    }
    report = _write_report(payload)
    payload["acceptance_report_md"] = str(report)
    append_ledger(
        {
            "op": "controlled_production_complete",
            "metered": False,
            "metered_delta": metered_delta,
            "report": str(report),
        }
    )
    _log(f"DONE report={report} metered_delta={metered_delta}")
    _log("STOP — awaiting owner review")
    return payload
