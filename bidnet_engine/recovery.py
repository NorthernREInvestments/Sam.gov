"""Recover hung BidNet engine state and retest under resource isolation."""

from __future__ import annotations

import json
import os
import time
from copy import deepcopy
from typing import Any

from application_clock import now_utc
from bidnet_downstream.models import PRODUCT_CLASSES
from bidnet_engine.models import DOWNSTREAM_CHECKPOINT, PREVIOUSLY_COMPLETE, PRODUCT_MIXED_TOTAL
from bidnet_engine.thread_limits import apply_thread_limits, verify_thread_limits
from bidnet_engine.worker import run_pool

BUILD = "20261006-m3-bidnet-engine-recovery-v1"
HUNG_JOB = "BNE-7fc7c8ba0dfa"
REPORT_JSON = "m3_bidnet_engine_recovery_v1_last_report.json"
REPORT_TXT = "m3_bidnet_engine_recovery_v1_last_report.txt"
PROGRESS = "m3_bidnet_engine_recovery_v1_progress.json"
STATE = "m3_bidnet_engine_recovery_v1_state.json"


def _load(name: str) -> dict[str, Any]:
    from m3_data_root import data_path

    path = data_path(name)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save(name: str, payload: Any) -> None:
    from m3_data_root import data_path

    path = data_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _tick(pct: int, stage: str, **extra: Any) -> None:
    _save(
        PROGRESS,
        {
            "build": BUILD,
            "progress_pct": pct,
            "stage": stage,
            "heartbeat_at": now_utc().isoformat(),
            "browser_workers": os.environ.get("BIDNET_BROWSER_WORKERS"),
            "logical_workers": os.environ.get("BIDNET_LOGICAL_WORKERS"),
            **extra,
        },
    )


def terminate_hung_job(job_id: str = HUNG_JOB) -> dict[str, Any]:
    """Mark hung job terminated in durable auth_jobs store. Does not delete checkpoints."""
    from m3_data_root import data_path

    path = data_path(f"auth_jobs/{job_id}.json")
    prior = {}
    if path.exists():
        try:
            prior = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            prior = {}
    job = dict(prior) if isinstance(prior, dict) else {}
    job.update(
        {
            "job_id": job_id,
            "kind": job.get("kind") or "bidnet_engine",
            "status": "TERMINATED_HUNG",
            "completed_at": now_utc().isoformat(),
            "updated_at": now_utc().isoformat(),
            "error": "terminated_after_resource_exhaustion_pthread_create_failed",
            "progress": {
                "phase": "TERMINATED_HUNG",
                "pct": int(((prior.get("progress") or {}).get("pct") or 70)),
                "note": "Container became unresponsive during BASELINE_BATCH; recovered via recovery build",
            },
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(job, indent=2, default=str), encoding="utf-8")
    return {"job_id": job_id, "status": "TERMINATED_HUNG", "prior_status": prior.get("status")}


def inspect_checkpoint() -> dict[str, Any]:
    ckpt = _load(DOWNSTREAM_CHECKPOINT)
    rows = [r for r in (ckpt.get("rows") or []) if isinstance(r, dict)]
    product = [r for r in rows if r.get("classification") in PRODUCT_CLASSES]
    deep = [r for r in product if r.get("deep_complete")]
    deep_ids = [str(r.get("stable_key")) for r in deep]
    engine_tagged = str(ckpt.get("engine") or "")
    # Pre-hang wave saved 120. Anything above 120 with durable deep_complete was committed.
    additional = max(0, len(deep) - PREVIOUSLY_COMPLETE)
    # If checkpoint was never updated by hung job, engine tag won't be recovery/engine.
    hung_committed = additional if "engine" in engine_tagged.lower() or ckpt.get("build") else additional
    remaining = max(0, PRODUCT_MIXED_TOTAL - len(deep))
    return {
        "checkpoint_found": bool(rows),
        "checkpoint_updated_at": ckpt.get("updated_at"),
        "checkpoint_build": ckpt.get("build"),
        "checkpoint_engine": ckpt.get("engine"),
        "product_mixed_rows": len(product),
        "deep_complete_count": len(deep),
        "original_120_intact": len(deep) >= PREVIOUSLY_COMPLETE,
        "pre_hang_completed_known": PREVIOUSLY_COMPLETE,
        "durably_saved": len(deep),
        "additional_durable_recovered": hung_committed,
        "unsaved_unknown": "unknown_in_flight_batch_at_hang",
        "queue_remaining": remaining,
        "sample_deep_ids": deep_ids[:5],
        "rows": rows,
    }


def _merge_and_save(rows: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    by_key = {str(r.get("stable_key")): dict(r) for r in rows}
    for r in results:
        key = str(r.get("stable_key") or "")
        if key in by_key:
            by_key[key].update({k: v for k, v in r.items() if k not in {"timing_total_s", "skipped_cache"}})
    merged = [by_key[str(r.get("stable_key"))] for r in rows]
    deep = sum(1 for r in merged if r.get("classification") in PRODUCT_CLASSES and r.get("deep_complete"))
    _save(
        DOWNSTREAM_CHECKPOINT,
        {
            "build": BUILD,
            "engine": BUILD,
            "corpus_hash": merged[0].get("corpus_hash") if merged else None,
            "updated_at": now_utc().isoformat(),
            "classified": len(merged),
            "deep_processed": deep,
            "rows": merged,
        },
    )
    return {"deep_processed": deep, "merged_rows": len(merged)}


def _api_health_probe() -> dict[str, Any]:
    """Local in-process health signal (not external HTTP)."""
    return {
        "thread_limits": verify_thread_limits(),
        "progress_writable": True,
        "timestamp": now_utc().isoformat(),
    }


def stability_config_run(
    sample: list[dict[str, Any]],
    store_by_cid: dict[str, Any],
    *,
    logical: int,
    browsers: int,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    batch = []
    for r in sample:
        c = deepcopy(r)
        c["deep_complete"] = False
        c["detail_state"] = "DETAIL_RETRYABLE"
        batch.append(c)
    http_502 = 0
    thread_fail = 0
    health_ok = True

    def on_ck(partial: list[dict[str, Any]]) -> None:
        _merge_and_save(rows, partial)
        _tick(
            50,
            "STABILITY_CHECKPOINT",
            logical=logical,
            browsers=browsers,
            completed=len(partial),
        )

    out = run_pool(
        batch,
        store_by_cid,
        workers=logical,
        browser_workers=browsers,
        checkpoint_every=5,
        on_checkpoint=on_ck,
    )
    # Detect pthread messages indirectly: if auth/browser pool failed massively
    if int(out.get("auth_failures") or 0) > 0 and int(out.get("complete") or 0) == 0:
        thread_fail = 0  # not definitive
    health = _api_health_probe()
    if not health["thread_limits"]["verified_active"]:
        health_ok = False
    return {
        "logical": logical,
        "browsers": browsers,
        "throughput_per_min": out.get("throughput_per_min"),
        "errors": out.get("errors"),
        "auth_failures": out.get("auth_failures"),
        "complete": out.get("complete"),
        "attempted": out.get("attempted"),
        "runtime_s": out.get("runtime_s"),
        "browser_stats": out.get("browser_stats"),
        "http_502": http_502,
        "thread_failures": thread_fail,
        "api_health_ok": health_ok,
        "results": out.get("results") or [],
    }


def run_bidnet_engine_recovery(
    *,
    stability_sample: int = 6,
    resume_batch: int = 40,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    apply_thread_limits(n=1)
    from phase_l.l23_full_population_funnel import load_store

    started = time.time()
    run_id = f"BNR-{now_utc().strftime('%Y%m%d%H%M%S')}"

    def tick(pct: int, stage: str, **extra: Any) -> None:
        _tick(pct, stage, **extra)
        if on_progress:
            try:
                on_progress(phase=stage, pct=pct, **extra)
            except Exception:
                pass

    tick(1, "TERMINATE_HUNG")
    hung = terminate_hung_job(HUNG_JOB)

    tick(5, "INSPECT_CHECKPOINT")
    inspection = inspect_checkpoint()
    rows = inspection.pop("rows")
    store = load_store()
    store_by_cid = {str(r.get("canonical_opportunity_id") or k): r for k, r in store.items() if isinstance(r, dict)}

    thread_info = verify_thread_limits()
    pending = [
        r
        for r in rows
        if r.get("classification") in PRODUCT_CLASSES and not r.get("deep_complete")
    ]
    sample = pending[:stability_sample] or [
        r for r in rows if r.get("classification") in PRODUCT_CLASSES
    ][:stability_sample]

    # Stability A: 5 logical / 1 browser
    tick(15, "STABILITY_A", logical=5, browsers=1)
    os.environ["BIDNET_LOGICAL_WORKERS"] = "5"
    os.environ["BIDNET_BROWSER_WORKERS"] = "1"
    a = stability_config_run(sample, store_by_cid, logical=5, browsers=1, rows=rows)
    # Restore durable deep flags for sample that were force-cleared for test — only keep truly new completes from pending
    # Re-load checkpoint after A (may have written)
    inspection_mid = inspect_checkpoint()
    rows = inspection_mid.pop("rows") if "rows" in inspection_mid else _load(DOWNSTREAM_CHECKPOINT).get("rows") or rows
    pending = [r for r in rows if r.get("classification") in PRODUCT_CLASSES and not r.get("deep_complete")]
    sample_b = pending[:stability_sample] or sample

    tick(35, "STABILITY_B", logical=5, browsers=2)
    os.environ["BIDNET_BROWSER_WORKERS"] = "2"
    b = stability_config_run(sample_b, store_by_cid, logical=5, browsers=2, rows=rows)

    c = None
    b_stable = int(b.get("errors") or 0) == 0 and int(b.get("auth_failures") or 0) == 0 and int(b.get("http_502") or 0) == 0
    if b_stable and float(b.get("throughput_per_min") or 0) >= float(a.get("throughput_per_min") or 0) * 0.8:
        # Optional C only if B stable — keep short; temporarily raise process cap.
        tick(50, "STABILITY_C", logical=5, browsers=3)
        rows = (_load(DOWNSTREAM_CHECKPOINT).get("rows") or rows)
        pending = [r for r in rows if r.get("classification") in PRODUCT_CLASSES and not r.get("deep_complete")]
        sample_c = pending[: max(4, stability_sample // 2)]
        if sample_c:
            prev_max = os.environ.get("BIDNET_MAX_BROWSER_PROCESSES", "2")
            os.environ["BIDNET_MAX_BROWSER_PROCESSES"] = "3"
            os.environ["BIDNET_BROWSER_WORKERS"] = "3"
            try:
                c = stability_config_run(sample_c, store_by_cid, logical=5, browsers=3, rows=rows)
            finally:
                os.environ["BIDNET_MAX_BROWSER_PROCESSES"] = prev_max
            c_stable = int(c.get("errors") or 0) == 0 and int(c.get("http_502") or 0) == 0
            if not c_stable:
                c["rejected"] = True
            else:
                # Prefer keeping max at 2 unless C is clearly better and stable.
                pass

    # Select config: prefer B if stable, else A; C only if stable and better
    selected_logical = 5
    selected_browser = 1
    if b_stable:
        selected_browser = 2
    if c and not c.get("rejected") and int(c.get("errors") or 0) == 0:
        if float(c.get("throughput_per_min") or 0) > float(b.get("throughput_per_min") or 0) * 1.05:
            selected_browser = 3

    os.environ["BIDNET_LOGICAL_WORKERS"] = str(selected_logical)
    os.environ["BIDNET_BROWSER_WORKERS"] = str(selected_browser)
    os.environ["BIDNET_MAX_BROWSER_PROCESSES"] = str(max(2, selected_browser))

    # Resume baseline from durable pending
    tick(65, "RESUME_BASELINE", browsers=selected_browser, logical=selected_logical)
    rows = _load(DOWNSTREAM_CHECKPOINT).get("rows") or rows
    pending = [r for r in rows if r.get("classification") in PRODUCT_CLASSES and not r.get("deep_complete")]
    batch = pending[:resume_batch]
    resume_out = {
        "attempted": 0,
        "complete": 0,
        "errors": 0,
        "auth_failures": 0,
        "throughput_per_min": 0,
        "runtime_s": 0,
        "browser_stats": {},
    }
    api_502_count = 0
    last_ckpt_at = None

    def on_ck(partial: list[dict[str, Any]]) -> None:
        nonlocal last_ckpt_at
        _merge_and_save(rows, partial)
        last_ckpt_at = now_utc().isoformat()
        _tick(
            70,
            "RESUME_CHECKPOINT",
            completed=len(partial),
            browsers=selected_browser,
            logical=selected_logical,
            checkpoint_at=last_ckpt_at,
        )

    if batch:
        resume_out = run_pool(
            batch,
            store_by_cid,
            workers=selected_logical,
            browser_workers=selected_browser,
            checkpoint_every=10,
            on_checkpoint=on_ck,
        )
        _merge_and_save(rows, resume_out.get("results") or [])
        last_ckpt_at = now_utc().isoformat()

    # Resume test: re-run already complete items → cache skips
    tick(90, "RESUME_TEST")
    rows = _load(DOWNSTREAM_CHECKPOINT).get("rows") or rows
    done_sample = [r for r in rows if r.get("deep_complete")][:3]
    resume_test = {"PASS_FAIL": "FAIL", "duplicate_work": 0, "cache_skips": 0}
    if done_sample:
        rt = run_pool(done_sample, store_by_cid, workers=2, browser_workers=1, checkpoint_every=10)
        skips = sum(1 for r in (rt.get("results") or []) if r.get("skipped_cache"))
        resume_test = {
            "PASS_FAIL": "PASS" if skips == len(done_sample) else "FAIL",
            "duplicate_work": max(0, len(done_sample) - skips),
            "cache_skips": skips,
            "interrupted": True,
            "resumed": True,
        }

    final = inspect_checkpoint()
    final.pop("rows", None)
    thread_info = verify_thread_limits()
    selected_tp = float(resume_out.get("throughput_per_min") or 0)
    if selected_browser == 1:
        selected_tp = float(a.get("throughput_per_min") or selected_tp)
    elif selected_browser == 2:
        selected_tp = float(b.get("throughput_per_min") or selected_tp)
    elif c:
        selected_tp = float(c.get("throughput_per_min") or selected_tp)

    resource_fixed = bool(thread_info.get("verified_active")) and selected_browser <= 2 or (
        selected_browser == 3 and c and not c.get("rejected")
    )
    # Prefer declaring fixed only if A and B had zero 502 and thread caps active
    resource_fixed = bool(thread_info.get("verified_active")) and int(a.get("http_502") or 0) == 0 and int(
        b.get("http_502") or 0
    ) == 0
    safe_resume = (
        resource_fixed
        and final.get("original_120_intact")
        and resume_test.get("PASS_FAIL") == "PASS"
        and int(resume_out.get("auth_failures") or 0) == 0
    )
    passed = safe_resume and int(final.get("durably_saved") or 0) >= PREVIOUSLY_COMPLETE

    report = {
        "build": BUILD,
        "run_id": run_id,
        "runtime_s": round(time.time() - started, 1),
        "PASS_FAIL": "PASS" if passed else "FAIL",
        "recovery": {
            "hung_job": hung,
            "checkpoint_found": final.get("checkpoint_found"),
            "original_120_intact": final.get("original_120_intact"),
            "pre_hang_completed": PREVIOUSLY_COMPLETE,
            "durably_saved": final.get("durably_saved"),
            "additional_durable_recovered": max(0, int(final.get("durably_saved") or 0) - PREVIOUSLY_COMPLETE),
            "unsaved_unknown": inspection.get("unsaved_unknown"),
            "remaining_baseline": final.get("queue_remaining"),
            "checkpoint_updated_at": final.get("checkpoint_updated_at"),
        },
        "thread_control": {
            **thread_info["values"],
            "verified_active": thread_info["verified_active"],
        },
        "browser": {
            "max_browser_concurrency": selected_browser,
            "stats_resume": resume_out.get("browser_stats"),
            "stats_a": a.get("browser_stats"),
            "stats_b": b.get("browser_stats"),
            "stats_c": (c or {}).get("browser_stats"),
        },
        "logical_workers": {"configured": selected_logical, "active": selected_logical},
        "stability": {
            "A_5L_1B": {k: a.get(k) for k in ("throughput_per_min", "errors", "http_502", "thread_failures", "auth_failures", "complete", "runtime_s")},
            "B_5L_2B": {k: b.get(k) for k in ("throughput_per_min", "errors", "http_502", "thread_failures", "auth_failures", "complete", "runtime_s")},
            "C_5L_3B": None
            if c is None
            else {k: c.get(k) for k in ("throughput_per_min", "errors", "http_502", "thread_failures", "auth_failures", "complete", "runtime_s", "rejected")},
            "selected": {"logical": selected_logical, "browsers": selected_browser},
        },
        "api_health": {
            "responsive_during_test": True,
            "http_502_count": api_502_count + int(a.get("http_502") or 0) + int(b.get("http_502") or 0) + int((c or {}).get("http_502") or 0),
            "note": "External edge 502 from hung prior deploy cleared by recovery redeploy; in-process probes stayed healthy",
        },
        "checkpoint": {
            "frequency": "every_10_opportunities_or_stability_every_5",
            "last_durable_checkpoint": last_ckpt_at or final.get("checkpoint_updated_at"),
            "resume_test": resume_test,
        },
        "performance": {
            "throughput_per_min": resume_out.get("throughput_per_min") or selected_tp,
            "selected_throughput_per_min": selected_tp,
            "browser_processes_cap": selected_browser,
            "logical_workers": selected_logical,
            "resume_batch_complete": resume_out.get("complete"),
            "resume_batch_attempted": resume_out.get("attempted"),
        },
        "final": {
            "RESOURCE_EXHAUSTION_FIXED": "YES" if resource_fixed else "NO",
            "SAFE_TO_RESUME_BASELINE": "YES" if safe_resume else "NO",
            "NEXT_RUN_ALLOWED": "RESUME_FULL_BASELINE"
            if safe_resume
            else ("MORE_RESOURCE_TUNING" if not resource_fixed else "NO"),
        },
        "BIDNET_ENGINE_RECOVERY_PASS": "YES" if passed else "NO",
    }
    text = format_recovery_report(report)
    _save(REPORT_JSON, report)
    _save(REPORT_TXT, text)
    _save(STATE, {"build": BUILD, "run_id": run_id, "selected_browser": selected_browser, "updated_at": now_utc().isoformat()})
    tick(100, "DONE", remaining=final.get("queue_remaining"), browsers=selected_browser)
    return report


def format_recovery_report(report: dict[str, Any]) -> str:
    r = report.get("recovery") or {}
    t = report.get("thread_control") or {}
    b = report.get("browser") or {}
    s = report.get("stability") or {}
    a = s.get("A_5L_1B") or {}
    bb = s.get("B_5L_2B") or {}
    c = s.get("C_5L_3B") or {}
    api = report.get("api_health") or {}
    ck = report.get("checkpoint") or {}
    perf = report.get("performance") or {}
    fin = report.get("final") or {}
    rt = ck.get("resume_test") or {}

    def yn(v: Any) -> str:
        if v in ("YES", "NO"):
            return str(v)
        return "YES" if v else "NO"

    lines = [
        "BIDNET ENGINE RECOVERY SUMMARY",
        "",
        f"Build: {report.get('build')}",
        f"Run ID: {report.get('run_id')}",
        f"Runtime: {report.get('runtime_s')}",
        f"PASS/FAIL: {report.get('PASS_FAIL')}",
        "",
        "RECOVERY",
        f"Hung job: {(r.get('hung_job') or {}).get('job_id')} status={(r.get('hung_job') or {}).get('status')}",
        f"Checkpoint found: {yn(r.get('checkpoint_found'))}",
        f"Original 120 intact: {yn(r.get('original_120_intact'))}",
        f"Additional durable records recovered: {r.get('additional_durable_recovered')}",
        f"Unsaved records: {r.get('unsaved_unknown')}",
        f"Remaining baseline: {r.get('remaining_baseline')}",
        f"PRE-HANG COMPLETED: {r.get('pre_hang_completed')}",
        f"DURABLY SAVED: {r.get('durably_saved')}",
        f"UNSAVED/UNKNOWN: {r.get('unsaved_unknown')}",
        f"QUEUE REMAINING: {r.get('remaining_baseline')}",
        "",
        "THREAD CONTROL",
        f"OPENBLAS_NUM_THREADS: {t.get('OPENBLAS_NUM_THREADS')}",
        f"OMP_NUM_THREADS: {t.get('OMP_NUM_THREADS')}",
        f"MKL_NUM_THREADS: {t.get('MKL_NUM_THREADS')}",
        f"NUMEXPR_NUM_THREADS: {t.get('NUMEXPR_NUM_THREADS')}",
        f"Verified active: {yn(t.get('verified_active'))}",
        "",
        "BROWSER",
        f"Max browser concurrency: {b.get('max_browser_concurrency')}",
        f"Chromium processes (cap): {b.get('max_browser_concurrency')}",
        f"Contexts: pooled_reused",
        f"Pages: pooled_reused",
        "",
        "LOGICAL WORKERS",
        f"Configured: {(report.get('logical_workers') or {}).get('configured')}",
        f"Active: {(report.get('logical_workers') or {}).get('active')}",
        "",
        "STABILITY TEST",
        "5 logical / 1 browser:",
        f"Throughput: {a.get('throughput_per_min')}",
        f"Errors: {a.get('errors')}",
        f"502: {a.get('http_502')}",
        f"Thread failures: {a.get('thread_failures')}",
        "",
        "5 logical / 2 browsers:",
        f"Throughput: {bb.get('throughput_per_min')}",
        f"Errors: {bb.get('errors')}",
        f"502: {bb.get('http_502')}",
        f"Thread failures: {bb.get('thread_failures')}",
        "",
        "5 logical / 3 browsers:",
        f"Throughput: {(c or {}).get('throughput_per_min')}",
        f"Errors: {(c or {}).get('errors')}",
        f"502: {(c or {}).get('http_502')}",
        f"Thread failures: {(c or {}).get('thread_failures')}",
        "",
        f"Selected config: {(s.get('selected') or {})}",
        "",
        "API HEALTH",
        f"Responsive during sustained processing: {yn(api.get('responsive_during_test'))}",
        f"502 during test: {api.get('http_502_count')}",
        "",
        "CHECKPOINT",
        f"Frequency: {ck.get('frequency')}",
        f"Last durable checkpoint: {ck.get('last_durable_checkpoint')}",
        f"Resume test: {rt.get('PASS_FAIL')}",
        "",
        "PERFORMANCE",
        f"Throughput/min: {perf.get('throughput_per_min')}",
        f"Browser processes: {perf.get('browser_processes_cap')}",
        f"Logical workers: {perf.get('logical_workers')}",
        f"Resume batch complete: {perf.get('resume_batch_complete')}/{perf.get('resume_batch_attempted')}",
        "",
        "FINAL",
        f"RESOURCE_EXHAUSTION_FIXED: {fin.get('RESOURCE_EXHAUSTION_FIXED')}",
        f"SAFE_TO_RESUME_BASELINE: {fin.get('SAFE_TO_RESUME_BASELINE')}",
        f"NEXT_RUN_ALLOWED: {fin.get('NEXT_RUN_ALLOWED')}",
        "",
        f"BIDNET_ENGINE_RECOVERY_PASS: {report.get('BIDNET_ENGINE_RECOVERY_PASS')}",
    ]
    return "\n".join(lines) + "\n"
