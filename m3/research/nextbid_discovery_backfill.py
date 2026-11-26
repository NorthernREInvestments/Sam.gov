"""Build 002M — discovery/search-only NextBid backfill (≤300 metered calls).

Persists every page before requesting the next. No detail/doc/file calls.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings
from m3.research.nextbid_acquire import EXCLUDED_SOURCES, _normalize_listing
from m3.research.nextbid_client import NextBidClient, NextBidQuotaError, load_checkpoint, save_checkpoint
from m3.research.nextbid_controller import (
    BUILD_002M_SCOPE,
    NextBidBudgetExhausted,
    NextBidUnauthorized,
    controller_status,
    new_job_id,
)
from m3.research.nextbid_inventory import upsert_inventory
from m3.research.procurement_registry import ingest_population

PAGE_SIZE = 25
CHECKPOINT_NAME = "nextbid_acquire_v1"
COVERAGE_NAME = "build002m_coverage_v1.json"

# Wave 1: per-source commodities (establishes source-level coverage stats).
# Wave 2: nationwide commodities without source filter — provider reports ~14k+ open;
#         this maximizes unique product listings per search call within the 300 budget.
SOURCE_PLAN: list[dict[str, Any]] = [
    {"source": "opengov", "params": {"tradelines": "commodities", "sources": "opengov", "open": "true"}},
    {"source": "bonfire", "params": {"tradelines": "commodities", "sources": "bonfire", "open": "true"}},
    {"source": "planetbids", "params": {"tradelines": "commodities", "sources": "planetbids", "open": "true"}},
    {"source": "calprocure", "params": {"tradelines": "commodities", "sources": "calprocure", "open": "true"}},
    {"source": "publicpurchase", "params": {"tradelines": "commodities", "sources": "publicpurchase", "open": "true"}},
    {"source": "instantmarkets", "params": {"tradelines": "commodities", "sources": "instantmarkets", "open": "true"}},
]

WAVE2_PLAN: list[dict[str, Any]] = [
    {
        "source": "all_priority_commodities",
        "params": {
            "tradelines": "commodities",
            "sources": "opengov,bonfire,planetbids,calprocure,publicpurchase,instantmarkets",
            "open": "true",
        },
    },
    # NOTE: never search commodities without a sources= whitelist — provider ranks
    # DIBBS/SAM heavily and wastes budget on excluded federal sources.
]

# Wave 3: open solicitations on authorized state/local sources without tradeline
# filter (broader than commodities-only), still excluding SAM/DIBBS via sources=.
WAVE3_PLAN: list[dict[str, Any]] = [
    {"source": "opengov_open", "params": {"sources": "opengov", "open": "true"}},
    {"source": "bonfire_open", "params": {"sources": "bonfire", "open": "true"}},
    {"source": "planetbids_open", "params": {"sources": "planetbids", "open": "true"}},
    {"source": "calprocure_open", "params": {"sources": "calprocure", "open": "true"}},
    {"source": "publicpurchase_open", "params": {"sources": "publicpurchase", "open": "true"}},
    {"source": "instantmarkets_open", "params": {"sources": "instantmarkets", "open": "true"}},
]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def coverage_path() -> Path:
    p = get_settings().data_root() / "nextbid_checkpoints" / COVERAGE_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_coverage() -> dict[str, Any]:
    path = coverage_path()
    if not path.exists():
        return {
            "version": 1,
            "build": "002M",
            "searches": {},
            "totals": {
                "search_calls": 0,
                "rows_returned": 0,
                "unique_new": 0,
                "duplicates": 0,
            },
            "updated_at": None,
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "build": "002M", "searches": {}, "totals": {}, "corrupt": True}


def save_coverage(cov: dict[str, Any]) -> None:
    cov["updated_at"] = _utc()
    path = coverage_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cov, indent=2, default=str), encoding="utf-8")
    try:
        tmp.replace(path)
    except PermissionError:
        path.write_text(json.dumps(cov, indent=2, default=str), encoding="utf-8")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _search_key(source: str, params: dict[str, Any]) -> str:
    return f"{source}|tradelines={params.get('tradelines')}|open={params.get('open')}"


def run_discovery_backfill(
    *,
    max_calls: int = 300,
    page_size: int = PAGE_SIZE,
    plans: list[dict[str, Any]] | None = None,
    prior_calls: int = 0,
    build_call_ceiling: int = 300,
) -> dict[str, Any]:
    """Exhaust broad source listings within max_calls. Search-only.

    prior_calls + this run must not exceed build_call_ceiling (hard 300).
    """
    if max_calls > 300 or build_call_ceiling > 300:
        raise ValueError("build_002m_hard_cap_300")
    remaining_build = max(0, build_call_ceiling - int(prior_calls))
    max_calls = min(max_calls, remaining_build)
    if max_calls <= 0:
        return {
            "ok": True,
            "search_calls": 0,
            "unique_new": 0,
            "stopped_reason": "build_ceiling_already_reached",
            "within_300": True,
            "prior_calls": prior_calls,
        }

    started = _utc()
    t0 = datetime.now(timezone.utc)
    plan_list = list(plans or (SOURCE_PLAN + WAVE2_PLAN))

    ck = load_checkpoint(CHECKPOINT_NAME)
    state = ck.get("state") or {}
    seen_ids: set[str] = set(str(x) for x in (state.get("seen_nb_ids") or []))
    items_by_id: dict[str, dict] = {
        str(i["nb_id"]): i for i in (state.get("listings") or []) if i.get("nb_id")
    }
    # Preserve enriched + inventory identities so we never drop paid history
    for row in state.get("enriched") or []:
        nb = str(row.get("nb_id") or "")
        if nb and nb not in items_by_id:
            items_by_id[nb] = row
            seen_ids.add(nb)
    try:
        from m3.research.nextbid_inventory import load_inventory

        for nb, row in load_inventory().items():
            seen_ids.add(str(nb))
            if str(nb) not in items_by_id:
                items_by_id[str(nb)] = {
                    "nb_id": nb,
                    "title": row.get("title"),
                    "solicitation_number": row.get("solicitation_number"),
                    "official_url": row.get("official_url"),
                    "nextbid_source": row.get("source"),
                    "from_inventory": True,
                }
    except Exception:
        pass

    baseline_unique = len(items_by_id)
    baseline_seen = len(seen_ids)

    cov = load_coverage()
    searches: dict[str, Any] = dict(cov.get("searches") or {})

    calls_used = 0
    rows_returned = 0
    unique_new = 0
    duplicates = 0
    errors: list[str] = []
    job_id = new_job_id("002m-discovery")
    job_calls = 0
    jobs_used = [job_id]
    stopped_reason: str | None = None

    # Initialize search trackers
    for plan in plan_list:
        sk = _search_key(plan["source"], plan["params"])
        if sk not in searches:
            searches[sk] = {
                "source": plan["source"],
                "query_parameters": dict(plan["params"]),
                "provider_total": None,
                "provider_total_pages": None,
                "pages_requested": 0,
                "records_returned": 0,
                "unique_records": 0,
                "duplicate_records": 0,
                "last_successful_page": 0,
                "remaining_unvisited_pages": None,
                "completion_status": "pending",
                "errors": [],
            }

    # Round-robin page acquisition until budget exhausted or all complete
    active = True
    while active and calls_used < max_calls:
        progressed = False
        for plan in plan_list:
            if calls_used >= max_calls:
                break
            sk = _search_key(plan["source"], plan["params"])
            meta = searches[sk]
            if meta.get("completion_status") in {
                "complete",
                "budget_exhausted",
                "error_stop",
                "aborted_excluded_source_flood",
            }:
                continue

            next_page = int(meta.get("last_successful_page") or 0) + 1
            total_pages = meta.get("provider_total_pages")
            if total_pages is not None and next_page > int(total_pages):
                meta["completion_status"] = "complete"
                meta["remaining_unvisited_pages"] = 0
                continue

            # Rotate job every 50 calls
            if job_calls >= 50:
                job_id = new_job_id("002m-discovery")
                jobs_used.append(job_id)
                job_calls = 0

            client = NextBidClient(job_id=job_id)
            params = {
                **plan["params"],
                "page": next_page,
                "page_size": page_size,
                "sort": "deadline",
            }
            try:
                res = client.search(params)
            except (NextBidBudgetExhausted, NextBidUnauthorized, NextBidQuotaError) as exc:
                stopped_reason = f"{type(exc).__name__}:{exc}"
                meta["completion_status"] = "budget_exhausted"
                meta["errors"] = list(meta.get("errors") or []) + [stopped_reason]
                errors.append(f"{sk}:{stopped_reason}")
                active = False
                break
            except Exception as exc:
                err = f"{type(exc).__name__}"
                meta["errors"] = list(meta.get("errors") or []) + [err]
                errors.append(f"{sk}:page{next_page}:{err}")
                meta["completion_status"] = "error_stop"
                continue

            calls_used += 1
            job_calls += 1
            progressed = True

            if not res.get("ok", True) and res.get("error"):
                meta["errors"] = list(meta.get("errors") or []) + [str(res.get("error"))]
                # Still count the call; do not advance page permanently on hard failure
                # but provider may have returned partial — stop this source
                meta["completion_status"] = "error_stop"
                _persist_page_state(
                    seen_ids=seen_ids,
                    items_by_id=items_by_id,
                    searches=searches,
                    cov=cov,
                    calls_used=calls_used,
                    rows_returned=rows_returned,
                    unique_new=unique_new,
                    duplicates=duplicates,
                )
                continue

            # Provider totals
            if meta.get("provider_total") is None:
                meta["provider_total"] = res.get("total") or res.get("total_count") or res.get("count")
            tp = res.get("total_pages")
            if tp is not None:
                meta["provider_total_pages"] = int(tp)

            page_rows = 0
            page_new = 0
            page_dup = 0
            batch_new_items: list[dict] = []
            for row in res.get("results") or []:
                if not isinstance(row, dict):
                    continue
                page_rows += 1
                src = str(row.get("source") or "").lower()
                if src in EXCLUDED_SOURCES:
                    meta["excluded_source_rows"] = int(meta.get("excluded_source_rows") or 0) + 1
                    # Abort this search if a page is dominated by excluded federal sources
                    continue
                nb_id = str(row.get("nb_id") or "")
                if not nb_id:
                    continue
                if nb_id in seen_ids:
                    duplicates += 1
                    page_dup += 1
                    meta["duplicate_records"] = int(meta.get("duplicate_records") or 0) + 1
                    continue
                seen_ids.add(nb_id)
                item = _normalize_listing(row)
                # 002M: persist ALL non-excluded listings (no title prefilter before storage)
                items_by_id[nb_id] = item
                batch_new_items.append(item)
                unique_new += 1
                page_new += 1
                meta["unique_records"] = int(meta.get("unique_records") or 0) + 1

            rows_returned += page_rows
            meta["pages_requested"] = int(meta.get("pages_requested") or 0) + 1
            meta["records_returned"] = int(meta.get("records_returned") or 0) + page_rows
            meta["last_successful_page"] = next_page
            excluded_page = int(meta.get("excluded_source_rows") or 0)
            # If this page contributed only excluded federal rows, stop the search
            if page_rows > 0 and page_new == 0 and page_dup == 0 and excluded_page > 0:
                # Check excluded grew by ~page_rows on this page via heuristic:
                meta["completion_status"] = "aborted_excluded_source_flood"
                meta["errors"] = list(meta.get("errors") or []) + [
                    "page_dominated_by_excluded_sources_sam_or_dibbs"
                ]
            elif meta.get("provider_total_pages") is not None:
                meta["remaining_unvisited_pages"] = max(
                    0, int(meta["provider_total_pages"]) - next_page
                )
                if next_page >= int(meta["provider_total_pages"]):
                    meta["completion_status"] = "complete"
                else:
                    meta["completion_status"] = "partial"
            else:
                # Unknown total — stop source if empty page
                if page_rows == 0:
                    meta["completion_status"] = "complete"
                    meta["remaining_unvisited_pages"] = 0
                else:
                    meta["completion_status"] = "partial"

            # DURABLE PERSIST BEFORE NEXT PAGE
            _persist_page_state(
                seen_ids=seen_ids,
                items_by_id=items_by_id,
                searches=searches,
                cov=cov,
                calls_used=calls_used,
                rows_returned=rows_returned,
                unique_new=unique_new,
                duplicates=duplicates,
                new_items=batch_new_items,
            )

            st = controller_status()
            eff = (st.get("budget_state") or {}).get("effective_available")
            if eff is not None and int(eff) <= 0:
                stopped_reason = "effective_available_0"
                active = False
                break

        if not progressed:
            # All sources complete or stalled
            break

    # Mark remaining incomplete as budget-stopped
    if calls_used >= max_calls or stopped_reason:
        for meta in searches.values():
            if meta.get("completion_status") == "partial":
                meta["completion_status"] = "budget_exhausted_partial"
            elif meta.get("completion_status") == "pending":
                meta["completion_status"] = "not_started_budget"

    cov["searches"] = searches
    prior_total = int((cov.get("totals") or {}).get("search_calls_build_total") or prior_calls or 0)
    # Prefer explicit prior_calls arg for build total
    build_total_calls = int(prior_calls) + calls_used
    cov["totals"] = {
        "search_calls": calls_used,
        "search_calls_build_total": build_total_calls,
        "rows_returned": rows_returned,
        "unique_new": unique_new,
        "duplicates": duplicates,
        "baseline_unique": baseline_unique,
        "baseline_seen": baseline_seen,
        "final_unique": len(items_by_id),
        "final_seen": len(seen_ids),
        "prior_calls": prior_calls,
    }
    cov["jobs"] = list(dict.fromkeys(list(cov.get("jobs") or []) + jobs_used))
    cov["stopped_reason"] = stopped_reason
    cov["finished_at"] = _utc()
    save_coverage(cov)

    # Final checkpoint + inventory + registry ingest
    ck_state = {
        "seen_nb_ids": sorted(seen_ids),
        "listings": list(items_by_id.values()),
        "enriched": (load_checkpoint(CHECKPOINT_NAME).get("state") or {}).get("enriched") or [],
        "completed_queries": sorted(
            set((load_checkpoint(CHECKPOINT_NAME).get("state") or {}).get("completed_queries") or [])
            | {f"002m:{k}" for k, v in searches.items() if v.get("pages_requested")}
        ),
        "build_002m": {
            "calls_used": calls_used,
            "unique_new": unique_new,
            "stopped_reason": stopped_reason,
        },
        "discovery_calls": calls_used,
        "duplicates_skipped": duplicates,
        "errors": errors[-50:],
    }
    # Preserve prior enriched
    prior_enriched = (state.get("enriched") or [])
    if prior_enriched and not ck_state["enriched"]:
        ck_state["enriched"] = prior_enriched
    save_checkpoint(ck_state, CHECKPOINT_NAME)
    inv = upsert_inventory(list(items_by_id.values()))
    registry = ingest_population(list(items_by_id.values()), actor="build_002m_discovery")

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    return {
        "ok": True,
        "started_at": started,
        "finished_at": _utc(),
        "elapsed_seconds": round(elapsed, 1),
        "search_calls": calls_used,
        "search_calls_build_total": int(prior_calls) + calls_used,
        "prior_calls": prior_calls,
        "max_calls": max_calls,
        "within_300": (int(prior_calls) + calls_used) <= 300,
        "rows_returned": rows_returned,
        "unique_new": unique_new,
        "duplicates": duplicates,
        "baseline_unique": baseline_unique,
        "final_unique": len(items_by_id),
        "final_seen": len(seen_ids),
        "jobs": jobs_used,
        "stopped_reason": stopped_reason,
        "searches": searches,
        "inventory": inv,
        "registry": registry,
        "errors": errors[-30:],
        "scope": BUILD_002M_SCOPE,
        "detail_calls": 0,
        "document_calls": 0,
    }


def _persist_page_state(
    *,
    seen_ids: set[str],
    items_by_id: dict[str, dict],
    searches: dict[str, Any],
    cov: dict[str, Any],
    calls_used: int,
    rows_returned: int,
    unique_new: int,
    duplicates: int,
    new_items: list[dict] | None = None,
) -> None:
    """Crash-safe: write checkpoint + coverage + inventory before next page."""
    prior = load_checkpoint(CHECKPOINT_NAME).get("state") or {}
    ck_state = {
        "seen_nb_ids": sorted(seen_ids),
        "listings": list(items_by_id.values()),
        "enriched": prior.get("enriched") or [],
        "completed_queries": prior.get("completed_queries") or [],
        "build_002m_progress": {
            "calls_used": calls_used,
            "unique_new": unique_new,
            "at": _utc(),
        },
        "duplicates_skipped": duplicates,
        "errors": prior.get("errors") or [],
    }
    save_checkpoint(ck_state, CHECKPOINT_NAME)
    cov["searches"] = searches
    cov["totals"] = {
        "search_calls": calls_used,
        "rows_returned": rows_returned,
        "unique_new": unique_new,
        "duplicates": duplicates,
        "final_unique": len(items_by_id),
        "final_seen": len(seen_ids),
    }
    save_coverage(cov)
    if new_items:
        upsert_inventory(new_items)
