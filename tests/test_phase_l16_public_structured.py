"""Phase L.16 public structured expansion tests — no SAM Opportunities API."""

from __future__ import annotations

import json
from pathlib import Path

from discovery.sam_api_parked import (
    SAM_API_PENDING_REPLACEMENT_KEY,
    SAM_API_BUDGETED,
    assert_no_sam_opportunities_api_url,
    block_sam_opportunities_fetch,
    sam_api_park_status,
)
from discovery.structured_adapters import (
    cross_source_dedupe_key,
    dedupe_structured_rows,
    map_discovery_row,
    map_history_row,
    parse_arcgis_features,
    parse_ckan_package_search,
    parse_csv_feed,
    parse_rss_atom,
    parse_socrata_json,
    structured_source_value_score,
)
from discovery.structured_source_registry import (
    FREE_STRUCTURED_ACCESS_QUEUE,
    STRUCTURED_HISTORY_SOURCES,
    STRUCTURED_LIVE_SOURCES,
    all_structured_live_candidates,
    state_structured_matrix,
)
from phase_l.acquisition_lanes import STAGE3_NO_ROW_CAP
from phase_l.bidnet_parked import BIDNET_AUTH_HISTORY_PARKED
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap
from phase_l.l16_public_structured_expansion import (
    BUILD,
    classify_commercial_category,
    infer_buyer_type,
)
from phase_l.original_solicitation import resolve_original_solicitation
from phase_l.quality_audit import VALIDATED_QUOTE_TARGET
from phase_l.recurring_buy_intelligence import RECURRING_BUY_SIGNAL, detect_recurring_buys

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
DOCS = ROOT / "docs"


def test_sam_api_pending_and_unused():
    st = sam_api_park_status()
    assert st["status"] in (SAM_API_PENDING_REPLACEMENT_KEY, SAM_API_BUDGETED)
    assert st["calls_consumed"] == 0
    assert st["calls_allowed_this_phase"] == 0
    assert assert_no_sam_opportunities_api_url("https://api.sam.gov/opportunities/v2/search")
    assert not assert_no_sam_opportunities_api_url("https://sam.gov/api/prod/sgs/v1/search/")
    blk = block_sam_opportunities_fetch("https://api.sam.gov/opportunities/v2/search?postedFrom=1")
    assert blk["blocked"] is True
    assert blk["calls_consumed"] == 0
    # Registry must not point live harvest at Opportunities API
    for s in STRUCTURED_LIVE_SOURCES + STRUCTURED_HISTORY_SOURCES:
        assert not assert_no_sam_opportunities_api_url(s.get("list_url"))
    # Free queue marks SAM as pending
    sam_q = next(x for x in FREE_STRUCTURED_ACCESS_QUEUE if "SAM" in x["source"])
    assert sam_q.get("status") in (SAM_API_PENDING_REPLACEMENT_KEY, SAM_API_BUDGETED)


def test_socrata_multi_source_registry():
    ids = {s["source_id"] for s in STRUCTURED_LIVE_SOURCES}
    assert "structured_socrata_montgomery_md_solicitations" in ids
    assert "structured_socrata_la_ramp_open_bids" in ids
    assert "structured_socrata_cook_buying_plan_2026" in ids
    cands = all_structured_live_candidates()
    assert len(cands) >= 5
    assert all(c["adapter_family"] == "live_structured" for c in cands)


def test_ckan_arcgis_rest_csv_rss():
    ckan = parse_ckan_package_search(
        json.dumps(
            {
                "success": True,
                "result": {
                    "results": [
                        {
                            "title": "Contracts",
                            "name": "c",
                            "organization": {"title": "City"},
                            "resources": [{"format": "CSV", "url": "https://ex/c.csv", "name": "c"}],
                        }
                    ]
                },
            }
        )
    )
    assert ckan[0]["structured_resources"]
    arc = parse_arcgis_features(
        json.dumps({"features": [{"attributes": {"TITLE": "Pump motors IFB", "ID": "1"}}]}),
        field_map={"title": "TITLE", "solicitation_number": "ID"},
        source_id="a",
        list_url="https://ex/FeatureServer/0/query",
    )
    assert len(arc) == 1
    csv_opps = parse_csv_feed(
        "title,id\nServer rack,R1\n",
        field_map={"title": "title", "solicitation_number": "id"},
        source_id="c",
        list_url="https://ex.csv",
    )
    assert len(csv_opps) == 1
    rss = parse_rss_atom(
        "<?xml version='1.0'?><rss><channel><item><title>Fleet tires</title><guid>g</guid></item></channel></rss>",
        source_id="r",
        list_url="https://ex/feed",
    )
    assert len(rss) == 1
    # generic REST-shaped socrata live
    live = parse_socrata_json(
        json.dumps([{"title": "Open commodity IFB", "rampid": "1", "stagename": "Open"}]),
        field_map={
            "title": "title",
            "solicitation_number": "rampid",
            "status": "stagename",
            "live_status_values": ["Open"],
        },
        source_id="la",
        list_url="https://data.lacity.org/resource/hf3r-utnq.json",
    )
    assert len(live) == 1


def test_live_vs_history_mapping_and_exact_history():
    opp = map_discovery_row(
        {"t": "Laptop computers", "n": "L-1", "d": "2026-12-01"},
        field_map={"title": "t", "solicitation_number": "n", "deadline": "d"},
        source_id="live",
        list_url="https://ex",
    )
    assert opp and opp.raw_metadata["canonical_discovery"]["solicitation_id"] == "L-1"
    hist = map_history_row(
        {"desc": "Laptop", "amt": "2000", "qty": "4", "vendor": "CDW"},
        field_map={"product": "desc", "total": "amt", "quantity": "qty", "vendor": "vendor"},
        source_id="hist",
        source_url="https://ex",
    )
    assert hist["unit_price"] == 500.0
    assert hist["evidence_grade_candidate"] in {"GOV_C", "GOV_D"}


def test_recurring_buy_signal():
    rows = [
        {"buyer": "City A", "product": "Dell laptop computers", "vendor": "CDW", "award_date": "2024-01-01", "total": 1000},
        {"buyer": "City A", "product": "Dell laptop computers", "vendor": "CDW", "award_date": "2025-01-15", "total": 1100},
        {"buyer": "City B", "product": "Unique once", "vendor": "X", "award_date": "2025-01-01", "total": 50},
    ]
    sigs = detect_recurring_buys(rows, min_occurrences=2)
    assert sigs and sigs[0]["kind"] == RECURRING_BUY_SIGNAL
    assert sigs[0]["occurrence_count"] >= 2
    assert sigs[0]["not_live_inventory"] is True


def test_cross_source_dedupe_and_original_solicitation():
    rows = [
        {"title": "A", "solicitation_number": "1", "agency": "City", "source_id": "s1"},
        {"title": "A", "solicitation_number": "1", "agency": "City", "source_id": "s2"},
    ]
    uniq, dups = dedupe_structured_rows(rows)
    assert len(uniq) == 1 and len(dups) == 1
    assert cross_source_dedupe_key(rows[0]) == cross_source_dedupe_key(rows[1])
    orig = resolve_original_solicitation(
        {"title": "IFB Electric Actuator", "solicitation_number": "232247", "detail_url": "https://rampla.org/1"}
    )
    assert orig.get("solicitation_number") == "232247" or orig.get("title")


def test_yield_classifiers_and_scoring():
    assert classify_commercial_category({"title": "Dell PowerEdge server"}) == "IT"
    assert classify_commercial_category({"title": "ELECTRIC ACTUATOR"}) == "electrical"
    assert infer_buyer_type({"agency": "Water & Power", "source_id": "structured_socrata_la_ramp_open_bids"}) == "PUBLIC_UTILITY"
    assert infer_buyer_type({"source_id": "structured_socrata_montgomery_md_solicitations", "state_code": "MD"}) == "COUNTY"
    matrix = state_structured_matrix()
    assert len(matrix) >= 50
    score = structured_source_value_score(
        unique_opportunity=40, commercial_yield=10, reliability=0.9, tier="TIER_2_STABLE_PUBLIC_STRUCTURED"
    )
    assert score["StructuredSourceValueScore"] > 0


def test_free_access_queue_and_parks():
    assert FREE_STRUCTURED_ACCESS_QUEUE
    assert BIDNET_AUTH_HISTORY_PARKED
    assert_no_fixed_positive_cap()
    assert STAGE3_NO_ROW_CAP
    assert VALIDATED_QUOTE_TARGET == "VALIDATED_QUOTE_TARGET"


def test_l16_artifacts_when_present():
    required = [
        "l16_structured_source_inventory.json",
        "l16_state_coverage.json",
        "l16_buyer_type_coverage.json",
        "l16_source_value.json",
        "l16_term_contract_sources.json",
        "l16_history_sources.json",
        "l16_recurring_buy_signals.json",
        "l16_free_access_queue.json",
        "l16_fresh_hunt.json",
        "l16_quote_targets.json",
        "l16_summary.json",
    ]
    if not (OUT / "l16_summary.json").exists():
        return
    for name in required:
        assert (OUT / name).exists(), name
    summary = json.loads((OUT / "l16_summary.json").read_text(encoding="utf-8"))
    assert summary["verdict"].startswith("PHASE_L16_")
    assert summary.get("no_sam_api_calls") is True
    assert summary.get("no_outreach") is True
    assert summary.get("evidence_gate_unchanged") is True
    assert summary["sam_api"]["status"] in (SAM_API_PENDING_REPLACEMENT_KEY, SAM_API_BUDGETED)
    assert summary["sam_api"]["calls_consumed"] == 0
    for doc in (
        "phase_l16_public_structured_expansion.md",
        "phase_l16_state_matrix.md",
        "phase_l16_buyer_type_matrix.md",
        "phase_l16_term_contract_research.md",
        "phase_l16_history_expansion.md",
        "phase_l16_recurring_buy_intelligence.md",
        "phase_l16_source_yield.md",
        "phase_l16_quote_target_delta.md",
        "phase_l16_legacy_cleanup.md",
        "phase_l16_regression.md",
    ):
        assert (DOCS / doc).exists(), doc
