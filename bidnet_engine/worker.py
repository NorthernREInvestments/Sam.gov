"""Bounded parallel BidNet workers with separate logical vs browser concurrency."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from typing import Any, Callable

from bidnet_downstream.deep import (
    _eligibility_from_text,
    _line_identity_revenue,
    _map_detail,
    _map_package,
)
from bidnet_engine.browser_pool import BrowserPool
from bidnet_engine.cache import CacheMetrics
from bidnet_engine.resource_budget import recommended_browser_workers, should_throttle, snapshot as resource_snapshot
from bidnet_engine.thread_limits import apply_thread_limits
from bidnet_engine.timing import StageTimer
from bidnet_full_production.process import process_bidnet_opportunity

apply_thread_limits(n=1)

_results_lock = threading.Lock()
_CHECKPOINT_EVERY_S = float(os.environ.get("BIDNET_CHECKPOINT_EVERY_S") or 120)


def process_one(
    item: dict[str, Any],
    store_row: dict[str, Any],
    *,
    pool: BrowserPool,
    timer: StageTimer | None = None,
    cache: CacheMetrics | None = None,
    store: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Process one opportunity. Browser access is leased from the shared pool."""
    timer = timer or StageTimer()
    cache = cache or CacheMetrics()
    started = time.perf_counter()
    cid = str(item.get("canonical_opportunity_id") or "")
    if item.get("deep_complete") and str(item.get("detail_state") or "").startswith("DETAIL_"):
        cache.hit(kind="detail")
        return {
            **item,
            "skipped_cache": True,
            "timing_total_s": 0.0,
            "error": None,
            "auth_failure": False,
        }

    meta = {
        "opportunity_id": cid,
        "canonical_opportunity_id": cid,
        "title": item.get("title") or store_row.get("title"),
        "buyer": item.get("buyer") or store_row.get("buyer"),
        "authoritative_url": item.get("authoritative_url") or store_row.get("authoritative_url"),
        "deadline": item.get("deadline") or store_row.get("deadline"),
    }
    if not meta["authoritative_url"]:
        ref = store_row.get("row_ref") if isinstance(store_row.get("row_ref"), dict) else {}
        meta["authoritative_url"] = ref.get("detail_url") or ref.get("source_url")

    try:
        with timer.measure("auth"):
            # Lease a reused authenticated Chromium client (not a new process each time).
            with pool.client() as client:
                with timer.measure("detail_fetch"):
                    out = process_bidnet_opportunity(
                        meta,
                        deepcopy(store_row),
                        client=client,
                        store=store or {},
                        skip_live_detail=False,
                    )
        with timer.measure("detail_parse"):
            detail = _map_detail(out.get("detail_status"))
        with timer.measure("package_lookup"):
            package = _map_package(out.get("package_state"))
        with timer.measure("eligibility"):
            can = out.get("canonical") if isinstance(out.get("canonical"), dict) else {}
            blob = " ".join(str(x or "") for x in (item.get("title"), can.get("description"), package))
            eligibility = _eligibility_from_text(blob, package)
        with timer.measure("lines"):
            metrics = _line_identity_revenue(cid, {**store_row, **(can or {})})
        result = {
            **item,
            "detail_state": detail,
            "package_state": package,
            "eligibility_state": eligibility,
            "deep_complete": True,
            "official_portal_identified": bool(out.get("official_portal_identified")),
            "portal_route": out.get("portal_route"),
            "raw_lines": metrics["raw_lines"],
            "material_lines": metrics["material_lines"],
            "product_lines": metrics["product_lines"],
            "service_install_lines": metrics["service_install_lines"],
            "usable_ae": metrics["usable_ae"],
            "identity_grades": metrics["identity_grades"],
            "material_coverage": metrics["material_coverage"],
            "revenue_state": metrics["revenue_state"],
            "acquisition_state": metrics["acquisition_state"],
            "quote_packets": metrics["quote_packets"],
            "quote_required": metrics["quote_required"],
            "basket_state": metrics["basket_state"],
            "economics_state": metrics["economics_state"],
            "P0": metrics["P0"],
            "P1": metrics["P1"],
            "ambiguous_lines": metrics["ambiguous_lines"],
            "skipped_cache": False,
            "error": None,
            "auth_failure": False,
            "timing_total_s": round(time.perf_counter() - started, 4),
        }
        cache.miss()
        return result
    except Exception as exc:
        msg = str(exc)
        auth_failure = "AUTH" in msg.upper() or "SESSION" in msg.upper()
        return {
            **item,
            "detail_state": "DETAIL_RETRYABLE",
            "package_state": "PACKAGE_RETRYABLE",
            "eligibility_state": "ELIGIBILITY_UNKNOWN",
            "deep_complete": False,
            "error": type(exc).__name__,
            "auth_failure": auth_failure,
            "retry_class": "RETRYABLE_SESSION" if auth_failure else "RETRYABLE_NETWORK",
            "skipped_cache": False,
            "timing_total_s": round(time.perf_counter() - started, 4),
        }


def run_pool(
    items: list[dict[str, Any]],
    store_by_cid: dict[str, dict[str, Any]],
    *,
    workers: int | None = None,
    browser_workers: int | None = None,
    on_done: Callable[[dict[str, Any]], None] | None = None,
    on_checkpoint: Callable[[list[dict[str, Any]]], None] | None = None,
    checkpoint_every: int | None = None,
) -> dict[str, Any]:
    """Process items with logical worker threads and a bounded browser pool."""
    apply_thread_limits(n=1)
    timer = StageTimer()
    cache = CacheMetrics()
    results: list[dict[str, Any]] = []
    errors = 0
    auth_failures = 0
    started = time.perf_counter()
    logical = max(1, int(workers or os.environ.get("BIDNET_LOGICAL_WORKERS") or 5))
    browsers = max(1, int(browser_workers or os.environ.get("BIDNET_BROWSER_WORKERS") or 2))
    browsers = recommended_browser_workers(min(browsers, logical))
    ck_every = max(1, int(checkpoint_every or os.environ.get("BIDNET_CHECKPOINT_EVERY") or 10))
    pool = BrowserPool(size=browsers)
    done_since_ck = 0
    last_ck_at = time.perf_counter()
    throttle_events = 0
    paused_intake = False

    def _job(item: dict[str, Any]) -> dict[str, Any]:
        cid = str(item.get("canonical_opportunity_id") or "")
        return process_one(
            item,
            store_by_cid.get(cid) or {},
            pool=pool,
            timer=timer,
            cache=cache,
            store=store_by_cid,
        )

    try:
        # Submit in waves so MAX_PENDING_TASKS / resource pressure can pause intake.
        pending_items = list(items)
        with ThreadPoolExecutor(max_workers=logical, thread_name_prefix="bn-log") as executor:
            futs: dict[Any, dict[str, Any]] = {}

            def _submit_more() -> None:
                nonlocal paused_intake, throttle_events
                while pending_items and len(futs) < logical:
                    throttle, reason = should_throttle(pending_tasks=len(futs) + len(pending_items))
                    if throttle:
                        paused_intake = True
                        throttle_events += 1
                        if on_checkpoint and results:
                            on_checkpoint(list(results))
                        # Brief cool-down before next intake decision.
                        time.sleep(1.0)
                        if should_throttle(pending_tasks=len(futs))[0]:
                            break
                    item = pending_items.pop(0)
                    futs[executor.submit(_job, item)] = item
                    paused_intake = False

            _submit_more()
            while futs or pending_items:
                if not futs:
                    _submit_more()
                    if not futs:
                        if pending_items:
                            time.sleep(2.0)
                            continue
                        break
                for fut in as_completed(list(futs.keys()), timeout=None):
                    futs.pop(fut, None)
                    r = fut.result()
                    should_ck = False
                    snap: list[dict[str, Any]] | None = None
                    with _results_lock:
                        results.append(r)
                        done_since_ck += 1
                        now = time.perf_counter()
                        if done_since_ck >= ck_every or (now - last_ck_at) >= _CHECKPOINT_EVERY_S:
                            done_since_ck = 0
                            last_ck_at = now
                            should_ck = True
                            snap = list(results)
                    if r.get("error"):
                        errors += 1
                    if r.get("auth_failure"):
                        auth_failures += 1
                    if on_done:
                        on_done(r)
                    if should_ck and on_checkpoint and snap is not None:
                        on_checkpoint(snap)
                    _submit_more()
                    break
        if on_checkpoint and results:
            on_checkpoint(list(results))
    finally:
        pool.close()

    elapsed = max(time.perf_counter() - started, 0.001)
    complete = sum(1 for r in results if r.get("deep_complete"))
    return {
        "workers": logical,
        "logical_workers": logical,
        "browser_workers": browsers,
        "browser_stats": pool.stats,
        "resource": resource_snapshot(),
        "throttle_events": throttle_events,
        "paused_intake": paused_intake,
        "attempted": len(items),
        "complete": complete,
        "errors": errors,
        "auth_failures": auth_failures,
        "runtime_s": round(elapsed, 2),
        "throughput_per_min": round(60.0 * complete / elapsed, 2),
        "timing": timer.summary(),
        "cache": cache.to_dict(),
        "results": results,
    }
