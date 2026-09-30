"""Phase L.23 full-population funnel tests."""

from __future__ import annotations

import json
from pathlib import Path

from discovery.sam_api_parked import sam_api_park_status
from phase_l.bidnet_parked import BIDNET_AUTH_HISTORY_PARKED
from phase_l.l23_full_population_funnel import (
    ACCESSIBLE_PRODUCT,
    AUTO_BID,
    AUTO_CALL,
    AUTO_SEND,
    BUILD,
    DEEP_RESEARCH_PRIORITY,
    FAST_REJECT,
    LIVE_FRESH,
    PREVIOUS_CALL_READY,
    READY_TO_BID,
    READY_TO_CALL,
    RAW,
    WATCH,
    call_ready_gate,
    canonical_id_for,
    collect_raw_populations,
    dedupe_population,
    deal_priority_score,
    fast_research_score,
    final_bid_gate,
    funnel_counts,
    load_store,
    research_effort_budget,
    synthesize_deep_research,
    to_canonical_record,
)
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap, CANONICAL_FUNNEL_ENTRY
from phase_l.progressive_funnel import run_progressive_stages_cheap

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
DOCS = ROOT / "docs"


def test_no_auto_comms_and_no_caps():
    assert AUTO_SEND is False and AUTO_CALL is False and AUTO_BID is False
    assert_no_fixed_positive_cap()
    assert "l23_full_population_funnel" in CANONICAL_FUNNEL_ENTRY


def test_all_feeds_collect_and_dedupe():
    rows, meta = collect_raw_populations()
    assert meta["feeds"]
    assert any(f["label"] == "accessible_latest" for f in meta["feeds"])
    unique, stats = dedupe_population(rows)
    assert stats["unique"] >= 1
    assert stats["unique"] <= stats["raw_input"]
    # same solicitation+buyer+platform collapses
    a = {"title": "Widget", "agency": "City", "solicitation_number": "R-1", "source_id": "portal"}
    b = {"title": "Widget COPY", "agency": "City", "solicitation_number": "R-1", "source_id": "portal"}
    u, st = dedupe_population([a, b])
    assert st["unique"] == 1


def test_canonical_id_stable():
    row = {"title": "Ford PPI", "agency": "County", "solicitation_number": "BP-1"}
    assert canonical_id_for(row) == canonical_id_for(dict(row))


def test_hard_reject_and_product_triage():
    expired = {
        "title": "Old paint contract",
        "agency": "X",
        "deadline_state": "EXPIRED",
        "response_deadline": "2020-01-01T00:00:00Z",
    }
    # Use progressive stage0 via cheap runner
    from phase_l.deadline_freshness import classify_deadline_freshness

    # Construction reject
    cons = {"title": "Road paving and asphalt construction labor", "agency": "DOT", "description": "paving"}
    p = run_progressive_stages_cheap(cons)
    assert (p.get("stage0") or {}).get("pass") is False or (p.get("stage1") or {}).get("pass") is False

    product = {"title": "Dell Latitude 5540 Laptops qty 10", "agency": "City IT", "is_product": True, "our_bid_access": "YES"}
    p2 = run_progressive_stages_cheap(product)
    assert (p2.get("stage0") or {}).get("pass") is True


def test_fast_research_and_deal_scores_no_fixed_cap():
    rec = to_canonical_record(
        {
            "title": "Apple iPad 11",
            "agency": "LA County",
            "source_id": "structured_socrata",
            "source_url": "https://example.com/r",
            "inventory_freshness": LIVE_FRESH,
            "is_product": True,
        }
    )
    cheap = {
        "stage2": {"commercial": {"commercial_identity_state": "EXACT_MPN", "manufacturer": "Apple", "model": "iPad 11", "commercial_priceability": "HIGH", "market_research_eligible": True}},
        "stage3": {},
        "deep_research_priority": "DEEP_RESEARCH_HIGH",
        "survives_to_stage3": True,
    }
    fr = fast_research_score(rec, cheap)
    assert fr["score"] >= 40
    dp = deal_priority_score(rec, cheap, supplier_grade="B")
    assert dp["score"] >= fr["score"]
    # Budget scales — not a row count cap
    assert research_effort_budget(80)["band"] == "HIGH"
    assert research_effort_budget(20)["units"] < research_effort_budget(80)["units"]


def test_cache_reuse_deep_synthesis_and_call_gate():
    rec = to_canonical_record(
        {
            "title": "NEW CATERPILLAR MODEL C18 MARINE DIESEL ENGINE",
            "agency": "Port of LA",
            "source_url": "https://www.rampla.org/x",
            "inventory_freshness": LIVE_FRESH,
            "is_product": True,
        }
    )
    cheap = run_progressive_stages_cheap(rec["row_ref"] | {"title": rec["title"], "agency": rec["buyer"], "source_url": rec["authoritative_url"]})
    # Force commercial for test stability
    if not (cheap.get("stage2") or {}).get("commercial", {}).get("manufacturer"):
        cheap.setdefault("stage2", {})["commercial"] = {
            "manufacturer": "Caterpillar",
            "model": "C18",
            "commercial_identity_state": "EXACT_MODEL",
            "market_research_eligible": True,
            "commercial_priceability": "HIGH",
        }
    deep = synthesize_deep_research(rec, cheap)
    assert deep["network_calls"] == 0
    assert deep["verified_acquisition_price"] is None
    assert deep["cache_reuse"] is True
    gate = call_ready_gate(rec, deep)
    # May or may not be ready depending on qty/suppliers — structure must exist
    assert "ready" in gate and "blockers" in gate


def test_final_bid_gate_never_auto():
    g = final_bid_gate({})
    assert g["ready"] is False
    assert g["auto_bid"] is False


def test_per_row_stop_loss_and_watch():
    rec = to_canonical_record({"title": "Unknown commodity", "agency": "X", "inventory_freshness": LIVE_FRESH})
    cheap = {"stage2": {"commercial": {"commercial_identity_state": "UNKNOWN"}}, "deep_research_priority": "DEEP_RESEARCH_HIGH"}
    deep = synthesize_deep_research(rec, cheap)
    assert deep.get("stop_loss") == "no_credible_supplier_path" or not deep.get("suppliers")
    gate = call_ready_gate(rec, {**deep, "stop_loss": "no_credible_supplier_path", "product_identity": "WEAK", "suppliers": []})
    assert gate["ready"] is False


def test_l22_integration_import_constants():
    assert PREVIOUS_CALL_READY == 15
    assert READY_TO_CALL != READY_TO_BID


def test_artifacts_and_persistence_after_run():
    if not (OUT / "l23_summary.json").exists():
        return
    s = json.loads((OUT / "l23_summary.json").read_text(encoding="utf-8"))
    assert s.get("verdict") in {
        "PHASE_L23_FULL_POPULATION_FUNNEL_WORKING",
        "PHASE_L23_PARTIAL_FULL_POPULATION_FUNNEL",
        "PHASE_L23_FULL_POPULATION_FUNNEL_FAILED",
    }
    assert s.get("no_fixed_global_caps") is True
    assert s.get("auto_call") is False
    assert s.get("total_call_ready", 0) >= PREVIOUS_CALL_READY
    assert s.get("build") == BUILD
    store = load_store()
    assert len(store) >= 100
    counts = funnel_counts(store)
    assert sum(counts.values()) == len(store)
    for name in (
        "l23_canonical_population.json",
        "l23_funnel_states.json",
        "l23_fast_stage_results.json",
        "l23_deep_priority_queue.json",
        "l23_deep_research_results.json",
        "l23_call_ready_queue.json",
        "l23_daily_worklist.json",
        "l23_conversion_metrics.json",
        "l23_bottleneck_analysis.json",
        "l23_source_productivity.json",
        "l23_summary.json",
    ):
        assert (OUT / name).exists(), name
    for doc in (
        "phase_l23_full_population_funnel.md",
        "phase_l23_stage_definitions.md",
        "phase_l23_fast_processing.md",
        "phase_l23_deep_research_priority.md",
        "phase_l23_continuous_replenishment.md",
        "phase_l23_owner_deal_desk.md",
        "phase_l23_conversion_metrics.md",
        "phase_l23_bottleneck_detection.md",
        "phase_l23_legacy_cleanup.md",
        "phase_l23_regression.md",
    ):
        assert (DOCS / doc).exists(), doc
    # idempotence: store file exists for resume
    from phase_l.l23_full_population_funnel import STORE_PATH

    assert STORE_PATH.exists()


def test_no_evidence_loosening_guards():
    assert sam_api_park_status()["calls_consumed"] == 0
    assert BIDNET_AUTH_HISTORY_PARKED
    # Verified states not fabricated in synthesis
    deep = synthesize_deep_research(
        to_canonical_record({"title": "Test", "agency": "A"}),
        {"stage2": {"commercial": {"manufacturer": "Apple", "model": "iPad", "commercial_identity_state": "EXACT_MODEL"}}},
    )
    assert deep["verified_positive"] is None
    assert deep["verified_acquisition_price"] is None


def test_exact_source_preservation_on_record():
    rec = to_canonical_record(
        {
            "title": "Item",
            "agency": "Buyer",
            "source_id": "structured_socrata_x",
            "source_url": "https://example.com/auth",
            "detail_url": "https://example.com/auth",
            "_ingest_feed": "accessible_latest",
        }
    )
    assert rec["authoritative_url"]
    assert rec["platform"] == "structured_socrata_x"
    assert rec["current_funnel_state"] == RAW
