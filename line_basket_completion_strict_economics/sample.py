"""Controlled 100-opportunity sample with strict economics classifier."""

from __future__ import annotations

import json
from typing import Any

from application_clock import now_utc
from line_basket_completion_strict_economics.models import (
    BUILD,
    CONTROLLED_100,
    CONTROLLED_SAMPLE_N,
    ECONOMICS_NOT_READY,
    ECONOMICS_READY,
    PRIOR_ACQ_CK,
    PRIOR_FUNNEL_CK,
    PRIOR_REV,
    PROFIT_LIKELY,
    PROFIT_POSSIBLE,
    PROFIT_PROVEN,
    PROFIT_UNPROVEN,
    UNPROFITABLE,
)
from line_basket_completion_strict_economics.provenance import classify_price_record
from m3_data_root import data_path


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(name: str, payload: dict[str, Any]) -> None:
    p = data_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def select_controlled_100() -> list[str]:
    from acquisition_scale.prioritize import load_same_100, prioritize_opportunities

    oids, _ = load_same_100()
    ranked = prioritize_opportunities(oids)
    pool = [r["opportunity_id"] for r in ranked]
    rev = _load(PRIOR_REV).get("by_opportunity") or {}
    acq = _load(PRIOR_ACQ_CK).get("by_opportunity") or {}
    pool.extend(rev.keys())
    pool.extend(acq.keys())
    # Include prior corpus
    both = _load("m3_both_sides_basket_corpus_v1.json").get("opportunity_ids") or []
    pool = list(both) + pool
    return list(dict.fromkeys(pool))[:CONTROLLED_SAMPLE_N]


def _material_coverage_proxy(oid: str, acq_row: dict, funnel_row: dict) -> float:
    """Honest proxy: only count provenance-valid priced share; else use funnel material cov if recomputed."""
    lines = ((funnel_row.get("lines") or {}).get("lines") or [])
    if lines:
        # Re-evaluate prior priced with provenance
        total = len(lines) or 1
        valid = 0
        for ln in lines:
            if ln.get("terminal_state") != "PRICED_EXECUTABLE":
                continue
            if classify_price_record(ln).get("is_valid_production"):
                valid += 1
        # Prior funnel line_coverage inflated by sentinels — replace with valid/total
        return valid / total

    cov = float(acq_row.get("coverage") or 0)
    # If acq claims coverage but only sentinel prices, treat as 0
    return 0.0 if cov > 0 and int(acq_row.get("lines_priced") or 0) > 0 else cov


def classify_sample_row(
    oid: str,
    *,
    acq: dict,
    rev: dict,
    funnel_ops: dict,
    corpus_results: dict,
) -> dict[str, Any]:
    # Prefer live corpus result when present
    if oid in corpus_results:
        r = corpus_results[oid]
        econ = r.get("economics") or {}
        cov = r.get("coverage") or {}
        basket = r.get("basket") or {}
        mat = float(cov.get("MATERIAL_VALUE_COVERAGE") or 0)
        priced = int(cov.get("priced_executable") or 0)
        econ_ready = econ.get("economics_status") == ECONOMICS_READY
        conf = econ.get("profit_confidence") or PROFIT_UNPROVEN
        profit = econ.get("expected_profit") if econ_ready and conf != PROFIT_UNPROVEN else None
        return {
            "opportunity_id": oid,
            "acquisition_ready": priced > 0,
            "material_coverage": mat,
            "basket_ready": basket.get("basket_class")
            in {"BASKET_READY_PUBLIC_PRICE", "BASKET_READY_QUOTE_DEPENDENT"},
            "economics_ready": econ_ready,
            "profit_confidence": conf,
            "profit": profit,
            "lender_ready": bool(r.get("lender_ready_object")),
            "source": "corpus_live",
        }

    a = acq.get(oid) or {}
    r = rev.get(oid) or {}
    f = funnel_ops.get(oid) or {}
    mat = _material_coverage_proxy(oid, a, f)
    # Valid priced?
    valid_priced = 0
    for ln in ((f.get("lines") or {}).get("lines") or []):
        if ln.get("terminal_state") == "PRICED_EXECUTABLE" and classify_price_record(ln).get("is_valid_production"):
            valid_priced += 1
    if valid_priced == 0 and int(a.get("lines_priced") or 0) > 0:
        # acq cache likely sentinel — not acquisition-ready under strict rules
        acquisition_ready = False
        mat = 0.0
    else:
        acquisition_ready = valid_priced > 0

    # Strict: economics not ready unless material coverage gate
    econ_ready = False
    conf = PROFIT_UNPROVEN
    profit = None
    if mat >= 0.9 and acquisition_ready:
        # Still require revenue
        rev_val = ((r.get("revenue") or {}).get("best") or {}).get("reference_value")
        if rev_val and float(rev_val) > 0:
            econ_ready = True
            # Without line-level cost, still UNPROVEN
            conf = PROFIT_UNPROVEN
    basket_ready = mat >= 0.9 and acquisition_ready

    return {
        "opportunity_id": oid,
        "acquisition_ready": acquisition_ready,
        "material_coverage": round(mat, 4),
        "basket_ready": basket_ready,
        "economics_ready": econ_ready,
        "profit_confidence": conf,
        "profit": profit,
        "lender_ready": False,
        "source": "proxy_strict",
    }


def run_controlled_100(*, corpus_results: dict[str, Any]) -> dict[str, Any]:
    ids = select_controlled_100()
    acq = _load(PRIOR_ACQ_CK).get("by_opportunity") or {}
    rev = _load(PRIOR_REV).get("by_opportunity") or {}
    funnel_ops = _load(PRIOR_FUNNEL_CK).get("opportunities") or {}

    rows = [
        classify_sample_row(oid, acq=acq, rev=rev, funnel_ops=funnel_ops, corpus_results=corpus_results)
        for oid in ids
    ]

    def _ge(th: float) -> int:
        return sum(1 for r in rows if float(r.get("material_coverage") or 0) >= th)

    def _profit_ge(th: float) -> int:
        return sum(
            1
            for r in rows
            if r.get("economics_ready")
            and r.get("profit_confidence") in {PROFIT_PROVEN, PROFIT_LIKELY}
            and r.get("profit") is not None
            and float(r["profit"]) >= th
        )

    summary = {
        "build": BUILD,
        "frozen_at": now_utc().isoformat(),
        "sample_size": len(ids),
        "opportunity_ids": ids,
        "Acquisition-ready": sum(1 for r in rows if r.get("acquisition_ready")),
        ">=25% material": _ge(0.25),
        ">=50%": _ge(0.50),
        ">=75%": _ge(0.75),
        ">=90%": _ge(0.90),
        "Basket-ready": sum(1 for r in rows if r.get("basket_ready")),
        "Economics-ready": sum(1 for r in rows if r.get("economics_ready")),
        "Profit proven": sum(1 for r in rows if r.get("profit_confidence") == PROFIT_PROVEN),
        "Profit likely": sum(1 for r in rows if r.get("profit_confidence") == PROFIT_LIKELY),
        "Profit possible": sum(1 for r in rows if r.get("profit_confidence") == PROFIT_POSSIBLE),
        "Profit unproven": sum(1 for r in rows if r.get("profit_confidence") == PROFIT_UNPROVEN),
        "Unprofitable": sum(1 for r in rows if r.get("profit_confidence") == UNPROFITABLE),
        ">=5K ready": _profit_ge(5000),
        ">=10K ready": _profit_ge(10000),
        ">=25K ready": _profit_ge(25000),
        "Lender-ready": sum(1 for r in rows if r.get("lender_ready")),
        "rows": rows,
        "note": "Strict classifier: sentinel/search-URL prices do not count; thin baskets cannot be profitable.",
    }
    # Persist IDs for sample size assertion
    assert summary["sample_size"] == len(summary["opportunity_ids"])
    _save(CONTROLLED_100, summary)
    return summary
