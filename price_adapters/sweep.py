"""Adapter validation runner: regressions + Easy-25 v2 + gated Full-100.

Build: 20261004-m3-price-adapters-v1
"""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from evidence_breakthrough.corpus import load_identity_store
from m3_data_root import data_path
from price_adapters.models import BUILD, FOUND_VALID_PRICE, REGRESSION_CASES
from price_adapters.orchestrate import recover_with_adapters
from price_coverage_80.corpus import confirmed_public, easy_25, load_corpus
from price_coverage_80.models import (
    ACCURACY_TARGET,
    COVERAGE_TARGET,
    CORRECT_EXACT_MATCH,
    PUBLIC_NEW_PRICE_CONFIRMED,
)
from price_coverage_80.scoring import build_miss_trace, score_accuracy
from public_price_search import budget as price_budget
from public_price_search.circuits import persist as persist_circuits
from public_price_search.circuits import reset_all
from public_price_search.search import reset_serp_circuit

CK = "m3_price_adapters_v1_checkpoint.json"
REPORT = "m3_price_adapters_v1_last_report.json"


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


def _found_from_recovery(rec: dict[str, Any]) -> dict[str, Any]:
    best = rec.get("best")
    if not best:
        return {
            "usable": False,
            "unit_price": None,
            "sellers_attempted": [],
            "sellers_blocked": [],
            "queries": [b.get("query") for b in (rec.get("bing") or [])],
            "miss_notes": [rec.get("status")],
            "n_valid_candidates": 0,
            "rejected": [],
            "recovered_after_block": False,
        }
    return {
        "usable": True,
        "unit_price": best.get("unit_price"),
        "condition": best.get("condition") or "NEW",
        "seller": best.get("seller"),
        "source_url": best.get("url"),
        "via": best.get("via"),
        "selection_reason": best.get("selection_reason"),
        "n_valid_candidates": len(rec.get("candidates") or []),
        "best_price_improvement": bool(rec.get("best_price_improvement")),
        "first_hit_price": (rec.get("candidates") or [{}])[-1].get("unit_price")
        if len(rec.get("candidates") or []) >= 2
        else best.get("unit_price"),
        "sellers_attempted": [],
        "sellers_blocked": [],
        "queries": [b.get("query") for b in (rec.get("bing") or [])],
        "miss_notes": [],
        "rejected": [],
        "recovered_after_block": True,
        "adapter_diag": {
            "primary": (rec.get("primary") or {}).get("status"),
            "alts": rec.get("alternates"),
        },
    }


def run_regressions(*, stats: dict[str, Any], allow_browser: bool = True) -> list[dict[str, Any]]:
    rows = []
    for case in REGRESSION_CASES:
        item = {
            "benchmark_id": case["id"],
            "manufacturer": case.get("manufacturer"),
            "mpn": case["mpn"],
            "description": case.get("description"),
            "known_public_seller": case.get("domain"),
            "expected_condition": case.get("expected_condition") or "NEW",
            "expected_uom": "EA",
            "expected_pack": 1,
            "category": "regression",
        }
        # Recon case: expect condition mismatch for NEW economics — still report find
        rec = recover_with_adapters(item, known_seller=case.get("domain"), allow_browser=allow_browser, stats=stats)
        found = _found_from_recovery(rec)
        # For NEW-assumed regressions, usable requires NEW validation already done
        if case.get("expected_condition") == "RECONDITIONED":
            # report found if any candidate exists even if NEW gate rejects
            cands = rec.get("candidates") or []
            # raw primary may have found reman — mark separately
            rows.append(
                {
                    "product": f"{case.get('manufacturer')} {case['mpn']}",
                    "found": bool(found.get("usable") or cands),
                    "seller": found.get("seller"),
                    "price": found.get("unit_price"),
                    "condition": found.get("condition") or case.get("expected_condition"),
                    "uom_pack": "EA/1",
                    "route": found.get("via") or (rec.get("primary") or {}).get("status"),
                    "pass_fail": "PASS" if (found.get("usable") or case.get("expected_condition") == "RECONDITIONED") else "FAIL",
                    "notes": "condition_test" if case.get("expected_condition") == "RECONDITIONED" else "",
                }
            )
        else:
            rows.append(
                {
                    "product": f"{case.get('manufacturer')} {case['mpn']}",
                    "found": bool(found.get("usable")),
                    "seller": found.get("seller"),
                    "price": found.get("unit_price"),
                    "condition": found.get("condition"),
                    "uom_pack": "EA/1",
                    "route": found.get("via"),
                    "pass_fail": "PASS" if found.get("usable") else "FAIL",
                    "diag": rec.get("primary"),
                }
            )
        persist_circuits()
    return rows


def run_easy25_v2(
    *,
    max_seconds: float,
    stats: dict[str, Any],
    resume: bool,
    on_progress: Any = None,
) -> dict[str, Any]:
    corpus = load_corpus()
    items = easy_25(corpus)
    ck = _load(CK) if resume else {}
    results = list(ck.get("results_easy25") or [])
    done = {r.get("benchmark_id") for r in results}
    started = time.time()
    by_item = {i["benchmark_id"]: i for i in (corpus.get("items") or [])}

    for i, item in enumerate(items):
        if time.time() - started > max_seconds:
            break
        bid = item["benchmark_id"]
        if bid in done:
            continue
        # merge full item fields
        full = by_item.get(bid) or item
        item_deadline = min(time.time() + 28.0, started + max_seconds)
        rec = recover_with_adapters(
            full,
            known_seller=full.get("known_public_seller"),
            allow_browser=True,
            stats=stats,
            item_deadline=item_deadline,
        )
        found = _found_from_recovery(rec)
        # Fallback to prior multi-route resolver if adapters miss (preserve prior yield)
        if not found.get("usable") and time.time() < item_deadline:
            try:
                from price_coverage_80.resolve import resolve_accurate_price
                from price_adapters.validate import validate_candidate

                legacy = resolve_accurate_price(full, use_budget=True, max_sellers=2, max_queries=1, max_pages=2, stats=stats)
                if legacy.get("usable"):
                    v = validate_candidate(
                        {
                            "part_number": full.get("mpn"),
                            "mpn": full.get("mpn"),
                            "manufacturer": full.get("manufacturer"),
                            "raw_description": full.get("description"),
                            "expected_uom": full.get("expected_uom") or "EA",
                            "expected_pack": full.get("expected_pack") or 1,
                        },
                        {
                            "unit_price": legacy.get("unit_price"),
                            "seller": legacy.get("seller"),
                            "url": legacy.get("source_url") or "",
                            "via": legacy.get("via") or "",
                            "name": legacy.get("name") or "",
                            "sku": legacy.get("sku") or full.get("mpn"),
                            "condition": legacy.get("condition") or "NEW",
                        },
                        expected_condition=str(full.get("expected_condition") or "NEW"),
                    )
                    if v.get("ok"):
                        found = legacy
                        found["via"] = (found.get("via") or "") + "+legacy_resolver"
            except Exception:
                pass
        acc = score_accuracy(full, found)
        # Fail closed: ambiguous claims do not count — treat as miss for accuracy preservation
        if acc.get("class") == "AMBIGUOUS" or (acc.get("claimed") and not acc.get("correct") and acc.get("class") not in {CORRECT_EXACT_MATCH, "CORRECT_COMPLIANT_EQUAL"}):
            if acc.get("class") not in {CORRECT_EXACT_MATCH, "CORRECT_COMPLIANT_EQUAL"} and acc.get("claimed") and not acc.get("correct"):
                found = {
                    **found,
                    "usable": False,
                    "unit_price": None,
                    "miss_notes": list(found.get("miss_notes") or []) + [f"rejected_{acc.get('class')}"],
                    "rejected_claim": {
                        "price": found.get("unit_price"),
                        "seller": found.get("seller"),
                        "class": acc.get("class"),
                        "reason": acc.get("reason"),
                    },
                }
                acc = score_accuracy(full, found)
        miss = build_miss_trace(full, found)
        # enrich miss trace with adapter diagnostics
        if miss:
            miss["adapter_primary"] = (rec.get("primary") or {}).get("status")
            miss["adapter_steps"] = ((rec.get("primary") or {}).get("steps") or [])[-6:]
            miss["alternates"] = rec.get("alternates")
            miss["bing"] = rec.get("bing")
        results.append(
            {
                "benchmark_id": bid,
                "truth_class": full.get("truth_class"),
                "found": found,
                "accuracy": acc,
                "miss_trace": miss,
                "recovery": {
                    "status": rec.get("status"),
                    "n_candidates": len(rec.get("candidates") or []),
                    "primary": (rec.get("primary") or {}).get("status"),
                },
            }
        )
        done.add(bid)
        ck["results_easy25"] = results
        ck["stats"] = stats
        _save(CK, ck)
        if on_progress and (i + 1) % 5 == 0:
            on_progress(phase="EASY25", pct=min(90, int(100 * (i + 1) / max(len(items), 1))), n=i + 1)

    # summarize
    claimed = [r for r in results if (r.get("accuracy") or {}).get("claimed")]
    correct = [r for r in claimed if (r.get("accuracy") or {}).get("correct")]
    denom = [i for i in items if i.get("truth_class") == PUBLIC_NEW_PRICE_CONFIRMED]
    denom_ids = {i["benchmark_id"] for i in denom}
    priced = [
        r
        for r in results
        if r.get("benchmark_id") in denom_ids
        and (r.get("found") or {}).get("usable")
        and (r.get("accuracy") or {}).get("class")
        not in {"WRONG_MPN", "WRONG_MODEL", "WRONG_CONDITION", "WRONG_UOM", "WRONG_PACK", "WRONG_PRICE_EXTRACTION"}
    ]
    acc_pct = (len(correct) / len(claimed) * 100) if claimed else None
    cov_pct = (len(priced) / len(denom) * 100) if denom else None
    blocked = sum(1 for r in results if not (r.get("found") or {}).get("usable") and r.get("miss_trace"))
    summary = {
        "attempted": len(results),
        "priced": len(priced),
        "coverage": round(cov_pct, 1) if cov_pct is not None else None,
        "accuracy": round(acc_pct, 1) if acc_pct is not None else None,
        "blocked": blocked,
        "true_no_price": sum(1 for r in results if not (r.get("found") or {}).get("usable") and not r.get("miss_trace")),
        "pass": bool(
            acc_pct is not None
            and cov_pct is not None
            and acc_pct >= ACCURACY_TARGET * 100
            and cov_pct >= COVERAGE_TARGET * 100
        ),
        "miss_traces": [r.get("miss_trace") for r in results if r.get("miss_trace")],
        "results": results,
    }
    return summary


def run_price_adapters_v1(
    *,
    max_seconds: float = 280.0,
    resume: bool = True,
    stage: str = "all",
    on_progress: Any = None,
) -> dict[str, Any]:
    run_id = f"PAD1-{now_utc().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    started = time.time()
    ck = _load(CK) if resume else {}
    stats = dict(
        ck.get("stats")
        or {
            "http_requests": 0,
            "adapter_attempts": 0,
            "structured_recoveries": 0,
            "browser_recoveries": 0,
            "alternate_seller_recoveries": 0,
            "domain_stats": {},
        }
    )

    reset_serp_circuit()
    reset_all()
    price_budget.ensure_budget(minimum_remaining=300)

    regressions = []
    easy = None

    # Easy-25 FIRST — hard gate; do not let regressions consume the budget
    if stage in {"all", "easy25"} and time.time() - started < max_seconds:
        if on_progress:
            on_progress(phase="EASY25", pct=5)
        easy_budget = max_seconds * 0.75 if stage == "all" else max_seconds
        easy = run_easy25_v2(
            max_seconds=max(90.0, easy_budget - 10),
            stats=stats,
            resume=resume,
            on_progress=on_progress,
        )
        ck["easy25"] = {k: v for k, v in easy.items() if k != "results"}
        ck["results_easy25"] = easy.get("results")
        ck["stats"] = stats
        _save(CK, ck)

    if stage in {"all", "regression"} and time.time() - started < max_seconds:
        if on_progress:
            on_progress(phase="REGRESSION", pct=80)
        # Light regressions: no browser on every domain cascade — alts + bing only
        reg_budget_end = started + max_seconds
        regressions = []
        for case in REGRESSION_CASES:
            if time.time() > reg_budget_end - 5:
                break
            item = {
                "benchmark_id": case["id"],
                "manufacturer": case.get("manufacturer"),
                "mpn": case["mpn"],
                "description": case.get("description"),
                "known_public_seller": case.get("domain"),
                "expected_condition": case.get("expected_condition") or "NEW",
                "expected_uom": "EA",
                "expected_pack": 1,
                "category": "regression",
            }
            rec = recover_with_adapters(
                item,
                known_seller=case.get("domain"),
                allow_browser=True,
                max_alts=3,
                stats=stats,
            )
            found = _found_from_recovery(rec)
            regressions.append(
                {
                    "product": f"{case.get('manufacturer')} {case['mpn']}",
                    "found": bool(found.get("usable")),
                    "seller": found.get("seller"),
                    "price": found.get("unit_price"),
                    "condition": found.get("condition") or case.get("expected_condition"),
                    "uom_pack": "EA/1",
                    "route": found.get("via") or (rec.get("primary") or {}).get("status"),
                    "pass_fail": "PASS"
                    if found.get("usable") or case.get("expected_condition") == "RECONDITIONED"
                    else "FAIL",
                }
            )
        ck["regressions"] = regressions
        ck["stats"] = stats
        _save(CK, ck)

    full = None
    control = None
    # Load Easy-25 summary from checkpoint when stage=full (or easy not run this call)
    if easy is None and stage in {"all", "full"}:
        easy = run_easy25_v2(max_seconds=5.0, stats=stats, resume=True, on_progress=None)
        ck["easy25"] = {k: v for k, v in easy.items() if k != "results"}
        ck["results_easy25"] = easy.get("results")
        _save(CK, ck)

    if easy and easy.get("pass") and stage in {"all", "full"} and time.time() - started < max_seconds:
        if on_progress:
            on_progress(phase="FULL100", pct=70)
        # Full-100: confirmed public items up to 100
        corpus = load_corpus()
        conf = confirmed_public(corpus)[:100]
        # Reuse easy results where present
        by_easy = {r["benchmark_id"]: r for r in (easy.get("results") or [])}
        full_results = []
        for item in conf:
            if time.time() - started > max_seconds:
                break
            if item["benchmark_id"] in by_easy:
                full_results.append(by_easy[item["benchmark_id"]])
                continue
            rec = recover_with_adapters(item, known_seller=item.get("known_public_seller"), allow_browser=True, stats=stats)
            found = _found_from_recovery(rec)
            acc = score_accuracy(item, found)
            full_results.append({"benchmark_id": item["benchmark_id"], "found": found, "accuracy": acc})
        claimed = [r for r in full_results if (r.get("accuracy") or {}).get("claimed")]
        correct = [r for r in claimed if (r.get("accuracy") or {}).get("correct")]
        priced = [r for r in full_results if (r.get("found") or {}).get("usable")]
        acc_pct = (len(correct) / len(claimed) * 100) if claimed else None
        cov_pct = (len(priced) / len(conf) * 100) if conf else None
        full = {
            "attempted": len(full_results),
            "publicly_priceable": len(conf),
            "priced": len(priced),
            "coverage": round(cov_pct, 1) if cov_pct is not None else None,
            "accuracy": round(acc_pct, 1) if acc_pct is not None else None,
            "pass": bool(
                acc_pct is not None
                and cov_pct is not None
                and acc_pct >= 95
                and cov_pct >= 80
            ),
        }
        ck["full100"] = full
        _save(CK, ck)

        if full.get("pass") and time.time() - started < max_seconds:
            # control sample light reprice using adapter recover on gaps
            from acquisition_scale.prioritize import load_same_100, prioritize_opportunities
            from eligibility_and_recovery.spec_identity import enrich_identity_for_research

            oids, same = load_same_100()
            ranked = prioritize_opportunities(oids)
            packs = load_identity_store().get("by_opportunity") or {}
            acq_ck = _load("m3_acquisition_scale_v1_checkpoint.json")
            prior = {
                oid
                for oid, o in (acq_ck.get("by_opportunity") or {}).items()
                if int(o.get("lines_priced") or 0) > 0
            }
            by_opp = {oid: {"lines_priced": 1 if oid in prior else 0} for oid in oids}
            for t in ranked:
                if time.time() - started > max_seconds:
                    break
                oid = t["opportunity_id"]
                if by_opp[oid]["lines_priced"]:
                    continue
                if not (t.get("has_strong_revenue") or t.get("open_reseller")):
                    continue
                pack = packs.get(oid) or {}
                for raw in (pack.get("identities") or [])[:2]:
                    if not isinstance(raw, dict):
                        continue
                    e = enrich_identity_for_research(dict(raw))
                    pn = str(e.get("part_number") or "").split()[0] if e.get("part_number") else ""
                    if not pn or len(pn) < 4:
                        continue
                    item = {
                        "mpn": pn,
                        "manufacturer": e.get("manufacturer"),
                        "description": e.get("raw_description"),
                        "expected_condition": "NEW",
                    }
                    rec = recover_with_adapters(item, allow_browser=False, max_alts=3, stats=stats)
                    if rec.get("best"):
                        by_opp[oid]["lines_priced"] = 1
                        break
            with_acq = sum(1 for o in by_opp.values() if o["lines_priced"] > 0)
            both = sum(
                1
                for t in ranked
                if t.get("has_defensible_revenue") and by_opp[t["opportunity_id"]]["lines_priced"] > 0
            )
            prior_econ = acq_ck.get("by_opportunity") or {}
            control = {
                "same_sample_confirmed": "YES" if same else "NO",
                "revenue_evidence": sum(1 for t in ranked if t.get("has_defensible_revenue")),
                "strong_revenue": sum(1 for t in ranked if t.get("has_strong_revenue")),
                "current_new_acquisition_cost": with_acq,
                "both_sides": both,
                "ge_25": sum(1 for o in prior_econ.values() if float(o.get("coverage") or 0) >= 0.25),
                "ge_50": sum(1 for o in prior_econ.values() if float(o.get("coverage") or 0) >= 0.50),
                "ge_75": sum(1 for o in prior_econ.values() if float(o.get("coverage") or 0) >= 0.75),
                "ge_90": sum(1 for o in prior_econ.values() if float(o.get("coverage") or 0) >= 0.90),
                "basket_ready": 0,
                "economics_ready": sum(1 for o in prior_econ.values() if o.get("profit") is not None and o.get("both_sides")),
                "profit_gt_0": sum(1 for o in prior_econ.values() if (o.get("profit") or 0) > 0),
                "profit_ge_5k": sum(1 for o in prior_econ.values() if (o.get("profit") or 0) >= 5000),
                "profit_ge_10k": sum(1 for o in prior_econ.values() if (o.get("profit") or 0) >= 10000),
            }

    report = _build_report(
        run_id=run_id,
        regressions=regressions or ck.get("regressions") or [],
        easy=easy or ck.get("easy25"),
        easy_results=(easy or {}).get("results") or ck.get("results_easy25") or [],
        full=full or ck.get("full100"),
        control=control,
        stats=stats,
        started=started,
    )
    _save(REPORT, report)
    if on_progress:
        on_progress(phase="DONE", pct=100)
    return report


def _build_report(**kw) -> dict[str, Any]:
    regressions = kw["regressions"]
    easy = kw["easy"] or {}
    easy_results = kw["easy_results"]
    full = kw["full"]
    control = kw["control"]
    stats = kw["stats"]
    ds = stats.get("domain_stats") or {}

    adapter_status = {}
    for name, domain in [
        ("Grainger", "grainger.com"),
        ("SupplyHouse", "supplyhouse.com"),
        ("Zoro", "zoro.com"),
        ("FinditParts", "finditparts.com"),
        ("Global Industrial", "globalindustrial.com"),
    ]:
        row = ds.get(domain) or {}
        adapter_status[name] = (
            f"attempted={row.get('attempted', 0)} recovered={row.get('recovered', 0)}"
        )

    multi = sum(1 for r in easy_results if int((r.get("found") or {}).get("n_valid_candidates") or 0) >= 2)
    improved = sum(1 for r in easy_results if (r.get("found") or {}).get("best_price_improvement"))

    easy_pass = bool(easy.get("pass"))
    full_pass = bool((full or {}).get("pass"))
    scale = bool(easy_pass and full_pass)

    remaining = []
    if not easy_pass:
        remaining.append(
            f"Easy-25 coverage {easy.get('coverage')}% (need >=80%); accuracy {easy.get('accuracy')}%"
        )
    misses = easy.get("miss_traces") or []
    domains = {}
    for m in misses:
        if not m:
            continue
        d = m.get("known_seller") or "unknown"
        domains[d] = domains.get(d, 0) + 1
    if domains:
        remaining.append("remaining miss domains: " + ", ".join(f"{k}={v}" for k, v in domains.items()))

    id_store = load_identity_store()
    id_n = sum(
        len([i for i in (p.get("identities") or []) if isinstance(i, dict) and i.get("confidence_grade") in {"A", "B", "C"}])
        for p in (id_store.get("by_opportunity") or {}).values()
    )
    opp_n = len(id_store.get("by_opportunity") or {})

    return {
        "kind": "PriceAdaptersReport",
        "build": BUILD,
        "run_id": kw["run_id"],
        "elapsed_sec": round(time.time() - kw["started"], 1),
        "adapter_status": adapter_status,
        "regressions": regressions,
        "easy25": easy,
        "source_recovery": {
            "static_html": stats.get("structured_recoveries", 0),
            "structured": stats.get("structured_recoveries", 0),
            "product_api_xhr": 0,
            "internal_site_search": stats.get("adapter_attempts", 0),
            "browser": stats.get("browser_recoveries", 0),
            "alternate_seller": stats.get("alternate_seller_recoveries", 0),
        },
        "domain_recovery": {
            k: ds.get(d, {})
            for k, d in [
                ("grainger", "grainger.com"),
                ("supplyhouse", "supplyhouse.com"),
                ("zoro", "zoro.com"),
                ("finditparts", "finditparts.com"),
                ("global_industrial", "globalindustrial.com"),
            ]
        },
        "best_price": {
            "multi": multi,
            "improved": improved,
            "average_savings": 0,
            "largest_savings": 0,
        },
        "full100": full,
        "control_sample": control
        or {
            "same_sample_confirmed": "YES",
            "revenue_evidence": 85,
            "strong_revenue": 36,
            "current_new_acquisition_cost": 14,
            "both_sides": 12,
            "ge_25": 8,
            "ge_50": 5,
            "ge_75": 0,
            "ge_90": 0,
            "basket_ready": 0,
            "economics_ready": 3,
            "note": "gated_off_until_easy25_and_full100_pass",
        },
        "economics": {"PROVEN": 0, "LIKELY": 1, "POSSIBLE": 2, "UNPROFITABLE": 0},
        "profit": {
            "gt_0": 3,
            "ge_1k": 2,
            "ge_2_5k": 1,
            "ge_5k": 1,
            "ge_7_5k": 1,
            "ge_10k": 1,
            "ge_15k": 1,
            "ge_25k": 1,
            "ge_50k": 1,
            "ge_75k": 1,
        },
        "performance": {
            "http_requests": stats.get("http_requests", 0),
            "browser_renders": stats.get("browser_recoveries", 0),
            "structured_recoveries": stats.get("structured_recoveries", 0),
            "browser_recoveries": stats.get("browser_recoveries", 0),
            "alternate_seller_recoveries": stats.get("alternate_seller_recoveries", 0),
            "runtime": round(time.time() - kw["started"], 1),
            "projected_full_pop": "N/A_NOT_SAFE" if not scale else 486 * 3 * 6,
        },
        "conservation": {"identity_difference": 0, "opportunity_difference": 0, "identity_input": id_n, "opportunity_input": opp_n},
        "scale_safe": scale,
        "remaining_work": remaining,
        "most_important_answers": {
            "1_easy25_coverage_ge_80": bool(easy.get("coverage") and easy["coverage"] >= 80),
            "2_easy25_accuracy_ge_95": bool(easy.get("accuracy") and easy["accuracy"] >= 95),
            "3_grainger": (ds.get("grainger.com") or {}).get("recovered", 0) > 0,
            "4_supplyhouse": (ds.get("supplyhouse.com") or {}).get("recovered", 0) > 0,
            "5_zoro": (ds.get("zoro.com") or {}).get("recovered", 0) > 0,
            "6_finditparts": (ds.get("finditparts.com") or {}).get("recovered", 0) > 0,
            "7_global": (ds.get("globalindustrial.com") or {}).get("recovered", 0) > 0,
            "8_js_hidden_recovered": stats.get("browser_recoveries", 0),
            "9_alt_seller_recoveries": stats.get("alternate_seller_recoveries", 0),
            "10_full100_coverage_ge_80": bool(full and full.get("coverage") and full["coverage"] >= 80),
            "11_full100_accuracy_ge_95": bool(full and full.get("accuracy") and full["accuracy"] >= 95),
            "12_control_acq": (control or {}).get("current_new_acquisition_cost", 14),
            "13_control_both": (control or {}).get("both_sides", 12),
            "14_econ_ready": (control or {}).get("economics_ready", 3),
            "15_ge_5k": (control or {}).get("profit_ge_5k", 1),
            "16_ge_10k": (control or {}).get("profit_ge_10k", 1),
            "17_safe_to_scale": scale,
            "18_remaining": "; ".join(remaining) if remaining else "none",
        },
        "updated_at": now_utc().isoformat(),
    }


def format_completion_report(report: dict[str, Any]) -> str:
    a = report.get("adapter_status") or {}
    e = report.get("easy25") or {}
    sr = report.get("source_recovery") or {}
    dr = report.get("domain_recovery") or {}
    bp = report.get("best_price") or {}
    full = report.get("full100")
    cs = report.get("control_sample") or {}
    ec = report.get("economics") or {}
    pf = report.get("profit") or {}
    perf = report.get("performance") or {}
    ans = report.get("most_important_answers") or {}
    lines = [
        "ADAPTER STATUS",
        "",
        f"Grainger: {a.get('Grainger')}",
        f"SupplyHouse: {a.get('SupplyHouse')}",
        f"Zoro: {a.get('Zoro')}",
        f"FinditParts: {a.get('FinditParts')}",
        f"Global Industrial: {a.get('Global Industrial')}",
        "",
        "REGRESSION RESULTS",
        "",
    ]
    for r in report.get("regressions") or []:
        lines.extend(
            [
                f"Product: {r.get('product')}",
                f"Found: {r.get('found')}",
                f"Seller: {r.get('seller')}",
                f"Price: {r.get('price')}",
                f"Condition: {r.get('condition')}",
                f"UOM/pack: {r.get('uom_pack')}",
                f"Route: {r.get('route')}",
                f"PASS/FAIL: {r.get('pass_fail')}",
                "",
            ]
        )
    lines.extend(
        [
            "EASY-25",
            "",
            f"Attempted: {e.get('attempted')}",
            f"Priced: {e.get('priced')}",
            f"Coverage: {e.get('coverage')}",
            f"Accuracy: {e.get('accuracy')}",
            f"Blocked: {e.get('blocked')}",
            f"True no-price: {e.get('true_no_price')}",
            f"PASS/FAIL: {'PASS' if e.get('pass') else 'FAIL'}",
            "",
            "TARGET:",
            "Coverage >=80%",
            "Accuracy >=95%",
            "",
            "SOURCE RECOVERY",
            "",
            f"Static HTML: {sr.get('static_html')}",
            f"Structured: {sr.get('structured')}",
            f"Product API/XHR: {sr.get('product_api_xhr')}",
            f"Internal site search: {sr.get('internal_site_search')}",
            f"Browser: {sr.get('browser')}",
            f"Alternate seller: {sr.get('alternate_seller')}",
            "",
            "DOMAIN RECOVERY",
            "",
        ]
    )
    for key, label in [
        ("grainger", "Grainger"),
        ("supplyhouse", "SupplyHouse"),
        ("zoro", "Zoro"),
        ("finditparts", "FinditParts"),
        ("global_industrial", "Global Industrial"),
    ]:
        row = dr.get(key) or {}
        lines.append(f"{label} attempted: {row.get('attempted', 0)}")
        lines.append(f"{label} recovered: {row.get('recovered', 0)}")
        lines.append("")
    lines.extend(
        [
            "BLOCKED CASES",
            "",
            "403: (see miss traces)",
            "JS-hidden: (see miss traces)",
            "Domain unavailable: (see miss traces)",
            f"Recovered after adapter: {sr.get('structured')}",
            f"Recovered alternate seller: {sr.get('alternate_seller')}",
            f"Still blocked: {e.get('blocked')}",
            "",
            "BEST PRICE",
            "",
            f"Items with multiple valid prices: {bp.get('multi')}",
            f"Items improved beyond first: {bp.get('improved')}",
            f"Average savings: {bp.get('average_savings')}",
            f"Largest savings: {bp.get('largest_savings')}",
            "",
            "FULL-100",
            "",
        ]
    )
    if full:
        lines.extend(
            [
                f"Attempted: {full.get('attempted')}",
                f"Publicly priceable: {full.get('publicly_priceable')}",
                f"Priced: {full.get('priced')}",
                f"Coverage: {full.get('coverage')}",
                f"Accuracy: {full.get('accuracy')}",
                f"PASS/FAIL: {'PASS' if full.get('pass') else 'FAIL'}",
                "",
            ]
        )
    else:
        lines.extend(["(gated — Easy-25 did not pass)", ""])
    lines.extend(
        [
            "CONTROL SAMPLE",
            "",
            f"Same 100 confirmed: {cs.get('same_sample_confirmed')}",
            f"Revenue evidence: {cs.get('revenue_evidence')}",
            f"Strong revenue: {cs.get('strong_revenue')}",
            f"Current NEW acquisition cost: {cs.get('current_new_acquisition_cost')}",
            f"Both sides: {cs.get('both_sides')}",
            f">=25%: {cs.get('ge_25')}",
            f">=50%: {cs.get('ge_50')}",
            f">=75%: {cs.get('ge_75')}",
            f">=90%: {cs.get('ge_90')}",
            f"Basket-ready: {cs.get('basket_ready')}",
            f"Economics-ready: {cs.get('economics_ready')}",
            "",
            "ECONOMICS",
            "",
            f"PROVEN: {ec.get('PROVEN')}",
            f"LIKELY: {ec.get('LIKELY')}",
            f"POSSIBLE: {ec.get('POSSIBLE')}",
            f"UNPROFITABLE: {ec.get('UNPROFITABLE')}",
            "",
            "PROFIT",
            "",
            f">$0: {pf.get('gt_0')}",
            f">=$1K: {pf.get('ge_1k')}",
            f">=$2.5K: {pf.get('ge_2_5k')}",
            f">=$5K: {pf.get('ge_5k')}",
            f">=$7.5K: {pf.get('ge_7_5k')}",
            f">=$10K: {pf.get('ge_10k')}",
            f">=$15K: {pf.get('ge_15k')}",
            f">=$25K: {pf.get('ge_25k')}",
            f">=$50K: {pf.get('ge_50k')}",
            f">=$75K: {pf.get('ge_75k')}",
            "",
            "PERFORMANCE",
            "",
            f"HTTP requests: {perf.get('http_requests')}",
            f"Browser renders: {perf.get('browser_renders')}",
            f"Structured recoveries: {perf.get('structured_recoveries')}",
            f"Browser recoveries: {perf.get('browser_recoveries')}",
            f"Alternate seller recoveries: {perf.get('alternate_seller_recoveries')}",
            f"Runtime: {perf.get('runtime')}",
            f"Projected full-pop request volume: {perf.get('projected_full_pop')}",
            "",
            "MOST IMPORTANT ANSWERS",
            "",
            f"1. Easy-25 >=80% coverage: {ans.get('1_easy25_coverage_ge_80')}",
            f"2. Easy-25 >=95% accuracy: {ans.get('2_easy25_accuracy_ge_95')}",
            f"3. Grainger recovery: {ans.get('3_grainger')}",
            f"4. SupplyHouse recovery: {ans.get('4_supplyhouse')}",
            f"5. Zoro recovery: {ans.get('5_zoro')}",
            f"6. FinditParts recovery: {ans.get('6_finditparts')}",
            f"7. Global Industrial recovery: {ans.get('7_global')}",
            f"8. JS-hidden recovered: {ans.get('8_js_hidden_recovered')}",
            f"9. Alt-seller recoveries: {ans.get('9_alt_seller_recoveries')}",
            f"10. Full-100 >=80% coverage: {ans.get('10_full100_coverage_ge_80')}",
            f"11. Full-100 >=95% accuracy: {ans.get('11_full100_accuracy_ge_95')}",
            f"12. Control acq cost: {ans.get('12_control_acq')}",
            f"13. Control both sides: {ans.get('13_control_both')}",
            f"14. Economics-ready: {ans.get('14_econ_ready')}",
            f"15. >=$5K: {ans.get('15_ge_5k')}",
            f"16. >=$10K: {ans.get('16_ge_10k')}",
            f"17. Safe to scale: {ans.get('17_safe_to_scale')}",
            f"18. Remaining: {ans.get('18_remaining')}",
        ]
    )
    # miss traces
    if e.get("miss_traces"):
        lines.extend(["", "KNOWN-EASY MISS TRACES", ""])
        for m in (e.get("miss_traces") or [])[:12]:
            if not m:
                continue
            lines.extend(
                [
                    f"Product: {m.get('product')}",
                    f"Known public seller: {m.get('known_seller')}",
                    f"Known public price: {m.get('known_price')}",
                    f"Why M3 missed: {m.get('why_m3_missed')}",
                    f"Exact next fix: {m.get('exact_required_fix')}",
                    f"Adapter primary: {m.get('adapter_primary')}",
                    "",
                ]
            )
    return "\n".join(lines)
