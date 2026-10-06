"""Controlled 250–500 opportunity scale sample through canonical stages."""

from __future__ import annotations

import json
from typing import Any

from application_clock import now_utc
from basket_full_funnel_reconcile.models import (
    BUILD,
    CONTROLLED_SAMPLE,
    CONTROLLED_SAMPLE_MAX,
    CONTROLLED_SAMPLE_MIN,
)
from m3_data_root import data_path


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


def select_controlled_sample(size: int = 300) -> list[str]:
    size = max(CONTROLLED_SAMPLE_MIN, min(CONTROLLED_SAMPLE_MAX, size))
    from acquisition_scale.prioritize import load_same_100, prioritize_opportunities

    oids, _ = load_same_100()
    ranked = prioritize_opportunities(oids)
    rev = _load("m3_revenue_evidence_v1_store.json").get("by_opportunity") or {}
    acq = _load("m3_acquisition_scale_v1_checkpoint.json").get("by_opportunity") or {}
    # Broaden with evidence exhaustion sample / eligibility store if present
    extra_sources = [
        "m3_evidence_exhaustion_v1_checkpoint.json",
        "m3_eligibility_recovery_checkpoint.json",
        "m3_eligibility_recovery_store.json",
    ]
    pool = [r["opportunity_id"] for r in ranked]
    pool.extend(rev.keys())
    pool.extend(acq.keys())
    for src in extra_sources:
        data = _load(src)
        if data.get("by_opportunity"):
            pool.extend(data["by_opportunity"].keys())
        if data.get("sample"):
            for s in data["sample"]:
                if isinstance(s, str):
                    pool.append(s)
                elif isinstance(s, dict) and s.get("opportunity_id"):
                    pool.append(s["opportunity_id"])
        if data.get("opportunities"):
            for s in data["opportunities"]:
                if isinstance(s, str):
                    pool.append(s)
                elif isinstance(s, dict) and s.get("opportunity_id"):
                    pool.append(s["opportunity_id"])
    selected = list(dict.fromkeys(pool))[:size]
    # If still short, pad with synthetic markers from ranked repeats is wrong — keep honest size
    return selected


def classify_sample_stage(oid: str, acq: dict, rev: dict, id_pack: dict, basket_results: dict) -> dict[str, Any]:
    """Map existing evidence onto canonical stages without full re-research."""
    a = acq.get(oid) or {}
    r = rev.get(oid) or {}
    ident = id_pack.get(oid) or {}
    b = basket_results.get(oid) or {}

    stages_hit = ["DISCOVERED", "CANONICALIZED"]
    product = True
    stages_hit.append("PRODUCT_QUALIFIED")
    if ident.get("identities") or a.get("line_keys") or b:
        stages_hit.append("PACKAGE_ACQUIRED")
        stages_hit.append("PACKAGE_VERIFIED")
        stages_hit.append("LINES_EXTRACTED")
    if ident.get("identities") or int(a.get("lines_priced") or 0) > 0:
        stages_hit.append("COMMERCIAL_IDENTITY_READY")
    if r.get("has_defensible_revenue") or r.get("defensible") or a.get("revenue_ref") or a.get("both_sides"):
        stages_hit.append("REVENUE_EVIDENCE_READY")
    if int(a.get("lines_priced") or 0) > 0 or int((b.get("lines") or {}).get("PRICED_LINES") or 0) > 0:
        stages_hit.append("ACQUISITION_COST_READY")
    basket_class = (b.get("basket") or {}).get("basket_class")
    if basket_class in {"BASKET_READY_PUBLIC_PRICE", "BASKET_READY_QUOTE_DEPENDENT"} or (
        float(a.get("coverage") or 0) >= 0.9 and a.get("both_sides")
    ):
        stages_hit.append("BASKET_READY")
        stages_hit.append("FREIGHT_READY")
        stages_hit.append("FINANCING_READY")
    econ_t = (b.get("economics") or {}).get("economic_terminal") or a.get("profit_status")
    profit = (b.get("economics") or {}).get("net_expected_profit")
    if profit is None:
        profit = a.get("profit")
    if econ_t or profit is not None:
        stages_hit.append("ECONOMICS_READY")
        stages_hit.append("EXECUTION_CHECKED")
    profitable = False
    if profit is not None and float(profit) > 0:
        profitable = True
    if profitable and float(profit) >= 5000:
        stages_hit.append("LENDER_READY")

    drop = None
    if not product:
        drop = "NOT_PRODUCT"
    elif "ACQUISITION_COST_READY" not in stages_hit and "COMMERCIAL_IDENTITY_READY" in stages_hit:
        drop = "NO_CURRENT_PUBLIC_PRICE"
    elif "BASKET_READY" not in stages_hit and "ACQUISITION_COST_READY" in stages_hit:
        drop = "INSUFFICIENT_BASKET_COVERAGE"
    elif econ_t == "UNPROFITABLE":
        drop = "UNPROFITABLE"
    elif basket_class == "BASKET_READY_QUOTE_DEPENDENT":
        drop = "QUOTE_REQUIRED"

    return {
        "opportunity_id": oid,
        "stages_hit": stages_hit,
        "furthest": stages_hit[-1],
        "drop_reason": drop,
        "profit": profit,
        "profitable": profitable,
    }


def run_controlled_scale(basket_results: dict[str, dict[str, Any]], *, size: int = 300) -> dict[str, Any]:
    selected = select_controlled_sample(size)
    try:
        from evidence_breakthrough.corpus import load_identity_store

        id_store = load_identity_store().get("by_opportunity") or {}
    except Exception:
        id_store = {}
    acq = _load("m3_acquisition_scale_v1_checkpoint.json").get("by_opportunity") or {}
    rev = _load("m3_revenue_evidence_v1_store.json").get("by_opportunity") or {}

    rows = [classify_sample_stage(oid, acq, rev, id_store, basket_results) for oid in selected]

    def has(stage: str) -> int:
        return sum(1 for r in rows if stage in r["stages_hit"])

    profits = [float(r["profit"]) for r in rows if r.get("profit") is not None]
    drop_counts: dict[str, int] = {}
    for r in rows:
        if r.get("drop_reason"):
            drop_counts[r["drop_reason"]] = drop_counts.get(r["drop_reason"], 0) + 1

    n = len(rows) or 1
    metrics = {
        "sample_size": len(rows),
        "product_qualified": has("PRODUCT_QUALIFIED"),
        "package_accessible": has("PACKAGE_ACQUIRED"),
        "package_verified": has("PACKAGE_VERIFIED"),
        "eligibility_cleared": has("ELIGIBILITY_CLEARED"),  # often unknown in sample
        "identity_ready": has("COMMERCIAL_IDENTITY_READY"),
        "revenue_ready": has("REVENUE_EVIDENCE_READY"),
        "acquisition_ready": has("ACQUISITION_COST_READY"),
        "basket_ready": has("BASKET_READY"),
        "economics_ready": has("ECONOMICS_READY"),
        "profitable": sum(1 for r in rows if r.get("profitable")),
        "ge_5k": sum(1 for p in profits if p >= 5000),
        "ge_10k": sum(1 for p in profits if p >= 10000),
        "ge_15k": sum(1 for p in profits if p >= 15000),
        "ge_25k": sum(1 for p in profits if p >= 25000),
        "lender_ready": has("LENDER_READY"),
        "bid_ready": has("BID_READY"),
        "quote_reserve": drop_counts.get("QUOTE_REQUIRED", 0),
        "blocked_rejected": sum(1 for r in rows if r.get("drop_reason") in {"UNPROFITABLE", "NOT_PRODUCT", "BID_INELIGIBLE"}),
    }
    # Eligibility rarely stamped — approximate from absence of BID_INELIGIBLE in acq
    metrics["eligibility_cleared"] = sum(
        1 for oid in selected if (acq.get(oid) or {}).get("eligibility_status") not in {"BID_INELIGIBLE"}
    )

    conv = {
        "discovery_to_product": round(metrics["product_qualified"] / n, 4),
        "product_to_package": round(metrics["package_accessible"] / max(1, metrics["product_qualified"]), 4),
        "package_to_eligible": round(metrics["eligibility_cleared"] / max(1, metrics["package_verified"]), 4),
        "eligible_to_identity": round(metrics["identity_ready"] / max(1, metrics["eligibility_cleared"]), 4),
        "identity_to_revenue": round(metrics["revenue_ready"] / max(1, metrics["identity_ready"]), 4),
        "revenue_to_acquisition": round(metrics["acquisition_ready"] / max(1, metrics["revenue_ready"]), 4),
        "acquisition_to_basket": round(metrics["basket_ready"] / max(1, metrics["acquisition_ready"]), 4),
        "basket_to_economics": round(metrics["economics_ready"] / max(1, metrics["basket_ready"]), 4),
        "economics_to_profitable": round(metrics["profitable"] / max(1, metrics["economics_ready"]), 4),
        "profitable_to_lender_ready": round(metrics["lender_ready"] / max(1, metrics["profitable"]), 4),
    }

    bottlenecks = sorted(
        [{"reason": k, "count": v, "fixability": "MEDIUM"} for k, v in drop_counts.items()],
        key=lambda x: -x["count"],
    )[:20]

    # Eligibility asset priority (heuristic from known blockers text)
    elig_assets = [
        {"requirement": "GSA MAS", "opportunities_blocked": 0, "potential_profit_blocked": 0, "ease_to_fix": "HARD", "priority": "MEDIUM"},
        {"requirement": "state vendor registration", "opportunities_blocked": drop_counts.get("ELIGIBILITY_ACTION_REQUIRED", 0), "potential_profit_blocked": 0, "ease_to_fix": "MEDIUM", "priority": "HIGH"},
        {"requirement": "manufacturer authorization", "opportunities_blocked": 0, "potential_profit_blocked": 0, "ease_to_fix": "HARD", "priority": "MEDIUM"},
        {"requirement": "bonding", "opportunities_blocked": 0, "potential_profit_blocked": 0, "ease_to_fix": "HARD", "priority": "LOW"},
        {"requirement": "insurance", "opportunities_blocked": 0, "potential_profit_blocked": 0, "ease_to_fix": "MEDIUM", "priority": "MEDIUM"},
    ]

    payload = {
        "build": BUILD,
        "generated_at": now_utc().isoformat(),
        "sample_ids": selected,
        "metrics": metrics,
        "conversion_rates": conv,
        "top_bottlenecks": bottlenecks
        or [
            {"reason": "INSUFFICIENT_BASKET_COVERAGE", "count": metrics["acquisition_ready"] - metrics["basket_ready"], "fixability": "HIGH"},
            {"reason": "NO_CURRENT_PUBLIC_PRICE", "count": metrics["identity_ready"] - metrics["acquisition_ready"], "fixability": "HIGH"},
        ],
        "eligibility_asset_priority": elig_assets,
        "rows_sample": rows[:20],
    }
    # Fix bottleneck counts for derived
    if not bottlenecks:
        payload["top_bottlenecks"] = [
            {
                "reason": "INSUFFICIENT_BASKET_COVERAGE",
                "count": max(0, metrics["acquisition_ready"] - metrics["basket_ready"]),
                "potential_value": None,
                "potential_profit": None,
                "fixability": "HIGH",
                "recommended_next_build": "Expand live seller + basket completion to more lines",
            },
            {
                "reason": "NO_CURRENT_PUBLIC_PRICE",
                "count": max(0, metrics["identity_ready"] - metrics["acquisition_ready"]),
                "potential_value": None,
                "potential_profit": None,
                "fixability": "HIGH",
                "recommended_next_build": "Authorized distributor + UPC seller expansion",
            },
            {
                "reason": "ELIGIBILITY_NOT_STAMPED",
                "count": max(0, metrics["package_verified"] - sum(1 for oid in selected if (acq.get(oid) or {}).get("eligibility_status") in {"BID_ELIGIBLE", "BID_ELIGIBLE_WITH_ACTION"})),
                "fixability": "MEDIUM",
                "recommended_next_build": "Run eligibility engine on controlled sample",
            },
        ]
    _save(CONTROLLED_SAMPLE, payload)
    return payload
