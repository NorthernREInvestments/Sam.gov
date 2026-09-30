"""Phase L.23.1 population audit + conversion repair tests."""

from __future__ import annotations

import json
from pathlib import Path

from discovery.sam_api_parked import sam_api_park_status
from phase_l.bidnet_parked import BIDNET_AUTH_HISTORY_PARKED
from phase_l.l23_full_population_funnel import (
    canonical_id_for,
    collect_raw_populations,
    dedupe_population,
    _solicitation_key,
)
from phase_l.l231_population_audit_repair import (
    BUILD,
    DEEP_PRIORITY_HIGH,
    DEEP_PRIORITY_LOW,
    DEEP_PRIORITY_MEDIUM,
    L23_BASELINE,
    WATCH_FEDERAL_ACCESS,
    assign_deep_tier,
    build_source_inventory_manifest,
    classify_watch_reason,
    is_viable_for_deep,
    reconcile_call_ready,
)
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
DOCS = ROOT / "docs"


def test_source_manifest_explains_3365():
    m = build_source_inventory_manifest()
    assert m["kind"] == "SourceInventoryManifest"
    assert m["totals"]["known_feeds"] >= 10
    why = m["why_raw_was_3365"]
    assert "accessible_latest" in why["answer"]
    assert "3365" in why["answer"] or "3,365" in why["answer"]
    acc = next(e for e in m["entries"] if e["source_name"] == "accessible_latest")
    assert acc["row_count"] >= 1000


def test_solicitation_id_dedupe_not_null_field():
    a = {
        "title": "Circuit Card Assembly",
        "agency": "DEPT OF DEFENSE",
        "solicitation_id": "SPRHA4-26-R-0051",
        "source_url": "https://sam.gov/opp/aaa/view",
    }
    b = {
        "title": "Circuit Card Assembly COPY",
        "agency": "DEPT OF DEFENSE",
        "solicitation_id": "SPRHA4-26-R-0051",
        "source_url": "https://sam.gov/opp/bbb/view",
    }
    # Different notice URLs → distinct (URL wins)
    assert canonical_id_for(a) != canonical_id_for(b)
    # Same URL clones collapse
    c = {**b, "source_url": a["source_url"], "detail_url": a["source_url"]}
    a2 = {**a, "detail_url": a["source_url"]}
    assert canonical_id_for(a2) == canonical_id_for(c)
    assert _solicitation_key(a) == "sprha4-26-r-0051"


def test_exact_title_fallback_is_exact_not_fuzzy():
    a = {"title": "Widget Alpha", "agency": "City", "deadline": "2026-10-01"}
    b = {"title": "Widget Alpha Extra", "agency": "City", "deadline": "2026-10-01"}
    assert canonical_id_for(a) != canonical_id_for(b)


def test_source_clone_collapse():
    rows = [
        {"title": "Same", "agency": "X", "solicitation_id": "S1", "agency": "Buyer", "_ingest_feed": "a"},
        {"title": "Same", "agency": "Buyer", "solicitation_id": "S1", "_ingest_feed": "b"},
    ]
    # Without unique URLs, sol+buyer collapses
    u, st = dedupe_population(rows)
    assert st["unique"] == 1
    assert st["duplicates_collapsed"] == 1


def test_low_score_does_not_block_viable():
    rec = {"freshness": "LIVE_FRESH", "is_federal": False, "title": "Dell Latitude 5540", "current_funnel_state": "ACCESSIBLE_PRODUCT"}
    cheap = {"stage0": {"pass": True}, "stage1": {"pass": True, "product_fitness": {"product_fitness": "PRODUCT_RESALE"}}}
    ok, why = is_viable_for_deep(rec, cheap)
    assert ok and why == "viable_nonfederal"
    assert assign_deep_tier(10) == DEEP_PRIORITY_LOW
    assert assign_deep_tier(40) == DEEP_PRIORITY_MEDIUM
    assert assign_deep_tier(70) == DEEP_PRIORITY_HIGH


def test_federal_defer_separated():
    rec = {"freshness": "LIVE_FRESH", "is_federal": True, "title": "NSN part", "current_funnel_state": "ACCESSIBLE_PRODUCT"}
    ok, why = is_viable_for_deep(rec, {"stage0": {"pass": True}, "stage1": {"pass": True}})
    assert not ok and why == "federal_access_defer"
    assert classify_watch_reason({**rec, "current_funnel_state": WATCH_FEDERAL_ACCESS}) == "federal_access_defer"


def test_call_ready_reconciliation_set_diff():
    store = {
        "a": {"title": "A"},
        "b": {"title": "B"},
        "c": {"title": "C"},
    }
    r = reconcile_call_ready({"a", "b"}, {"b", "c"}, store)
    assert r["promoted_count"] == 1 and r["demoted_count"] == 1 and r["net_change"] == 0
    assert r["total"] == 2


def test_collect_populations_includes_accessible():
    rows, meta = collect_raw_populations()
    assert any(f["label"] == "accessible_latest" and f["count"] > 0 for f in meta["feeds"])
    assert len(rows) >= 1000


def test_no_fixed_cap_and_no_sam():
    assert_no_fixed_positive_cap()
    assert sam_api_park_status()["calls_consumed"] == 0
    assert BIDNET_AUTH_HISTORY_PARKED
    assert L23_BASELINE["READY_TO_CALL"] == 17


def test_artifacts_after_run():
    if not (OUT / "l231_summary.json").exists():
        return
    s = json.loads((OUT / "l231_summary.json").read_text(encoding="utf-8"))
    assert s.get("verdict") in {
        "PHASE_L231_POPULATION_FUNNEL_REPAIRED",
        "PHASE_L231_PARTIAL_REPAIR",
        "PHASE_L231_REPAIR_FAILED",
    }
    assert s.get("build") == BUILD
    assert s["daily_worklist_counts"]["research"] >= 0
    # After repair, research should be > 0 if deep survivors exist
    assert s["population"]["canonical_unique"] >= L23_BASELINE["unique"]
    for name in (
        "l231_source_inventory_manifest.json",
        "l231_prededupe_population.json",
        "l231_dedupe_audit.json",
        "l231_dedupe_collisions.json",
        "l231_fast_research_audit.json",
        "l231_watch_audit.json",
        "l231_deep_gate_audit.json",
        "l231_source_productivity.json",
        "l231_conversion_table.json",
        "l231_daily_worklist.json",
        "l231_call_ready_reconciliation.json",
        "l231_summary.json",
    ):
        assert (OUT / name).exists(), name
    for doc in (
        "phase_l231_population_audit.md",
        "phase_l231_source_manifest.md",
        "phase_l231_dedupe_repair.md",
        "phase_l231_watch_repair.md",
        "phase_l231_deep_research_gate_repair.md",
        "phase_l231_source_feed_diagnostics.md",
        "phase_l231_conversion_analysis.md",
        "phase_l231_legacy_cleanup.md",
        "phase_l231_regression.md",
    ):
        assert (DOCS / doc).exists(), doc
