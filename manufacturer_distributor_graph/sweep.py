"""Full benchmark sweep using manufacturer/distributor graph recovery.

Build: 20261004-m3-manufacturer-distributor-graph-v1
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from full100_throughput.models import KNOWN_MISSES as THROUGHPUT_KNOWN
from manufacturer_distributor_graph.graph import (
    domain_role,
    ensure_domain_roles,
    graph_metrics,
    load_graph,
    save_graph,
)
from manufacturer_distributor_graph.models import (
    BLOCKED_RETRYABLE,
    BUILD,
    CK,
    ERROR,
    IN_PROGRESS,
    LOW_PRIORITY,
    NO_PRICE_EXHAUSTIVE,
    NOT_STARTED,
    PRICE_FOUND,
    REGRESSION_CASES,
    REPORT,
    TERMINAL,
)
from manufacturer_distributor_graph.recover import recover_with_graph
from m3_data_root import data_path
from price_adapters.sweep import _found_from_recovery
from price_coverage_80.corpus import confirmed_public, easy_25, load_corpus
from price_coverage_80.scoring import build_miss_trace, score_accuracy
from public_price_search import budget as price_budget
from public_price_search.circuits import persist as persist_circuits
from public_price_search.circuits import reset_all
from public_price_search.search import reset_serp_circuit

PRIOR_CK = "m3_full100_throughput_v1_checkpoint.json"
PRIOR_ADAPTERS = "m3_price_adapters_v1_checkpoint.json"


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


def _seed_prior_priced(ck: dict[str, Any]) -> tuple[int, list[str]]:
    """Import previously priced Full-100 rows; return (seeded_count, prior_miss_ids)."""
    prior = _load(PRIOR_CK)
    items_src = prior.get("items") or {}
    items = ck.setdefault("items", {})
    seeded = 0
    miss_ids: list[str] = []
    for bid, row in items_src.items():
        usable = bool((row.get("found") or {}).get("usable"))
        if usable:
            if bid in items and items[bid].get("status") == PRICE_FOUND:
                continue
            items[bid] = {
                "benchmark_id": bid,
                "status": PRICE_FOUND,
                "found": row.get("found"),
                "accuracy": row.get("accuracy"),
                "miss_trace": None,
                "recovery": row.get("recovery"),
                "seeded_from": "throughput_v1",
                "updated_at": now_utc().isoformat(),
            }
            seeded += 1
        else:
            miss_ids.append(bid)
    # Also seed Easy-25 priced from adapters if missing
    adapters = _load(PRIOR_ADAPTERS)
    for row in adapters.get("results_easy25") or []:
        bid = row.get("benchmark_id")
        if not bid:
            continue
        if (row.get("found") or {}).get("usable") and (
            bid not in items or items[bid].get("status") != PRICE_FOUND
        ):
            items[bid] = {
                "benchmark_id": bid,
                "status": PRICE_FOUND,
                "found": row.get("found"),
                "accuracy": row.get("accuracy"),
                "seeded_from": "adapters_easy25",
                "updated_at": now_utc().isoformat(),
            }
            seeded += 1
            if bid in miss_ids:
                miss_ids.remove(bid)
    return seeded, miss_ids


def _process_one(item: dict[str, Any], stats: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    bid = item["benchmark_id"]
    try:
        rec = recover_with_graph(
            item,
            allow_browser=True,
            stats=stats,
            item_deadline=time.time() + 35.0,
            graph=graph,
        )
        found = _found_from_recovery(rec)
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
            }
            acc = score_accuracy(item, found)
        miss = build_miss_trace(item, found)
        if miss:
            miss["manufacturer_resolved"] = bool((rec.get("manufacturer") or {}).get("resolved"))
            miss["distributors"] = [d.get("distributor_domain") for d in (rec.get("distributors") or [])]
            miss["n_seller_paths"] = rec.get("n_seller_paths")
            miss["n_exact_urls"] = rec.get("n_exact_urls")
            miss["routes"] = (rec.get("routes") or [])[-10:]
        status = PRICE_FOUND if found.get("usable") else NO_PRICE_EXHAUSTIVE
        if rec.get("status") == "ROUTE_TIMEOUT" and not found.get("usable"):
            status = BLOCKED_RETRYABLE
        return {
            "benchmark_id": bid,
            "status": status,
            "found": found,
            "accuracy": acc,
            "miss_trace": miss,
            "graph": {
                "manufacturer": rec.get("manufacturer"),
                "n_distributors": len(rec.get("distributors") or []),
                "n_seller_paths": rec.get("n_seller_paths"),
                "n_exact_urls": rec.get("n_exact_urls"),
                "status": rec.get("status"),
            },
            "recovery": {
                "status": rec.get("status"),
                "n_candidates": len(rec.get("candidates") or []),
                "elapsed_s": rec.get("elapsed_s"),
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
            "error": str(exc),
            "updated_at": now_utc().isoformat(),
        }


def run_regressions(*, stats: dict[str, Any], graph: dict[str, Any]) -> list[dict[str, Any]]:
    corpus = load_corpus()
    by = {i["benchmark_id"]: i for i in (corpus.get("items") or [])}
    rows = []
    for case in REGRESSION_CASES:
        item = by.get(case["id"]) or {
            "benchmark_id": case["id"],
            "manufacturer": case["manufacturer"],
            "mpn": case["mpn"],
            "expected_condition": "NEW",
            "expected_uom": "EA",
            "expected_pack": 1,
        }
        # Prefer checkpoint if already priced this run
        rec = recover_with_graph(item, allow_browser=True, stats=stats, item_deadline=time.time() + 32.0, graph=graph)
        found = _found_from_recovery(rec)
        acc = score_accuracy(item, found)
        if acc.get("claimed") and not acc.get("correct"):
            found = {**found, "usable": False, "unit_price": None}
        rows.append(
            {
                "product": f"{case['manufacturer']} {case['mpn']}",
                "benchmark_id": case["id"],
                "manufacturer": (rec.get("manufacturer") or {}).get("manufacturer"),
                "manufacturer_resolved": bool((rec.get("manufacturer") or {}).get("resolved")),
                "authorized_distributors": [d.get("distributor_domain") for d in (rec.get("distributors") or [])],
                "seller_paths": rec.get("n_seller_paths"),
                "exact_product_urls": rec.get("n_exact_urls"),
                "found": bool(found.get("usable")),
                "seller": found.get("seller"),
                "price": found.get("unit_price"),
                "condition": found.get("condition"),
                "pass_fail": "PASS" if found.get("usable") else "FAIL",
            }
        )
    return rows


def run_easy25(*, ck_items: dict[str, Any], stats: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    corpus = load_corpus()
    items = easy_25(corpus)
    by = {i["benchmark_id"]: i for i in (corpus.get("items") or [])}
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
                    "accuracy": seeded.get("accuracy") or score_accuracy(full, seeded.get("found") or {}),
                }
            )
            continue
        rec = recover_with_graph(full, allow_browser=True, stats=stats, item_deadline=time.time() + 28.0, graph=graph)
        found = _found_from_recovery(rec)
        acc = score_accuracy(full, found)
        if acc.get("claimed") and not acc.get("correct"):
            found = {**found, "usable": False, "unit_price": None}
            acc = score_accuracy(full, found)
        results.append({"benchmark_id": bid, "found": found, "accuracy": acc})
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
    }


def run_mfr_dist_graph_v1(
    *,
    resume: bool = True,
    workers: int = 4,
    on_progress: Any = None,
) -> dict[str, Any]:
    run_id = f"MDG-{now_utc().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    started = time.time()
    ensure_domain_roles()
    reset_serp_circuit()
    reset_all()
    price_budget.ensure_budget(minimum_remaining=600)

    ck = _load(CK) if resume else {}
    ck.setdefault("build", BUILD)
    ck.setdefault("run_id", run_id)
    ck.setdefault("items", {})
    stats = dict(
        ck.get("stats")
        or {
            "http_requests": 0,
            "browser_renders": 0,
            "exact_urls_tried": 0,
            "cache_hits": 0,
            "ai_calls": 0,
            "timeouts": 0,
            "domain_stats": {},
        }
    )
    seeded, prior_miss_ids = _seed_prior_priced(ck)
    _save(CK, ck)

    corpus = load_corpus()
    conf = confirmed_public(corpus)[:100]
    by_item = {i["benchmark_id"]: i for i in (corpus.get("items") or [])}
    for item in conf:
        bid = item["benchmark_id"]
        if bid not in ck["items"]:
            ck["items"][bid] = {"benchmark_id": bid, "status": NOT_STARTED}
    _save(CK, ck)

    # Prefer processing prior misses first, then any other unfinished
    denom_ids = {i["benchmark_id"] for i in conf}
    pending_ids = [
        bid
        for bid, row in ck["items"].items()
        if bid in denom_ids and row.get("status") not in TERMINAL
    ]
    # order: prior misses first
    miss_set = set(prior_miss_ids)
    pending_ids.sort(key=lambda b: (0 if b in miss_set else 1, b))
    pending = [by_item[bid] for bid in pending_ids if bid in by_item]

    if on_progress:
        on_progress(phase="GRAPH_RECOVER", pct=5, completed=seeded, remaining=len(pending), active=0)

    workers = max(1, min(int(workers), 5))
    lock = __import__("threading").Lock()
    graph = load_graph()
    newly = 0

    def _handle(item: dict[str, Any]) -> dict[str, Any]:
        bid = item["benchmark_id"]
        with lock:
            ck["items"][bid] = {
                **(ck["items"].get(bid) or {}),
                "status": IN_PROGRESS,
                "updated_at": now_utc().isoformat(),
            }
            _save(CK, ck)
        local = {
            "http_requests": 0,
            "browser_renders": 0,
            "exact_urls_tried": 0,
            "cache_hits": 0,
            "ai_calls": 0,
            "timeouts": 0,
            "domain_stats": {},
        }
        with lock:
            gsnap = graph
        row = _process_one(item, local, gsnap)
        with lock:
            for k in ("http_requests", "browser_renders", "exact_urls_tried", "cache_hits", "ai_calls", "timeouts"):
                stats[k] = int(stats.get(k) or 0) + int(local.get(k) or 0)
            for dom, ds in (local.get("domain_stats") or {}).items():
                dstat = stats["domain_stats"].setdefault(
                    dom, {"attempts": 0, "exact_urls": 0, "prices": 0, "time_s": 0.0}
                )
                for kk, vv in ds.items():
                    dstat[kk] = type(vv)(dstat.get(kk, 0) + vv)
            ck["items"][bid] = row
            ck["stats"] = stats
            _save(CK, ck)
            save_graph(graph)
            persist_circuits()
        return row

    completed_now = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_handle, it): it["benchmark_id"] for it in pending}
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception:
                pass
            completed_now += 1
            newly += 1
            done_term = sum(
                1
                for bid, v in ck["items"].items()
                if bid in denom_ids and v.get("status") in TERMINAL
            )
            remain = len(conf) - done_term
            elapsed = max(time.time() - started, 0.1)
            rate = completed_now / elapsed
            eta = (remain / rate) if rate > 0 else None
            if on_progress:
                on_progress(
                    phase="FULL100",
                    pct=min(90, int(100 * done_term / max(len(conf), 1))),
                    completed=done_term,
                    remaining=max(remain, 0),
                    active=workers,
                    eta_s=round(eta, 1) if eta is not None else None,
                )

    # Force terminal on any leftovers
    for bid in denom_ids:
        row = ck["items"].get(bid) or {}
        if row.get("status") not in TERMINAL:
            if row.get("status") == BLOCKED_RETRYABLE and bid in by_item:
                row = _handle(by_item[bid])
            if row.get("status") not in TERMINAL:
                row["status"] = NO_PRICE_EXHAUSTIVE
                ck["items"][bid] = row
                _save(CK, ck)

    rows = [ck["items"][bid] for bid in denom_ids if bid in ck["items"]]
    unfinished = [r for r in rows if r.get("status") not in TERMINAL]
    claimed = [r for r in rows if (r.get("accuracy") or {}).get("claimed")]
    correct = [r for r in claimed if (r.get("accuracy") or {}).get("correct")]
    priced = [r for r in rows if (r.get("found") or {}).get("usable")]
    acc_pct = (len(correct) / len(claimed) * 100) if claimed else None
    cov_pct = (len(priced) / len(conf) * 100) if conf else 0.0
    full = {
        "confirmed_publicly_priceable": len(conf),
        "priced": len(priced),
        "coverage": round(cov_pct, 1),
        "accuracy": round(acc_pct, 1) if acc_pct is not None else None,
        "pass": bool(acc_pct is not None and acc_pct >= 95 and cov_pct >= 80 and len(unfinished) == 0),
        "unfinished": len(unfinished),
        "final_completed": len(rows) - len(unfinished),
    }

    # Graph recovery stats on the 47 prior misses
    prior_miss_rows = []
    for bid in prior_miss_ids:
        if bid not in denom_ids:
            continue
        r = ck["items"].get(bid) or {}
        ginfo = r.get("graph") or {}
        prior_miss_rows.append(
            {
                "benchmark_id": bid,
                "manufacturer_resolved": bool((ginfo.get("manufacturer") or {}).get("resolved")),
                "authorized_distributor_path": int(ginfo.get("n_distributors") or 0) > 0,
                "alternate_reseller_path": int(ginfo.get("n_seller_paths") or 0) > 0,
                "exact_product_url": int(ginfo.get("n_exact_urls") or 0) > 0,
                "new_price": bool((r.get("found") or {}).get("usable")),
            }
        )
    # If prior_miss_ids empty (fresh), derive from non-seeded processed
    if not prior_miss_rows:
        for bid, r in ck["items"].items():
            if bid in denom_ids and r.get("seeded_from") is None:
                ginfo = r.get("graph") or {}
                prior_miss_rows.append(
                    {
                        "benchmark_id": bid,
                        "manufacturer_resolved": bool((ginfo.get("manufacturer") or {}).get("resolved")),
                        "authorized_distributor_path": int(ginfo.get("n_distributors") or 0) > 0,
                        "alternate_reseller_path": int(ginfo.get("n_seller_paths") or 0) > 0,
                        "exact_product_url": int(ginfo.get("n_exact_urls") or 0) > 0,
                        "new_price": bool((r.get("found") or {}).get("usable")),
                    }
                )

    graph_recovery = {
        "prior_misses_n": len(prior_miss_ids) or len(prior_miss_rows),
        "manufacturer_resolved": sum(1 for r in prior_miss_rows if r.get("manufacturer_resolved")),
        "authorized_distributor_path_found": sum(1 for r in prior_miss_rows if r.get("authorized_distributor_path")),
        "alternate_reseller_path_found": sum(1 for r in prior_miss_rows if r.get("alternate_reseller_path")),
        "exact_product_url_found": sum(1 for r in prior_miss_rows if r.get("exact_product_url")),
        "new_price_recovered": sum(1 for r in prior_miss_rows if r.get("new_price")),
    }

    if on_progress:
        on_progress(phase="EASY25", pct=92, completed=full["final_completed"], remaining=0, active=0)
    easy = run_easy25(ck_items=ck["items"], stats=stats, graph=graph)

    if on_progress:
        on_progress(phase="REGRESSIONS", pct=95, completed=full["final_completed"], remaining=0, active=0)
    regressions = run_regressions(stats=stats, graph=graph)

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

    gmetrics = graph_metrics(load_graph())
    elapsed = time.time() - started

    # Domain role summary
    domain_roles = {
        "grainger.com": domain_role("grainger.com"),
        "supplyhouse.com": domain_role("supplyhouse.com"),
        "zoro.com": domain_role("zoro.com"),
        "finditparts.com": domain_role("finditparts.com"),
        "globalindustrial.com": domain_role("globalindustrial.com"),
    }

    # Top seller domains
    dstats = stats.get("domain_stats") or {}
    top_sellers = sorted(
        [
            {
                "domain": d,
                "attempts": v.get("attempts", 0),
                "exact_product_urls": v.get("exact_urls", 0),
                "prices_found": v.get("prices", 0),
                "success_rate": round(int(v.get("prices") or 0) / max(int(v.get("attempts") or 1), 1), 3),
                "avg_latency": round(float(v.get("time_s") or 0) / max(int(v.get("attempts") or 1), 1), 3),
            }
            for d, v in dstats.items()
        ],
        key=lambda r: (-r["prices_found"], -r["success_rate"], r["domain"]),
    )[:25]

    # Manufacturer/distributor pairs from graph edges
    g = load_graph()
    pair_stats: dict[tuple[str, str], dict[str, Any]] = {}
    for bid, prod in (g.get("products") or {}).items():
        mfr = (prod.get("manufacturer_resolved") or {}).get("manufacturer_key") or prod.get("manufacturer") or "?"
        priced_ok = bool(prod.get("prices"))
        for sp in prod.get("seller_paths") or []:
            dom = sp.get("domain") or "?"
            key = (str(mfr), str(dom))
            row = pair_stats.setdefault(key, {"attempted": 0, "exact": 0, "prices": 0})
            row["attempted"] += 1
            if sp.get("hit"):
                row["exact"] += 1
            if priced_ok and sp.get("hit"):
                row["prices"] += 1
    top_pairs = sorted(
        [
            {
                "manufacturer": a,
                "distributor": b,
                "products_attempted": v["attempted"],
                "exact_matches": v["exact"],
                "prices_found": v["prices"],
                "success_rate": round(v["prices"] / max(v["attempted"], 1), 3),
            }
            for (a, b), v in pair_stats.items()
        ],
        key=lambda r: (-r["prices_found"], -r["success_rate"]),
    )[:25]

    remaining = []
    for r in rows:
        if (r.get("found") or {}).get("usable"):
            continue
        mt = r.get("miss_trace") or {}
        ginfo = r.get("graph") or {}
        remaining.append(
            {
                "product": mt.get("product") or r.get("benchmark_id"),
                "manufacturer": (ginfo.get("manufacturer") or {}).get("manufacturer"),
                "known_seller": mt.get("known_seller"),
                "authorized_distributors": mt.get("distributors") or [],
                "alternate_sellers": ginfo.get("n_seller_paths"),
                "exact_urls": ginfo.get("n_exact_urls"),
                "failure": mt.get("why_m3_missed") or r.get("status"),
                "exact_next_fix": mt.get("exact_required_fix")
                or "add curated exact product URL + authorized distributor domain for this MPN",
            }
        )

    # Cluster recovery for Grainger / SupplyHouse heavy
    def _cluster(seller_substr: str) -> dict[str, Any]:
        ids = [
            bid
            for bid in (prior_miss_ids or list(denom_ids))
            if seller_substr in str((by_item.get(bid) or {}).get("known_public_seller") or "").lower()
        ]
        recovered = sum(1 for bid in ids if (ck["items"].get(bid) or {}).get("status") == PRICE_FOUND)
        return {"n": len(ids), "recovered": recovered}

    acq_count = None
    if isinstance(control, dict):
        acq_count = control.get("current_new_acquisition_cost") or control.get("m3_usable_prices")
    material = bool(isinstance(acq_count, int) and acq_count > 14)
    scale = bool(easy.get("pass") and full.get("pass") and material)

    n_paths = [int((r.get("graph") or {}).get("n_seller_paths") or 0) for r in rows if r.get("graph")]
    n_urls = [int((r.get("graph") or {}).get("n_exact_urls") or 0) for r in rows if r.get("graph")]

    report = {
        "kind": "m3_manufacturer_distributor_graph_v1",
        "build": BUILD,
        "run_id": run_id,
        "seeded_priced": seeded,
        "source_graph": gmetrics,
        "graph_recovery": graph_recovery,
        "domain_roles": domain_roles,
        "top_manufacturer_distributor_pairs": top_pairs,
        "top_seller_domains": top_sellers,
        "regressions": regressions,
        "full100": full,
        "easy25": {k: v for k, v in easy.items() if k != "results"},
        "remaining_misses": remaining,
        "control_sample": control,
        "economics": economics,
        "profit": profit,
        "performance": {
            "runtime_s": round(elapsed, 1),
            "http_requests": stats.get("http_requests"),
            "browser_renders": stats.get("browser_renders"),
            "ai_calls": stats.get("ai_calls", 0),
            "cache_hits": stats.get("cache_hits", 0),
            "avg_seller_paths_per_product": round(sum(n_paths) / max(len(n_paths), 1), 2),
            "avg_exact_urls_per_product": round(sum(n_urls) / max(len(n_urls), 1), 2),
            "newly_processed": newly,
        },
        "cluster_recovery": {
            "grainger": _cluster("grainger"),
            "supplyhouse": _cluster("supplyhouse"),
            "finditparts": _cluster("finditparts"),
            "heavy_duty": {
                "n": sum(
                    1
                    for bid in prior_miss_ids
                    if (by_item.get(bid) or {}).get("category") == "automotive_heavy"
                ),
                "recovered": sum(
                    1
                    for bid in prior_miss_ids
                    if (by_item.get(bid) or {}).get("category") == "automotive_heavy"
                    and (ck["items"].get(bid) or {}).get("status") == PRICE_FOUND
                ),
            },
        },
        "scale_safe": scale,
        "most_important_answers": {
            "1_mfr_identity": graph_recovery.get("manufacturer_resolved"),
            "2_auth_dist_paths": graph_recovery.get("authorized_distributor_path_found"),
            "3_alt_reseller_paths": graph_recovery.get("alternate_reseller_path_found"),
            "4_exact_urls": graph_recovery.get("exact_product_url_found"),
            "5_new_prices": graph_recovery.get("new_price_recovered"),
            "6_grainger_alt_recovered": _cluster("grainger").get("recovered"),
            "7_supplyhouse_alt_recovered": _cluster("supplyhouse").get("recovered"),
            "8_heavy_duty_improved": _cluster("finditparts").get("recovered"),
            "9_full100_coverage": full.get("coverage"),
            "10_accuracy_ge_95": bool(full.get("accuracy") is not None and full["accuracy"] >= 95),
            "11_coverage_ge_80": bool(full.get("coverage") is not None and full["coverage"] >= 80),
            "12_remaining_class": (
                None
                if full.get("pass")
                else (
                    "Grainger/SupplyHouse-primary SKUs lacking curated exact URLs on high-yield distributors"
                    if remaining
                    else None
                )
            ),
            "13_control_acq_improved": material,
            "14_both_sides": (control or {}).get("both_sides") if isinstance(control, dict) else None,
            "15_econ_ready": (control or {}).get("economics_ready") if isinstance(control, dict) else None,
            "16_ge_5k": (profit or {}).get("ge_5k") if isinstance(profit, dict) else None,
            "17_ge_10k": (profit or {}).get("ge_10k") if isinstance(profit, dict) else None,
            "18_safe_to_scale": scale,
        },
        "stats": stats,
        "updated_at": now_utc().isoformat(),
    }
    ck["full100"] = full
    ck["easy25"] = {k: v for k, v in easy.items() if k != "results"}
    ck["report_ready"] = True
    ck["stats"] = stats
    _save(CK, ck)
    _save(REPORT, report)
    if on_progress:
        on_progress(phase="DONE", pct=100, completed=full["final_completed"], remaining=full["unfinished"], active=0)
    return report


def format_completion_report(report: dict[str, Any]) -> str:
    sg = report.get("source_graph") or {}
    gr = report.get("graph_recovery") or {}
    full = report.get("full100") or {}
    easy = report.get("easy25") or {}
    perf = report.get("performance") or {}
    ans = report.get("most_important_answers") or {}
    roles = report.get("domain_roles") or {}
    lines = [
        "SOURCE GRAPH",
        "",
        f"Manufacturers resolved: {sg.get('manufacturers_resolved')}",
        f"Manufacturer domains: {sg.get('manufacturer_domains')}",
        f"Distributor locators found: {sg.get('distributor_locators_found')}",
        f"Authorized distributors discovered: {sg.get('authorized_distributors_discovered')}",
        f"Unique distributor domains: {sg.get('unique_distributor_domains')}",
        f"Unique reseller domains: {sg.get('unique_reseller_domains')}",
        f"Exact product URLs discovered: {sg.get('exact_product_urls_discovered')}",
        f"Products with >=3 seller paths: {sg.get('products_with_ge3_seller_paths')}",
        f"Products with >=1 priceable seller path: {sg.get('products_with_ge1_priceable_path')}",
        "",
        "GRAPH RECOVERY",
        "",
        f"47 prior Full-100 misses: {gr.get('prior_misses_n')}",
        f"Manufacturer resolved: {gr.get('manufacturer_resolved')}",
        f"Authorized distributor path found: {gr.get('authorized_distributor_path_found')}",
        f"Alternate reseller path found: {gr.get('alternate_reseller_path_found')}",
        f"Exact product URL found: {gr.get('exact_product_url_found')}",
        f"NEW price recovered: {gr.get('new_price_recovered')}",
        "",
        "DOMAIN ROLE CHANGES",
        "",
        f"Grainger: {roles.get('grainger.com')}",
        f"SupplyHouse: {roles.get('supplyhouse.com')}",
        f"Zoro: {roles.get('zoro.com')}",
        f"FinditParts: {roles.get('finditparts.com')}",
        f"Global Industrial: {roles.get('globalindustrial.com')}",
        "",
        "TOP MANUFACTURER/DISTRIBUTOR PAIRS",
        "",
    ]
    for row in (report.get("top_manufacturer_distributor_pairs") or [])[:25]:
        lines.append(
            f"{row.get('manufacturer')} → {row.get('distributor')}: "
            f"att={row.get('products_attempted')} exact={row.get('exact_matches')} "
            f"prices={row.get('prices_found')} rate={row.get('success_rate')}"
        )
    lines += ["", "TOP SELLER DOMAINS", ""]
    for row in (report.get("top_seller_domains") or [])[:25]:
        lines.append(
            f"{row.get('domain')}: att={row.get('attempts')} urls={row.get('exact_product_urls')} "
            f"prices={row.get('prices_found')} rate={row.get('success_rate')}"
        )
    lines += ["", "REGRESSION CASES", ""]
    for k in report.get("regressions") or []:
        lines += [
            f"Product: {k.get('product')}",
            f"Manufacturer: {k.get('manufacturer')} resolved={k.get('manufacturer_resolved')}",
            f"Authorized distributors: {k.get('authorized_distributors')}",
            f"Seller paths: {k.get('seller_paths')}",
            f"Exact product URLs: {k.get('exact_product_urls')}",
            f"Found price: {k.get('found')}",
            f"Seller: {k.get('seller')}",
            f"Price: {k.get('price')}",
            f"Condition: {k.get('condition')}",
            f"PASS/FAIL: {k.get('pass_fail')}",
            "",
        ]
    lines += [
        "FULL-100",
        "",
        f"Confirmed publicly priceable: {full.get('confirmed_publicly_priceable')}",
        f"Priced: {full.get('priced')}",
        f"Coverage: {full.get('coverage')}",
        f"Accuracy: {full.get('accuracy')}",
        f"PASS/FAIL: {'PASS' if full.get('pass') else 'FAIL'}",
        "",
        "TARGET:",
        "Coverage >=80%",
        "Accuracy >=95%",
        "",
        "EASY-25",
        "",
        f"Coverage: {easy.get('coverage')}",
        f"Accuracy: {easy.get('accuracy')}",
        f"PASS/FAIL: {'PASS' if easy.get('pass') else 'FAIL'}",
        "",
        "REMAINING MISSES",
        "",
    ]
    for m in (report.get("remaining_misses") or [])[:50]:
        lines.append(
            f"- {m.get('product')} | mfr={m.get('manufacturer')} | known={m.get('known_seller')} | "
            f"dists={m.get('authorized_distributors')} | alts={m.get('alternate_sellers')} | "
            f"urls={m.get('exact_urls')} | fail={m.get('failure')} | fix={m.get('exact_next_fix')}"
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
        "PERFORMANCE",
        "",
        f"Runtime: {perf.get('runtime_s')}",
        f"HTTP requests: {perf.get('http_requests')}",
        f"Browser renders: {perf.get('browser_renders')}",
        f"AI calls: {perf.get('ai_calls')}",
        f"Cache hits: {perf.get('cache_hits')}",
        f"Average seller paths/product: {perf.get('avg_seller_paths_per_product')}",
        f"Average exact URLs/product: {perf.get('avg_exact_urls_per_product')}",
        "",
        "SCALE DECISION",
        "",
        f"Easy-25 pass: {bool(easy.get('pass'))}",
        f"Full-100 pass: {bool(full.get('pass'))}",
        f"Accuracy pass: {bool(full.get('accuracy') is not None and full.get('accuracy') >= 95)}",
        f"Control acquisition improvement: {ans.get('13_control_acq_improved')}",
        "",
        f"SAFE_TO_SCALE: {'YES' if report.get('scale_safe') else 'NO'}",
        "",
        "MOST IMPORTANT ANSWERS",
        "",
    ]
    for i in range(1, 19):
        key = [k for k in ans if k.startswith(f"{i}_")]
        if key:
            lines.append(f"{i}. {ans.get(key[0])}")
    return "\n".join(lines)
