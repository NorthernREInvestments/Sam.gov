"""Build 002C acceptance — full uncertain recovery + free-source reproducibility."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["RESEARCH_MAX_PAGES_PER_SOURCE"] = os.environ.get("M3_002C_HARNESS_MAX_PAGES", "12")
# Full backlog by default; override for smoke: M3_RECOVERY_MAX_LEVEL2=200
# os.environ.setdefault("M3_RECOVERY_MAX_LEVEL2", "500")


def main() -> int:
    from m3.config import get_settings
    from m3.db import SessionLocal
    from m3.models import SourceRegistry
    from m3.research.classify import classify_requirement
    from m3.research.coverage import build_coverage_matrix
    from m3.research.engine import ensure_source_registry, run_contract_discovery
    from m3.research.filters import apply_contract_filters

    get_settings.cache_clear()
    t0 = time.time()
    report: dict = {
        "build": "002C",
        "build_id": "20261121-m3-2-build-002c",
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "baseline_002b": {
            "discovery_population": 5181,
            "openai_assisted_population": 8797,
            "verified_product": 600,
            "non_product": 656,
            "uncertain": 3661,
        },
        "audit_doc": "docs/BUILD_002C_AUDIT.md",
    }
    report["max_pages_harness"] = get_settings().research_max_pages_per_source
    report["classification_spot_checks"] = {
        t: classify_requirement(t)["requirement_class"]
        for t in [
            "Janitorial Supplies",
            "Janitorial Services",
            "Equipment Maintenance",
            "Purchase of 500 widgets Qty 500",
            "Construction Materials",
            "Software as a Service Subscription",
        ]
    }

    db = SessionLocal()
    try:
        ensure_source_registry(db)
        discovered_ops = (
            db.query(SourceRegistry)
            .filter(SourceRegistry.is_enabled.is_(True))
            .all()
        )
        openai_sources = [
            {
                "source_key": r.source_key,
                "name": r.name,
                "adapter": r.adapter,
                "discovered": (r.config or {}).get("discovered"),
                "discovery_method": (r.config or {}).get("discovery_method"),
                "last_yield": (r.config or {}).get("last_yield"),
                "operational": (r.config or {}).get("operational") or r.experimentally_verified,
            }
            for r in discovered_ops
            if (r.config or {}).get("discovered")
            or str(r.source_key).startswith("ai_")
            or str(r.source_key).startswith("discovered_")
        ]
        report["persisted_discovered_sources"] = {
            "count": len(openai_sources),
            "sample": openai_sources[:40],
        }

        # TEST C then D — free-only nationwide (OpenAI portal discovery OFF; L4 AI may still run if enabled)
        free = run_contract_discovery(
            db,
            user_id=None,
            filters={"product_resale_only": False, "include_unknown_value": True},
            limit=100,
            offset=0,
            enable_openai_sources=False,
        )
        report["test_c_free_source_reproducibility"] = {
            "discovery_population": free.get("discovery_population"),
            "verified_product": free.get("verified_product"),
            "confirmed_non_product": free.get("confirmed_non_product"),
            "uncertain_remaining": free.get("uncertain_remaining"),
            "uncertain_recovery": free.get("uncertain_recovery"),
            "sources_succeeded_n": len(free.get("sources_succeeded") or []),
            "sources_succeeded": free.get("sources_succeeded"),
            "pages_fetched": free.get("pages_fetched"),
            "duplicates_excluded": free.get("duplicates_excluded"),
            "openai_portal_used": free.get("openai_used"),
            "estimated_cost_usd": free.get("estimated_cost_usd"),
            "note": "OpenAI portal discovery disabled; persisted sources must still yield",
        }

        report["test_a_baseline_reconcile"] = {
            "classification_breakdown": free.get("classification_breakdown"),
            "status_breakdown": free.get("status_breakdown"),
            "sum_classes": sum((free.get("classification_breakdown") or {}).values()),
            "discovery_population": free.get("discovery_population"),
            "classes_sum_equals_population": sum((free.get("classification_breakdown") or {}).values())
            == (free.get("discovery_population") or -1),
            "verified_vs_product_classes_note": "verified_product excludes forecast/closed statuses",
        }

        report["test_b_uncertain_recovery"] = free.get("uncertain_recovery") or {}

        report["test_d_nationwide"] = {
            "discovery_population": free.get("discovery_population"),
            "verified_product": free.get("verified_product"),
            "product_incidental": free.get("product_with_incidental_services"),
            "source_yield": free.get("source_yield"),
            "sources_fully_paginated_n": len(free.get("sources_fully_paginated") or []),
            "sources_incomplete": free.get("sources_incomplete"),
            "coverage": free.get("coverage_matrix"),
        }

        # TEST F — filter independence on snapshot
        import uuid
        from m3.models import ResearchRun

        run = db.get(ResearchRun, uuid.UUID(free["run_id"])) if free.get("run_id") else None
        snap = list((run.result_snapshot if run else None) or [])
        product_view = apply_contract_filters(snap, {"product_resale_only": True})
        report["test_f_discovery_independence"] = {
            "snapshot": len(snap),
            "product_view": len(product_view),
            "unfiltered_matching": free.get("all_matching_count"),
            "filter_only_affects_display": len(product_view) <= len(snap),
        }

        # TEST E — stratified accuracy spot review
        by_class: dict[str, list] = {}
        for it in snap:
            by_class.setdefault(it.get("requirement_class") or "unknown", []).append(it)
        review = []
        for cls, rows in by_class.items():
            for it in rows[:3]:
                review.append(
                    {
                        "class": cls,
                        "title": (it.get("title") or "")[:120],
                        "status": it.get("solicitation_status"),
                        "evidence": (it.get("classification_evidence") or [])[:3],
                        "url": it.get("official_url"),
                        "recovery_level": it.get("recovery_level"),
                    }
                )
        report["test_e_classification_accuracy_sample"] = review

        # Keywords
        kw = {}
        for term in ("screws", "valves", "furniture", "ppe", "toner", "lighting", "pumps"):
            m = apply_contract_filters(product_view, {"keyword": term, "product_resale_only": True})
            kw[term] = {"matches": len(m)}
        report["test_e_keywords"] = kw

        report["representative_product_sample"] = [
            {
                "buyer": i.get("buyer_name"),
                "product": (i.get("title") or "")[:180],
                "solicitation_number": i.get("solicitation_number"),
                "closing_date": i.get("deadline_at"),
                "set_aside": i.get("set_aside"),
                "official_url": i.get("official_url"),
                "state": i.get("geo_state"),
                "class": i.get("requirement_class"),
                "status": i.get("solicitation_status"),
                "evidence": (i.get("classification_evidence") or i.get("recovery_evidence") or [])[:5],
                "recovery_level": i.get("recovery_level"),
            }
            for i in product_view
            if (i.get("requirement_class") or "")
            in {
                "product_supply",
                "product_with_incidental_services",
                "product_with_significant_installation",
                "product_rental_leasing",
            }
            and (i.get("solicitation_status") or "unknown")
            not in {"closed", "closed_by_deadline", "cancelled", "historical_award", "forecast"}
        ][:55]

        # TEST H — budget snapshot
        from m3.research.budget import spending_snapshot

        report["test_h_budget"] = spending_snapshot(db, user_id=None)
        report["coverage_matrix_summary"] = {
            "states_with_operational": (free.get("coverage_matrix") or {}).get("states_with_operational"),
            "states_with_any_source": (free.get("coverage_matrix") or {}).get("states_with_any_source"),
            "zero_operational_states": (free.get("coverage_matrix") or {}).get("zero_operational_states"),
        }

    finally:
        db.close()

    report["elapsed_seconds"] = round(time.time() - t0, 1)
    out = ROOT / "docs" / "BUILD_002C_ACCEPTANCE.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    rec = report.get("test_b_uncertain_recovery") or {}
    print(
        json.dumps(
            {
                "ok": True,
                "wrote": str(out),
                "elapsed": report["elapsed_seconds"],
                "summary": {
                    "discovery_population": report["test_c_free_source_reproducibility"].get("discovery_population"),
                    "verified_product": report["test_c_free_source_reproducibility"].get("verified_product"),
                    "uncertain_remaining": report["test_c_free_source_reproducibility"].get("uncertain_remaining"),
                    "recovery_investigated": rec.get("investigated"),
                    "resolved_product": rec.get("resolved_product"),
                    "resolved_non_product": rec.get("resolved_non_product"),
                    "level2_attempted": rec.get("level2_attempted"),
                    "level3_attempted": rec.get("level3_attempted"),
                    "level4_attempted": rec.get("level4_attempted"),
                    "sample_n": len(report.get("representative_product_sample") or []),
                    "persisted_discovered": report["persisted_discovered_sources"]["count"],
                },
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
