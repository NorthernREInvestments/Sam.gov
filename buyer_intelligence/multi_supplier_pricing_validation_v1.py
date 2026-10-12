"""Isolated before/after multi-supplier pricing validation + Burks benchmark.

Does not touch production stores or active Batch A checkpoints.
Writes only under data/_og_commercial_research_pipeline_out/.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from application_clock import now_utc
from buyer_intelligence.multi_supplier_price_discovery_v1 import (
    BUILD as MULTI_BUILD,
    discover_multi_supplier_prices,
    lookup_retail_multi,
)

BUILD = "20261010-m3-multi-supplier-pricing-validation-v1"
OUT_DIR = "_og_commercial_research_pipeline_out"
REPORT = "m3_multi_supplier_pricing_validation_report_v1.json"
BURKS_REPORT = "m3_burks_multi_supplier_benchmark_v1.json"
BEFORE_AFTER = "m3_multi_supplier_before_after_cohort_v1.json"

# Leads only — must be freshly verified, never hardcoded into resolver
BURKS_LEADS = [
    {"label": "prior_m3", "unit_price": 3140.25, "note": "Original M3 supplier price (baseline)"},
    {"label": "lead_a", "unit_price": 2334.36, "note": "External research lead — verify"},
    {"label": "lead_b", "unit_price": 2318.88, "note": "External research lead — verify"},
    {"label": "lead_c", "unit_price": 2308.16, "note": "External research lead — verify"},
]


def _utc() -> str:
    return now_utc().isoformat()


def _canon() -> Path:
    return Path(__file__).resolve().parents[1] / "data"


def _out() -> Path:
    d = _canon() / OUT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save(name: str, obj: Any) -> Path:
    p = _out() / name
    p.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    return p


def _load_json(*candidates: str) -> Any:
    for c in candidates:
        p = _canon() / c
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
        p2 = _out() / c
        if p2.exists():
            try:
                return json.loads(p2.read_text(encoding="utf-8"))
            except Exception:
                continue
    return None


def burks_benchmark() -> dict[str, Any]:
    """Fresh multi-supplier discovery for Burks T310WT5 — no hardcoded winners."""
    t0 = time.time()
    identity = {
        "manufacturer": "Burks",
        "model": "T310WT5",
        "part_number": "T310WT5",
        "catalog_number": "H18PG00",
        "raw_description": "Burks T310WT5 H18PG00 turbine pump",
    }
    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        multi = discover_multi_supplier_prices(
            identity,
            client=client,
            max_queries=5,
            max_pages=10,
            min_suppliers_target=3,
            qty=10,
            use_budget=False,  # isolated benchmark; do not deplete global budget
        )
    best = multi.get("BEST_OFFER")
    lowest = multi.get("LOWEST_VERIFIED_UNIT_PRICE")
    baseline = 3140.25
    delta_vs_baseline = None
    if lowest is not None:
        delta_vs_baseline = round((baseline - float(lowest)) * 10, 2)

    report = {
        "kind": "BurksMultiSupplierBenchmark",
        "build": BUILD,
        "multi_build": MULTI_BUILD,
        "created_at": _utc(),
        "IDENTITY": identity,
        "BASELINE_M3_UNIT_PRICE": baseline,
        "EXTERNAL_LEADS_UNVERIFIED": BURKS_LEADS,
        "AUTOMATED_MULTI_SUPPLIER": multi,
        "VERIFIED_LOWEST_UNIT_PRICE": lowest,
        "VERIFIED_SUPPLIER_COUNT": multi.get("SUPPLIER_COUNT"),
        "EXACT_VERIFIED_COUNT": multi.get("EXACT_VERIFIED_COUNT"),
        "BEST_OFFER": best,
        "DELTA_EXTENDED_10_UNITS_VS_BASELINE": delta_vs_baseline,
        "FULFILLMENT_REMAINING": [
            "Confirm exact T310WT5 / H18PG00 configuration on supplier PDP",
            "Verify freight to delivery location",
            "Verify lead time vs solicitation deadline",
            "Confirm payment terms (Net terms / card) — currently UNKNOWN",
            "Confirm stock/availability",
        ],
        "HARDCODED_BENCHMARK_IN_RESOLVER": False,
        "WALL_CLOCK_S": round(time.time() - t0, 2),
        "SAM_API_CALLS": 0,
    }
    _save(BURKS_REPORT, report)
    return report


def _before_metrics_from_artifacts() -> dict[str, Any]:
    consol = _load_json(
        "_og_commercial_id_val_out/m3_opengov_commercial_id_bottleneck_consolidated_v1.json"
    ) or {}
    before = (consol.get("BEFORE_AFTER") or {}).get("BEFORE") or {}
    batch_a = _load_json("m3_scalable_batch_report_v1.json") or {}
    cum = _load_json("m3_scalable_cumulative_findings_v1.json") or {}
    manual = _load_json(
        "_og_commercial_id_val_out/m3_opengov_commercial_id_pricing_repair_v1_manual_evidence.json"
    ) or {}
    return {
        "consolidated_before": before,
        "batch_a_summary": {
            "FINAL_STATUS": batch_a.get("FINAL_STATUS"),
            "BATCH": batch_a.get("BATCH"),
            "STATS": batch_a.get("BATCH_STATS") or batch_a.get("stats"),
        },
        "cumulative": {
            k: cum.get(k)
            for k in (
                "owner_review_candidates",
                "execution_ready",
                "proposed_bid_only",
                "liberty",
                "burks",
            )
            if k in cum or True
        },
        "manual_evidence_keys": list(manual.keys())[:20] if isinstance(manual, dict) else [],
        "SOURCE_NOTE": "BEFORE metrics from existing artifacts — not re-run",
    }


# Representative durable product identities previously researched / failed
COHORT: list[dict[str, Any]] = [
    {
        "id": "burks_t310wt5",
        "manufacturer": "Burks",
        "part_number": "T310WT5",
        "model": "T310WT5",
        "catalog_number": "H18PG00",
        "raw_description": "Burks turbine pump T310WT5",
        "qty": 10,
        "prior_unit": 3140.25,
        "prior_source": "manual_m3",
    },
    {
        "id": "liberty_le51a2",
        "manufacturer": "Liberty",
        "part_number": "LE51A-2",
        "model": "LE51A-2",
        "raw_description": "Liberty sewage pump LE51A-2",
        "qty": 1,
        "prior_unit": 459.10,
        "prior_source": "manual_m3",
    },
    {
        "id": "liberty_le51m2",
        "manufacturer": "Liberty",
        "part_number": "LE51M-2",
        "model": "LE51M-2",
        "raw_description": "Liberty sewage pump LE51M-2",
        "qty": 1,
        "prior_unit": 468.46,
        "prior_source": "manual_m3",
    },
    {
        "id": "liberty_2572",
        "manufacturer": "Liberty",
        "part_number": "257-2",
        "model": "257-2",
        "raw_description": "Liberty pump accessory 257-2",
        "qty": 1,
        "prior_unit": 232.50,
        "prior_source": "manual_m3",
    },
    {
        "id": "paper_tm1616s",
        "manufacturer": "Tork",
        "part_number": "TM1616S",
        "model": "TM1616S",
        "raw_description": "Tork paper towel TM1616S",
        "qty": 10,
        "prior_unit": None,
        "prior_source": "manual_paper",
    },
]


def run_cohort_validation(*, max_products: int = 5) -> dict[str, Any]:
    t0 = time.time()
    before = _before_metrics_from_artifacts()
    cache = _load_json(
        "_og_profit_val_out/m3_commercial_acquisition_cache_v1.json",
        "m3_commercial_acquisition_cache_v1.json",
    ) or {"entries": {}}

    rows = []
    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        for item in COHORT[:max_products]:
            ident = {
                "manufacturer": item.get("manufacturer"),
                "model": item.get("model"),
                "part_number": item.get("part_number"),
                "catalog_number": item.get("catalog_number"),
                "raw_description": item.get("raw_description"),
            }
            t1 = time.time()
            multi = discover_multi_supplier_prices(
                ident,
                client=client,
                qty=item.get("qty"),
                use_budget=False,
                max_queries=4,
                max_pages=8,
            )
            retail = lookup_retail_multi(
                ident, client=client, allow_live=False, cache=cache, qty=item.get("qty")
            )
            # Prefer live multi; cache used only as supplemental
            best = multi.get("BEST_OFFER")
            rows.append(
                {
                    "PRODUCT_ID": item["id"],
                    "IDENTITY": ident,
                    "PRIOR_UNIT": item.get("prior_unit"),
                    "PRIOR_SOURCE": item.get("prior_source"),
                    "AFTER_SUPPLIER_COUNT": multi.get("SUPPLIER_COUNT"),
                    "AFTER_EXACT_VERIFIED": multi.get("EXACT_VERIFIED_COUNT"),
                    "AFTER_LOWEST_UNIT": multi.get("LOWEST_VERIFIED_UNIT_PRICE"),
                    "AFTER_BEST_SUPPLIER": multi.get("LOWEST_VERIFIED_SUPPLIER"),
                    "AFTER_BEST_URL": multi.get("LOWEST_VERIFIED_URL"),
                    "AFTER_OFFERS": multi.get("OFFERS"),
                    "CACHE_HIT_UNIT": retail.get("RETAIL_UNIT_PRICE"),
                    "IMPROVED_VS_PRIOR": (
                        bool(
                            item.get("prior_unit")
                            and multi.get("LOWEST_VERIFIED_UNIT_PRICE")
                            and float(multi["LOWEST_VERIFIED_UNIT_PRICE"])
                            < float(item["prior_unit"]) * 0.98
                        )
                    ),
                    "ELAPSED_S": round(time.time() - t1, 2),
                    "RESOLVER_STATUS": multi.get("RESOLVER_STATUS"),
                }
            )

    priced = [r for r in rows if r.get("AFTER_LOWEST_UNIT")]
    multi_ge_2 = [r for r in rows if (r.get("AFTER_SUPPLIER_COUNT") or 0) >= 2]
    multi_ge_3 = [r for r in rows if (r.get("AFTER_SUPPLIER_COUNT") or 0) >= 3]
    improved = [r for r in rows if r.get("IMPROVED_VS_PRIOR")]

    table = {
        "products_in_cohort": len(rows),
        "products_with_public_price_after": len(priced),
        "products_with_ge_2_suppliers": len(multi_ge_2),
        "products_with_ge_3_suppliers": len(multi_ge_3),
        "avg_suppliers_per_priced_product": round(
            sum(r.get("AFTER_SUPPLIER_COUNT") or 0 for r in priced) / max(1, len(priced)), 2
        ),
        "improved_vs_prior_unit_price": len(improved),
        "execution_ready": 0,  # none claimed without financing/freight confirmation
        "owner_review_candidates": len(improved),
    }

    report = {
        "kind": "MultiSupplierBeforeAfterCohort",
        "build": BUILD,
        "created_at": _utc(),
        "BEFORE": before,
        "AFTER_COHORT_ROWS": rows,
        "AFTER_TABLE": table,
        "METRICS_TABLE": {
            "Products correctly identified": {"before": "manual/partial", "after": len(rows)},
            "Public supplier listings found": {
                "before": "often 0–1 automated",
                "after": sum(r.get("AFTER_SUPPLIER_COUNT") or 0 for r in rows),
            },
            "Exact verified supplier matches": {
                "before": "manual Liberty/Burks",
                "after": sum(r.get("AFTER_EXACT_VERIFIED") or 0 for r in rows),
            },
            "Average suppliers per resolved product": {
                "before": "~1 (single best)",
                "after": table["avg_suppliers_per_priced_product"],
            },
            "Opportunities with modeled $5k+ profit": {
                "before": "Liberty/Burks proposed-bid only",
                "after": "not re-projected in this isolated pricing pass",
            },
            "Execution-ready deals": {"before": 0, "after": 0},
        },
        "WALL_CLOCK_S": round(time.time() - t0, 2),
        "SAM_API_CALLS": 0,
        "ISOLATION": {"TOUCHED_PRODUCTION": False, "TOUCHED_BATCH_A_CHECKPOINT": False},
    }
    _save(BEFORE_AFTER, report)
    return report


def run_all() -> dict[str, Any]:
    burks = burks_benchmark()
    cohort = run_cohort_validation(max_products=5)
    summary = {
        "kind": "MultiSupplierPricingValidationSummary",
        "build": BUILD,
        "created_at": _utc(),
        "BURKS": {
            "verified_lowest": burks.get("VERIFIED_LOWEST_UNIT_PRICE"),
            "supplier_count": burks.get("VERIFIED_SUPPLIER_COUNT"),
            "exact_count": burks.get("EXACT_VERIFIED_COUNT"),
            "delta_10_vs_3140": burks.get("DELTA_EXTENDED_10_UNITS_VS_BASELINE"),
            "file": BURKS_REPORT,
        },
        "COHORT": cohort.get("AFTER_TABLE"),
        "files": {
            "burks": BURKS_REPORT,
            "cohort": BEFORE_AFTER,
            "summary": REPORT,
        },
    }
    _save(REPORT, summary)
    return summary


if __name__ == "__main__":
    print(json.dumps(run_all(), indent=2, default=str))
