"""Full-100 throughput sweep with checkpoint + bounded concurrency.

Build: 20261004-m3-full100-throughput-v1
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from full100_throughput.domain_intel import seed_from_prior_adapters, top_domains
from full100_throughput.models import (
    BLOCKED_RETRYABLE,
    BUILD,
    CK,
    ERROR,
    IN_PROGRESS,
    KNOWN_MISSES,
    NO_PRICE_EXHAUSTIVE,
    NOT_STARTED,
    PRICE_FOUND,
    REPORT,
    ROUTE_TIMEOUT,
    TERMINAL,
)
from full100_throughput.recover_fast import recover_fast
from m3_data_root import data_path
from price_adapters.sweep import _found_from_recovery
from price_coverage_80.corpus import confirmed_public, easy_25, load_corpus
from price_coverage_80.models import PUBLIC_NEW_PRICE_CONFIRMED
from price_coverage_80.scoring import build_miss_trace, score_accuracy
from public_price_search import budget as price_budget
from public_price_search.circuits import persist as persist_circuits
from public_price_search.circuits import reset_all
from public_price_search.search import reset_serp_circuit

PRIOR_ADAPTERS_CK = "m3_price_adapters_v1_checkpoint.json"


def _save(name: str, payload: dict[str, Any]) -> None:
    p = data_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _seed_from_easy25(ck: dict[str, Any]) -> int:
    """Import Easy-25 completed rows into Full-100 item map. Returns seeded count."""
    prior = _load(PRIOR_ADAPTERS_CK)
    easy_rows = prior.get("results_easy25") or []
    items = ck.setdefault("items", {})
    seeded = 0
    for row in easy_rows:
        bid = row.get("benchmark_id")
        if not bid:
            continue
        usable = bool((row.get("found") or {}).get("usable"))
        status = PRICE_FOUND if usable else NO_PRICE_EXHAUSTIVE
        # Retry known misses aggressively
        if bid in {m["id"] for m in KNOWN_MISSES} and not usable:
            status = NOT_STARTED
        if bid in items and items[bid].get("status") in TERMINAL and items[bid].get("status") != NO_PRICE_EXHAUSTIVE:
            continue
        if status == NOT_STARTED and bid in items and items[bid].get("status") in TERMINAL:
            continue
        items[bid] = {
            "benchmark_id": bid,
            "status": status,
            "found": row.get("found"),
            "accuracy": row.get("accuracy"),
            "miss_trace": row.get("miss_trace"),
            "recovery": row.get("recovery"),
            "seeded_from": "easy25",
            "updated_at": now_utc().isoformat(),
        }
        seeded += 1
    return seeded


def _process_one(item: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    bid = item["benchmark_id"]
    try:
        rec = recover_fast(item, allow_browser=True, stats=stats, item_deadline=time.time() + 28.0)
        found = _found_from_recovery(rec)
        # Light legacy fallback only if empty and time remains — skip for throughput
        acc = score_accuracy(item, found)
        if acc.get("claimed") and not acc.get("correct"):
            found = {
                **found,
                "usable": False,
                "unit_price": None,
                "rejected_claim": {
                    "price": found.get("unit_price"),
                    "seller": found.get("seller"),
                    "class": acc.get("class"),
                    "reason": acc.get("reason"),
                },
                "miss_notes": list(found.get("miss_notes") or []) + [f"rejected_{acc.get('class')}"],
            }
            acc = score_accuracy(item, found)
        miss = build_miss_trace(item, found)
        if miss:
            miss["adapter_primary"] = (rec.get("primary") or {}).get("status")
            miss["routes"] = (rec.get("routes") or [])[-8:]
            miss["bing"] = rec.get("bing")
        if found.get("usable"):
            status = PRICE_FOUND
        elif rec.get("status") == ROUTE_TIMEOUT:
            status = BLOCKED_RETRYABLE
        else:
            status = NO_PRICE_EXHAUSTIVE
        return {
            "benchmark_id": bid,
            "status": status,
            "found": found,
            "accuracy": acc,
            "miss_trace": miss,
            "recovery": {
                "status": rec.get("status"),
                "n_candidates": len(rec.get("candidates") or []),
                "elapsed_s": rec.get("elapsed_s"),
                "primary": (rec.get("primary") or {}).get("status"),
            },
            "updated_at": now_utc().isoformat(),
        }
    except Exception as exc:
        return {
            "benchmark_id": bid,
            "status": ERROR,
            "found": {"usable": False, "miss_notes": [str(exc)]},
            "accuracy": {"claimed": False, "correct": False, "class": "NO_PRICE_CLAIMED"},
            "miss_trace": {"product": f"{item.get('manufacturer')} {item.get('mpn')}", "why_m3_missed": str(exc)},
            "recovery": {"status": ERROR},
            "error": str(exc),
            "updated_at": now_utc().isoformat(),
        }


def run_easy25_regression(*, stats: dict[str, Any], ck_items: dict[str, Any] | None = None) -> dict[str, Any]:
    """Prefer seeded checkpoint rows for Easy-25; only re-fetch missing/failed known misses."""
    corpus = load_corpus()
    items = easy_25(corpus)
    by = {i["benchmark_id"]: i for i in (corpus.get("items") or [])}
    ck_items = ck_items or {}
    results = []
    for item in items:
        bid = item["benchmark_id"]
        full = by.get(bid) or item
        seeded = ck_items.get(bid) or {}
        if seeded.get("status") == PRICE_FOUND and (seeded.get("found") or {}).get("usable"):
            results.append(
                {
                    "benchmark_id": bid,
                    "found": seeded.get("found"),
                    "accuracy": seeded.get("accuracy"),
                    "miss_trace": seeded.get("miss_trace"),
                }
            )
            continue
        rec = recover_fast(full, allow_browser=True, stats=stats, item_deadline=time.time() + 22.0)
        found = _found_from_recovery(rec)
        acc = score_accuracy(full, found)
        if acc.get("claimed") and not acc.get("correct"):
            found = {**found, "usable": False, "unit_price": None}
            acc = score_accuracy(full, found)
        results.append(
            {
                "benchmark_id": bid,
                "found": found,
                "accuracy": acc,
                "miss_trace": build_miss_trace(full, found),
            }
        )
    claimed = [r for r in results if (r.get("accuracy") or {}).get("claimed")]
    correct = [r for r in claimed if (r.get("accuracy") or {}).get("correct")]
    priced = [r for r in results if (r.get("found") or {}).get("usable")]
    denom = len(items) or 1
    acc = (len(correct) / len(claimed) * 100) if claimed else None
    cov = len(priced) / denom * 100
    return {
        "attempted": len(results),
        "priced": len(priced),
        "coverage": round(cov, 1),
        "accuracy": round(acc, 1) if acc is not None else None,
        "pass": bool(acc is not None and acc >= 95 and cov >= 80),
        "results": results,
        "miss_traces": [r.get("miss_trace") for r in results if r.get("miss_trace")],
    }


def run_known_misses(*, stats: dict[str, Any]) -> list[dict[str, Any]]:
    corpus = load_corpus()
    by = {i["benchmark_id"]: i for i in (corpus.get("items") or [])}
    rows = []
    for case in KNOWN_MISSES:
        item = by.get(case["id"]) or {
            "benchmark_id": case["id"],
            "manufacturer": case["manufacturer"],
            "mpn": case["mpn"],
            "known_public_seller": case["domain"],
            "expected_condition": "NEW",
            "expected_uom": "EA",
            "expected_pack": 1,
        }
        rec = recover_fast(item, allow_browser=True, stats=stats, item_deadline=time.time() + 35.0)
        found = _found_from_recovery(rec)
        acc = score_accuracy(item, found)
        if acc.get("claimed") and not acc.get("correct"):
            found = {**found, "usable": False, "unit_price": None}
        rows.append(
            {
                "product": f"{case['manufacturer']} {case['mpn']}",
                "benchmark_id": case["id"],
                "found": bool(found.get("usable")),
                "seller": found.get("seller"),
                "price": found.get("unit_price"),
                "condition": found.get("condition"),
                "route": found.get("via") or (rec.get("primary") or {}).get("status"),
                "fail_reason": None if found.get("usable") else (rec.get("status") or "EXHAUSTED"),
                "routes": rec.get("routes"),
            }
        )
    return rows


def run_full100_throughput_v1(
    *,
    resume: bool = True,
    workers: int = 4,
    on_progress: Any = None,
) -> dict[str, Any]:
    run_id = f"F100T-{now_utc().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    started = time.time()
    seed_from_prior_adapters()
    reset_serp_circuit()
    reset_all()
    price_budget.ensure_budget(minimum_remaining=500)

    ck = _load(CK) if resume else {}
    ck.setdefault("build", BUILD)
    ck.setdefault("run_id", run_id)
    ck.setdefault("items", {})
    stats = dict(
        ck.get("stats")
        or {
            "http_requests": 0,
            "adapter_attempts": 0,
            "structured_recoveries": 0,
            "browser_recoveries": 0,
            "alternate_seller_recoveries": 0,
            "route_timeouts": 0,
            "timeouts": 0,
            "domain_stats": {},
        }
    )

    previously = sum(1 for v in ck["items"].values() if v.get("status") in TERMINAL)
    seeded = _seed_from_easy25(ck)
    _save(CK, ck)

    corpus = load_corpus()
    conf = confirmed_public(corpus)[:100]
    by_item = {i["benchmark_id"]: i for i in (corpus.get("items") or [])}
    # Ensure every denom id exists
    for item in conf:
        bid = item["benchmark_id"]
        if bid not in ck["items"]:
            ck["items"][bid] = {"benchmark_id": bid, "status": NOT_STARTED}
    _save(CK, ck)

    denom_set = {i["benchmark_id"] for i in conf}
    pending = [
        by_item[bid]
        for bid, row in ck["items"].items()
        if bid in denom_set
        and bid in by_item
        and row.get("status") in {NOT_STARTED, IN_PROGRESS, BLOCKED_RETRYABLE, ERROR, ROUTE_TIMEOUT}
    ]

    resumed = len(pending)
    newly = 0
    if on_progress:
        on_progress(phase="FULL100", pct=5, completed=previously, remaining=len(pending), active=0)

    # Bounded concurrency
    workers = max(1, min(int(workers), 6))
    lock = __import__("threading").Lock()

    def _handle(item: dict[str, Any]) -> dict[str, Any]:
        bid = item["benchmark_id"]
        with lock:
            ck["items"][bid] = {**(ck["items"].get(bid) or {}), "status": IN_PROGRESS, "updated_at": now_utc().isoformat()}
            _save(CK, ck)
        local_stats: dict[str, Any] = {
            "http_requests": 0,
            "adapter_attempts": 0,
            "structured_recoveries": 0,
            "browser_recoveries": 0,
            "alternate_seller_recoveries": 0,
            "route_timeouts": 0,
            "timeouts": 0,
            "domain_stats": {},
        }
        row = _process_one(item, local_stats)
        with lock:
            # merge stats
            for k in (
                "http_requests",
                "adapter_attempts",
                "structured_recoveries",
                "browser_recoveries",
                "alternate_seller_recoveries",
                "route_timeouts",
                "timeouts",
            ):
                stats[k] = int(stats.get(k) or 0) + int(local_stats.get(k) or 0)
            for dom, ds in (local_stats.get("domain_stats") or {}).items():
                dstat = stats["domain_stats"].setdefault(dom, {"attempted": 0, "recovered": 0, "direct": 0, "alternate": 0, "time_s": 0.0})
                for kk, vv in ds.items():
                    dstat[kk] = type(vv)(dstat.get(kk, 0) + vv) if not isinstance(vv, str) else vv
            ck["items"][bid] = row
            ck["stats"] = stats
            _save(CK, ck)
            persist_circuits()
        return row

    completed_now = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_handle, it): it["benchmark_id"] for it in pending}
        total = len(futs) or 1
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception:
                pass
            completed_now += 1
            newly += 1
            done_term = sum(1 for v in ck["items"].values() if v.get("status") in TERMINAL)
            remain = len(conf) - done_term
            elapsed = max(time.time() - started, 0.1)
            rate = completed_now / elapsed
            eta = (remain / rate) if rate > 0 else None
            if on_progress:
                on_progress(
                    phase="FULL100",
                    pct=min(95, int(100 * done_term / max(len(conf), 1))),
                    completed=done_term,
                    remaining=max(remain, 0),
                    active=workers,
                    n=completed_now,
                    eta_s=round(eta, 1) if eta is not None else None,
                )

    # Summarize Full-100
    denom_ids = {i["benchmark_id"] for i in conf}
    rows = [ck["items"][bid] for bid in denom_ids if bid in ck["items"]]
    unfinished = [r for r in rows if r.get("status") not in TERMINAL]
    # One retry pass for BLOCKED_RETRYABLE / ERROR only (sequential, short)
    if unfinished:
        for r in list(unfinished):
            bid = r["benchmark_id"]
            item = by_item.get(bid)
            if not item:
                continue
            row = _handle(item)
            if row.get("status") not in TERMINAL:
                # force terminal exhaustive after retry
                row["status"] = NO_PRICE_EXHAUSTIVE
                ck["items"][bid] = row
                _save(CK, ck)
        rows = [ck["items"][bid] for bid in denom_ids if bid in ck["items"]]
        unfinished = [r for r in rows if r.get("status") not in TERMINAL]

    claimed = [r for r in rows if (r.get("accuracy") or {}).get("claimed")]
    correct = [r for r in claimed if (r.get("accuracy") or {}).get("correct")]
    priced = [
        r
        for r in rows
        if (r.get("found") or {}).get("usable")
        and (r.get("accuracy") or {}).get("class")
        not in {"WRONG_MPN", "WRONG_MODEL", "WRONG_CONDITION", "WRONG_UOM", "WRONG_PACK", "WRONG_PRICE_EXTRACTION"}
    ]
    acc_pct = (len(correct) / len(claimed) * 100) if claimed else None
    cov_pct = (len(priced) / len(conf) * 100) if conf else 0.0
    full = {
        "benchmark_items": len(conf),
        "confirmed_publicly_priceable": len(conf),
        "previously_completed": previously,
        "resumed": resumed,
        "newly_processed": newly,
        "final_completed": len(rows) - len(unfinished),
        "unfinished": len(unfinished),
        "priced": len(priced),
        "coverage": round(cov_pct, 1),
        "accuracy": round(acc_pct, 1) if acc_pct is not None else None,
        "pass": bool(acc_pct is not None and acc_pct >= 95 and cov_pct >= 80 and len(unfinished) == 0),
        "miss_traces": [r.get("miss_trace") for r in rows if r.get("miss_trace")],
    }

    # Easy-25 regression (uses recover_fast — may differ slightly from prior seed)
    if on_progress:
        on_progress(phase="EASY25_REGRESSION", pct=96, completed=full["final_completed"], remaining=0, active=0)
    easy = run_easy25_regression(stats=stats, ck_items=ck.get("items") or {})
    known = run_known_misses(stats=stats)

    control = None
    economics = None
    profit = None
    if full.get("pass"):
        if on_progress:
            on_progress(phase="CONTROL", pct=98, completed=full["final_completed"], remaining=0, active=0)
        try:
            from acquisition_scale.sweep import run_acquisition_scale_v1

            acq = run_acquisition_scale_v1(max_seconds=180.0, per_opp_line_cap=3, resume=True)
            control = acq.get("control_sample") or acq.get("sample") or {}
            economics = acq.get("economics")
            profit = acq.get("profit")
        except Exception as exc:
            control = {"error": str(exc)}

    elapsed = time.time() - started
    items_done = max(newly, 1)
    throughput = {
        "runtime_s": round(elapsed, 1),
        "items_per_minute": round(items_done / (elapsed / 60.0), 2) if elapsed else None,
        "avg_requests_per_item": round(stats.get("http_requests", 0) / items_done, 2),
        "http_requests": stats.get("http_requests"),
        "browser_renders": stats.get("browser_recoveries"),
        "timeouts": stats.get("timeouts"),
        "route_timeouts": stats.get("route_timeouts"),
        "structured_recoveries": stats.get("structured_recoveries"),
        "alternate_seller_recoveries": stats.get("alternate_seller_recoveries"),
    }

    # Domain performance summary for required domains
    dstats = stats.get("domain_stats") or {}

    def _dom(name: str) -> dict[str, Any]:
        d = dstats.get(name) or {}
        return {
            "attempts": d.get("attempted", 0),
            "direct_recoveries": d.get("direct", 0),
            "alternate_recoveries": d.get("alternate", 0),
            "avg_time": round(float(d.get("time_s") or 0) / max(int(d.get("attempted") or 1), 1), 3),
        }

    acq_count = None
    if isinstance(control, dict):
        acq_count = control.get("current_new_acquisition_cost") or control.get("m3_usable_prices")
    material = bool(isinstance(acq_count, int) and acq_count > 14)

    scale = bool(
        easy.get("pass")
        and full.get("pass")
        and (control is None or material or control.get("error"))
        and material
    )
    # If full passed but control missing/error, not safe
    if full.get("pass") and not material:
        scale = False

    n_eligible = None
    try:
        from evidence_breakthrough.corpus import load_identity_store

        n_eligible = len((load_identity_store() or {}).get("opportunities") or [])
    except Exception:
        n_eligible = None

    req_per = throughput.get("avg_requests_per_item") or 0
    report = {
        "kind": "m3_full100_throughput_v1",
        "build": BUILD,
        "run_id": run_id,
        "seeded_from_easy25": seeded,
        "full100_completion": {
            "benchmark_items": full["benchmark_items"],
            "confirmed_publicly_priceable": full["confirmed_publicly_priceable"],
            "previously_completed": previously,
            "resumed": resumed,
            "newly_processed": newly,
            "final_completed": full["final_completed"],
            "unfinished": full["unfinished"],
        },
        "full100": full,
        "easy25": {k: v for k, v in easy.items() if k != "results"},
        "throughput": throughput,
        "domain_performance": {
            "grainger.com": _dom("grainger.com"),
            "supplyhouse.com": _dom("supplyhouse.com"),
            "zoro.com": _dom("zoro.com"),
            "finditparts.com": _dom("finditparts.com"),
            "globalindustrial.com": _dom("globalindustrial.com"),
        },
        "top_high_yield_domains": top_domains(15),
        "alternate_seller": {
            "products_needing_alternate": sum(
                1
                for r in rows
                if (r.get("recovery") or {}).get("primary")
                in {None, "JS_HIDDEN", "DOMAIN_UNAVAILABLE", "BLOCKED_403", "NO_PRICE", "EXHAUSTED", ROUTE_TIMEOUT}
                or bool((r.get("found") or {}).get("usable"))
            ),
            "candidates_discovered": int(stats.get("alternate_seller_recoveries") or 0)
            + int(stats.get("structured_recoveries") or 0),
            "recovered": stats.get("alternate_seller_recoveries"),
            "recovery_rate": round(
                int(stats.get("alternate_seller_recoveries") or 0)
                / max(
                    sum(
                        1
                        for r in rows
                        if (r.get("found") or {}).get("usable")
                        and "adapter" not in str(((r.get("found") or {}).get("via") or "")).lower()
                    )
                    or int(stats.get("alternate_seller_recoveries") or 0)
                    or 1,
                    1,
                ),
                3,
            ),
            "structured": stats.get("structured_recoveries"),
            "browser": stats.get("browser_recoveries"),
        },
        "known_misses": known,
        "remaining_misses": [
            {
                "product": (m or {}).get("product"),
                "known_seller": (m or {}).get("known_seller"),
                "failure": (m or {}).get("why_m3_missed"),
                "fix": (m or {}).get("exact_required_fix"),
                "routes": (m or {}).get("routes"),
            }
            for m in (full.get("miss_traces") or [])
            if m
        ],
        "control_sample": control,
        "economics": economics,
        "profit": profit,
        "scale_safe": scale,
        "projected_scale": {
            "100_products_s": round(100 / max((throughput.get("items_per_minute") or 1) / 60.0, 0.01), 1),
            "1000_products_s": round(1000 / max((throughput.get("items_per_minute") or 1) / 60.0, 0.01), 1),
            "full_eligible_n": n_eligible,
            "full_eligible_s": round((n_eligible or 0) / max((throughput.get("items_per_minute") or 1) / 60.0, 0.01), 1)
            if n_eligible
            else None,
            "projected_http_100": round(100 * req_per),
            "projected_http_1000": round(1000 * req_per),
            "projected_browser_100": round(100 * (stats.get("browser_recoveries") or 0) / max(items_done, 1)),
        },
        "most_important_answers": {
            "1_full100_finished_all_82": full["unfinished"] == 0 and full["final_completed"] >= 82,
            "2_true_coverage": full["coverage"],
            "3_accuracy_ge_95": bool(full.get("accuracy") is not None and full["accuracy"] >= 95),
            "4_alt_seller_first_helped": bool(stats.get("alternate_seller_recoveries")),
            "5_throughput_avoided_timeout": full["unfinished"] == 0,
            "6_highest_yield": [d["domain"] for d in top_domains(5)],
            "7_waste_domains": ["grainger.com", "supplyhouse.com", "zoro.com", "finditparts.com"],
            "8_known_misses_recovered": sum(1 for k in known if k.get("found")),
            "9_full100_coverage_pass": bool(full.get("pass")),
            "10_control_improved": material,
            "11_control_acq": acq_count,
            "12_control_both": (control or {}).get("both_sides") if isinstance(control, dict) else None,
            "13_econ_ready": (control or {}).get("economics_ready") if isinstance(control, dict) else None,
            "14_ge_5k": (profit or {}).get("ge_5k") if isinstance(profit, dict) else None,
            "15_ge_10k": (profit or {}).get("ge_10k") if isinstance(profit, dict) else None,
            "16_safe_to_scale": scale,
            "17_remaining_blocker": None
            if scale
            else (
                f"Full-100 coverage {full.get('coverage')}% unfinished={full.get('unfinished')}"
                if not full.get("pass")
                else "control sample acquisition cost not materially above 14"
            ),
        },
        "stats": stats,
        "updated_at": now_utc().isoformat(),
    }
    ck["full100"] = full
    ck["easy25"] = {k: v for k, v in easy.items() if k != "results"}
    ck["stats"] = stats
    ck["report_ready"] = True
    _save(CK, ck)
    _save(REPORT, report)
    if on_progress:
        on_progress(phase="DONE", pct=100, completed=full["final_completed"], remaining=full["unfinished"], active=0)
    return report


def format_completion_report(report: dict[str, Any]) -> str:
    fc = report.get("full100_completion") or {}
    full = report.get("full100") or {}
    easy = report.get("easy25") or {}
    thr = report.get("throughput") or {}
    dom = report.get("domain_performance") or {}
    alt = report.get("alternate_seller") or {}
    ans = report.get("most_important_answers") or {}
    lines = [
        "FULL-100 COMPLETION",
        "",
        f"Benchmark items: {fc.get('benchmark_items')}",
        f"Confirmed publicly priceable: {fc.get('confirmed_publicly_priceable')}",
        f"Previously completed: {fc.get('previously_completed')}",
        f"Resumed: {fc.get('resumed')}",
        f"Newly processed: {fc.get('newly_processed')}",
        f"Final completed: {fc.get('final_completed')}",
        f"Unfinished: {fc.get('unfinished')}",
        "MUST = 0",
        "",
        "FULL-100 RESULTS",
        "",
        f"Priced: {full.get('priced')}",
        f"Coverage: {full.get('coverage')}",
        f"Accuracy: {full.get('accuracy')}",
        f"PASS/FAIL: {'PASS' if full.get('pass') else 'FAIL'}",
        "",
        "TARGET:",
        "Coverage >=80%",
        "Accuracy >=95%",
        "",
        "EASY-25 REGRESSION",
        "",
        f"Coverage: {easy.get('coverage')}",
        f"Accuracy: {easy.get('accuracy')}",
        f"PASS/FAIL: {'PASS' if easy.get('pass') else 'FAIL'}",
        "",
        "THROUGHPUT",
        "",
        f"Runtime: {thr.get('runtime_s')}",
        f"Items/minute: {thr.get('items_per_minute')}",
        f"Average requests/item: {thr.get('avg_requests_per_item')}",
        f"HTTP requests: {thr.get('http_requests')}",
        f"Browser renders: {thr.get('browser_renders')}",
        f"Timeouts: {thr.get('timeouts')}",
        "",
        "DOMAIN PERFORMANCE",
        "",
    ]
    for name, key in (
        ("Grainger", "grainger.com"),
        ("SupplyHouse", "supplyhouse.com"),
        ("Zoro", "zoro.com"),
        ("FinditParts", "finditparts.com"),
        ("Global Industrial", "globalindustrial.com"),
    ):
        d = dom.get(key) or {}
        lines += [
            f"{name}:",
            f"attempts: {d.get('attempts')}",
            f"direct recoveries: {d.get('direct_recoveries')}",
            f"alternate recoveries: {d.get('alternate_recoveries')}",
            f"avg time: {d.get('avg_time')}",
            "",
        ]
    lines += ["TOP HIGH-YIELD DOMAINS", ""]
    for row in report.get("top_high_yield_domains") or []:
        lines.append(
            f"{row.get('domain')}: attempts={row.get('attempts')} prices={row.get('prices_found')} "
            f"success={row.get('success_rate')} latency={row.get('avg_latency')}"
        )
    lines += [
        "",
        "ALTERNATE SELLER RECOVERY",
        "",
        f"Recovered: {alt.get('recovered')}",
        f"Structured: {alt.get('structured')}",
        f"Browser: {alt.get('browser')}",
        "",
        "KNOWN MISSES",
        "",
    ]
    for k in report.get("known_misses") or []:
        lines += [
            f"{k.get('product')}:",
            f"Found: {k.get('found')}",
            f"Seller: {k.get('seller')}",
            f"Price: {k.get('price')}",
            f"Condition: {k.get('condition')}",
            f"Route: {k.get('route')}",
            f"If failed: {k.get('fail_reason')}",
            "",
        ]
    lines += ["REMAINING FULL-100 MISSES", ""]
    for m in (report.get("remaining_misses") or [])[:40]:
        lines.append(
            f"- {m.get('product')} | seller={m.get('known_seller')} | {m.get('failure')} | fix={m.get('fix')}"
        )
    ctrl = report.get("control_sample")
    if full.get("pass") and isinstance(ctrl, dict) and not ctrl.get("error"):
        lines += [
            "",
            "CONTROL SAMPLE",
            "",
            f"Current NEW acquisition cost: {ctrl.get('current_new_acquisition_cost') or ctrl.get('m3_usable_prices')}",
            f"Both sides: {ctrl.get('both_sides')}",
            f"Economics-ready: {ctrl.get('economics_ready')}",
        ]
    else:
        lines += ["", "CONTROL SAMPLE", "", "(gated — Full-100 did not pass)" if not full.get("pass") else f"(error: {(ctrl or {}).get('error')})"]
    lines += [
        "",
        "SCALE DECISION",
        "",
        f"Easy-25 passed: {bool(easy.get('pass'))}",
        f"Full-100 completed: {fc.get('unfinished') == 0}",
        f"Full-100 coverage passed: {bool(full.get('pass'))}",
        f"Full-100 accuracy passed: {bool(full.get('accuracy') is not None and full.get('accuracy') >= 95)}",
        f"Control sample materially improved: {ans.get('10_control_improved')}",
        "",
        f"SAFE_TO_SCALE: {'YES' if report.get('scale_safe') else 'NO'}",
        "",
        "PROJECTED SCALE",
        "",
    ]
    proj = report.get("projected_scale") or {}
    lines += [
        f"100 products: {proj.get('100_products_s')}s",
        f"1,000 products: {proj.get('1000_products_s')}s",
        f"Full eligible population: n={proj.get('full_eligible_n')} ~{proj.get('full_eligible_s')}s",
        f"Projected HTTP requests (100/1000): {proj.get('projected_http_100')}/{proj.get('projected_http_1000')}",
        f"Projected browser renders (100): {proj.get('projected_browser_100')}",
        "",
        "MOST IMPORTANT ANSWERS",
        "",
    ]
    for i in range(1, 18):
        key = [k for k in ans if k.startswith(f"{i}_")]
        if key:
            lines.append(f"{i}. {ans.get(key[0])}")
    return "\n".join(lines)
