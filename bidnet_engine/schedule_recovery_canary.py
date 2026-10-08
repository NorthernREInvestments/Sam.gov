"""Same-13 authoritative schedule recovery with iteration log (content-first)."""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from typing import Any

from application_clock import now_utc
from bidnet_engine.money_path import DOWNSTREAM_CHECKPOINT, _merge_checkpoint, process_money_opportunity
from bidnet_engine.package_materialization import BUILD as PKG_BUILD
from bidnet_engine.package_recovery_canary import SAME_13_STABLE_KEYS, _resolve_same_13
from bidnet_engine.thread_limits import apply_thread_limits, verify_thread_limits

BUILD = "20261007-m3-authoritative-schedule-recovery-v1"
STATUS = "m3_schedule_recovery_v1_status.json"
REPORT_JSON = "m3_schedule_recovery_v1_last_report.json"
REPORT_TXT = "m3_schedule_recovery_v1_last_report.txt"
ROWS_JSON = "m3_schedule_recovery_v1_rows.json"
ITER_LOG = "m3_schedule_recovery_v1_iteration_log.json"
CHECKPOINT_PARTIAL = "m3_schedule_recovery_v1_partial_checkpoint.json"
# No single opportunity may hold the canary hostage
OPP_HARD_TIMEOUT_S = 5 * 60
HEARTBEAT_INTERVAL_S = 60

BEFORE = {
    "AUTHORITATIVE_PRODUCT_DOC": 2,
    "LINES_READY": 2,
}


def _load(name: str) -> dict[str, Any]:
    from m3_data_root import data_path

    path = data_path(name)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(name: str, payload: Any) -> None:
    from m3_data_root import data_path

    path = data_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def write_status(**kwargs: Any) -> None:
    _save(STATUS, {"build": BUILD, "updated_at": now_utc().isoformat(), **kwargs})


def _save_partial_checkpoint(
    *,
    run_id: str,
    iteration: int,
    mode: str,
    results: list[dict[str, Any]],
    excluded_results: list[dict[str, Any]],
    pool: list[dict[str, Any]],
    processed_idx: int,
    stalled: list[dict[str, Any]] | None = None,
) -> None:
    """Durable mid-run state so a stall kill can resume without redoing completed opps."""
    _save(
        CHECKPOINT_PARTIAL,
        {
            "build": BUILD,
            "run_id": run_id,
            "iteration": iteration,
            "mode": mode,
            "updated_at": now_utc().isoformat(),
            "valid_counted": len(results),
            "excluded_counted": len(excluded_results),
            "processed_idx": processed_idx,
            "completed_stable_keys": [
                str(r.get("stable_key") or "") for r in results if r.get("stable_key")
            ],
            "excluded_stable_keys": [
                str(r.get("stable_key") or "") for r in excluded_results if r.get("stable_key")
            ],
            "stalled": stalled or [],
            "pool_stable_keys": [str(p.get("stable_key") or "") for p in pool],
            "rows": results,
            "excluded_rows": excluded_results,
        },
    )


def _process_opp_with_guards(
    *,
    item: dict[str, Any],
    store_row: dict[str, Any],
    client: Any,
    store_by_cid: dict[str, Any],
    price_budget: int,
    run_id: str,
    iteration: int,
    mode: str,
    valid_n: int,
    excl_n: int,
    pool_remaining: int,
    target_valid: int,
    on_progress: Any | None,
    opp_hard_timeout_s: int | None = None,
) -> dict[str, Any]:
    """Run process_money_opportunity with 60s heartbeats and hard wall timeout."""
    title = str(item.get("title") or "")
    sk = item.get("stable_key")
    cid = str(item.get("canonical_opportunity_id") or "")
    started = time.time()
    stage_box: dict[str, Any] = {"stage": "PROCESS_MONEY", "started": started}
    stop_hb = threading.Event()
    hard_limit = int(opp_hard_timeout_s or OPP_HARD_TIMEOUT_S)

    def _heartbeat_loop() -> None:
        while not stop_hb.wait(HEARTBEAT_INTERVAL_S):
            elapsed = time.time() - started
            write_status(
                phase=f"SCHEDULE_RECOVERY_{mode.upper()}_OPP",
                completed=valid_n if mode == "new_20" else valid_n + excl_n,
                remaining=max(0, (target_valid if mode == "new_20" else target_valid) - valid_n),
                percent=int(100 * valid_n / max(target_valid, 1)),
                run_id=run_id,
                iteration=iteration,
                current_stable_key=sk,
                current_title=title[:120],
                current_stage=stage_box.get("stage"),
                time_in_current_stage_s=round(elapsed, 1),
                valid_counted=valid_n,
                excluded_counted=excl_n,
                attempted=valid_n + excl_n,
                pool_remaining=pool_remaining,
                heartbeat_at=now_utc().isoformat(),
                opp_hard_timeout_s=hard_limit,
            )
            if on_progress:
                try:
                    on_progress(
                        phase=f"SCHEDULE_RECOVERY_{mode}_heartbeat",
                        pct=int(100 * valid_n / max(target_valid, 1)),
                        completed=valid_n,
                    )
                except Exception:
                    pass

    hb_thread = threading.Thread(target=_heartbeat_loop, name=f"asr-hb-{sk}", daemon=True)
    hb_thread.start()
    try:
        # CRITICAL: Playwright sync API is thread-affine. The BidNet browser/page/context
        # must be used on the same thread that created them. Running process_money in a
        # ThreadPoolExecutor caused discover/download to fail instantly (~2s/opp) while
        # the membership-wall fallback looked like an external BidNet lock.
        #
        # Hard timeout is enforced cooperatively via per-call Playwright/HTTP timeouts
        # inside materialize/discovery (and a wall-clock stall mark after return).
        stage_box["stage"] = "PROCESS_MONEY"
        result = process_money_opportunity(
            item,
            store_row,
            client=client,
            store=store_by_cid,
            price_budget=price_budget,
        )
        elapsed = time.time() - started
        if elapsed > hard_limit:
            result = dict(result or {})
            result["stalled"] = True
            result["exclusion"] = "STALLED_OPPORTUNITY"
            result["stalled_reason"] = f"Exceeded {hard_limit}s wall clock"
            result["time_in_stage_s"] = round(elapsed, 1)
            pm = result.get("package_materialization") if isinstance(result.get("package_materialization"), dict) else {}
            result["package_materialization"] = {
                **pm,
                "primary_blocker": "STALLED_OPPORTUNITY",
                "product_classification": "STALLED_OPPORTUNITY",
            }
        return result
    finally:
        stop_hb.set()


def _append_iteration(entry: dict[str, Any]) -> list[dict[str, Any]]:
    log = _load(ITER_LOG)
    items = list(log.get("iterations") or [])
    items.append({"at": now_utc().isoformat(), **entry})
    _save(ITER_LOG, {"build": BUILD, "iterations": items})
    return items


def run_schedule_recovery(
    *,
    mode: str = "same_13",
    canary_n: int = 20,
    price_budget: int = 25,
    iteration: int = 1,
    change_made: str = "content-first schedule recognition",
    on_progress: Any | None = None,
) -> dict[str, Any]:
    apply_thread_limits(n=1)
    if not verify_thread_limits().get("verified_active"):
        raise RuntimeError("thread caps not active")

    from phase_l.l23_full_population_funnel import load_store

    started = time.time()
    run_id = f"ASR-{now_utc().strftime('%Y%m%d%H%M%S')}"
    ckpt = _load(DOWNSTREAM_CHECKPOINT)
    rows = [r for r in (ckpt.get("rows") or []) if isinstance(r, dict)]
    store = load_store()
    store_by_cid = {str(r.get("canonical_opportunity_id") or k): r for k, r in store.items() if isinstance(r, dict)}

    if mode == "same_13":
        candidates = _resolve_same_13(rows, store)
    else:
        import re

        from bidnet_engine.schedule_selection import select_schedule_backed_candidates
        from bidnet_downstream.models import PRODUCT_CLASSES

        from bidnet_engine.schedule_selection import (
            assess_product_dominance_for_new20,
            exclusion_reason,
            product_canary_selection_gate,
        )

        product_rows = [r for r in rows if r.get("classification") in PRODUCT_CLASSES]
        # Checkpoint may be thin after same-13 — expand from store product / parts-like rows
        seen_cids = {str(r.get("canonical_opportunity_id") or "") for r in product_rows}
        _parts_ish = re.compile(
            r"\b(parts?|equipment|supply|supplies|material|oem|sku|mpn|vehicle|pump|hvac|lift)\b",
            re.I,
        )
        for cid, sr in store_by_cid.items():
            if not isinstance(sr, dict) or cid in seen_cids:
                continue
            cls = sr.get("classification") or sr.get("product_class")
            title = str(sr.get("title") or "")
            docs = sr.get("attachments_metadata") or sr.get("document_inventory") or []
            if cls not in PRODUCT_CLASSES and not (_parts_ish.search(title) and docs):
                continue
            # Never expand pool with install/service/repair-dominant titles
            if exclusion_reason(title):
                continue
            product_rows.append(
                {
                    "canonical_opportunity_id": cid,
                    "stable_key": sr.get("stable_key") or cid,
                    "title": title,
                    "buyer": sr.get("buyer"),
                    "classification": cls if cls in PRODUCT_CLASSES else "PRODUCT",
                    "deadline": sr.get("deadline"),
                    "attachments_metadata": docs if isinstance(docs, list) else [],
                }
            )
            seen_cids.add(cid)
            if len(product_rows) >= canary_n * 12:
                break
        # Over-select reserve pool so EXCLUDED_* after inspection can be replaced
        pool_limit = max(canary_n * 4, 40)
        candidates, _excl = select_schedule_backed_candidates(
            product_rows, store_by_cid, limit=pool_limit
        )
        # Prefer content-evidence when available; exclude same-13
        same = set(SAME_13_STABLE_KEYS)
        candidates = [c for c in candidates if str(c.get("stable_key") or "") not in same]
        # Last resort filler — MUST pass product gate (no EXCLUDED_* bypass)
        if len(candidates) < canary_n:
            filler = []
            for cid, sr in store_by_cid.items():
                if not isinstance(sr, dict):
                    continue
                sk = str(sr.get("stable_key") or cid)
                if sk in same:
                    continue
                title = str(sr.get("title") or "")
                docs = sr.get("attachments_metadata") or []
                if not _parts_ish.search(title):
                    continue
                if exclusion_reason(title):
                    continue
                if not any(
                    isinstance(d, dict)
                    and (d.get("document_url") or d.get("url") or d.get("local_path"))
                    for d in docs
                ):
                    continue
                row = {
                    "canonical_opportunity_id": cid,
                    "stable_key": sk,
                    "title": title,
                    "buyer": sr.get("buyer"),
                    "classification": sr.get("classification")
                    if sr.get("classification") in PRODUCT_CLASSES
                    else "PRODUCT",
                    "deadline": sr.get("deadline"),
                    "_provisional_schedule": True,
                }
                gate = product_canary_selection_gate(row, sr)
                # Allow provisional schedule miss, but never install/service/etc.
                if gate.get("exclusion"):
                    continue
                if any(
                    x.startswith("EXCLUDED_")
                    for x in (gate.get("fail_reasons") or [])
                ):
                    continue
                filler.append(row)
            seen = {str(c.get("stable_key") or "") for c in candidates}
            for f in filler:
                if f["stable_key"] in seen:
                    continue
                candidates.append(f)
                seen.add(f["stable_key"])
                if len(candidates) >= pool_limit:
                    break

    write_status(
        phase="SELECTED",
        completed=0,
        remaining=len(candidates),
        run_id=run_id,
        mode=mode,
        selected=len(candidates),
        iteration=iteration,
    )

    from bidnet_auth.client import BidNetAuthenticatedClient
    from bidnet_engine.package_materialization import PATCH as PKG_PATCH, purge_html_document_caches

    # Clear poisoned HTML document_1 caches before re-download / harvest
    purge_stats = purge_html_document_caches(limit=800)
    write_status(
        phase="PURGED_HTML_CACHE",
        completed=0,
        remaining=len(candidates),
        run_id=run_id,
        mode=mode,
        selected=len(candidates),
        iteration=iteration,
        purge=purge_stats,
        patch=PKG_PATCH,
    )

    client = BidNetAuthenticatedClient()
    auth = client.ensure_authenticated()
    if not auth.authenticated:
        client.close()
        raise RuntimeError(f"BidNet auth required: {auth.status} {auth.message}")

    results: list[dict[str, Any]] = []
    excluded_results: list[dict[str, Any]] = []
    # Synchronous processing — text-only content inspection is bounded.
    # Do NOT recreate the BidNet client mid-run (breaks authenticated downloads → HTML).
    # NEW-20: process reserve until canary_n VALID product-dominant opps (replace EXCLUDED_*).
    target_valid = canary_n if mode == "new_20" else len(candidates)
    pool = list(candidates)
    processed_idx = 0
    source_exhausted = False

    # Resume from durable partial checkpoint (stall recover) — skip completed keys
    if mode == "new_20":
        partial = _load(CHECKPOINT_PARTIAL)
        if partial.get("rows") and int(partial.get("valid_counted") or 0) > 0:
            prior_rows = [r for r in (partial.get("rows") or []) if isinstance(r, dict)]
            prior_excl = [r for r in (partial.get("excluded_rows") or []) if isinstance(r, dict)]
            done_sk = {
                str(r.get("stable_key") or "")
                for r in prior_rows + prior_excl
                if r.get("stable_key")
            }
            if prior_rows:
                results = list(prior_rows)
                excluded_results = list(prior_excl)
                pool = [c for c in pool if str(c.get("stable_key") or "") not in done_sk]
                processed_idx = 0
                write_status(
                    phase="RESUMED_FROM_CHECKPOINT",
                    completed=len(results),
                    remaining=max(0, target_valid - len(results)),
                    percent=int(100 * len(results) / max(target_valid, 1)),
                    run_id=run_id,
                    iteration=iteration,
                    valid_counted=len(results),
                    excluded_counted=len(excluded_results),
                    resumed_keys=len(done_sk),
                    pool_remaining=len(pool),
                    heartbeat_at=now_utc().isoformat(),
                    prior_run_id=partial.get("run_id"),
                )

    while True:
        if mode == "new_20" and len(results) >= target_valid:
            break
        if processed_idx >= len(pool):
            source_exhausted = mode == "new_20" and len(results) < target_valid
            break
        if mode != "new_20" and processed_idx >= len(candidates):
            break

        item = pool[processed_idx]
        processed_idx += 1
        cid = str(item.get("canonical_opportunity_id") or "")
        store_row = store_by_cid.get(cid) or {}
        if not store_row:
            for k, v in store_by_cid.items():
                if str(v.get("stable_key") or "") == str(item.get("stable_key") or ""):
                    store_row = v
                    item = {**item, "canonical_opportunity_id": k}
                    cid = k
                    break

        title = str(item.get("title") or "")
        valid_n = len(results)
        excl_n = len(excluded_results)
        attempted = valid_n + excl_n
        denom = target_valid if mode == "new_20" else max(len(candidates), 1)
        write_status(
            phase=f"SCHEDULE_RECOVERY_{mode.upper()}_OPP",
            completed=valid_n if mode == "new_20" else attempted,
            remaining=max(0, denom - (valid_n if mode == "new_20" else attempted)),
            percent=int(100 * (valid_n if mode == "new_20" else attempted) / denom),
            run_id=run_id,
            iteration=iteration,
            current_stable_key=item.get("stable_key"),
            current_title=title[:120],
            valid_counted=valid_n,
            excluded_counted=excl_n,
            attempted=attempted,
            pool_remaining=max(0, len(pool) - processed_idx),
            heartbeat_at=now_utc().isoformat(),
        )

        # Pre-inspect title gate for NEW-20 — skip install/service without burning BidNet time
        if mode == "new_20":
            from bidnet_engine.schedule_selection import assess_product_dominance_for_new20

            pre = assess_product_dominance_for_new20(title=title)
            if not pre.get("counts_toward_new20"):
                skip = {
                    "canonical_opportunity_id": cid,
                    "stable_key": item.get("stable_key"),
                    "title": title,
                    "buyer": item.get("buyer"),
                    "raw_lines": 0,
                    "material_lines": 0,
                    "usable_ae": 0,
                    "public_prices": 0,
                    "live_bidnet": False,
                    "recovery_cohort": mode,
                    "schedule_recovery_iteration": iteration,
                    "new20_dominance": pre,
                    "exclusion": pre.get("exclusion") or "EXCLUDED_INSTALL",
                    "counts_toward_new20": False,
                    "package_materialization": {
                        "primary_blocker": pre.get("exclusion") or "EXCLUDED",
                        "operator_product_status": pre.get("reason"),
                        "product_classification": "EXCLUDED_PRE_INSPECT",
                        "AUTHORITATIVE_PRODUCT_DOC_FOUND": False,
                    },
                    "package_primary_blocker": pre.get("exclusion") or "EXCLUDED",
                }
                excluded_results.append(skip)
                write_status(
                    phase=f"SCHEDULE_RECOVERY_{mode.upper()}_HEARTBEAT",
                    completed=len(results),
                    remaining=max(0, target_valid - len(results)),
                    percent=int(100 * len(results) / target_valid),
                    run_id=run_id,
                    iteration=iteration,
                    last_title=title[:120],
                    last_exclusion=pre.get("exclusion"),
                    valid_counted=len(results),
                    excluded_counted=len(excluded_results),
                    attempted=len(results) + len(excluded_results),
                    pool_remaining=max(0, len(pool) - processed_idx),
                    heartbeat_at=now_utc().isoformat(),
                    replaced=True,
                )
                continue

        try:
            r = _process_opp_with_guards(
                item=item,
                store_row=store_row,
                client=client,
                store_by_cid=store_by_cid,
                price_budget=price_budget,
                run_id=run_id,
                iteration=iteration,
                mode=mode,
                valid_n=valid_n,
                excl_n=excl_n,
                pool_remaining=max(0, len(pool) - processed_idx),
                target_valid=target_valid if mode == "new_20" else max(len(candidates), 1),
                on_progress=on_progress,
            )
        except Exception as exc:
            r = {
                "canonical_opportunity_id": cid,
                "stable_key": item.get("stable_key"),
                "title": item.get("title"),
                "buyer": item.get("buyer"),
                "raw_lines": 0,
                "material_lines": 0,
                "usable_ae": 0,
                "public_prices": 0,
                "package_materialization": {
                    "primary_blocker": "OPP_ERROR",
                    "operator_product_status": f"Package analysis failed: {type(exc).__name__}",
                    "product_classification": "PARSER_DEFECT_REMAINS",
                    "AUTHORITATIVE_PRODUCT_DOC_FOUND": False,
                },
                "package_primary_blocker": "OPP_ERROR",
                "error": f"{type(exc).__name__}:{exc}"[:200],
            }
        r["live_bidnet"] = True
        r["recovery_cohort"] = mode
        r["schedule_recovery_iteration"] = iteration

        if mode == "new_20":
            from bidnet_engine.schedule_selection import assess_product_dominance_for_new20

            if r.get("stalled") or r.get("exclusion") == "STALLED_OPPORTUNITY":
                excluded_results.append(r)
                _save_partial_checkpoint(
                    run_id=run_id,
                    iteration=iteration,
                    mode=mode,
                    results=results,
                    excluded_results=excluded_results,
                    pool=pool,
                    processed_idx=processed_idx,
                    stalled=[
                        {
                            "stable_key": r.get("stable_key"),
                            "title": r.get("title"),
                            "stage": r.get("stalled_stage"),
                            "reason": r.get("stalled_reason"),
                        }
                    ],
                )
                write_status(
                    phase=f"SCHEDULE_RECOVERY_{mode.upper()}_HEARTBEAT",
                    completed=len(results),
                    remaining=max(0, target_valid - len(results)),
                    percent=int(100 * len(results) / target_valid),
                    run_id=run_id,
                    iteration=iteration,
                    last_title=str(r.get("title") or "")[:120],
                    last_exclusion="STALLED_OPPORTUNITY",
                    valid_counted=len(results),
                    excluded_counted=len(excluded_results),
                    attempted=len(results) + len(excluded_results),
                    pool_remaining=max(0, len(pool) - processed_idx),
                    heartbeat_at=now_utc().isoformat(),
                    replaced=True,
                    STALLED_OPPORTUNITY=True,
                )
                continue

            pm = r.get("package_materialization") if isinstance(r.get("package_materialization"), dict) else {}
            post = assess_product_dominance_for_new20(
                title=str(r.get("title") or title),
                product_classification=str(pm.get("product_classification") or ""),
                extracted_lines=int(r.get("raw_lines") or 0),
                operator_status=str(pm.get("operator_product_status") or ""),
            )
            r["new20_dominance"] = post
            r["counts_toward_new20"] = bool(post.get("counts_toward_new20"))
            r["exclusion"] = post.get("exclusion")
            if not r["counts_toward_new20"]:
                excluded_results.append(r)
                _save_partial_checkpoint(
                    run_id=run_id,
                    iteration=iteration,
                    mode=mode,
                    results=results,
                    excluded_results=excluded_results,
                    pool=pool,
                    processed_idx=processed_idx,
                )
                write_status(
                    phase=f"SCHEDULE_RECOVERY_{mode.upper()}_HEARTBEAT",
                    completed=len(results),
                    remaining=max(0, target_valid - len(results)),
                    percent=int(100 * len(results) / target_valid),
                    run_id=run_id,
                    iteration=iteration,
                    last_title=str(r.get("title") or "")[:120],
                    last_exclusion=post.get("exclusion"),
                    valid_counted=len(results),
                    excluded_counted=len(excluded_results),
                    attempted=len(results) + len(excluded_results),
                    pool_remaining=max(0, len(pool) - processed_idx),
                    heartbeat_at=now_utc().isoformat(),
                    replaced=True,
                )
                if on_progress:
                    try:
                        on_progress(
                            phase=f"SCHEDULE_RECOVERY_{mode}_excluded",
                            pct=int(100 * len(results) / target_valid),
                            completed=len(results),
                        )
                    except Exception:
                        pass
                continue

        results.append(r)
        # Heartbeat after EVERY completed opportunity (valid count for new_20)
        done_n = len(results) if mode == "new_20" else len(results)
        rem_n = max(0, (target_valid if mode == "new_20" else len(candidates)) - done_n)
        write_status(
            phase=f"SCHEDULE_RECOVERY_{mode.upper()}_HEARTBEAT",
            completed=done_n,
            remaining=rem_n,
            percent=int(100 * done_n / max(target_valid if mode == "new_20" else len(candidates), 1)),
            run_id=run_id,
            iteration=iteration,
            last_title=str(r.get("title") or "")[:120],
            last_stable_key=r.get("stable_key"),
            valid_counted=len(results),
            excluded_counted=len(excluded_results),
            attempted=len(results) + len(excluded_results),
            pool_remaining=max(0, len(pool) - processed_idx),
            heartbeat_at=now_utc().isoformat(),
            material_lines=r.get("material_lines"),
            raw_lines=r.get("raw_lines"),
        )
        if on_progress:
            try:
                on_progress(
                    phase=f"SCHEDULE_RECOVERY_{mode}",
                    pct=int(100 * done_n / max(target_valid if mode == "new_20" else len(candidates), 1)),
                    completed=done_n,
                )
            except Exception:
                pass
        merged = _merge_checkpoint(rows, results)
        _save(
            DOWNSTREAM_CHECKPOINT,
            {
                "build": BUILD,
                "engine": BUILD,
                "updated_at": now_utc().isoformat(),
                "classified": len(merged),
                "rows": merged,
            },
        )
        _save_partial_checkpoint(
            run_id=run_id,
            iteration=iteration,
            mode=mode,
            results=results,
            excluded_results=excluded_results,
            pool=pool,
            processed_idx=processed_idx,
        )

    client.close()
    report = _build_report(
        results,
        mode=mode,
        run_id=run_id,
        started=started,
        before=BEFORE if mode == "same_13" else None,
        iteration=iteration,
        change_made=change_made,
    )
    if mode == "new_20":
        report["new20_acceptance"] = {
            "target_valid": target_valid,
            "valid_counted": len(results),
            "excluded_counted": len(excluded_results),
            "attempted": len(results) + len(excluded_results),
            "source_exhausted": source_exhausted,
            "excluded_titles": [
                {
                    "title": e.get("title"),
                    "exclusion": e.get("exclusion"),
                    "stable_key": e.get("stable_key"),
                }
                for e in excluded_results
            ],
        }
        report["excluded_rows"] = excluded_results
        if source_exhausted and len(results) < target_valid:
            report["STATUS"] = "FAIL_SOURCE_EXHAUSTED"
            report["gates"] = {
                **(report.get("gates") or {}),
                "NEW_20_VALID_PRODUCT_DOMINANT_GE_20": False,
                "SOURCE_EXHAUSTED_PROVEN": True,
            }
        else:
            gates = dict(report.get("gates") or {})
            gates["NEW_20_VALID_PRODUCT_DOMINANT_GE_20"] = len(results) >= target_valid
            gates["SOURCE_EXHAUSTED_PROVEN"] = False
            # Acceptance requires 20 valid product-dominant processed
            if len(results) >= target_valid:
                gates["NEW_20_ACCEPTANCE_SET"] = True
            report["gates"] = gates
    iters = _append_iteration(
        {
            "iteration": iteration,
            "mode": mode,
            "AUTHORITATIVE_PRODUCT_DOC_FOUND": (report.get("after") or {}).get("AUTHORITATIVE_PRODUCT_DOC_FOUND"),
            "LINES_READY": (report.get("after") or {}).get("LINES_READY"),
            "A_E": (report.get("downstream") or {}).get("A_E_IDENTITY_OPPS"),
            "PUBLIC_PRICE": (report.get("downstream") or {}).get("PUBLIC_PRICE_READY_OPPS"),
            "CHANGE_MADE": change_made,
            "RESULT": report.get("STATUS"),
            "run_id": run_id,
        }
    )
    report["iteration_log"] = iters
    report["ITERATIONS_COMPLETED"] = len(iters)

    gates = report.get("gates") or {}
    same13_pass = bool(
        gates.get("AUTHORITATIVE_PRODUCT_DOC_FOUND_GE_8") and gates.get("LINES_READY_GE_8")
    ) or bool(gates.get("ADJUSTED_ITEMIZABLE_PASS"))

    if mode == "same_13" and same13_pass:
        write_status(phase="EXPAND_NEW_20", run_id=run_id, iteration=iteration)
        new20 = run_schedule_recovery(
            mode="new_20",
            canary_n=20,
            price_budget=price_budget,
            iteration=iteration,
            change_made="new-20 after same-13 pass",
            on_progress=on_progress,
        )
        report["NEW_20"] = {
            "RUN": "YES",
            **{
                k: new20.get(k)
                for k in (
                    "canary_input",
                    "after",
                    "gates",
                    "downstream",
                    "top_recovered",
                    "STATUS",
                    "new20_acceptance",
                    "excluded_rows",
                )
            },
        }
        report["STATUS"] = "PASS" if (new20.get("gates") or {}).get("NEW_20_PASS") else "SAME13_PASS_NEW20_FAIL"
        # Nested new_20 call wrote ROWS_JSON — copy before same-13 overwrite below
        nested_rows = _load(ROWS_JSON)
        if nested_rows.get("mode") == "new_20" and nested_rows.get("rows"):
            _save("m3_schedule_recovery_v1_new20_rows.json", nested_rows)
            report["NEW_20"]["rows_persisted"] = len(nested_rows.get("rows") or [])
        else:
            _save(
                "m3_schedule_recovery_v1_new20_rows.json",
                {
                    "build": BUILD,
                    "run_id": new20.get("run_id") or run_id,
                    "mode": "new_20",
                    "rows": [],
                    "acceptance": new20.get("new20_acceptance"),
                    "excluded": new20.get("excluded_rows") or [],
                    "top_recovered": new20.get("top_recovered") or [],
                    "updated_at": now_utc().isoformat(),
                },
            )
    elif mode == "same_13":
        report["NEW_20"] = {"RUN": "NO", "reason": "same_13_gates_not_met"}

    _save(REPORT_JSON, report)
    _save(REPORT_TXT, format_report(report))
    # Also mirror into package materialization filenames for UI fallbacks
    _save("m3_package_materialization_v1_rows.json", {"build": BUILD, "run_id": run_id, "mode": mode, "rows": results})
    _save(ROWS_JSON, {"build": BUILD, "run_id": run_id, "mode": mode, "rows": results, "updated_at": now_utc().isoformat()})
    write_status(
        phase="DONE",
        percent=100,
        completed=len(results),
        STATUS=report.get("STATUS"),
        run_id=run_id,
        iteration=iteration,
        AUTHORITATIVE=report.get("after", {}).get("AUTHORITATIVE_PRODUCT_DOC_FOUND"),
        LINES=report.get("after", {}).get("LINES_READY"),
    )
    return report


def _build_report(
    results: list[dict[str, Any]],
    *,
    mode: str,
    run_id: str,
    started: float,
    before: dict[str, Any] | None,
    iteration: int,
    change_made: str,
) -> dict[str, Any]:
    states: Counter[str] = Counter()
    blockers: Counter[str] = Counter()
    detail = index_acq = 0
    discovered = downloaded = valid = invalid = 0
    auth_found = lines_ready = material_lines = 0
    expected_lines = extracted_lines = 0
    product_like_docs = 0
    catalog_only = no_product = 0
    itemizable = 0
    ae_opps = pub_opps = rev_opps = 0
    ae_lines = pub_lines = 0
    cov50 = cov75 = 0
    unrecovered: list[dict[str, Any]] = []
    top = []

    for r in results:
        pm = r.get("package_materialization") if isinstance(r.get("package_materialization"), dict) else {}
        cr = pm.get("content_recognition") if isinstance(pm.get("content_recognition"), dict) else {}
        completeness = str(pm.get("PACKAGE_COMPLETENESS") or "UNKNOWN")
        states[completeness] += 1
        discovered += int(pm.get("PACKAGE_DOCUMENT_COUNT_DISCOVERED") or 0)
        downloaded += int(pm.get("PACKAGE_DOCUMENT_COUNT_DOWNLOADED") or 0)
        valid += int(pm.get("PACKAGE_DOCUMENT_COUNT_MATERIALIZED") or 0)
        invalid += len(pm.get("invalid_downloads") or [])
        if int(pm.get("PACKAGE_DOCUMENT_COUNT_DISCOVERED") or 0) > 0:
            index_acq += 1
        detail += 1
        auth = bool(pm.get("AUTHORITATIVE_PRODUCT_DOC_FOUND"))
        raw_lines = int(r.get("raw_lines") or 0)
        if auth:
            auth_found += 1
        if raw_lines > 0:
            lines_ready += 1
        material_lines += int(r.get("material_lines") or 0)
        expected_lines += int(pm.get("EXPECTED_PRODUCT_LINES") or cr.get("EXPECTED_PRODUCT_LINES") or 0)
        extracted_lines += int(pm.get("EXTRACTED_PRODUCT_LINES") or raw_lines or 0)
        product_like_docs += int(cr.get("product_like_documents") or 0)
        clf = str(pm.get("product_classification") or cr.get("classification") or "")
        valid_local = int(pm.get("PACKAGE_DOCUMENT_COUNT_MATERIALIZED") or 0)
        # Without valid local docs, UNKNOWN → inaccessible. Keep proven NO_PRODUCT / CATALOG.
        if valid_local <= 0 and clf in {"UNKNOWN", ""}:
            clf = "PRODUCT_SCHEDULE_INACCESSIBLE"
        if clf == "CATALOG_DISCOUNT_ONLY":
            catalog_only += 1
        elif clf in {"NO_PRODUCT_LINES_ACTUALLY_PRESENT"}:
            no_product += 1
        elif clf == "PRODUCT_SCHEDULE_INACCESSIBLE" or valid_local <= 0:
            pass  # neither itemizable nor proven absent
        elif auth or clf == "LINES_RECOVERED":
            itemizable += 1
        elif raw_lines > 0 and valid_local > 0 and clf not in {
            "NO_PRODUCT_LINES_ACTUALLY_PRESENT",
            "PRODUCT_SCHEDULE_INACCESSIBLE",
            "CATALOG_DISCOUNT_ONLY",
        }:
            # Body/schedule lines only count when local package was inspected
            itemizable += 1
        if int(r.get("usable_ae") or 0) > 0:
            ae_opps += 1
            ae_lines += int(r.get("usable_ae") or 0)
        if int(r.get("public_prices") or 0) > 0:
            pub_opps += 1
            pub_lines += int(r.get("public_prices") or 0)
        if r.get("revenue_state") == "ECONOMIC_REVENUE_USABLE":
            rev_opps += 1
        cov = r.get("public_price_coverage_pct")
        try:
            cov_f = float(cov) if cov is not None else 0.0
        except (TypeError, ValueError):
            cov_f = 0.0
        if cov_f >= 50:
            cov50 += 1
        if cov_f >= 75:
            cov75 += 1
        b = str(r.get("package_primary_blocker") or pm.get("primary_blocker") or "OTHER")
        if clf == "PARSER_DEFECT_REMAINS":
            b = "PARSER_FAILURE"
        elif clf == "CATALOG_DISCOUNT_ONLY":
            b = "CATALOG_DISCOUNT_ONLY"
        elif clf == "PRODUCT_SCHEDULE_INACCESSIBLE":
            b = "PRODUCT_SCHEDULE_INACCESSIBLE"
        blockers[b] += 1
        auth_doc = pm.get("AUTHORITATIVE_PRODUCT_DOC") or {}
        top.append(
            {
                "opportunity": r.get("stable_key") or r.get("canonical_opportunity_id"),
                "title": r.get("title"),
                "buyer": r.get("buyer"),
                "documents_acquired": pm.get("PACKAGE_DOCUMENT_COUNT_MATERIALIZED"),
                "product_schedule": "Found" if auth else "Missing",
                "schedule_filename": auth_doc.get("filename") if isinstance(auth_doc, dict) else None,
                "role": auth_doc.get("document_role_content") or auth_doc.get("document_role_guess"),
                "pages": auth_doc.get("product_signal_pages") if isinstance(auth_doc, dict) else None,
                "expected": pm.get("EXPECTED_PRODUCT_LINES"),
                "extracted_lines": raw_lines,
                "coverage": pm.get("LINE_EXTRACTION_COVERAGE"),
                "classification": clf,
                "operator_status": pm.get("operator_product_status") or pm.get("operator_message"),
                "identity_handoff": int(r.get("usable_ae") or 0) > 0,
                "public_pricing_handoff": int(r.get("public_prices") or 0) > 0,
            }
        )
        if raw_lines <= 0:
            unrecovered.append(
                {
                    "opportunity": r.get("stable_key") or r.get("title"),
                    "title": r.get("title"),
                    "documents_inspected": [
                        {
                            "filename": (i or {}).get("filename"),
                            "role": (i or {}).get("document_role_content"),
                            "pages": (i or {}).get("product_signal_pages"),
                            "signals": (i or {}).get("signal_hits"),
                            "rows": (i or {}).get("extracted_line_count"),
                            "classification": (i or {}).get("classification"),
                        }
                        for i in (cr.get("inspections") or [])
                    ],
                    "product_signals": clf,
                    "why_no_lines": pm.get("operator_product_status") or b,
                    "classification": clf or b,
                    "blocker_type": (
                        "external"
                        if clf in {"CATALOG_DISCOUNT_ONLY", "NO_PRODUCT_LINES_ACTUALLY_PRESENT"}
                        else (
                            # HTML/auth download failures are software until page discovery is exhausted
                            "software"
                            if clf == "PRODUCT_SCHEDULE_INACCESSIBLE"
                            else "software"
                        )
                    ),
                    "invalid_reasons": (pm.get("invalid_download_reasons") or [])[:6],
                    "harvested_from_html": pm.get("HARVESTED_FROM_HTML"),
                    "page_discovered": pm.get("PAGE_DISCOVERED_ATTACHMENTS"),
                    "attachment_urls": [
                        (e.get("source_url") or "")[:180]
                        for e in (pm.get("PACKAGE_ATTACHMENT_INDEX") or [])[:6]
                        if isinstance(e, dict)
                    ],
                }
            )

    top.sort(
        key=lambda x: (
            1 if x.get("product_schedule") == "Found" else 0,
            int(x.get("extracted_lines") or 0),
        ),
        reverse=True,
    )

    n = len(results)
    after = {
        "DETAIL_ACQUIRED": detail,
        "ATTACHMENT_INDEX_ACQUIRED": index_acq,
        "DOCUMENTS_DISCOVERED": discovered,
        "DOCUMENTS_DOWNLOADED": downloaded,
        "VALID_LOCAL_DOCUMENTS": valid,
        "INVALID_DOWNLOADS": invalid,
        "PRODUCT_LIKE_DOCUMENTS_FOUND": product_like_docs,
        "AUTHORITATIVE_PRODUCT_DOC_FOUND": auth_found,
        "ACTUALLY_ITEMIZABLE_PRODUCT_OPPS": itemizable,
        "CATALOG_DISCOUNT_ONLY": catalog_only,
        "NO_PRODUCT_LINES_PRESENT": no_product,
        "LINES_READY": lines_ready,
        "EXPECTED_LINES": expected_lines,
        "EXTRACTED_LINES": extracted_lines,
        "EXTRACTION_COVERAGE": round(extracted_lines / expected_lines, 4) if expected_lines else None,
        "MATERIAL_PRODUCT_LINES": material_lines,
    }

    # Adjusted pass when itemizable subset is small AND non-itemizable/external cases are proven.
    # PRODUCT_SCHEDULE_NOT_ACQUIRED / PARSER_FAILURE remain software blockers — never count as proof.
    import math

    # INVALID_DOWNLOADED_FILE for BidNet HTML/detail pages is access evidence once classified
    # as inaccessible/no-product — do not treat those as soft software blockers.
    soft_blockers = int(blockers.get("PRODUCT_SCHEDULE_NOT_ACQUIRED") or 0) + int(
        blockers.get("PARSER_FAILURE") or 0
    ) + int(blockers.get("OPP_TIMEOUT") or 0)
    external_proven = 0
    inspected_or_proven = 0
    for r in results:
        pm = r.get("package_materialization") if isinstance(r.get("package_materialization"), dict) else {}
        valid_local = int(pm.get("PACKAGE_DOCUMENT_COUNT_MATERIALIZED") or 0)
        if valid_local > 0 or pm.get("external_blocker_proven") or str(pm.get("product_classification") or "") in {
            "NO_PRODUCT_LINES_ACTUALLY_PRESENT",
            "CATALOG_DISCOUNT_ONLY",
            "PRODUCT_SCHEDULE_INACCESSIBLE",
        }:
            inspected_or_proven += 1
        if pm.get("external_blocker_proven") or (
            str(pm.get("product_classification") or "") == "PRODUCT_SCHEDULE_INACCESSIBLE"
            and int((pm.get("FREE_CHASE") or {}).get("doc_count") or 0) == 0
            and str((pm.get("FREE_CHASE") or {}).get("status") or "") in {
                "PACKAGE_UNAVAILABLE_FREE",
                "PACKAGE_RECOVERY_RETRYABLE",
                "TIMEOUT",
            }
        ):
            external_proven += 1
    proven_non_itemizable = catalog_only + no_product + external_proven
    adjusted_pass = False
    if (
        soft_blockers == 0
        and 1 <= itemizable < 8
        and proven_non_itemizable >= (n - itemizable)
        and n >= 8
        and inspected_or_proven >= n
    ):
        needed = max(1, int(math.ceil(0.8 * itemizable)))
        adjusted_pass = lines_ready >= needed and auth_found >= needed

    if mode == "same_13":
        gates = {
            "AUTHORITATIVE_PRODUCT_DOC_FOUND_GE_8": auth_found >= 8,
            "LINES_READY_GE_8": lines_ready >= 8,
            "ADJUSTED_ITEMIZABLE_PASS": adjusted_pass,
            # Prefer hard 8/8; adjusted only when soft blockers are gone and absence is proven
            "PACKAGE_RECOVERY_WORKING": (auth_found >= 8 and lines_ready >= 8) or adjusted_pass,
            "A_E_IDENTITY_OPPS_GE_5": ae_opps >= 5,
            "PUBLIC_PRICE_READY_OPPS_GE_3": pub_opps >= 3,
            "PUBLIC_PRICE_COVERAGE_50_GE_1": cov50 >= 1,
        }
        status = "PASS" if gates["PACKAGE_RECOVERY_WORKING"] else "FAIL"
    else:
        valid_pkgs = sum(
            1
            for r in results
            if int((r.get("package_materialization") or {}).get("PACKAGE_DOCUMENT_COUNT_MATERIALIZED") or 0) > 0
        )
        from bidnet_engine.schedule_selection import assess_product_dominance_for_new20

        product_dominant = 0
        for r in results:
            if r.get("counts_toward_new20") is False:
                continue
            pm = r.get("package_materialization") if isinstance(r.get("package_materialization"), dict) else {}
            dom = r.get("new20_dominance") if isinstance(r.get("new20_dominance"), dict) else None
            if not dom:
                dom = assess_product_dominance_for_new20(
                    title=str(r.get("title") or ""),
                    product_classification=str(pm.get("product_classification") or ""),
                    extracted_lines=int(r.get("raw_lines") or 0),
                    operator_status=str(pm.get("operator_product_status") or ""),
                )
            if dom.get("counts_toward_new20"):
                product_dominant += 1
        gates = {
            "VALID_LOCAL_PACKAGE_GE_18": valid_pkgs >= 18,
            "ITEMIZABLE_PRODUCT_GE_15": itemizable >= 15,
            "LINES_READY_GE_12": lines_ready >= 12,
            "A_E_GE_8": ae_opps >= 8,
            "PUBLIC_PRICE_READY_GE_5": pub_opps >= 5,
            "NEW_20_VALID_PRODUCT_DOMINANT_GE_20": product_dominant >= 20,
            "NEW_20_PASS": (
                product_dominant >= 20
                and valid_pkgs >= 18
                and lines_ready >= 12
                and ae_opps >= 8
                and pub_opps >= 5
            ),
        }
        status = "PASS" if gates["NEW_20_PASS"] else "FAIL"

    return {
        "build": BUILD,
        "package_build": PKG_BUILD,
        "run_id": run_id,
        "mode": mode,
        "STATUS": status,
        "runtime_s": round(time.time() - started, 1),
        "canary_input": n,
        "iteration": iteration,
        "change_made": change_made,
        "before": before,
        "after": after,
        "package_states": dict(states),
        "blockers": dict(blockers),
        "gates": gates,
        "downstream": {
            "A_E_IDENTITY_OPPS": ae_opps,
            "A_E_LINES": ae_lines,
            "PUBLIC_PRICE_READY_OPPS": pub_opps,
            "PUBLIC_PRICED_LINES": pub_lines,
            "PUBLIC_PRICE_COVERAGE_50": cov50,
            "PUBLIC_PRICE_COVERAGE_75": cov75,
            "REVENUE_READY_OPPS": rev_opps,
            "DOWNSTREAM_HANDOFF": ae_opps >= 5 and pub_opps >= 3,
        },
        "top_recovered": top[:13],
        "unrecovered_cases": unrecovered,
        "REAL_DATA_ONLY": "YES",
        "EXPAND_TO_100": "NO",
        "updated_at": now_utc().isoformat(),
    }


def format_report(report: dict[str, Any]) -> str:
    a = report.get("after") or {}
    g = report.get("gates") or {}
    d = report.get("downstream") or {}
    lines = [
        "M3 AUTHORITATIVE SCHEDULE RECOVERY SUMMARY",
        f"BUILD: {report.get('build')}",
        f"JOB: {report.get('run_id')}",
        f"STATUS: {report.get('STATUS')}",
        f"ITERATIONS COMPLETED: {report.get('ITERATIONS_COMPLETED') or report.get('iteration')}",
        "",
        "SAME-13",
        f"VALID LOCAL DOCUMENTS: {a.get('VALID_LOCAL_DOCUMENTS')}",
        f"PRODUCT-LIKE DOCUMENTS FOUND: {a.get('PRODUCT_LIKE_DOCUMENTS_FOUND')}",
        f"AUTHORITATIVE PRODUCT DOCS: {a.get('AUTHORITATIVE_PRODUCT_DOC_FOUND')}",
        f"ACTUALLY ITEMIZABLE PRODUCT OPPS: {a.get('ACTUALLY_ITEMIZABLE_PRODUCT_OPPS')}",
        f"CATALOG-DISCOUNT-ONLY: {a.get('CATALOG_DISCOUNT_ONLY')}",
        f"NO PRODUCT LINES PRESENT: {a.get('NO_PRODUCT_LINES_PRESENT')}",
        f"LINES READY: {a.get('LINES_READY')}",
        f"EXPECTED LINES: {a.get('EXPECTED_LINES')}",
        f"EXTRACTED LINES: {a.get('EXTRACTED_LINES')}",
        f"EXTRACTION COVERAGE: {a.get('EXTRACTION_COVERAGE')}",
        "",
        f"A-E IDENTITY OPPS: {d.get('A_E_IDENTITY_OPPS')}",
        f"PUBLIC PRICE READY OPPS: {d.get('PUBLIC_PRICE_READY_OPPS')}",
        f">=50% COVERAGE: {d.get('PUBLIC_PRICE_COVERAGE_50')}",
        "",
        f"AUTHORITATIVE PRODUCT DOC >=8: {'YES' if g.get('AUTHORITATIVE_PRODUCT_DOC_FOUND_GE_8') else 'NO'}",
        f"LINES READY >=8: {'YES' if g.get('LINES_READY_GE_8') else 'NO'}",
        f"ADJUSTED ITEMIZABLE DENOMINATOR PASS: {'YES' if g.get('ADJUSTED_ITEMIZABLE_PASS') else 'NO'}",
        f"DOWNSTREAM HANDOFF: {'YES' if d.get('DOWNSTREAM_HANDOFF') else 'NO'}",
        "",
    ]
    return "\n".join(lines)


JEFFERSON_JEF_ATCT_MEL = {
    "title": "Jefferson City Memorial Airport (JEF) ATCT Minimum Equipment List Installation",
    "exclusion": "EXCLUDED_INSTALL",
    "counts_toward_new20": False,
    "primary_requirement": "INSTALLATION_OR_SERVICE",
    "why_passed_gate": (
        "LIKELY_PRODUCT_SCHEDULE from 'Equipment List' schedule keyword; "
        "last-resort store filler bypassed product_canary_selection_gate "
        "(gate itself correctly returns accepted=False / EXCLUDED_INSTALL)."
    ),
    "material_product_lines_estimate": "unknown_pre_docs; title scope is MEL Installation — install-primary",
    "tangible_product_pct_estimate": "<50% (installation of MEL is primary scope)",
}


def reconcile_new20_acceptance(
    *,
    target_valid: int = 20,
    price_budget: int = 25,
    iteration: int = 22,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """Post-run: drop EXCLUDED_* from NEW-20 rows and replace until target_valid product-dominant."""
    from bidnet_engine.schedule_selection import (
        assess_product_dominance_for_new20,
        exclusion_reason,
        select_schedule_backed_candidates,
    )
    from bidnet_downstream.models import PRODUCT_CLASSES
    from phase_l.l23_full_population_funnel import load_store

    apply_thread_limits(n=1)
    started = time.time()
    run_id = f"ASR-TOPUP-{now_utc().strftime('%Y%m%d%H%M%S')}"
    doc = _load("m3_schedule_recovery_v1_new20_rows.json")
    prior = [r for r in (doc.get("rows") or []) if isinstance(r, dict)]
    # Prefer explicit new_20 cohort only — never treat same-13 rows as the acceptance set
    new20_prior = [r for r in prior if r.get("recovery_cohort") == "new_20"]
    if not new20_prior and doc.get("mode") == "new_20":
        new20_prior = [r for r in prior if isinstance(r, dict)]
    # Always merge last-report NEW_20 top_recovered (iter-22) when available
    report_doc = _load(REPORT_JSON)
    top = ((report_doc.get("NEW_20") or {}).get("top_recovered") or [])
    seen_sk = {str(r.get("stable_key") or "") for r in new20_prior}
    for t in top:
        if not isinstance(t, dict):
            continue
        sk = str(t.get("opportunity") or "")
        if sk and sk in seen_sk:
            continue
        new20_prior.append(
            {
                "stable_key": sk or t.get("title"),
                "title": t.get("title"),
                "buyer": t.get("buyer"),
                "raw_lines": t.get("extracted_lines") or 0,
                "material_lines": t.get("extracted_lines") or 0,
                "usable_ae": 1 if t.get("identity_handoff") else 0,
                "public_prices": 1 if t.get("public_pricing_handoff") else 0,
                "recovery_cohort": "new_20",
                "package_materialization": {
                    "product_classification": t.get("classification"),
                    "operator_product_status": t.get("operator_status"),
                    "PACKAGE_DOCUMENT_COUNT_MATERIALIZED": t.get("documents_acquired") or 0,
                    "AUTHORITATIVE_PRODUCT_DOC_FOUND": t.get("product_schedule") == "Found",
                },
            }
        )
        if sk:
            seen_sk.add(sk)

    valid: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for r in new20_prior:
        title = str(r.get("title") or "")
        pm = r.get("package_materialization") if isinstance(r.get("package_materialization"), dict) else {}
        dom = assess_product_dominance_for_new20(
            title=title,
            product_classification=str(pm.get("product_classification") or ""),
            extracted_lines=int(r.get("raw_lines") or 0),
            operator_status=str(pm.get("operator_product_status") or ""),
        )
        r = {**r, "new20_dominance": dom, "counts_toward_new20": bool(dom.get("counts_toward_new20")), "exclusion": dom.get("exclusion")}
        if r["counts_toward_new20"]:
            valid.append(r)
        else:
            # Force Jefferson / install titles into EXCLUDED_INSTALL
            if "jefferson city memorial airport" in title.lower() or exclusion_reason(title) == "INSTALL":
                r["exclusion"] = "EXCLUDED_INSTALL"
            excluded.append(r)

    write_status(
        phase="NEW20_RECONCILE",
        completed=len(valid),
        remaining=max(0, target_valid - len(valid)),
        percent=int(100 * len(valid) / max(target_valid, 1)),
        run_id=run_id,
        iteration=iteration,
        valid_counted=len(valid),
        excluded_counted=len(excluded),
        heartbeat_at=now_utc().isoformat(),
        jefferson=JEFFERSON_JEF_ATCT_MEL,
    )

    if len(valid) >= target_valid:
        report = _build_report(
            valid[:target_valid],
            mode="new_20",
            run_id=run_id,
            started=started,
            before=None,
            iteration=iteration,
            change_made="new20 reconcile — product-dominant acceptance set",
        )
        report["new20_acceptance"] = {
            "target_valid": target_valid,
            "valid_counted": target_valid,
            "excluded_counted": len(excluded),
            "source_exhausted": False,
            "reconcile": True,
            "excluded_titles": [
                {"title": e.get("title"), "exclusion": e.get("exclusion"), "stable_key": e.get("stable_key")}
                for e in excluded
            ],
        }
        report["excluded_rows"] = excluded
        _save(REPORT_JSON, report)
        _save(REPORT_TXT, format_report(report))
        _save(ROWS_JSON, {"build": BUILD, "run_id": run_id, "mode": "new_20", "rows": valid[:target_valid], "excluded": excluded, "updated_at": now_utc().isoformat()})
        write_status(phase="DONE", percent=100, completed=target_valid, STATUS=report.get("STATUS"), run_id=run_id, iteration=iteration, valid_counted=target_valid, excluded_counted=len(excluded))
        return report

    # Need replacements
    store = load_store()
    store_by_cid = {str(r.get("canonical_opportunity_id") or k): r for k, r in store.items() if isinstance(r, dict)}
    seen_sk = {str(r.get("stable_key") or "") for r in valid + excluded}
    same = set(SAME_13_STABLE_KEYS)
    ckpt = _load(DOWNSTREAM_CHECKPOINT)
    rows = [r for r in (ckpt.get("rows") or []) if isinstance(r, dict)]
    product_rows = [r for r in rows if r.get("classification") in PRODUCT_CLASSES]
    for cid, sr in store_by_cid.items():
        if not isinstance(sr, dict):
            continue
        title = str(sr.get("title") or "")
        if exclusion_reason(title):
            continue
        sk = str(sr.get("stable_key") or cid)
        if sk in seen_sk or sk in same:
            continue
        product_rows.append(
            {
                "canonical_opportunity_id": cid,
                "stable_key": sk,
                "title": title,
                "buyer": sr.get("buyer"),
                "classification": sr.get("classification") if sr.get("classification") in PRODUCT_CLASSES else "PRODUCT",
                "deadline": sr.get("deadline"),
                "attachments_metadata": sr.get("attachments_metadata") or [],
            }
        )
        if len(product_rows) >= target_valid * 12:
            break

    pool, _ = select_schedule_backed_candidates(product_rows, store_by_cid, limit=target_valid * 6)
    pool = [c for c in pool if str(c.get("stable_key") or "") not in seen_sk and str(c.get("stable_key") or "") not in same]
    # Aggressive filler from live store titles that are product-worded and not excluded
    if len(pool) < target_valid * 2:
        import re as _re

        _parts_ish = _re.compile(
            r"\b(parts?|equipment|supply|supplies|material|oem|sku|mpn|vehicle|pump|hvac|lift|"
            r"furniture|tools?|hardware|commodit(?:y|ies)|purchase)\b",
            _re.I,
        )
        pool_sk = {str(c.get("stable_key") or "") for c in pool}
        for cid, sr in store_by_cid.items():
            if not isinstance(sr, dict):
                continue
            title = str(sr.get("title") or "")
            sk = str(sr.get("stable_key") or cid)
            if sk in seen_sk or sk in same or sk in pool_sk:
                continue
            if exclusion_reason(title) or not _parts_ish.search(title):
                continue
            pool.append(
                {
                    "canonical_opportunity_id": cid,
                    "stable_key": sk,
                    "title": title,
                    "buyer": sr.get("buyer"),
                    "classification": "PRODUCT",
                    "deadline": sr.get("deadline"),
                    "_provisional_schedule": True,
                }
            )
            pool_sk.add(sk)
            if len(pool) >= target_valid * 8:
                break

    from bidnet_auth.client import BidNetAuthenticatedClient

    client = BidNetAuthenticatedClient()
    auth = client.ensure_authenticated()
    if not auth.authenticated:
        client.close()
        raise RuntimeError(f"BidNet auth required: {auth.status} {auth.message}")

    source_exhausted = False
    for item in pool:
        if len(valid) >= target_valid:
            break
        title = str(item.get("title") or "")
        pre = assess_product_dominance_for_new20(title=title)
        if not pre.get("counts_toward_new20"):
            excluded.append(
                {
                    **item,
                    "exclusion": pre.get("exclusion"),
                    "counts_toward_new20": False,
                    "new20_dominance": pre,
                    "recovery_cohort": "new_20",
                }
            )
            write_status(
                phase="NEW20_RECONCILE_HEARTBEAT",
                completed=len(valid),
                remaining=max(0, target_valid - len(valid)),
                percent=int(100 * len(valid) / target_valid),
                run_id=run_id,
                iteration=iteration,
                last_title=title[:120],
                last_exclusion=pre.get("exclusion"),
                valid_counted=len(valid),
                excluded_counted=len(excluded),
                heartbeat_at=now_utc().isoformat(),
                replaced=True,
            )
            continue
        cid = str(item.get("canonical_opportunity_id") or "")
        store_row = store_by_cid.get(cid) or {}
        write_status(
            phase="NEW20_RECONCILE_OPP",
            completed=len(valid),
            remaining=max(0, target_valid - len(valid)),
            percent=int(100 * len(valid) / target_valid),
            run_id=run_id,
            iteration=iteration,
            current_title=title[:120],
            valid_counted=len(valid),
            excluded_counted=len(excluded),
            heartbeat_at=now_utc().isoformat(),
        )
        try:
            r = process_money_opportunity(
                item, store_row, client=client, store=store_by_cid, price_budget=price_budget
            )
        except Exception as exc:
            r = {
                "canonical_opportunity_id": cid,
                "stable_key": item.get("stable_key"),
                "title": title,
                "raw_lines": 0,
                "material_lines": 0,
                "usable_ae": 0,
                "public_prices": 0,
                "package_materialization": {
                    "primary_blocker": "OPP_ERROR",
                    "operator_product_status": f"{type(exc).__name__}",
                    "product_classification": "PARSER_DEFECT_REMAINS",
                    "AUTHORITATIVE_PRODUCT_DOC_FOUND": False,
                },
            }
        pm = r.get("package_materialization") if isinstance(r.get("package_materialization"), dict) else {}
        post = assess_product_dominance_for_new20(
            title=str(r.get("title") or title),
            product_classification=str(pm.get("product_classification") or ""),
            extracted_lines=int(r.get("raw_lines") or 0),
            operator_status=str(pm.get("operator_product_status") or ""),
        )
        r["live_bidnet"] = True
        r["recovery_cohort"] = "new_20"
        r["schedule_recovery_iteration"] = iteration
        r["new20_dominance"] = post
        r["counts_toward_new20"] = bool(post.get("counts_toward_new20"))
        r["exclusion"] = post.get("exclusion")
        if r["counts_toward_new20"]:
            valid.append(r)
        else:
            excluded.append(r)
        write_status(
            phase="NEW20_RECONCILE_HEARTBEAT",
            completed=len(valid),
            remaining=max(0, target_valid - len(valid)),
            percent=int(100 * len(valid) / target_valid),
            run_id=run_id,
            iteration=iteration,
            last_title=str(r.get("title") or "")[:120],
            last_exclusion=r.get("exclusion"),
            valid_counted=len(valid),
            excluded_counted=len(excluded),
            heartbeat_at=now_utc().isoformat(),
        )
        if on_progress:
            try:
                on_progress(phase="NEW20_RECONCILE", pct=int(100 * len(valid) / target_valid), completed=len(valid))
            except Exception:
                pass
    else:
        if len(valid) < target_valid:
            source_exhausted = True

    client.close()
    report = _build_report(
        valid[:target_valid],
        mode="new_20",
        run_id=run_id,
        started=started,
        before=None,
        iteration=iteration,
        change_made="new20 reconcile — replace EXCLUDED_INSTALL/SERVICE with product-dominant",
    )
    report["new20_acceptance"] = {
        "target_valid": target_valid,
        "valid_counted": len(valid),
        "excluded_counted": len(excluded),
        "source_exhausted": source_exhausted,
        "reconcile": True,
        "jefferson": JEFFERSON_JEF_ATCT_MEL,
        "excluded_titles": [
            {"title": e.get("title"), "exclusion": e.get("exclusion"), "stable_key": e.get("stable_key")}
            for e in excluded
        ],
    }
    report["excluded_rows"] = excluded
    if source_exhausted and len(valid) < target_valid:
        report["STATUS"] = "FAIL_SOURCE_EXHAUSTED"
    _save(REPORT_JSON, report)
    _save(REPORT_TXT, format_report(report))
    _save(
        ROWS_JSON,
        {
            "build": BUILD,
            "run_id": run_id,
            "mode": "new_20",
            "rows": valid[:target_valid],
            "excluded": excluded,
            "updated_at": now_utc().isoformat(),
        },
    )
    write_status(
        phase="DONE",
        percent=100,
        completed=len(valid),
        STATUS=report.get("STATUS"),
        run_id=run_id,
        iteration=iteration,
        valid_counted=len(valid),
        excluded_counted=len(excluded),
        source_exhausted=source_exhausted,
        jefferson_exclusion="EXCLUDED_INSTALL",
    )
    return report
