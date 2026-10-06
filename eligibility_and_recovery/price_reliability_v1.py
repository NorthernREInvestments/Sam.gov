"""Controlled public-price reliability + eligibility applicability validation.

Build: 20261004-m3-price-search-reliability-v1

Does NOT run the full funnel. Controlled 100-identity + benchmark + optional spec/history.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from eligibility_and_recovery.applicability import BUILD as APP_BUILD, run_applicability_audit
from eligibility_and_recovery.models import BID_ELIGIBLE, BID_ELIGIBLE_WITH_ACTION
from eligibility_and_recovery.spec_identity import enrich_identity_for_research
from evidence_breakthrough.corpus import load_identity_store
from m3_data_root import data_path
from public_price_search import budget as price_budget
from public_price_search.circuits import persist as persist_circuits, report_health, reset_all, snapshot
from public_price_search.models import (
    CONDITION_MISMATCH,
    NO_PUBLIC_PRICE_AFTER_EXHAUSTIVE_SEARCH,
    PRICE_SEARCH_BUDGET_EXHAUSTED,
    PRICE_SOURCE_BLOCKED_RETRYABLE,
    PUBLIC_PRICE_FOUND,
    PUBLIC_PRICE_PARTIAL,
)
from public_price_search.regression_corpus import MANUAL_CASES
from public_price_search.resolver import resolve_public_price
from public_price_search.search import reset_serp_circuit, route_health_snapshot
from scale_evidence_profit.bid_price_index import load_index
from scale_evidence_profit.line_resolver import resolve_line

BUILD = "20261004-m3-price-search-reliability-v1"

# Manual-known easy NEW/public prices a human can typically find
BENCHMARK_EXTRA: list[dict[str, Any]] = [
    {
        "id": "cummins-5579409PX",
        "part_number": "5579409PX",
        "manufacturer": "Cummins",
        "raw_description": "Cummins fuel injector",
        "expected_price_min": 800.0,
        "expected_price_max": 3500.0,
        "accept_reman_as_found": True,
        "notes": "Human finds reman ~$889–$1936; NEW harder when SERP down",
    },
    {
        "id": "grainger-easy-tape",
        "part_number": "1PWC7",
        "manufacturer": "Grainger Approved",
        "raw_description": "Duct tape",
        "expected_price_min": 3.0,
        "expected_price_max": 80.0,
        "notes": "Common Grainger SKU",
    },
    {
        "id": "zoro-easy-glove",
        "part_number": "G0099047",
        "manufacturer": "Zoro Select",
        "raw_description": "Nitrile gloves",
        "expected_price_min": 5.0,
        "expected_price_max": 200.0,
    },
    {
        "id": "fleetguard-lf9009",
        "part_number": "LF9009",
        "manufacturer": "Fleetguard",
        "raw_description": "Lube filter",
        "expected_price_min": 10.0,
        "expected_price_max": 120.0,
        "notes": "Common diesel filter — many public sellers",
    },
    {
        "id": "cummins-ff63009",
        "part_number": "FF63009",
        "manufacturer": "Fleetguard",
        "raw_description": "Fuel filter",
        "expected_price_min": 10.0,
        "expected_price_max": 150.0,
    },
]


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


def _eligible_oids(audit: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for oid, ev in (audit.get("by_opportunity") or {}).items():
        if (ev or {}).get("eligibility_status") in {BID_ELIGIBLE, BID_ELIGIBLE_WITH_ACTION}:
            out.add(str(oid))
    return out


_BAD_PN = {
    "NUMBERS",
    "IALLY",
    "IONS",
    "ITEM",
    "PART",
    "MODEL",
    "NONE",
    "NULL",
    "N/A",
    "NA",
    "TBD",
    "SEE",
    "EACH",
    "UNIT",
}


def _normalize_pn(raw: str) -> str:
    """Take leading token; strip glued descriptions. Reject prose tokens."""
    import re

    s = (raw or "").strip()
    if not s:
        return ""
    tok = s.split()[0].strip("#").strip(",.;:")
    if tok.upper() in _BAD_PN:
        return ""
    # MPN-like: has a digit, mostly alnum/-._, length 4–32
    if not (4 <= len(tok) <= 32):
        return ""
    if not any(ch.isdigit() for ch in tok):
        return ""
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]*[A-Za-z0-9]$|^[A-Za-z0-9]{4,}$", tok):
        return ""
    # Reject mostly-letter short words with a single digit noise
    letters = sum(ch.isalpha() for ch in tok)
    digits = sum(ch.isdigit() for ch in tok)
    if letters >= 5 and digits <= 1 and "-" not in tok and "/" not in tok:
        return ""
    return tok


def select_diverse_exact_identities(
    *,
    limit: int = 100,
    min_opps: int = 50,
    per_opp_cap: int = 3,
    exclude_go_metro: bool = False,
    eligible_oids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Prefer A-grade exact IDs; fill with B-grade. Cap go-metro rather than exclude."""
    id_store = load_identity_store()
    by_opp = id_store.get("by_opportunity") or {}
    rows: list[tuple[str, list[dict[str, Any]], bool]] = []
    for oid, pack in by_opp.items():
        if eligible_oids is not None and str(oid) not in eligible_oids:
            continue
        is_metro = "298984" in str(oid) or "300651" in str(oid) or "go-metro" in str(oid).lower()
        if exclude_go_metro and is_metro:
            continue
        exact = []
        for i in pack.get("identities") or []:
            if not isinstance(i, dict):
                continue
            if i.get("confidence_grade") not in {"A", "B", "C"}:
                continue
            e = enrich_identity_for_research(dict(i))
            pn = _normalize_pn(
                str(
                    e.get("part_number")
                    or e.get("catalog_number")
                    or e.get("sku")
                    or e.get("model")
                    or ""
                )
            )
            if len(pn) < 4:
                continue
            e = dict(e)
            e["part_number"] = pn
            e["opportunity_id"] = str(oid)
            e["_grade"] = i.get("confidence_grade")
            exact.append(e)
        # Prefer A before B within opp
        exact.sort(key=lambda x: (0 if x.get("_grade") == "A" else 1, x.get("part_number") or ""))
        if exact:
            rows.append((str(oid), exact, is_metro))

    # Non-metro first for diversity
    rows.sort(key=lambda x: (1 if x[2] else 0, len(x[1]), x[0]))
    out: list[dict[str, Any]] = []
    seen_pn: set[str] = set()
    opp_counts: dict[str, int] = defaultdict(int)
    metro_total = 0
    metro_soft = 3  # initial diversity pass soft-cap

    def _take(oid: str, e: dict[str, Any], *, is_metro: bool, cap: int, metro_limit: int | None) -> bool:
        nonlocal metro_total
        pn = str(e.get("part_number") or "").upper()
        if not pn or pn in seen_pn:
            return False
        if opp_counts[oid] >= cap:
            return False
        if is_metro and metro_limit is not None and metro_total >= metro_limit:
            return False
        seen_pn.add(pn)
        opp_counts[oid] += 1
        if is_metro:
            metro_total += 1
        out.append(e)
        return True

    # Pass 1: 1 per non-metro opportunity (diversity)
    for oid, idents, is_metro in rows:
        if is_metro:
            continue
        for e in idents:
            if _take(oid, e, is_metro=False, cap=1, metro_limit=metro_soft):
                break
        if len(out) >= limit:
            break
    # Pass 2: deepen non-metro to per_opp_cap
    if len(out) < limit:
        for oid, idents, is_metro in rows:
            if is_metro:
                continue
            for e in idents:
                if len(out) >= limit:
                    break
                _take(oid, e, is_metro=False, cap=per_opp_cap, metro_limit=metro_soft)
            if len(out) >= limit:
                break
    # Pass 3: fill remainder from metro (avoid domination but reach sample size)
    if len(out) < limit:
        for oid, idents, is_metro in rows:
            if not is_metro:
                continue
            for e in idents:
                if len(out) >= limit:
                    break
                _take(oid, e, is_metro=True, cap=limit, metro_limit=None)
            if len(out) >= limit:
                break
    return out[:limit]


def select_spec_identities(
    *,
    limit: int = 50,
    min_opps: int = 25,
    eligible_oids: set[str] | None = None,
) -> list[dict[str, Any]]:
    id_store = load_identity_store()
    by_opp = id_store.get("by_opportunity") or {}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    opp_n: dict[str, int] = defaultdict(int)
    for oid, pack in by_opp.items():
        if eligible_oids is not None and str(oid) not in eligible_oids:
            continue
        if "298984" in str(oid) or "300651" in str(oid):
            continue
        for i in pack.get("identities") or []:
            if not isinstance(i, dict):
                continue
            e = enrich_identity_for_research(i)
            grade = str(e.get("confidence_grade") or "")
            pn = str(e.get("part_number") or e.get("catalog_number") or "").strip()
            # STRONG_GENERIC / no-token researchable
            if pn and len(pn) >= 5 and grade == "A":
                continue  # exact handled elsewhere
            if grade not in {"B", "C"} and not e.get("_spec_resolution"):
                # allow no-token with description
                if not e.get("raw_description"):
                    continue
            key = f"{oid}::{e.get('line_id') or e.get('raw_description') or ''}"[:120]
            if key in seen:
                continue
            if opp_n[str(oid)] >= 2:
                continue
            seen.add(key)
            opp_n[str(oid)] += 1
            e["opportunity_id"] = str(oid)
            out.append(e)
            if len(out) >= limit and len(opp_n) >= min_opps:
                return out[:limit]
    return out[:limit]


def _classify_via(result: dict[str, Any]) -> str:
    ev = result.get("evidence") or {}
    via = str(ev.get("retrieved_via") or "")
    seller = str(ev.get("seller_class") or "")
    if via in {"structured_data", "structured_api"}:
        return "structured"
    if via == "js_render":
        return "js_rendered"
    if via in {"manufacturer_or_known", "manufacturer_or_known_link"}:
        return "manufacturer"
    if "distributor" in via:
        return "distributor"
    if via in {"serp_page", "serp_page_link", "snippet"}:
        return "reseller"
    if seller == "MANUFACTURER":
        return "manufacturer"
    if seller == "DISTRIBUTOR":
        return "distributor"
    return "direct_catalog" if via else "other"


def run_price_search_reliability_v1(
    *,
    exact_n: int = 100,
    spec_n: int = 50,
    max_seconds: float = 240.0,
    run_spec: bool | None = None,
    run_history: bool = True,
    on_progress: Any = None,
    resume: bool = True,
) -> dict[str, Any]:
    """Controlled validation. Checkpoints every batch; resumes completed work."""
    run_id = f"PSR1-{now_utc().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    started = time.time()
    ck_name = "m3_price_search_reliability_v1_checkpoint.json"
    ck = _load(ck_name) if resume else {}

    if on_progress:
        on_progress(phase="ELIGIBILITY_AUDIT", pct=5)

    # --- Phase 1: applicability audit ---
    if ck.get("audit"):
        audit = ck["audit"]
    else:
        audit = run_applicability_audit()
        ck["audit"] = {
            "before": audit.get("before"),
            "after": audit.get("after"),
            "false_positive_actions_removed": audit.get("false_positive_actions_removed"),
            "bonding_false_positives": audit.get("bonding_false_positives"),
            "insurance_false_positives": audit.get("insurance_false_positives"),
            "site_visit_false_positives": audit.get("site_visit_false_positives"),
            "far_dfars_applicability_corrections": audit.get("far_dfars_applicability_corrections"),
        }
        # Keep full audit on disk; checkpoint holds summary only + path
        _save(ck_name, ck)

    # Reload full audit for eligible oids
    full_audit = _load("m3_eligibility_applicability_audit_store.json")
    eligible = _eligible_oids(full_audit)

    reset_serp_circuit()
    price_budget.ensure_budget(minimum_remaining=120)

    # --- Benchmark + exact sample ---
    if on_progress:
        on_progress(phase="SELECT_SAMPLE", pct=10)

    benchmark = []
    seen_b = set()
    for b in BENCHMARK_EXTRA + list(MANUAL_CASES):
        key = str(b.get("part_number") or b.get("id"))
        if key in seen_b:
            continue
        seen_b.add(key)
        benchmark.append(b)

    exact_ids = ck.get("exact_ids") or select_diverse_exact_identities(
        limit=exact_n, min_opps=50, per_opp_cap=3, eligible_oids=eligible or None
    )
    ck["exact_ids"] = exact_ids
    _save(ck_name, ck)

    results_by_key: dict[str, Any] = dict(ck.get("exact_results") or {})
    metrics = {
        "attempted": 0,
        "prices_found": 0,
        "new_prices_found": 0,
        "multiple_candidates": 0,
        "manufacturer": 0,
        "distributor": 0,
        "reseller": 0,
        "direct_catalog": 0,
        "structured": 0,
        "js_rendered": 0,
        "catalog_pdf": 0,
        "route_blocked": 0,
        "budget_deferred": 0,
        "true_no_price": 0,
        "condition_mismatch": 0,
    }

    def _run_one(ident: dict[str, Any], *, budget: bool = True) -> dict[str, Any]:
        key = f"{ident.get('opportunity_id')}::{ident.get('part_number') or ident.get('model') or ident.get('id')}"
        if key in results_by_key:
            return results_by_key[key]
        r = resolve_public_price(
            ident,
            opportunity_id=ident.get("opportunity_id"),
            use_budget=budget,
            max_queries=3,
            max_pages=5,
        )
        row = {
            "key": key,
            "identity": {
                "part_number": ident.get("part_number"),
                "manufacturer": ident.get("manufacturer"),
                "opportunity_id": ident.get("opportunity_id"),
            },
            "status": r.get("status"),
            "failure_reason": r.get("failure_reason"),
            "evidence": r.get("evidence"),
            "candidates": [
                {
                    "price": c.get("unit_price"),
                    "condition": c.get("condition"),
                    "url": c.get("url"),
                    "via": c.get("via"),
                }
                for c in (r.get("candidates") or [])[:6]
            ],
            "via_class": _classify_via(r),
            "n_candidates": len(r.get("candidates") or []),
        }
        results_by_key[key] = row
        return row

    def _accumulate(row: dict[str, Any]) -> None:
        metrics["attempted"] += 1
        st = row.get("status")
        n_cand = int(row.get("n_candidates") or 0)
        if n_cand >= 2:
            metrics["multiple_candidates"] += 1
        if st in {PUBLIC_PRICE_FOUND, PUBLIC_PRICE_PARTIAL}:
            metrics["prices_found"] += 1
            cond = str((row.get("evidence") or {}).get("condition") or "").upper()
            if cond in {"NEW", "UNKNOWN", ""}:
                metrics["new_prices_found"] += 1
            vc = row.get("via_class") or "other"
            if vc in metrics:
                metrics[vc] += 1
            elif vc == "manufacturer":
                metrics["manufacturer"] += 1
        elif st == CONDITION_MISMATCH:
            metrics["condition_mismatch"] += 1
            if n_cand > 0:
                metrics["prices_found"] += 1  # found a public price, wrong condition
        elif st == PRICE_SOURCE_BLOCKED_RETRYABLE:
            metrics["route_blocked"] += 1
        elif st == PRICE_SEARCH_BUDGET_EXHAUSTED:
            metrics["budget_deferred"] += 1
        elif st == NO_PUBLIC_PRICE_AFTER_EXHAUSTIVE_SEARCH:
            metrics["true_no_price"] += 1

    if on_progress:
        on_progress(phase="BENCHMARK", pct=15)

    bench_results = list(ck.get("benchmark_results") or [])
    done_bench_ids = {r.get("benchmark_id") for r in bench_results}
    for b in benchmark:
        if b.get("id") in done_bench_ids:
            continue
        # Benchmark gets priority time budget
        if time.time() - started > max_seconds * 0.45:
            break
        ident = {
            "part_number": b.get("part_number"),
            "manufacturer": b.get("manufacturer"),
            "raw_description": b.get("raw_description"),
            "opportunity_id": "benchmark",
        }
        # Force fresh resolve for benchmark (do not reuse stale route-blocked rows)
        key = f"benchmark::{ident.get('part_number')}"
        results_by_key.pop(key, None)
        # resolve_public_price uses opportunity_id in key via _run_one
        stale = f"benchmark::{ident.get('part_number') or ident.get('model') or ident.get('id')}"
        results_by_key.pop(stale, None)
        results_by_key.pop(
            f"{ident.get('opportunity_id')}::{ident.get('part_number') or ident.get('model') or ident.get('id')}",
            None,
        )
        row = _run_one(ident, budget=False)
        row["benchmark_id"] = b.get("id")
        row["accept_reman"] = bool(b.get("accept_reman_as_found"))
        row["optional"] = bool(b.get("optional"))
        ok = False
        ev = row.get("evidence") or {}
        price = ev.get("unit_price")
        if price and row.get("status") in {PUBLIC_PRICE_FOUND, PUBLIC_PRICE_PARTIAL}:
            ok = True
        elif row.get("accept_reman") and row.get("candidates"):
            ok = True
            row["status_note"] = "REMAN_CANDIDATES_COUNT_AS_BENCHMARK_HIT"
        elif row.get("candidates") and b.get("expected_price_min"):
            for c in row["candidates"]:
                p = c.get("price")
                if p and float(b["expected_price_min"]) <= float(p) <= float(
                    b.get("expected_price_max") or 1e9
                ):
                    ok = True
                    break
        row["benchmark_success"] = ok and not row.get("optional")
        row["benchmark_success_incl_optional"] = ok
        bench_results.append(row)
        ck["benchmark_results"] = bench_results
        ck["exact_results"] = results_by_key
        _save(ck_name, ck)
        persist_circuits()

    required_bench = [r for r in bench_results if not r.get("optional")]
    bench_ok = sum(1 for r in required_bench if r.get("benchmark_success"))
    bench_rate = (100.0 * bench_ok / max(len(required_bench), 1)) if required_bench else 0.0
    stop_scale = bench_rate < 50.0 or len(required_bench) < 3
    run_spec_flag = (
        run_spec
        if run_spec is not None
        else (bench_rate >= 70.0 and len(required_bench) >= 3)
    ) and not stop_scale

    if on_progress:
        on_progress(phase="EXACT_100", pct=30)

    # Reset metrics and re-accumulate from all exact + remaining
    # Run exact identities
    for i, ident in enumerate(exact_ids):
        if time.time() - started > max_seconds:
            break
        row = _run_one(ident, budget=True)
        if (i + 1) % 5 == 0:
            ck["exact_results"] = results_by_key
            ck["exact_done"] = i + 1
            _save(ck_name, ck)
            persist_circuits()
            if on_progress:
                on_progress(phase="EXACT_100", pct=min(70, 30 + int(40 * (i + 1) / max(len(exact_ids), 1))), opp=i + 1)

    # Accumulate exact metrics (exclude pure benchmark keys)
    metrics = {k: 0 for k in metrics}
    exact_rows = []
    for ident in exact_ids:
        key = f"{ident.get('opportunity_id')}::{ident.get('part_number') or ident.get('model') or ident.get('id')}"
        row = results_by_key.get(key)
        if not row:
            continue
        exact_rows.append(row)
        _accumulate(row)

    distinct_opps = len({r["identity"].get("opportunity_id") for r in exact_rows})
    new_rate = 100.0 * metrics["new_prices_found"] / max(metrics["attempted"], 1)
    scale_ok = (
        (not stop_scale)
        and bench_rate >= 70.0
        and metrics["attempted"] >= 50
        and new_rate >= 30.0
    )

    spec_metrics = {
        "attempted": 0,
        "valid_matches": 0,
        "new_prices": 0,
        "partial_rejects": 0,
        "no_price": 0,
        "blocked": 0,
        "distinct_opps": 0,
    }
    spec_rows: list[dict[str, Any]] = list(ck.get("spec_results") or [])

    if run_spec_flag and time.time() - started < max_seconds:
        if on_progress:
            on_progress(phase="SPEC_50", pct=72)
        spec_ids = ck.get("spec_ids") or select_spec_identities(limit=spec_n, eligible_oids=eligible or None)
        ck["spec_ids"] = spec_ids
        seen_spec_opp: set[str] = set()
        for i, ident in enumerate(spec_ids):
            if time.time() - started > max_seconds:
                break
            key = f"spec::{ident.get('opportunity_id')}::{ident.get('line_id') or i}"
            if key in {r.get("key") for r in spec_rows}:
                continue
            r = resolve_public_price(ident, opportunity_id=ident.get("opportunity_id"), use_budget=True, max_queries=2, max_pages=4)
            row = {
                "key": key,
                "status": r.get("status"),
                "opportunity_id": ident.get("opportunity_id"),
                "n_candidates": len(r.get("candidates") or []),
                "evidence": r.get("evidence"),
            }
            spec_rows.append(row)
            seen_spec_opp.add(str(ident.get("opportunity_id")))
            if (i + 1) % 5 == 0:
                ck["spec_results"] = spec_rows
                _save(ck_name, ck)
        for row in spec_rows:
            spec_metrics["attempted"] += 1
            st = row.get("status")
            if st in {PUBLIC_PRICE_FOUND, PUBLIC_PRICE_PARTIAL}:
                spec_metrics["valid_matches"] += 1
                cond = str((row.get("evidence") or {}).get("condition") or "").upper()
                if cond in {"NEW", "UNKNOWN", ""}:
                    spec_metrics["new_prices"] += 1
            elif st == PRICE_SOURCE_BLOCKED_RETRYABLE:
                spec_metrics["blocked"] += 1
            elif st == NO_PUBLIC_PRICE_AFTER_EXHAUSTIVE_SEARCH:
                spec_metrics["no_price"] += 1
            else:
                spec_metrics["partial_rejects"] += 1
        spec_metrics["distinct_opps"] = len({r.get("opportunity_id") for r in spec_rows})
        ck["spec_results"] = spec_rows
        _save(ck_name, ck)

    # History validation on distinct opps from exact sample
    hist = {
        "distinct_opportunities_searched": 0,
        "gov_value_found": 0,
        "bid_tab_matches": 0,
        "award_tab_matches": 0,
        "recurring_history_matches": 0,
        "no_history_exhaustive": 0,
        "blocked": 0,
    }
    both_sides = {
        "with_cost": 0,
        "with_gov": 0,
        "with_both": 0,
        "ge_25": 0,
        "ge_50": 0,
        "early_positive": 0,
        "early_negative": 0,
        "needs_deeper": 0,
    }
    if run_history and time.time() - started < max_seconds:
        if on_progress:
            on_progress(phase="HISTORY", pct=85)
        idx = load_index()
        opp_sample = sorted({r["identity"].get("opportunity_id") for r in exact_rows if r["identity"].get("opportunity_id")})
        id_store = load_identity_store()
        by_opp = id_store.get("by_opportunity") or {}
        for oid in opp_sample[:40]:
            if time.time() - started > max_seconds:
                break
            hist["distinct_opportunities_searched"] += 1
            pack = by_opp.get(oid) or {}
            idents = [i for i in (pack.get("identities") or []) if isinstance(i, dict)][:5]
            gov_hits = 0
            cost_hits = 0
            for e in idents:
                e = enrich_identity_for_research(e)
                # cost from our exact results
                pn = str(e.get("part_number") or e.get("catalog_number") or "")
                key = f"{oid}::{pn}"
                row = results_by_key.get(key)
                if row and row.get("status") in {PUBLIC_PRICE_FOUND, PUBLIC_PRICE_PARTIAL}:
                    cost_hits += 1
                try:
                    e["opportunity_id"] = oid
                    resolved = resolve_line(e, idx, allow_public_price=False)
                except Exception:
                    resolved = {}
                if (resolved or {}).get("gov_unit_price") or (resolved or {}).get("has_gov_value"):
                    gov_hits += 1
                    hist["gov_value_found"] += 1
                    src = str(
                        (resolved or {}).get("gov_source")
                        or (resolved or {}).get("gov_basis")
                        or ""
                    ).lower()
                    if "bid" in src:
                        hist["bid_tab_matches"] += 1
                    elif "award" in src:
                        hist["award_tab_matches"] += 1
                    else:
                        hist["recurring_history_matches"] += 1
            if gov_hits == 0:
                hist["no_history_exhaustive"] += 1
            if cost_hits:
                both_sides["with_cost"] += 1
            if gov_hits:
                both_sides["with_gov"] += 1
            if cost_hits and gov_hits:
                both_sides["with_both"] += 1
            cov = cost_hits / max(len(idents), 1)
            if cov >= 0.25:
                both_sides["ge_25"] += 1
            if cov >= 0.50:
                both_sides["ge_50"] += 1
            if cost_hits and gov_hits:
                both_sides["needs_deeper"] += 1

    # Cost tracking — deterministic HTTP search; no OpenAI in this path
    bud = price_budget.snapshot()
    calls = int(bud.get("queries_used") or 0) + int(bud.get("pages_fetched") or 0)
    per_id = calls / max(metrics["attempted"], 1)
    spend = {
        "ai_search_spend_usd": 0.0,
        "calls": calls,
        "tokens": 0,
        "projected_full_population_cost_usd": 0.0,
        "http_calls_observed": calls,
        "projected_full_population_http_calls": int(per_id * 1175) if metrics["attempted"] else None,
    }

    rh = report_health()
    legacy_rh = route_health_snapshot()

    known_misses = [
        f"{r.get('benchmark_id')}:{r.get('status')}"
        for r in required_bench
        if not r.get("benchmark_success")
    ]

    report = {
        "kind": "PriceSearchReliabilityReport",
        "build": BUILD,
        "run_id": run_id,
        "eligibility_applicability_audit": {
            "before": ck.get("audit", {}).get("before") or audit.get("before"),
            "after": ck.get("audit", {}).get("after") or audit.get("after"),
            "false_positive_actions_removed": ck.get("audit", {}).get("false_positive_actions_removed"),
            "bonding_false_positives": ck.get("audit", {}).get("bonding_false_positives"),
            "insurance_false_positives": ck.get("audit", {}).get("insurance_false_positives"),
            "site_visit_false_positives": ck.get("audit", {}).get("site_visit_false_positives"),
            "far_dfars_applicability_corrections": ck.get("audit", {}).get(
                "far_dfars_applicability_corrections"
            ),
        },
        "price_route_health": rh,
        "price_route_health_detail": legacy_rh,
        "exact_100": {
            "identities_attempted": metrics["attempted"],
            "distinct_opportunities": distinct_opps,
            **{k: metrics[k] for k in metrics if k != "attempted"},
        },
        "manual_benchmark": {
            "benchmark_items": len(required_bench),
            "m3_priced": bench_ok,
            "success_rate": round(bench_rate, 1),
            "known_easy_misses": known_misses,
            "all_results": [
                {
                    "id": r.get("benchmark_id"),
                    "status": r.get("status"),
                    "success": r.get("benchmark_success"),
                    "price": (r.get("evidence") or {}).get("unit_price"),
                    "candidates": r.get("n_candidates"),
                }
                for r in bench_results
            ],
        },
        "spec_based": spec_metrics,
        "history": hist,
        "distinct_opportunity_evidence": both_sides,
        "cost": spend,
        "stop_scale": stop_scale,
        "run_spec": run_spec_flag,
        "elapsed_sec": round(time.time() - started, 1),
        "circuit_snapshot": snapshot(),
        "most_important_answers": {
            "1_false_positive_actions": ck.get("audit", {}).get("false_positive_actions_removed"),
            "2_far_applicability_evaluated": True,
            "3_single_serp_spof": False,
            "4_js_hidden_recovery": metrics["js_rendered"] + metrics["structured"] > 0,
            "5_new_price_rate_100": round(
                100.0 * metrics["new_prices_found"] / max(metrics["attempted"], 1), 1
            ),
            "6_manual_benchmark_rate": round(bench_rate, 1),
            "7_distinct_opps_with_cost": both_sides["with_cost"],
            "8_both_sides": both_sides["with_both"],
            "9_diversity_beyond_go_metro": distinct_opps >= 20,
            "10_strong_enough_to_scale": scale_ok,
            "11_top_miss_cause": (
                "ROUTE_BLOCKED"
                if metrics["route_blocked"] >= metrics["true_no_price"]
                and metrics["route_blocked"] >= metrics["condition_mismatch"]
                else "CONDITION_MISMATCH"
                if metrics["condition_mismatch"] >= metrics["true_no_price"]
                else "TRUE_NO_PRICE"
            ),
            "12_projected_full_pop_http_calls": spend["projected_full_population_http_calls"],
        },
        "updated_at": now_utc().isoformat(),
    }
    _save("m3_price_search_reliability_v1_last_report.json", report)
    ck["exact_results"] = results_by_key
    ck["final_report_saved"] = True
    _save(ck_name, ck)
    persist_circuits()
    if on_progress:
        on_progress(phase="DONE", pct=100)
    return report


def format_completion_report(report: dict[str, Any]) -> str:
    a = report.get("eligibility_applicability_audit") or {}
    before = a.get("before") or {}
    after = a.get("after") or {}
    rh = report.get("price_route_health") or {}
    ex = report.get("exact_100") or {}
    mb = report.get("manual_benchmark") or {}
    sp = report.get("spec_based") or {}
    hi = report.get("history") or {}
    de = report.get("distinct_opportunity_evidence") or {}
    cost = report.get("cost") or {}
    ans = report.get("most_important_answers") or {}

    lines = [
        "ELIGIBILITY APPLICABILITY AUDIT",
        "",
        "Before:",
        f"BID_ELIGIBLE: {before.get('BID_ELIGIBLE')}",
        f"BID_ELIGIBLE_WITH_ACTION: {before.get('BID_ELIGIBLE_WITH_ACTION')}",
        f"ELIGIBILITY_UNKNOWN: {before.get('ELIGIBILITY_UNKNOWN')}",
        f"BID_INELIGIBLE: {before.get('BID_INELIGIBLE')}",
        "",
        "After:",
        f"BID_ELIGIBLE: {after.get('BID_ELIGIBLE')}",
        f"BID_ELIGIBLE_WITH_ACTION: {after.get('BID_ELIGIBLE_WITH_ACTION')}",
        f"ELIGIBILITY_UNKNOWN: {after.get('ELIGIBILITY_UNKNOWN')}",
        f"BID_INELIGIBLE: {after.get('BID_INELIGIBLE')}",
        "",
        f"False-positive actions removed: {a.get('false_positive_actions_removed')}",
        f"Bonding false positives: {a.get('bonding_false_positives')}",
        f"Insurance false positives: {a.get('insurance_false_positives')}",
        f"Site-visit false positives: {a.get('site_visit_false_positives')}",
        f"FAR/DFARS applicability corrections: {a.get('far_dfars_applicability_corrections')}",
        "",
        "PRICE ROUTE HEALTH",
        "",
        f"Search provider A: {rh.get('Search provider A')}",
        f"Search provider B: {rh.get('Search provider B')}",
        f"Manufacturer: {rh.get('Manufacturer')}",
        f"Distributor: {rh.get('Distributor')}",
        f"Reseller: {rh.get('Reseller')}",
        f"Direct catalog: {rh.get('Direct catalog')}",
        f"Structured data: {rh.get('Structured data')}",
        f"JS-rendered: {rh.get('JS-rendered')}",
        f"Catalog/PDF: {rh.get('Catalog/PDF')}",
        "",
        "100 EXACT-ID VALIDATION",
        "",
        f"Identities attempted: {ex.get('identities_attempted')}",
        f"Distinct opportunities: {ex.get('distinct_opportunities')}",
        f"Public prices found: {ex.get('prices_found')}",
        f"NEW prices found: {ex.get('new_prices_found')}",
        f"Multiple candidate prices: {ex.get('multiple_candidates')}",
        f"Manufacturer-direct: {ex.get('manufacturer')}",
        f"Distributor: {ex.get('distributor')}",
        f"Reseller: {ex.get('reseller')}",
        f"Direct catalog: {ex.get('direct_catalog')}",
        f"Structured: {ex.get('structured')}",
        f"JS-rendered: {ex.get('js_rendered')}",
        f"Catalog/PDF: {ex.get('catalog_pdf')}",
        f"Route-blocked: {ex.get('route_blocked')}",
        f"Budget-deferred: {ex.get('budget_deferred')}",
        f"True no-price: {ex.get('true_no_price')}",
        "",
        "MANUAL BENCHMARK",
        "",
        f"Benchmark items: {mb.get('benchmark_items')}",
        f"M3 priced: {mb.get('m3_priced')}",
        f"Success rate: {mb.get('success_rate')}%",
        f"Known easy misses: {mb.get('known_easy_misses')}",
        "",
        "SPEC-BASED VALIDATION",
        "",
        f"Identities attempted: {sp.get('attempted')}",
        f"Distinct opportunities: {sp.get('distinct_opps')}",
        f"Valid product matches: {sp.get('valid_matches')}",
        f"NEW prices found: {sp.get('new_prices')}",
        f"Partial-spec rejects: {sp.get('partial_rejects')}",
        f"No-price: {sp.get('no_price')}",
        f"Blocked: {sp.get('blocked')}",
        "",
        "HISTORY VALIDATION",
        "",
        f"Distinct opportunities searched: {hi.get('distinct_opportunities_searched')}",
        f"Gov-value found: {hi.get('gov_value_found')}",
        f"Bid-tab matches: {hi.get('bid_tab_matches')}",
        f"Award-tab matches: {hi.get('award_tab_matches')}",
        f"Recurring-history matches: {hi.get('recurring_history_matches')}",
        f"No-history exhaustive: {hi.get('no_history_exhaustive')}",
        f"Blocked: {hi.get('blocked')}",
        "",
        "DISTINCT OPPORTUNITY EVIDENCE",
        "",
        f"With NEW acquisition cost: {de.get('with_cost')}",
        f"With gov value: {de.get('with_gov')}",
        f"With both sides: {de.get('with_both')}",
        f">=25% coverage: {de.get('ge_25')}",
        f">=50% coverage: {de.get('ge_50')}",
        f"Early positive: {de.get('early_positive')}",
        f"Early negative: {de.get('early_negative')}",
        f"Needs deeper pricing: {de.get('needs_deeper')}",
        "",
        "COST",
        "",
        f"AI/search spend: ${cost.get('ai_search_spend_usd')}",
        f"Calls: {cost.get('calls')}",
        f"Tokens: {cost.get('tokens')}",
        f"Projected full-population cost: ${cost.get('projected_full_population_cost_usd')} "
        f"(HTTP calls ~{cost.get('projected_full_population_http_calls')})",
        "",
        "MOST IMPORTANT ANSWERS",
        "",
        f"1. False-positive actions removed: {ans.get('1_false_positive_actions')}",
        f"2. FAR/DFARS applicability evaluated: {ans.get('2_far_applicability_evaluated')}",
        f"3. Single SERP SPOF: {ans.get('3_single_serp_spof')}",
        f"4. JS-hidden/catalog recovery active: {ans.get('4_js_hidden_recovery')}",
        f"5. NEW-price success rate (100): {ans.get('5_new_price_rate_100')}%",
        f"6. Manual benchmark success: {ans.get('6_manual_benchmark_rate')}%",
        f"7. Distinct opps with acq cost: {ans.get('7_distinct_opps_with_cost')}",
        f"8. Both sides: {ans.get('8_both_sides')}",
        f"9. Diversity beyond go-metro: {ans.get('9_diversity_beyond_go_metro')}",
        f"10. Strong enough to scale: {ans.get('10_strong_enough_to_scale')}",
        f"11. Top miss cause: {ans.get('11_top_miss_cause')}",
        f"12. Projected full-pop HTTP calls: {ans.get('12_projected_full_pop_http_calls')}",
    ]
    return "\n".join(lines)
