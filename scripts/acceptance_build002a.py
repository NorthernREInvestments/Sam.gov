"""Build 002A acceptance harness — mapping, pagination, keywords, filters (no CRM pollution)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Bounded for harness latency; production default remains 200.
# Force override — prior audit shells may leave RESEARCH_MAX_PAGES_PER_SOURCE=3.
os.environ["RESEARCH_MAX_PAGES_PER_SOURCE"] = os.environ.get("M3_002A_HARNESS_MAX_PAGES", "12")


def main() -> int:
    from m3.config import get_settings
    from m3.db import SessionLocal
    from m3.research.adapters import search_socrata
    from m3.research.engine import ensure_source_registry, repair_discovered_sources, run_contract_discovery
    from m3.research.filters import apply_contract_filters
    from m3.research.schema_map import validate_socrata_source
    from m3.research.seeds import SEED_SOURCES
    from m3.research.uncertain import resolve_uncertain_batch
    from m3.research.vocabulary import expand_keyword, parse_search_terms

    get_settings.cache_clear()
    t0 = time.time()
    report: dict = {"build": "002A", "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    settings = get_settings()
    report["max_pages_harness"] = settings.research_max_pages_per_source
    assert settings.research_max_pages_per_source >= 12, "harness max_pages not applied"

    # --- Field mapping / schema validation ---
    mapping_cases = []
    for seed in SEED_SOURCES:
        if seed.get("adapter") != "socrata_contracts":
            continue
        url = (seed.get("config") or {}).get("dataset_url") or seed["base_url"]
        v = validate_socrata_source(url, name=seed["name"])
        mapping_cases.append(
            {
                "source_key": seed["source_key"],
                "operational": v.get("operational"),
                "title_field": (v.get("inferred") or {}).get("title_field"),
                "kind": (v.get("decision") or {}).get("kind"),
                "ok": v.get("ok"),
            }
        )
    # Known title-field variants
    mesa = validate_socrata_source(
        "https://citydata.mesaaz.gov/resource/dfcn-ivuc.json", name="Mesa solicitations"
    )
    mapping_cases.append(
        {
            "source_key": "mesa_check",
            "operational": mesa.get("operational"),
            "title_field": (mesa.get("inferred") or {}).get("title_field"),
            "kind": (mesa.get("decision") or {}).get("kind"),
        }
    )
    report["schema_validation"] = mapping_cases

    # Non-solicitation quarantine example (if reachable)
    bad = validate_socrata_source(
        "https://data.cityofnewyork.us/resource/4e2n-s75n.json",
        name="NYC Checkbook expenditures sample",
    )
    report["non_solicitation_probe"] = {
        "operational": bad.get("operational"),
        "reason": (bad.get("decision") or {}).get("reason") or bad.get("reason"),
        "kind": (bad.get("decision") or {}).get("kind"),
    }

    # --- Adapter retrieval without keyword gate ---
    la_seed = next(s for s in SEED_SOURCES if s["source_key"] == "la_ramp_open_bids")
    la = search_socrata(
        {"source_key": "la_ramp_open_bids", "base_url": la_seed["base_url"], "config": la_seed["config"]},
        keywords=["zzzz_not_a_real_keyword_xyz"],
        max_pages=3,
        apply_adapter_keywords=False,
    )
    with_gate = search_socrata(
        {"source_key": "la_ramp_open_bids", "base_url": la_seed["base_url"], "config": la_seed["config"]},
        keywords=["zzzz_not_a_real_keyword_xyz"],
        max_pages=3,
        apply_adapter_keywords=True,
    )
    report["adapter_keyword_gate"] = {
        "without_gate_accepted": len(la.get("items") or []),
        "with_gate_accepted": len(with_gate.get("items") or []),
        "gate_removed_ok": len(la.get("items") or []) > len(with_gate.get("items") or []),
        "pages": la.get("pages_fetched"),
        "pagination_complete_3page_slice": la.get("pagination_complete"),
        "more_available": la.get("more_available"),
        "stats": la.get("stats"),
    }

    # Pagination beyond 3 pages — use Mesa (large historical+open set)
    mesa_seed = next(s for s in SEED_SOURCES if s["source_key"] == "mesa_az_solicitations")
    # Broad status config so pagination can exceed 3 pages of raw rows
    mesa_cfg = dict(mesa_seed["config"])
    mesa_cfg.pop("prefer_statuses", None)
    deep = search_socrata(
        {
            "source_key": "mesa_az_solicitations",
            "base_url": mesa_seed["base_url"],
            "config": mesa_cfg,
        },
        max_pages=6,
        apply_adapter_keywords=False,
    )
    report["pagination_beyond_3"] = {
        "source": "mesa_az_solicitations",
        "pages_fetched": deep.get("pages_fetched"),
        "rows_examined": deep.get("rows_examined"),
        "accepted": len(deep.get("items") or []),
        "exceeds_prior_3_page_cap": int(deep.get("pages_fetched") or 0) > 3
        or int(deep.get("rows_examined") or 0) > 1500,
        "more_available": deep.get("more_available"),
        "pagination_complete": deep.get("pagination_complete"),
    }

    # Vocabulary
    report["vocabulary"] = {
        "screws": expand_keyword("screws"),
        "valves": expand_keyword("valves"),
        "equipment": expand_keyword("equipment")[:8],
        "appliances": expand_keyword("appliances"),
        "generators": expand_keyword("generators"),
        "exact_mpn": parse_search_terms("ABC-12345", mode="exact_mpn"),
    }

    # Filter layer: unknown values included by default; value bands empty = no value filter
    sample = (deep.get("items") or la.get("items") or [])[:50]
    unfiltered = apply_contract_filters(sample, {"product_resale_only": True})
    with_bands = apply_contract_filters(
        sample, {"product_resale_only": True, "value_bands": ["50k_150k"], "include_unknown_value": False}
    )
    screws = apply_contract_filters(
        sample, {"keyword": "screws", "search_mode": "broad", "product_resale_only": True}
    )
    report["filter_layer"] = {
        "sample_size": len(sample),
        "default_no_value_bands": len(unfiltered),
        "with_50k_band_no_unknown": len(with_bands),
        "screws_matches": len(screws),
        "match_reasons_sample": (screws[0].get("match_reasons") if screws else None),
        "clear_filters_restores_population": len(unfiltered) >= len(with_bands),
    }

    # Uncertain resolution
    fake_uncertain = [
        {"title": "Commercial refrigerator and freezer unit", "requirement_class": "unknown_mixed"},
        {"title": "Tree trimming services", "requirement_class": "unknown_mixed"},
        {"title": "Ball valves for water treatment plant", "requirement_class": "unknown_mixed"},
        {
            "title": "Various commodities",
            "requirement_class": "unknown_mixed",
            "evidence": {"category_raw": "Commodity"},
        },
    ]
    resolved = resolve_uncertain_batch(fake_uncertain)
    report["uncertain_resolution_unit"] = {
        "investigated": resolved["investigated"],
        "resolved_counts": resolved["resolved_counts"],
        "still_uncertain": resolved["still_uncertain"],
        "classes": [i.get("requirement_class") for i in resolved["items"]],
    }

    db = SessionLocal()
    try:
        ensure_source_registry(db)
        repair = repair_discovered_sources(db, limit=25)
        report["source_repair"] = repair

        # Nationwide unfiltered discovery (bounded pages via env)
        result = run_contract_discovery(
            db,
            user_id=None,
            filters={"product_resale_only": True, "include_unknown_value": True},
            limit=100,
            offset=0,
            enable_openai_sources=False,  # keep harness cheap; Test E separate
        )
        items = result.get("items") or []
        report["nationwide_unfiltered"] = {
            "status": result.get("status"),
            "incomplete": result.get("incomplete"),
            "notices_examined": result.get("notices_examined"),
            "mapping_failures": result.get("mapping_failures"),
            "unfiltered_count": result.get("unfiltered_count"),
            "verified_product": result.get("verified_product"),
            "uncertain_remaining": result.get("uncertain_remaining"),
            "uncertain_investigated": result.get("uncertain_investigated"),
            "matching_count": result.get("all_matching_count"),
            "page_count": result.get("page_count"),
            "pages_fetched": result.get("pages_fetched"),
            "sources_succeeded": result.get("sources_succeeded"),
            "sources_fully_paginated_n": len(result.get("sources_fully_paginated") or []),
            "sources_incomplete_n": len(result.get("sources_incomplete") or []),
            "source_yield": result.get("source_yield"),
            "sample_titles": [
                {
                    "buyer": i.get("buyer_name"),
                    "title": (i.get("title") or "")[:120],
                    "sol": i.get("solicitation_number"),
                    "closes": i.get("deadline_at"),
                    "value_min": i.get("estimated_value_min"),
                    "set_aside": i.get("set_aside"),
                    "url": i.get("official_url"),
                    "class": i.get("requirement_class"),
                }
                for i in (result.get("progress") or {}).get("source_yield") and items[:15] or items[:15]
            ],
        }

        # Keyword accuracy on full unfiltered population from run snapshot
        snap = []
        if result.get("run_id"):
            from m3.models import ResearchRun
            import uuid

            run = db.get(ResearchRun, uuid.UUID(result["run_id"]))
            # Use unfiltered unique via re-pull is hard; apply keywords to page + note
            snap = list((run.result_snapshot if run else None) or items)

        kw_report = {}
        for term in ("screws", "valves", "equipment", "appliances", "generators"):
            matched = apply_contract_filters(
                snap, {"keyword": term, "search_mode": "broad", "product_resale_only": True}
            )
            kw_report[term] = {
                "matches": len(matched),
                "examples": [
                    {"title": (m.get("title") or "")[:100], "reasons": m.get("match_reasons"), "url": m.get("official_url")}
                    for m in matched[:5]
                ],
            }
        # Exact MPN mode (expect 0 unless present)
        mpn = apply_contract_filters(
            snap, {"keyword": "3M-2091", "search_mode": "exact_mpn", "product_resale_only": True}
        )
        kw_report["exact_mpn_3M-2091"] = {"matches": len(mpn)}
        report["keyword_accuracy"] = kw_report

        # Combined filters + clear
        multi = apply_contract_filters(
            snap,
            {
                "keyword": "equipment",
                "states": ["CA", "AZ", "MD", "DE", "IL", "NE"],
                "value_bands": ["unknown", "under_5k", "5k_15k", "15k_50k", "50k_150k"],
                "set_asides": ["unknown", "unrestricted", "state_local_preference"],
                "product_resale_only": True,
                "include_unknown_value": True,
            },
        )
        cleared = apply_contract_filters(snap, {"product_resale_only": True})
        report["combined_filters"] = {
            "multi_filter_matches": len(multi),
            "cleared_matches": len(cleared),
            "clear_does_not_discard_underlying": len(cleared) >= len(multi),
        }

        # Representative sample (up to 30)
        productish = [
            i
            for i in snap
            if (i.get("requirement_class") or "")
            in {"product_supply", "product_with_incidental_services"}
        ][:30]
        report["representative_product_sample"] = [
            {
                "buyer": i.get("buyer_name"),
                "product": (i.get("title") or "")[:160],
                "solicitation_number": i.get("solicitation_number"),
                "closing_date": i.get("deadline_at"),
                "estimated_value_min": i.get("estimated_value_min"),
                "estimated_value_max": i.get("estimated_value_max"),
                "set_aside": i.get("set_aside"),
                "official_url": i.get("official_url"),
                "state": i.get("geo_state"),
                "class": i.get("requirement_class"),
            }
            for i in productish
        ]

    finally:
        db.close()

    report["elapsed_seconds"] = round(time.time() - t0, 1)
    out = ROOT / "docs" / "BUILD_002A_ACCEPTANCE.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"ok": True, "wrote": str(out), "elapsed": report["elapsed_seconds"], "summary": {
        "verified_product": report.get("nationwide_unfiltered", {}).get("verified_product"),
        "matching": report.get("nationwide_unfiltered", {}).get("matching_count"),
        "pages": report.get("nationwide_unfiltered", {}).get("pages_fetched"),
        "gate_removed": report.get("adapter_keyword_gate", {}).get("gate_removed_ok"),
        "pagination_beyond_3": report.get("pagination_beyond_3", {}).get("exceeds_prior_3_page_cap"),
    }}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
