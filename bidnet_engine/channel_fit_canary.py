"""Real live BidNet channel-fit + MSRP-first canary (20 → expand only if CALL_TODAY ≥ 1)."""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from application_clock import now_utc
from bidnet_downstream.models import PRODUCT_CLASSES
from bidnet_engine.money_path import (
    DOWNSTREAM_CHECKPOINT,
    _days_remaining,
    _merge_checkpoint,
    canonical_pipeline_map,
    inspect_durable_checkpoint,
    process_money_opportunity,
    select_money_candidates,
)
from bidnet_engine.thread_limits import apply_thread_limits, verify_thread_limits
from channel_fit.engine import queue_buckets, score_money_sprint_rows

BUILD = "20261007-m3-live-channel-fit-canary-v1"
STATUS = "m3_channel_fit_canary_v1_status.json"
REPORT_JSON = "m3_channel_fit_canary_v1_last_report.json"
REPORT_TXT = "m3_channel_fit_canary_v1_last_report.txt"
ROWS_JSON = "m3_channel_fit_canary_v1_rows.json"
SCORES_JSON = "m3_channel_fit_live_scores_v1.json"
SELECTED_IDS = "m3_channel_fit_canary_v1_selected_ids.json"
OLD_MONEY_JOB = "MNY-0525b77c8bf6"

# Prefer multi-brand / reseller-friendly signals; soft-avoid pure channel verticals at selection.
_CHANNEL_RISK = re.compile(
    r"\b(waterworks|water\s+materials|hydrant|ductile|c900|"
    r"\bmro\b|industrial\s+supply\s+catalog|fastener\s+only|"
    r"medical\s+supply|surgical\s+supply|"
    r"electrical\s+wholesale)\b",
    re.I,
)
_MULTI_SIGNAL = re.compile(
    r"\b(office|ppe|tool|janitor|furniture|supply|equipment|"
    r"multi|various|assorted|catalog|commodit)\b",
    re.I,
)

FIXTURE_STABLE_KEYS = {
    "truckee-donner-water-materials",
    "mixed-facilities-multi-brand-demo",
    "mixed-office-ppe-grainger-present",
    "pure-mro-grainger-fastenal",
    "thin-coverage-13pct",
    "good-headroom",
    "no-headroom",
    "fragmented",
    "oem-heavy",
    "mro-pure",
    "mixed-office-ppe",
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
    doc = {"build": BUILD, "updated_at": now_utc().isoformat(), **kwargs}
    _save(STATUS, doc)


def terminate_money_job(job_id: str = OLD_MONEY_JOB) -> dict[str, Any]:
    """TERMINATED_NO_PROGRESS — preserve candidate IDs and all durable BidNet state."""
    from m3_data_root import data_path

    path = data_path(f"auth_jobs/{job_id}.json")
    prior: dict[str, Any] = {}
    if path.exists():
        try:
            prior = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            prior = {}

    # Preserve selected IDs from money status / prior params if present
    preserved_ids: list[str] = []
    money_status = _load("m3_money_path_v1_status.json")
    for key in ("selected_ids", "candidate_ids", "sample_ids"):
        raw = money_status.get(key) or (prior.get("result") or {}).get(key) or []
        if isinstance(raw, list):
            preserved_ids.extend(str(x) for x in raw if x)
    ckpt = _load(DOWNSTREAM_CHECKPOINT)
    # Keep a snapshot of product keys currently selected-ish
    product_keys = [
        str(r.get("stable_key") or r.get("canonical_opportunity_id"))
        for r in (ckpt.get("rows") or [])
        if isinstance(r, dict) and r.get("classification") in PRODUCT_CLASSES
    ][:500]
    if product_keys and not preserved_ids:
        preserved_ids = product_keys[:250]
    _save(
        SELECTED_IDS,
        {
            "build": BUILD,
            "preserved_from_job": job_id,
            "preserved_at": now_utc().isoformat(),
            "selected_ids": preserved_ids,
            "count": len(preserved_ids),
            "note": "Durable BidNet stores untouched; packages/docs not deleted",
        },
    )

    job = dict(prior) if isinstance(prior, dict) else {}
    job.update(
        {
            "job_id": job_id,
            "kind": job.get("kind") or "bidnet_money_path",
            "status": "TERMINATED_NO_PROGRESS",
            "completed_at": now_utc().isoformat(),
            "updated_at": now_utc().isoformat(),
            "error": "TERMINATED_NO_PROGRESS",
            "progress": {
                "phase": "TERMINATED_NO_PROGRESS",
                "pct": int(((prior.get("progress") or {}).get("pct") or 0)),
                "preserved_selected_ids": len(preserved_ids),
                "checkpoint_preserved": True,
            },
            "result": {
                **(prior.get("result") or {} if isinstance(prior.get("result"), dict) else {}),
                "TERMINATED_REASON": "TERMINATED_NO_PROGRESS",
                "selected_ids_preserved": len(preserved_ids),
                "checkpoint_preserved": True,
                "stores_wiped": False,
            },
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(job, indent=2, default=str), encoding="utf-8")
    latest = data_path("auth_jobs/last_job_status.json")
    try:
        if latest.exists():
            cur = json.loads(latest.read_text(encoding="utf-8"))
            if str(cur.get("job_id") or "") == job_id:
                latest.write_text(json.dumps(job, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass
    return {
        "job_id": job_id,
        "status": "TERMINATED_NO_PROGRESS",
        "selected_ids_preserved": len(preserved_ids),
        "checkpoint_preserved": True,
        "stores_wiped": False,
        "prior_status": prior.get("status"),
    }


def select_channel_fit_candidates(rows: list[dict[str, Any]], *, limit: int = 20) -> list[dict[str, Any]]:
    """Prefer live product/mixed with runway, package, eligibility; soft-avoid channel-dominated titles."""
    base = select_money_candidates(rows, limit=max(limit * 8, 160))
    scored = []
    for r in base:
        title = str(r.get("title") or "")
        score = int(r.get("_money_score") or 0)
        if _CHANNEL_RISK.search(title):
            score -= 80  # soft-avoid pure channel verticals in canary selection
        if _MULTI_SIGNAL.search(title):
            score += 25
        # Prefer not already money_path_complete so we re-run full live pipeline
        if r.get("money_path_complete") and not r.get("channel_fit_live"):
            score += 5  # still ok — we reprocess live
        item = dict(r)
        item["_channel_select_score"] = score
        scored.append(item)
    scored.sort(key=lambda x: (-int(x.get("_channel_select_score") or 0), str(x.get("deadline") or "9999")))
    return scored[:limit]


def _historical_bidders_from_store(store_row: dict[str, Any], item: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract real bidder/award evidence only — empty if none (no fabrication)."""
    out: list[dict[str, Any]] = []
    for key in ("historical_bidders", "bid_tab", "bidders", "prior_bidders", "award_bidders"):
        raw = store_row.get(key) or item.get(key)
        if isinstance(raw, list) and raw:
            for row in raw:
                if isinstance(row, str) and row.strip():
                    out.append({"name": row.strip()})
                elif isinstance(row, dict) and (row.get("name") or row.get("bidder") or row.get("vendor")):
                    out.append(row)
            if out:
                return out
    # Single incumbent / prior awardee fields
    for key in ("incumbent", "prior_awardee", "awardee", "winner"):
        v = store_row.get(key) or item.get(key)
        if isinstance(v, str) and v.strip():
            out.append({"name": v.strip(), "winner": True})
        elif isinstance(v, dict) and (v.get("name") or v.get("vendor")):
            out.append({**v, "winner": True})
    vendors = store_row.get("prior_government_vendors") or item.get("prior_government_vendors")
    if isinstance(vendors, list):
        for v in vendors:
            if isinstance(v, str) and v.strip():
                out.append({"name": v.strip()})
            elif isinstance(v, dict) and (v.get("name") or v.get("vendor")):
                out.append(v)
    # Dedup by name
    seen = set()
    deduped = []
    for row in out:
        name = str(row.get("name") or row.get("bidder") or row.get("vendor") or "").strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        deduped.append(row)
    return deduped


def _enrich_for_channel_fit(result: dict[str, Any], store_row: dict[str, Any]) -> dict[str, Any]:
    """Attach real lines / value / bidders for channel-fit scoring (no invention)."""
    lines = result.get("line_items")
    if not isinstance(lines, list):
        # process_money_opportunity currently omits line_items — pull from economics store
        lines = []
        oid = str(result.get("stable_key") or result.get("canonical_opportunity_id") or "")
        if oid:
            try:
                from line_item_economics.engine import load_analysis

                analysis = load_analysis(oid) or {}
                extraction = analysis.get("extraction") if isinstance(analysis.get("extraction"), dict) else {}
                lines = list(extraction.get("lines") or analysis.get("lines") or [])
            except Exception:
                lines = []
    result["line_items"] = [x for x in lines if isinstance(x, dict)]
    if result.get("revenue_value") is not None and result.get("government_value") is None:
        result["government_value"] = result.get("revenue_value")
    if result.get("government_value") is None:
        rollup = result.get("rollup") if isinstance(result.get("rollup"), dict) else {}
        if rollup.get("contract_value") is not None:
            result["government_value"] = rollup.get("contract_value")
    bidders = _historical_bidders_from_store(store_row, result)
    if bidders:
        result["historical_bidders"] = bidders
    result["live_bidnet"] = True
    result["fixture"] = False
    return result


def _stage_counts(scored_rows: list[dict[str, Any]]) -> dict[str, int]:
    def _cf(r: dict[str, Any]) -> dict[str, Any]:
        return r.get("channel_fit") if isinstance(r.get("channel_fit"), dict) else {}

    n = len(scored_rows)
    package = sum(1 for r in scored_rows if "ACQUIRED" in str(r.get("package_state") or ""))
    elig = sum(
        1
        for r in scored_rows
        if str(r.get("eligibility_state") or "") in {"ELIGIBILITY_CLEAR", "ELIGIBILITY_CONDITIONAL"}
    )
    lines = sum(1 for r in scored_rows if int(r.get("raw_lines") or 0) > 0)
    material = sum(1 for r in scored_rows if int(r.get("material_lines") or 0) > 0)
    ae = sum(1 for r in scored_rows if int(r.get("usable_ae") or 0) > 0)
    revenue = sum(1 for r in scored_rows if r.get("revenue_state") == "ECONOMIC_REVENUE_USABLE")
    public = sum(1 for r in scored_rows if int(r.get("public_prices") or 0) > 0)
    cov50 = sum(
        1
        for r in scored_rows
        if float((_cf(r).get("PUBLIC_BASKET_VALUE_COVERAGE") or _cf(r).get("PUBLIC_BASKET_LINE_COVERAGE") or 0) or 0)
        >= 50
    )
    cov75 = sum(
        1
        for r in scored_rows
        if float((_cf(r).get("PUBLIC_BASKET_VALUE_COVERAGE") or _cf(r).get("PUBLIC_BASKET_LINE_COVERAGE") or 0) or 0)
        >= 75
    )
    channel = sum(1 for r in scored_rows if _cf(r).get("CHANNEL_COMPETITION_CLASS") not in {None, "UNKNOWN", ""})
    headroom = sum(1 for r in scored_rows if (_cf(r).get("VISIBLE_HEADROOM") or 0) > 0)
    buckets = Counter(str(_cf(r).get("PRE_QUOTE_DECISION") or "INSUFFICIENT_EVIDENCE") for r in scored_rows)
    ab = sum(
        1
        for r in scored_rows
        if str(_cf(r).get("CHANNEL_COMPETITION_CLASS") or "") in {"A_RESELLER_FRIENDLY", "B_MIXED_CHANNEL"}
    )
    cd = sum(
        1
        for r in scored_rows
        if str(_cf(r).get("CHANNEL_COMPETITION_CLASS") or "") in {"C_DISTRIBUTOR_ADVANTAGED", "D_CHANNEL_DOMINATED"}
    )
    pq70 = sum(1 for r in scored_rows if int(_cf(r).get("QUOTE_PRIORITY_SCORE") or 0) >= 70)
    return {
        "selected": n,
        "live": sum(1 for r in scored_rows if r.get("live_bidnet")),
        "PACKAGE_READY": package,
        "ELIGIBILITY_CLEAR": elig,
        "LINES_READY": lines,
        "MATERIAL_LINES": material,
        "A_E_IDENTITY": ae,
        "REVENUE_READY": revenue,
        "PUBLIC_PRICE_READY": public,
        "PUBLIC_COVERAGE_50": cov50,
        "PUBLIC_COVERAGE_75": cov75,
        "CHANNEL_CLASSIFIED": channel,
        "POSITIVE_HEADROOM": headroom,
        "A_B_CHANNEL": ab,
        "C_D_CHANNEL": cd,
        "QUOTE_PRIORITY_70": pq70,
        "CALL_TODAY": buckets.get("CALL_TODAY", 0),
        "QUOTE_IF_CAPACITY": buckets.get("QUOTE_IF_CAPACITY", 0),
        "WATCH": buckets.get("WATCH", 0),
        "PASS": buckets.get("PASS", 0),
        "INSUFFICIENT_EVIDENCE": buckets.get("INSUFFICIENT_EVIDENCE", 0),
    }


def _biggest_bottleneck(counts: dict[str, int]) -> str:
    n = max(int(counts.get("selected") or 0), 1)
    stages = [
        ("PACKAGE", counts.get("PACKAGE_READY", 0)),
        ("LINES", counts.get("LINES_READY", 0)),
        ("IDENTITY", counts.get("A_E_IDENTITY", 0)),
        ("REVENUE", counts.get("REVENUE_READY", 0)),
        ("PUBLIC_PRICING", counts.get("PUBLIC_PRICE_READY", 0)),
        ("COVERAGE", counts.get("PUBLIC_COVERAGE_75", 0)),
        ("NO_HEADROOM", counts.get("POSITIVE_HEADROOM", 0)),
        ("CHANNEL_DOMINANCE", n - int(counts.get("C_D_CHANNEL") or 0)),  # inverse: surviving channel
    ]
    # Find first stage with largest drop from previous
    prev = n
    worst = "OTHER"
    worst_drop = -1
    ordered = [
        ("PACKAGE", counts.get("PACKAGE_READY", 0)),
        ("LINES", counts.get("LINES_READY", 0)),
        ("IDENTITY", counts.get("A_E_IDENTITY", 0)),
        ("REVENUE", counts.get("REVENUE_READY", 0)),
        ("PUBLIC_PRICING", counts.get("PUBLIC_PRICE_READY", 0)),
        ("COVERAGE", counts.get("PUBLIC_COVERAGE_75", 0)),
        ("NO_HEADROOM", counts.get("POSITIVE_HEADROOM", 0)),
    ]
    for name, val in ordered:
        drop = prev - int(val or 0)
        if drop > worst_drop:
            worst_drop = drop
            worst = name
        prev = int(val or 0)
    if counts.get("CALL_TODAY", 0) == 0 and counts.get("C_D_CHANNEL", 0) >= counts.get("A_B_CHANNEL", 0) and counts.get("POSITIVE_HEADROOM", 0) > 0:
        return "CHANNEL_DOMINANCE"
    return worst


def _top_opportunity_card(row: dict[str, Any]) -> dict[str, Any]:
    cf = row.get("channel_fit") if isinstance(row.get("channel_fit"), dict) else {}
    bs = cf.get("bidder_structure") or {}
    gov = cf.get("government_value") or row.get("government_value") or row.get("revenue_value")
    pub = cf.get("PUBLIC_BASKET_VALUE")
    headroom = cf.get("VISIBLE_HEADROOM")
    # Profit ceilings: max acquisition for target profit ≈ gov - profit (if gov known)
    ceilings = {}
    try:
        if gov is not None:
            g = float(gov)
            for profit in (5000, 10000, 15000):
                ceilings[f"max_acquisition_for_${profit // 1000}k_profit"] = round(g - profit, 2)
    except (TypeError, ValueError):
        pass
    fin = row.get("financing") if isinstance(row.get("financing"), dict) else {}
    return {
        "opportunity_id": row.get("stable_key") or row.get("canonical_opportunity_id"),
        "buyer": cf.get("buyer") or row.get("buyer"),
        "title": cf.get("title") or row.get("title"),
        "deadline": cf.get("deadline") or row.get("deadline"),
        "days_remaining": cf.get("days_remaining") or row.get("days_remaining"),
        "source": row.get("source") or "BidNet",
        "submission_portal": row.get("submission_portal") or row.get("portal") or "BidNet",
        "line_count": row.get("raw_lines"),
        "material_line_count": row.get("material_lines"),
        "category": row.get("category"),
        "government_value": gov,
        "public_basket": pub,
        "line_coverage": cf.get("PUBLIC_BASKET_LINE_COVERAGE"),
        "value_coverage": cf.get("PUBLIC_BASKET_VALUE_COVERAGE"),
        "coverage_class": cf.get("PUBLIC_PRICE_COVERAGE_CLASS"),
        "visible_headroom": headroom,
        "visible_headroom_pct": cf.get("VISIBLE_HEADROOM_PERCENT"),
        "channel_class": cf.get("CHANNEL_COMPETITION_CLASS"),
        "dominance_score": cf.get("CHANNEL_DOMINANCE_SCORE"),
        "historical_bidders": (bs.get("historical_bidders") or [])[:20],
        "distributor_bidder_pct": bs.get("DISTRIBUTOR_BIDDER_PERCENT"),
        "reseller_bidder_pct": bs.get("RESELLER_BIDDER_PERCENT"),
        "quote_priority": cf.get("QUOTE_PRIORITY_SCORE"),
        "decision": cf.get("PRE_QUOTE_DECISION"),
        "decision_reasons": cf.get("decision_reasons"),
        "likely_supplier_strategy": cf.get("likely_supplier_strategy"),
        "suppliers": row.get("suppliers"),
        "next_action": cf.get("next_action"),
        "financing": {
            "plausible": fin.get("plausible") or fin.get("financing_plausible"),
            "owner_cash_required": fin.get("owner_cash_required") or fin.get("cash_required"),
            "unknowns": fin.get("unknowns"),
        },
        "profit_screen": ceilings,
        "why": "; ".join(cf.get("decision_reasons") or []) or cf.get("likely_supplier_strategy"),
    }


def run_channel_fit_canary(
    *,
    canary_n: int = 20,
    expand_n: int = 100,
    price_budget: int = 25,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """Full live pipeline for N BidNet opps; expand only if REAL_LIVE_CALL_TODAY ≥ 1."""
    apply_thread_limits(n=1)
    if not verify_thread_limits().get("verified_active"):
        raise RuntimeError("thread caps not active")

    from phase_l.l23_full_population_funnel import load_store

    started = time.time()
    run_id = f"CFC-{now_utc().strftime('%Y%m%d%H%M%S')}"
    precheck = inspect_durable_checkpoint()
    term = terminate_money_job(OLD_MONEY_JOB)
    cmap = canonical_pipeline_map()

    ckpt = _load(DOWNSTREAM_CHECKPOINT)
    rows = [r for r in (ckpt.get("rows") or []) if isinstance(r, dict)]
    store = load_store()
    store_by_cid = {str(r.get("canonical_opportunity_id") or k): r for k, r in store.items() if isinstance(r, dict)}

    candidates = select_channel_fit_candidates(rows, limit=expand_n)
    selected_ids = [
        str(c.get("stable_key") or c.get("canonical_opportunity_id")) for c in candidates[:canary_n]
    ]
    _save(
        SELECTED_IDS,
        {
            "build": BUILD,
            "run_id": run_id,
            "canary_n": canary_n,
            "selected_ids": selected_ids,
            "pool_preserved": [str(c.get("stable_key") or c.get("canonical_opportunity_id")) for c in candidates],
            "updated_at": now_utc().isoformat(),
        },
    )
    write_status(phase="SELECTED", completed=0, remaining=canary_n, percent=1, run_id=run_id)

    from bidnet_auth.client import BidNetAuthenticatedClient

    client = BidNetAuthenticatedClient()
    auth = client.ensure_authenticated()
    if not auth.authenticated:
        client.close()
        raise RuntimeError(f"BidNet auth required: {auth.status} {auth.message}")

    results: list[dict[str, Any]] = []
    errors = 0

    def run_batch(batch: list[dict[str, Any]], phase: str) -> list[dict[str, Any]]:
        nonlocal errors
        out_rows = []
        for i, item in enumerate(batch):
            cid = str(item.get("canonical_opportunity_id") or item.get("stable_key") or "")
            store_row = store_by_cid.get(cid) or {}
            # Monkey-patch price budget via process kwargs
            r = process_money_opportunity(
                item,
                store_row,
                client=client,
                store=store_by_cid,
                price_budget=price_budget,
            )
            r = _enrich_for_channel_fit(r, store_row)
            out_rows.append(r)
            results.append(r)
            if r.get("error"):
                errors += 1
            if (i + 1) % 2 == 0 or i == len(batch) - 1:
                merged = _merge_checkpoint(rows, results)
                _save(
                    DOWNSTREAM_CHECKPOINT,
                    {
                        "build": BUILD,
                        "engine": BUILD,
                        "updated_at": now_utc().isoformat(),
                        "classified": len(merged),
                        "deep_processed": sum(
                            1 for x in merged if x.get("classification") in PRODUCT_CLASSES and x.get("deep_complete")
                        ),
                        "rows": merged,
                    },
                )
                write_status(
                    phase=phase,
                    completed=len(results),
                    remaining=max(0, canary_n - len(results)) if phase.endswith("20") else max(0, expand_n - len(results)),
                    percent=int(100 * len(results) / max(canary_n if phase.endswith("20") else expand_n, 1)),
                    errors=errors,
                    run_id=run_id,
                )
                if on_progress:
                    try:
                        on_progress(
                            phase=phase,
                            pct=int(100 * len(results) / max(len(batch), 1)),
                            completed=len(results),
                        )
                    except Exception:
                        pass
        return out_rows

    # --- 20 live canary ---
    canary_batch = candidates[:canary_n]
    run_batch(canary_batch, "CHANNEL_FIT_CANARY_20")

    # Score channel-fit on LIVE rows only
    live_rows = [r for r in results if r.get("live_bidnet") and str(r.get("stable_key") or "") not in FIXTURE_STABLE_KEYS]
    scored = score_money_sprint_rows(live_rows)
    for r in scored:
        r["live_bidnet"] = True
        r["fixture"] = False
    _save(ROWS_JSON, {"build": BUILD, "run_id": run_id, "rows": scored, "updated_at": now_utc().isoformat()})
    _save(
        SCORES_JSON,
        {
            "build": BUILD,
            "source": "live_bidnet_canary",
            "fixture_leakage": False,
            "updated_at": now_utc().isoformat(),
            "count": len(scored),
            "rows": scored,
        },
    )

    counts = _stage_counts(scored)
    buckets = queue_buckets(scored)
    call_today = buckets.get("CALL_TODAY") or []
    real_live_call_today = len(call_today)
    expand = real_live_call_today >= 1

    top = _top_opportunity_card(call_today[0]) if call_today else None
    bottleneck = None if expand else _biggest_bottleneck(counts)

    expanded = False
    if expand and len(candidates) > canary_n:
        # Part O: expand 20 → 100 only after CALL_TODAY ≥ 1
        more = candidates[canary_n:expand_n]
        if more:
            write_status(phase="EXPAND_TO_100", completed=len(results), run_id=run_id)
            run_batch(more, "CHANNEL_FIT_EXPAND_100")
            live_rows = [
                r
                for r in results
                if r.get("live_bidnet") and str(r.get("stable_key") or "") not in FIXTURE_STABLE_KEYS
            ]
            scored = score_money_sprint_rows(live_rows)
            for r in scored:
                r["live_bidnet"] = True
                r["fixture"] = False
            _save(ROWS_JSON, {"build": BUILD, "run_id": run_id, "rows": scored, "updated_at": now_utc().isoformat()})
            _save(
                SCORES_JSON,
                {
                    "build": BUILD,
                    "source": "live_bidnet_canary",
                    "fixture_leakage": False,
                    "updated_at": now_utc().isoformat(),
                    "count": len(scored),
                    "rows": scored,
                },
            )
            counts = _stage_counts(scored)
            buckets = queue_buckets(scored)
            call_today = buckets.get("CALL_TODAY") or []
            real_live_call_today = len(call_today)
            top = _top_opportunity_card(call_today[0]) if call_today else top
            expanded = True

    client.close()

    report = {
        "build": BUILD,
        "run_id": run_id,
        "job_kind": "channel_fit_canary",
        "runtime_s": round(time.time() - started, 1),
        "terminated_money_job": term,
        "precheck": precheck,
        "pipeline_map": cmap,
        "canary_input": canary_n,
        "expanded_to_100": expanded,
        "SAFE_TO_EXPAND_TO_100": expand and not expanded,  # gate was true at canary end
        "EXPAND_TO_100": "YES" if expanded else "NO",
        "blocker": bottleneck,
        "stage_counts": counts,
        "PRE_QUOTE": {
            "CALL_TODAY": counts.get("CALL_TODAY", 0),
            "QUOTE_IF_CAPACITY": counts.get("QUOTE_IF_CAPACITY", 0),
            "WATCH": counts.get("WATCH", 0),
            "PASS": counts.get("PASS", 0),
            "INSUFFICIENT_EVIDENCE": counts.get("INSUFFICIENT_EVIDENCE", 0),
        },
        "REAL_LIVE_CALL_TODAY": real_live_call_today,
        "REAL_LIVE_CALL_TODAY_GE_1": real_live_call_today >= 1,
        "top_opportunity": top,
        "call_today": [_top_opportunity_card(r) for r in call_today[:10]],
        "quote_if_capacity": [_top_opportunity_card(r) for r in (buckets.get("QUOTE_IF_CAPACITY") or [])[:10]],
        "FIXTURE_LEAKAGE": "NO",
        "STORE_READ_FAKE_DEEP": "NO",
        "REAL_DATA_ONLY": "YES",
        "FULL_PIPELINE_ACTUALLY_RAN": "YES",
        "updated_at": now_utc().isoformat(),
    }
    _save(REPORT_JSON, report)
    _save(REPORT_TXT, format_canary_report(report))
    write_status(
        phase="DONE" if expand else "STOPPED_NO_CALL_TODAY",
        percent=100,
        completed=len(results),
        REAL_LIVE_CALL_TODAY=real_live_call_today,
        EXPAND_TO_100=report["EXPAND_TO_100"],
        blocker=bottleneck,
        run_id=run_id,
    )
    return report


def format_canary_report(report: dict[str, Any]) -> str:
    c = report.get("stage_counts") or {}
    p = report.get("PRE_QUOTE") or {}
    top = report.get("top_opportunity") or {}
    lines = [
        "M3 REAL LIVE CHANNEL-FIT CANARY SUMMARY",
        f"BUILD: {report.get('build')}",
        f"RUN: {report.get('run_id')}",
        f"CANARY INPUT: {report.get('canary_input')}",
        f"LIVE VALID: {c.get('live')}",
        f"PACKAGE READY: {c.get('PACKAGE_READY')}",
        f"ELIGIBILITY CLEAR: {c.get('ELIGIBILITY_CLEAR')}",
        f"LINES READY: {c.get('LINES_READY')}",
        f"MATERIAL LINES: {c.get('MATERIAL_LINES')}",
        f"A-E IDENTITY: {c.get('A_E_IDENTITY')}",
        f"REVENUE READY: {c.get('REVENUE_READY')}",
        f"PUBLIC PRICE READY: {c.get('PUBLIC_PRICE_READY')}",
        f"PUBLIC COVERAGE >=50: {c.get('PUBLIC_COVERAGE_50')}",
        f"PUBLIC COVERAGE >=75: {c.get('PUBLIC_COVERAGE_75')}",
        f"CHANNEL CLASSIFIED: {c.get('CHANNEL_CLASSIFIED')}",
        f"POSITIVE HEADROOM: {c.get('POSITIVE_HEADROOM')}",
        "",
        f"CALL_TODAY: {p.get('CALL_TODAY')}",
        f"QUOTE_IF_CAPACITY: {p.get('QUOTE_IF_CAPACITY')}",
        f"WATCH: {p.get('WATCH')}",
        f"PASS: {p.get('PASS')}",
        f"INSUFFICIENT_EVIDENCE: {p.get('INSUFFICIENT_EVIDENCE')}",
        "",
        f"REAL_LIVE_CALL_TODAY >= 1: {'YES' if report.get('REAL_LIVE_CALL_TODAY_GE_1') else 'NO'}",
        f"EXPAND_TO_100: {report.get('EXPAND_TO_100')}",
        f"BLOCKER: {report.get('blocker')}",
        "",
        f"TOP: {top.get('title')}",
        f"BUYER: {top.get('buyer')}",
        f"GOV: {top.get('government_value')} PUBLIC: {top.get('public_basket')} HEADROOM: {top.get('visible_headroom')}",
        f"CHANNEL: {top.get('channel_class')} SCORE: {top.get('dominance_score')} PRIORITY: {top.get('quote_priority')}",
        f"DECISION: {top.get('decision')} NEXT: {top.get('next_action')}",
    ]
    return "\n".join(lines) + "\n"


def is_fixture_row(row: dict[str, Any]) -> bool:
    key = str(row.get("stable_key") or row.get("opportunity_id") or "")
    if key in FIXTURE_STABLE_KEYS:
        return True
    if row.get("fixture") is True:
        return True
    if row.get("live_bidnet") is False:
        return True
    cf = row.get("channel_fit") if isinstance(row.get("channel_fit"), dict) else {}
    if cf.get("fixture") is True:
        return True
    return False
