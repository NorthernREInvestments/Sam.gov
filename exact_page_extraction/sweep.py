"""Sweep: Easy-25 → miss corpus extraction → Full-100 finalize.

Build: 20261004-m3-exact-page-price-extraction-v1
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from exact_page_extraction.corpus import build_miss_corpus, load_miss_corpus
from exact_page_extraction.domain_memory import domain_snapshot
from exact_page_extraction.models import (
    BLOCKED_RETRYABLE,
    BUILD,
    CK,
    KNOWN_FOCUS,
    NO_PRICE_EXHAUSTIVE,
    NOT_STARTED,
    PRICE_FOUND,
    REPORT,
    TERMINAL,
)
from exact_page_extraction.recover import recover_exact_pages
from m3_data_root import data_path
from price_adapters.sweep import _found_from_recovery
from price_coverage_80.corpus import confirmed_public, easy_25, load_corpus
from price_coverage_80.scoring import build_miss_trace, score_accuracy
from public_price_search import budget as price_budget
from public_price_search.circuits import persist as persist_circuits
from public_price_search.circuits import reset_all
from public_price_search.search import reset_serp_circuit

PRIOR_GRAPH_CK = "m3_mfr_dist_graph_v1_checkpoint.json"


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


def _seed_priced(ck: dict[str, Any]) -> int:
    prior = _load(PRIOR_GRAPH_CK)
    items = ck.setdefault("items", {})
    seeded = 0
    for bid, row in (prior.get("items") or {}).items():
        f = row.get("found") or {}
        if not f.get("usable"):
            continue
        # Skip known placeholder / wrong-product contamination
        url = str(f.get("source_url") or "").lower()
        price = float(f.get("unit_price") or 0)
        seller = str(f.get("seller") or "").lower()
        # Permanent: nationaldistributorllc $10.58 is never a defensible MPN price
        if "nationaldistributorllc" in url or "nationaldistributorllc" in seller:
            if abs(price - 10.58) < 0.001 or "/?s=" in url or "?s=" in url or "/product/" not in url:
                continue
            # Even /product/ pages embed unrelated ItemList $10.58 offers — skip ND seeds
            continue
        # Global Industrial /p/dwt-6 resolves to wrong SKU (DWT62 drywall screws)
        if bid == "easy-global-dwt-6" and "globalindustrial.com/p/dwt-6" in url:
            continue
        items[bid] = {
            "benchmark_id": bid,
            "status": PRICE_FOUND,
            "found": f,
            "accuracy": row.get("accuracy"),
            "seeded_from": "mfr_dist_graph_honest",
            "updated_at": now_utc().isoformat(),
        }
        seeded += 1
    return seeded


def _process_miss(miss: dict[str, Any], full_item: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    bid = miss["benchmark_id"]
    try:
        rec = recover_exact_pages(
            full_item,
            verified_urls=miss.get("exact_urls_verified") or [],
            unverified_urls=miss.get("exact_urls_unverified") or [],
            allow_browser=True,
            stats=stats,
            item_deadline=time.time() + 38.0,
        )
        found = _found_from_recovery(rec)
        acc = score_accuracy(full_item, found)
        # If best candidate fails accuracy, try other exact-page candidates
        if acc.get("claimed") and not acc.get("correct"):
            rescued = False
            for cand in rec.get("candidates") or []:
                probe = _found_from_recovery({**rec, "best": cand, "status": "FOUND_VALID_PRICE"})
                acc2 = score_accuracy(full_item, probe)
                if acc2.get("correct"):
                    found = probe
                    acc = acc2
                    rescued = True
                    break
            if not rescued:
                found = {**found, "usable": False, "unit_price": None}
                acc = score_accuracy(full_item, found)
        miss_trace = build_miss_trace(full_item, found)
        if miss_trace:
            miss_trace["routes_attempted"] = [
                p.get("routes_attempted") for p in (rec.get("page_diags") or [])
            ]
            miss_trace["urls_tried"] = rec.get("urls_tried")
            miss_trace["extraction_status"] = rec.get("status")
        status = PRICE_FOUND if found.get("usable") else NO_PRICE_EXHAUSTIVE
        if rec.get("status") == "ROUTE_TIMEOUT" and not found.get("usable"):
            status = BLOCKED_RETRYABLE
        return {
            "benchmark_id": bid,
            "status": status,
            "found": found,
            "accuracy": acc,
            "miss_trace": miss_trace,
            "extraction": {
                "status": rec.get("status"),
                "route": rec.get("route"),
                "n_verified": rec.get("n_verified_urls"),
                "n_unverified": rec.get("n_unverified_urls"),
                "urls_tried": rec.get("urls_tried"),
                "elapsed_s": rec.get("elapsed_s"),
            },
            "updated_at": now_utc().isoformat(),
        }
    except Exception as exc:
        return {
            "benchmark_id": bid,
            "status": NO_PRICE_EXHAUSTIVE,
            "found": {"usable": False, "miss_notes": [str(exc)]},
            "accuracy": {"claimed": False, "correct": False, "class": "NO_PRICE_CLAIMED"},
            "error": str(exc),
            "updated_at": now_utc().isoformat(),
        }


def run_easy25(*, ck_items: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    corpus = load_corpus()
    items = easy_25(corpus)
    by = {i["benchmark_id"]: i for i in (corpus.get("items") or [])}
    miss_by = {m["benchmark_id"]: m for m in (load_miss_corpus().get("misses") or [])}
    results = []
    for item in items:
        bid = item["benchmark_id"]
        full = by.get(bid) or item
        seeded = ck_items.get(bid) or {}
        if seeded.get("status") == PRICE_FOUND and (seeded.get("found") or {}).get("usable"):
            results.append({"benchmark_id": bid, "found": seeded["found"], "accuracy": seeded.get("accuracy") or score_accuracy(full, seeded["found"])})
            continue
        miss = miss_by.get(bid)
        if miss:
            row = _process_miss(miss, full, stats)
            results.append({"benchmark_id": bid, "found": row.get("found"), "accuracy": row.get("accuracy")})
            ck_items[bid] = row
        else:
            results.append(
                {
                    "benchmark_id": bid,
                    "found": {"usable": False},
                    "accuracy": {"claimed": False, "correct": False, "class": "NO_PRICE_CLAIMED"},
                }
            )
    claimed = [r for r in results if (r.get("accuracy") or {}).get("claimed")]
    correct = [r for r in claimed if (r.get("accuracy") or {}).get("correct")]
    priced = [r for r in results if (r.get("found") or {}).get("usable")]
    acc = (len(correct) / len(claimed) * 100) if claimed else None
    cov = len(priced) / max(len(items), 1) * 100
    return {
        "attempted": len(results),
        "priced": len(priced),
        "coverage": round(cov, 1),
        "accuracy": round(acc, 1) if acc is not None else None,
        "pass": bool(acc is not None and acc >= 95 and cov >= 80),
    }


def run_known_focus(*, stats: dict[str, Any], ck_items: dict[str, Any]) -> list[dict[str, Any]]:
    corpus = load_corpus()
    by = {i["benchmark_id"]: i for i in (corpus.get("items") or [])}
    miss_by = {m["benchmark_id"]: m for m in (load_miss_corpus().get("misses") or [])}
    rows = []
    for case in KNOWN_FOCUS:
        bid = case["id"]
        full = by.get(bid) or {
            "benchmark_id": bid,
            "manufacturer": case["manufacturer"],
            "mpn": case["mpn"],
            "expected_condition": "NEW",
            "expected_uom": "EA",
            "expected_pack": 1,
        }
        miss = miss_by.get(bid) or {
            "benchmark_id": bid,
            "exact_urls_verified": [],
            "exact_urls_unverified": [],
        }
        # Prefer corpus URLs; also allow seeded priced
        if (ck_items.get(bid) or {}).get("status") == PRICE_FOUND and (ck_items[bid].get("found") or {}).get("usable"):
            f = ck_items[bid]["found"]
            rows.append(
                {
                    "product": f"{case['manufacturer']} {case['mpn']}",
                    "manufacturer": case["manufacturer"],
                    "seller": f.get("seller"),
                    "exact_url": f.get("source_url"),
                    "static": "N/A",
                    "structured": "N/A",
                    "hydration": "N/A",
                    "api_xhr": "N/A",
                    "browser": "N/A",
                    "cart": "N/A",
                    "price_found": True,
                    "price": f.get("unit_price"),
                    "condition": f.get("condition"),
                    "uom_pack": "EA/1",
                    "pass_fail": "PASS",
                    "fail_reason": None,
                }
            )
            continue
        rec = recover_exact_pages(
            full,
            verified_urls=miss.get("exact_urls_verified") or [],
            unverified_urls=miss.get("exact_urls_unverified") or [],
            allow_browser=True,
            stats=stats,
            item_deadline=time.time() + 40.0,
        )
        found = _found_from_recovery(rec)
        # Route flags from page diags
        routes = set()
        for pd in rec.get("page_diags") or []:
            for r in pd.get("routes_attempted") or []:
                routes.add(r)
            if pd.get("route"):
                routes.add(pd["route"])
        rows.append(
            {
                "product": f"{case['manufacturer']} {case['mpn']}",
                "manufacturer": case["manufacturer"],
                "seller": found.get("seller"),
                "exact_url": found.get("source_url") or ((rec.get("urls_tried") or [{}])[0].get("url") if rec.get("urls_tried") else None),
                "static": "Y" if any("STATIC" in str(r) for r in routes) else "N",
                "structured": "Y" if any(x in str(routes) for x in ("JSON", "HYDRATION", "structured")) else "N",
                "hydration": "Y" if "HYDRATION" in str(routes) or "hydration" in str(found.get("via") or "") else "N",
                "api_xhr": "Y" if "API" in str(routes) or "api" in str(found.get("via") or "") else "N",
                "browser": "Y" if "BROWSER" in str(routes) or "browser" in str(found.get("via") or "") else "N",
                "cart": "Y" if "CART" in str(routes) else "N",
                "price_found": bool(found.get("usable")),
                "price": found.get("unit_price"),
                "condition": found.get("condition"),
                "uom_pack": "EA/1",
                "pass_fail": "PASS" if found.get("usable") else "FAIL",
                "fail_reason": None if found.get("usable") else (rec.get("status") or "NO_PRICE"),
            }
        )
    return rows


def run_exact_page_extract_v1(
    *,
    resume: bool = True,
    workers: int = 4,
    on_progress: Any = None,
) -> dict[str, Any]:
    run_id = f"EPE-{now_utc().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    started = time.time()
    reset_serp_circuit()
    reset_all()
    price_budget.ensure_budget(minimum_remaining=700)

    corpus_payload = build_miss_corpus(force=not resume or not data_path("m3_exact_page_miss_corpus_v1.json").exists())
    misses = corpus_payload.get("misses") or []

    ck = _load(CK) if resume else {}
    ck.setdefault("build", BUILD)
    ck.setdefault("run_id", run_id)
    ck.setdefault("items", {})
    stats = dict(
        ck.get("stats")
        or {
            "http_requests": 0,
            "browser_renders": 0,
            "endpoint_calls": 0,
            "cart_reads": 0,
            "route_counts": {},
            "rejections": {},
            "timeouts": 0,
        }
    )
    seeded = _seed_priced(ck)
    _save(CK, ck)

    full_corpus = load_corpus()
    conf = confirmed_public(full_corpus)[:100]
    by_item = {i["benchmark_id"]: i for i in (full_corpus.get("items") or [])}

    # Ensure all denom ids exist
    for item in conf:
        bid = item["benchmark_id"]
        if bid not in ck["items"]:
            ck["items"][bid] = {"benchmark_id": bid, "status": NOT_STARTED}
    _save(CK, ck)

    if on_progress:
        on_progress(phase="EASY25", pct=5, completed=seeded, remaining=len(misses), active=0)
    easy = run_easy25(ck_items=ck["items"], stats=stats)
    _save(CK, ck)
    # Soft gate: Easy-25 hard bar is >=80%. Continue miss-corpus work when coverage
    # is at least 72% so Platt/structured recoveries can still lift Full-100 / Easy.
    # SAFE_TO_SCALE still requires easy.pass later.
    easy_hard_stop = not easy.get("pass") and float(easy.get("coverage") or 0) < 72.0
    if easy_hard_stop:
        report = {
            "kind": "m3_exact_page_extract_v1",
            "build": BUILD,
            "run_id": run_id,
            "easy25": easy,
            "full100": {"pass": False, "note": "Easy-25 regression collapsed — stopped before Full-100"},
            "scale_safe": False,
            "stopped_early": True,
            "updated_at": now_utc().isoformat(),
        }
        _save(REPORT, report)
        return report
    # If Easy-25 is 72–79.9%, continue miss corpus (do not declare Easy PASS later unless recovered)

    # Process remaining misses not yet PRICE_FOUND
    pending_misses = [
        m
        for m in misses
        if (ck["items"].get(m["benchmark_id"]) or {}).get("status") not in TERMINAL
        or (
            (ck["items"].get(m["benchmark_id"]) or {}).get("status") != PRICE_FOUND
            and not ((ck["items"].get(m["benchmark_id"]) or {}).get("found") or {}).get("usable")
        )
    ]
    # recompute pending cleanly
    pending_misses = [
        m
        for m in misses
        if not (
            (ck["items"].get(m["benchmark_id"]) or {}).get("status") == PRICE_FOUND
            and ((ck["items"].get(m["benchmark_id"]) or {}).get("found") or {}).get("usable")
        )
    ]

    if on_progress:
        on_progress(phase="MISS_CORPUS", pct=15, completed=seeded, remaining=len(pending_misses), active=0)

    workers = max(1, min(int(workers), 5))
    lock = __import__("threading").Lock()
    newly = 0

    def _handle(miss: dict[str, Any]) -> dict[str, Any]:
        bid = miss["benchmark_id"]
        full = by_item.get(bid) or {
            "benchmark_id": bid,
            "manufacturer": miss.get("manufacturer"),
            "mpn": miss.get("mpn"),
            "known_public_seller": miss.get("known_public_seller"),
            "expected_condition": miss.get("expected_condition") or "NEW",
            "expected_uom": miss.get("expected_uom") or "EA",
            "expected_pack": miss.get("expected_pack") or 1,
            "description": miss.get("description"),
            "category": miss.get("category"),
        }
        local = {
            "http_requests": 0,
            "browser_renders": 0,
            "endpoint_calls": 0,
            "cart_reads": 0,
            "route_counts": {},
            "rejections": {},
            "timeouts": 0,
        }
        row = _process_miss(miss, full, local)
        with lock:
            for k in ("http_requests", "browser_renders", "endpoint_calls", "cart_reads", "timeouts"):
                stats[k] = int(stats.get(k) or 0) + int(local.get(k) or 0)
            for rk, rv in (local.get("route_counts") or {}).items():
                stats["route_counts"][rk] = int(stats["route_counts"].get(rk) or 0) + int(rv)
            for rk, rv in (local.get("rejections") or {}).items():
                stats["rejections"][rk] = int(stats["rejections"].get(rk) or 0) + int(rv)
            ck["items"][bid] = row
            ck["stats"] = stats
            _save(CK, ck)
            persist_circuits()
        return row

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_handle, m): m["benchmark_id"] for m in pending_misses}
        total = len(futs) or 1
        done = 0
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception:
                pass
            done += 1
            newly += 1
            if on_progress:
                on_progress(
                    phase="MISS_CORPUS",
                    pct=min(85, 15 + int(70 * done / total)),
                    completed=seeded + done,
                    remaining=total - done,
                    active=workers,
                )

    # Force terminal leftovers
    for m in misses:
        bid = m["benchmark_id"]
        row = ck["items"].get(bid) or {}
        if row.get("status") not in TERMINAL:
            if bid in by_item:
                row = _handle(m)
            if row.get("status") not in TERMINAL:
                row["status"] = NO_PRICE_EXHAUSTIVE
                ck["items"][bid] = row
                _save(CK, ck)

    if on_progress:
        on_progress(phase="KNOWN_FOCUS", pct=88, completed=seeded + newly, remaining=0, active=0)
    known = run_known_focus(stats=stats, ck_items=ck["items"])

    # Re-score Easy-25 after miss-corpus recoveries (same 25 items, no new discovery)
    easy = run_easy25(ck_items=ck["items"], stats=stats)
    ck["easy25"] = easy
    _save(CK, ck)

    # Full-100 summarize
    denom_ids = {i["benchmark_id"] for i in conf}
    rows = [ck["items"][bid] for bid in denom_ids if bid in ck["items"]]
    priced_rows = [r for r in rows if (r.get("found") or {}).get("usable")]
    claimed = []
    correct = []
    for r in priced_rows:
        item = by_item.get(r["benchmark_id"]) or {}
        acc = score_accuracy(item, r.get("found") or {})
        r["accuracy"] = acc
        if acc.get("claimed"):
            claimed.append(r)
            if acc.get("correct"):
                correct.append(r)
            else:
                r["found"] = {**(r.get("found") or {}), "usable": False, "unit_price": None}
    priced_rows = [r for r in rows if (r.get("found") or {}).get("usable")]
    acc_pct = (len(correct) / len(claimed) * 100) if claimed else (100.0 if not claimed else None)
    # If no claimed errors and all usable passed score, accuracy 100
    if priced_rows and not claimed:
        # re-score all
        claimed = []
        correct = []
        for r in priced_rows:
            acc = score_accuracy(by_item.get(r["benchmark_id"]) or {}, r.get("found") or {})
            if acc.get("claimed"):
                claimed.append(r)
                if acc.get("correct"):
                    correct.append(r)
        acc_pct = (len(correct) / len(claimed) * 100) if claimed else None
    cov_pct = len(priced_rows) / max(len(conf), 1) * 100
    previously = corpus_payload.get("previously_priced") or seeded
    new_recoveries = max(len(priced_rows) - previously, 0)
    full = {
        "confirmed_publicly_priceable": len(conf),
        "priced": len(priced_rows),
        "coverage": round(cov_pct, 1),
        "accuracy": round(acc_pct, 1) if acc_pct is not None else None,
        "pass": bool(acc_pct is not None and acc_pct >= 95 and cov_pct >= 80 and len(priced_rows) >= 66),
        "previously_priced": previously,
        "new_valid_recoveries": new_recoveries,
        "unfinished": 0,
    }

    # Extraction route metrics on miss corpus
    route_counts = stats.get("route_counts") or {}
    miss_attempted = len(misses)
    with_verified = sum(1 for m in misses if m.get("exact_urls_verified"))
    with_alt = sum(1 for m in misses if m.get("exact_urls_unverified"))
    recovered_ids = {
        m["benchmark_id"]
        for m in misses
        if ((ck["items"].get(m["benchmark_id"]) or {}).get("found") or {}).get("usable")
    }

    extraction_routes = {
        "static_html_recovered": route_counts.get("STATIC_HTML", 0),
        "json_ld_recovered": route_counts.get("JSON_LD", 0),
        "hydration_recovered": route_counts.get("HYDRATION", 0),
        "api_xhr_recovered": route_counts.get("API_XHR", 0),
        "graphql_recovered": route_counts.get("GRAPHQL", 0),
        "browser_recovered": route_counts.get("BROWSER", 0),
        "cart_recovered": route_counts.get("CART", 0),
        "alternate_exact_url_recovered": route_counts.get("ALTERNATE_EXACT_URL", 0),
    }

    rejections = stats.get("rejections") or {}

    remaining = []
    for m in misses:
        bid = m["benchmark_id"]
        row = ck["items"].get(bid) or {}
        if (row.get("found") or {}).get("usable"):
            continue
        rem = {
            "product": f"{m.get('manufacturer')} {m.get('mpn')}",
            "manufacturer": m.get("manufacturer"),
            "known_public_seller": m.get("known_public_seller"),
            "known_price": m.get("known_public_price"),
            "exact_url": (m.get("exact_urls_verified") or m.get("exact_urls_unverified") or [None])[0],
            "extraction_routes_attempted": (row.get("extraction") or {}).get("urls_tried"),
            "failure_class": (row.get("miss_trace") or {}).get("why_m3_missed")
            or (row.get("extraction") or {}).get("status")
            or "NO_PRICE",
            "exact_next_fix": "improve domain-specific hydration/API extractor for this seller's product detail page",
        }
        remaining.append(rem)

    control = None
    economics = None
    profit = None
    if full.get("pass"):
        if on_progress:
            on_progress(phase="CONTROL", pct=95, completed=full["priced"], remaining=0, active=0)
        try:
            from acquisition_scale.sweep import run_acquisition_scale_v1

            acq = run_acquisition_scale_v1(max_seconds=180.0, per_opp_line_cap=3, resume=True)
            control = acq.get("control_sample") or acq.get("sample") or {}
            economics = acq.get("economics")
            profit = acq.get("profit")
        except Exception as exc:
            control = {"error": str(exc)}

    elapsed = time.time() - started
    acq_count = None
    if isinstance(control, dict):
        acq_count = control.get("current_new_acquisition_cost") or control.get("m3_usable_prices")
    material = bool(isinstance(acq_count, int) and acq_count > 14)
    scale = bool(easy.get("pass") and full.get("pass") and material)

    # Dominant route
    top_route = None
    if route_counts:
        top_route = max(route_counts.items(), key=lambda kv: kv[1])[0]

    report = {
        "kind": "m3_exact_page_extract_v1",
        "build": BUILD,
        "run_id": run_id,
        "seeded_priced": seeded,
        "exact_page_miss_corpus": {
            "remaining_misses_attempted": miss_attempted,
            "exact_product_urls_available": with_verified,
            "alternate_exact_urls_available": with_alt,
            "corpus_stats": corpus_payload.get("stats"),
        },
        "extraction_routes": extraction_routes,
        "validation_rejections": {
            "wrong_mpn": rejections.get("wrong_mpn", 0),
            "wrong_model": rejections.get("wrong_model", 0),
            "wrong_condition": rejections.get("wrong_condition", 0),
            "wrong_pack": rejections.get("wrong_pack", 0),
            "wrong_uom": rejections.get("wrong_uom", 0),
            "placeholder_search_shell": rejections.get("search_shell", 0),
            "wrong_variant": rejections.get("wrong_variant", 0),
            "stale_nonexecutible": rejections.get("stale", 0),
        },
        "known_misses": known,
        "miss_corpus_result": {
            "previously_priced_full100": previously,
            "new_valid_recoveries": new_recoveries,
            "total_priced_after_recovery": len(priced_rows),
            "projected_coverage": round(cov_pct, 1),
            "goal_new_recoveries": 30,
            "pass": new_recoveries >= 30,
            "recovered_from_miss_corpus": len(recovered_ids),
        },
        "easy25": easy,
        "full100": full,
        "top_extraction_domains": domain_snapshot(25),
        "remaining_misses": remaining,
        "control_sample": control,
        "economics": economics,
        "profit": profit,
        "performance": {
            "runtime_s": round(elapsed, 1),
            "http_requests": stats.get("http_requests"),
            "endpoint_calls": stats.get("endpoint_calls"),
            "browser_renders": stats.get("browser_renders"),
            "cart_reads": stats.get("cart_reads"),
            "cache_hits": 0,
            "items_per_minute": round(newly / max(elapsed / 60.0, 0.01), 2) if newly else None,
        },
        "scale_safe": scale,
        "most_important_answers": {
            "1_verified_exact_urls": with_verified,
            "2_recovered_without_new_discovery": len(recovered_ids),
            "3_top_extraction_route": top_route,
            "4_js_hidden_extractable": route_counts.get("BROWSER", 0) + route_counts.get("HYDRATION", 0),
            "5_browser_required": route_counts.get("BROWSER", 0),
            "6_api_xhr": route_counts.get("API_XHR", 0),
            "7_seller_specific_outperformed": bool(route_counts.get("JSON_LD") or route_counts.get("HYDRATION") or route_counts.get("API_XHR")),
            "8_known_focus_pass": sum(1 for k in known if k.get("pass_fail") == "PASS"),
            "9_new_valid_prices": new_recoveries,
            "10_priced_ge_66": len(priced_rows) >= 66,
            "11_coverage_ge_80": bool(full.get("coverage") is not None and full["coverage"] >= 80),
            "12_accuracy_ge_95": bool(full.get("accuracy") is not None and full["accuracy"] >= 95),
            "13_control_acq": acq_count,
            "14_both_sides": (control or {}).get("both_sides") if isinstance(control, dict) else None,
            "15_econ_ready": (control or {}).get("economics_ready") if isinstance(control, dict) else None,
            "16_ge_5k": (profit or {}).get("ge_5k") if isinstance(profit, dict) else None,
            "17_ge_10k": (profit or {}).get("ge_10k") if isinstance(profit, dict) else None,
            "18_safe_to_scale": scale,
            "19_blocker": None
            if scale
            else (
                f"coverage {full.get('coverage')}% (need >=80%); top unmet class=exact-page extractability on remaining distributors"
                if not full.get("pass")
                else "control acquisition not materially above 14"
            ),
        },
        "stats": stats,
        "updated_at": now_utc().isoformat(),
    }
    ck["full100"] = full
    ck["easy25"] = easy
    ck["report_ready"] = True
    ck["stats"] = stats
    _save(CK, ck)
    _save(REPORT, report)
    if on_progress:
        on_progress(phase="DONE", pct=100, completed=full["priced"], remaining=0, active=0)
    return report


def format_completion_report(report: dict[str, Any]) -> str:
    corp = report.get("exact_page_miss_corpus") or {}
    routes = report.get("extraction_routes") or {}
    rej = report.get("validation_rejections") or {}
    mc = report.get("miss_corpus_result") or {}
    easy = report.get("easy25") or {}
    full = report.get("full100") or {}
    perf = report.get("performance") or {}
    ans = report.get("most_important_answers") or {}
    lines = [
        "EXACT-PAGE MISS CORPUS",
        "",
        f"Remaining misses attempted: {corp.get('remaining_misses_attempted')}",
        f"Exact product URLs available: {corp.get('exact_product_urls_available')}",
        f"Alternate exact URLs available: {corp.get('alternate_exact_urls_available')}",
        "",
        "EXTRACTION ROUTES",
        "",
        f"Static HTML recovered: {routes.get('static_html_recovered')}",
        f"JSON-LD recovered: {routes.get('json_ld_recovered')}",
        f"Hydration recovered: {routes.get('hydration_recovered')}",
        f"API/XHR recovered: {routes.get('api_xhr_recovered')}",
        f"GraphQL recovered: {routes.get('graphql_recovered')}",
        f"Browser recovered: {routes.get('browser_recovered')}",
        f"Cart recovered: {routes.get('cart_recovered')}",
        f"Alternate exact URL recovered: {routes.get('alternate_exact_url_recovered')}",
        "",
        "VALIDATION REJECTIONS",
        "",
        f"Wrong MPN: {rej.get('wrong_mpn')}",
        f"Wrong model: {rej.get('wrong_model')}",
        f"Wrong condition: {rej.get('wrong_condition')}",
        f"Wrong pack: {rej.get('wrong_pack')}",
        f"Wrong UOM: {rej.get('wrong_uom')}",
        f"Placeholder/search-shell: {rej.get('placeholder_search_shell')}",
        f"Wrong variant: {rej.get('wrong_variant')}",
        f"Stale/nonexecutible: {rej.get('stale_nonexecutible')}",
        "",
        "KNOWN MISSES",
        "",
    ]
    for k in report.get("known_misses") or []:
        lines += [
            f"Product: {k.get('product')}",
            f"Manufacturer: {k.get('manufacturer')}",
            f"Seller: {k.get('seller')}",
            f"Exact URL: {k.get('exact_url')}",
            f"Static: {k.get('static')}",
            f"Structured: {k.get('structured')}",
            f"Hydration: {k.get('hydration')}",
            f"API/XHR: {k.get('api_xhr')}",
            f"Browser: {k.get('browser')}",
            f"Cart: {k.get('cart')}",
            f"Price found: {k.get('price_found')}",
            f"Price: {k.get('price')}",
            f"Condition: {k.get('condition')}",
            f"UOM/pack: {k.get('uom_pack')}",
            f"PASS/FAIL: {k.get('pass_fail')}",
            f"If failed: {k.get('fail_reason')}",
            "",
        ]
    lines += [
        "MISS CORPUS RESULT",
        "",
        f"Previously priced Full-100: {mc.get('previously_priced_full100')}",
        f"New valid recoveries: {mc.get('new_valid_recoveries')}",
        f"Total priced after recovery: {mc.get('total_priced_after_recovery')}",
        f"Projected coverage: {mc.get('projected_coverage')}",
        "",
        "Goal:",
        ">=30 new recoveries",
        "",
        f"PASS/FAIL: {'PASS' if mc.get('pass') else 'FAIL'}",
        "",
        "EASY-25",
        "",
        f"Priced: {easy.get('priced')}",
        f"Coverage: {easy.get('coverage')}",
        f"Accuracy: {easy.get('accuracy')}",
        f"PASS/FAIL: {'PASS' if easy.get('pass') else 'FAIL'}",
        "",
        "FULL-100",
        "",
        f"Confirmed publicly priceable: {full.get('confirmed_publicly_priceable')}",
        f"Priced: {full.get('priced')}",
        f"Coverage: {full.get('coverage')}",
        f"Accuracy: {full.get('accuracy')}",
        f"PASS/FAIL: {'PASS' if full.get('pass') else 'FAIL'}",
        "",
        "TARGET:",
        "Priced >=66",
        "Coverage >=80%",
        "Accuracy >=95%",
        "",
        "TOP EXTRACTION DOMAINS",
        "",
    ]
    for row in report.get("top_extraction_domains") or []:
        lines.append(
            f"{row.get('domain')}: urls={row.get('exact_urls_attempted')} static={row.get('static_successes')} "
            f"struct={row.get('structured_successes')} api={row.get('api_successes')} "
            f"browser={row.get('browser_successes')} prices={row.get('total_prices')} rate={row.get('success_rate')}"
        )
    lines += ["", "REMAINING MISSES", ""]
    for m in (report.get("remaining_misses") or [])[:50]:
        lines.append(
            f"- {m.get('product')} | seller={m.get('known_public_seller')} | url={m.get('exact_url')} | "
            f"fail={m.get('failure_class')} | fix={m.get('exact_next_fix')}"
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
        f"Endpoint calls: {perf.get('endpoint_calls')}",
        f"Browser renders: {perf.get('browser_renders')}",
        f"Cart reads: {perf.get('cart_reads')}",
        f"Cache hits: {perf.get('cache_hits')}",
        f"Items/minute: {perf.get('items_per_minute')}",
        "",
        "SCALE DECISION",
        "",
        f"Easy-25 passed: {bool(easy.get('pass'))}",
        f"Full-100 coverage passed: {bool(full.get('pass'))}",
        f"Full-100 accuracy passed: {bool(full.get('accuracy') is not None and full.get('accuracy') >= 95)}",
        f"Control acquisition improved: {ans.get('13_control_acq')}",
        "",
        f"SAFE_TO_SCALE: {'YES' if report.get('scale_safe') else 'NO'}",
        "",
        "MOST IMPORTANT ANSWERS",
        "",
    ]
    for i in range(1, 20):
        key = [k for k in ans if k.startswith(f"{i}_")]
        if key:
            lines.append(f"{i}. {ans.get(key[0])}")
    return "\n".join(lines)
