"""Phase L.15 structured commercial expansion tests."""

from __future__ import annotations

import json
from pathlib import Path

from discovery.live_fetchers import StructuredOpenDataLiveFetcher, get_live_fetcher
from discovery.structured_adapters import (
    PARKED_FRAGILE_SOURCE,
    TIER_FRAGILE,
    TIER_OFFICIAL,
    TIER_STABLE,
    apply_engineering_stop_loss,
    classify_structured_tier,
    cross_source_dedupe_key,
    dedupe_structured_rows,
    map_discovery_row,
    map_history_row,
    parse_arcgis_features,
    parse_ckan_package_search,
    parse_csv_feed,
    parse_rss_atom,
    parse_socrata_json,
    parse_xlsx_rows,
    structured_source_value_score,
    unique_contribution_score,
)
from discovery.structured_source_registry import (
    FREE_STRUCTURED_ACCESS_QUEUE,
    PARKED_FRAGILE_SOURCES,
    STRUCTURED_LIVE_SOURCES,
    all_structured_live_candidates,
    expansion_queue,
    state_structured_matrix,
)
from phase_l.acquisition_lanes import STAGE3_NO_ROW_CAP
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap
from phase_l.quality_audit import VALIDATED_QUOTE_TARGET

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
DOCS = ROOT / "docs"

SOCRATA_FIXTURE = json.dumps(
    [
        {
            "status": "Active",
            "type": "RFP",
            "number": "1196087",
            "description": "Dell laptop computers and docking stations",
            "issuancedate": "2026-08-10T00:00:00.000",
            "closingdate": "2026-12-28T00:00:00.000",
            "department": "IT",
        },
        {
            "status": "Closed",
            "type": "IFB",
            "number": "OLD1",
            "description": "Closed services contract",
            "closingdate": "2020-01-01T00:00:00.000",
            "department": "Ops",
        },
    ]
)


def test_rest_and_socrata_adapter():
    field_map = {
        "title": "description",
        "solicitation_number": "number",
        "status": "status",
        "deadline": "closingdate",
        "buyer": "department",
        "live_status_values": ["Active"],
    }
    opps = parse_socrata_json(
        SOCRATA_FIXTURE,
        field_map=field_map,
        source_id="structured_test",
        list_url="https://example.com/resource/abcd-efgh.json",
        role="LIVE",
    )
    assert len(opps) == 1
    assert "laptop" in opps[0].title.lower()
    assert opps[0].solicitation_number == "1196087"


def test_ckan_adapter():
    body = json.dumps(
        {
            "success": True,
            "result": {
                "results": [
                    {
                        "title": "City Contracts",
                        "name": "city-contracts",
                        "organization": {"title": "City"},
                        "notes": "procurement",
                        "resources": [
                            {"name": "contracts.csv", "format": "CSV", "url": "https://example.com/c.csv"}
                        ],
                    }
                ]
            },
        }
    )
    pkgs = parse_ckan_package_search(body)
    assert pkgs and pkgs[0]["structured_resources"]


def test_arcgis_adapter():
    body = json.dumps(
        {
            "features": [
                {
                    "attributes": {
                        "TITLE": "Fleet vehicle purchase",
                        "BID_ID": "F-1",
                        "DUE": "2026-11-01",
                        "DEPT": "Public Works",
                    }
                }
            ]
        }
    )
    opps = parse_arcgis_features(
        body,
        field_map={"title": "TITLE", "solicitation_number": "BID_ID", "deadline": "DUE", "buyer": "DEPT"},
        source_id="arcgis_test",
        list_url="https://example.com/FeatureServer/0/query",
    )
    assert len(opps) == 1
    assert opps[0].solicitation_number == "F-1"


def test_csv_xlsx_rss_adapters():
    csv_body = "title,id,due\nServer hardware RFP,S-9,2026-10-01\n"
    opps = parse_csv_feed(
        csv_body,
        field_map={"title": "title", "solicitation_number": "id", "deadline": "due"},
        source_id="csv_test",
        list_url="https://example.com/bids.csv",
    )
    assert len(opps) == 1
    xops = parse_xlsx_rows(
        [{"Title": "Pump motors", "Sol": "P-1"}],
        field_map={"title": "Title", "solicitation_number": "Sol"},
        source_id="xlsx_test",
        list_url="https://example.com/a.xlsx",
    )
    assert len(xops) == 1
    rss = """<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>Network switches IFB</title><link>https://ex/1</link><guid>g1</guid></item>
    </channel></rss>"""
    rops = parse_rss_atom(rss, source_id="rss_test", list_url="https://ex/feed.xml")
    assert len(rops) == 1


def test_canonical_discovery_and_history_mapping():
    opp = map_discovery_row(
        {"t": "Tool kits", "n": "TK-1", "d": "2026-12-01"},
        field_map={"title": "t", "solicitation_number": "n", "deadline": "d"},
        source_id="s",
        list_url="https://ex",
    )
    assert opp and opp.raw_metadata["canonical_discovery"]["solicitation_id"] == "TK-1"
    hist = map_history_row(
        {"desc": "Laptop", "amt": "1000", "qty": "2", "vendor": "Acme", "date": "2025-01-01"},
        field_map={"product": "desc", "total": "amt", "quantity": "qty", "vendor": "vendor", "award_date": "date"},
        source_id="h",
        source_url="https://ex",
    )
    assert hist and hist["unit_price"] == 500.0
    assert hist["kind"] == "CanonicalHistoryRow"


def test_cross_source_dedupe_and_unique_contribution():
    rows = [
        {"title": "A", "solicitation_number": "1", "agency": "City", "source_id": "s1"},
        {"title": "A dup", "solicitation_number": "1", "agency": "City", "source_id": "s2"},
        {"title": "B", "solicitation_number": "2", "agency": "City", "source_id": "s1"},
    ]
    uniq, dups = dedupe_structured_rows(rows)
    assert len(uniq) == 2
    assert len(dups) == 1
    assert cross_source_dedupe_key(rows[0]) == cross_source_dedupe_key(rows[1])
    score = unique_contribution_score(unique_live=10, unique_commercial=5, unique_stage3=2, quote_targets=1)
    assert score["UniqueCommercialContribution"] > 0


def test_structured_source_scoring_and_tier():
    assert classify_structured_tier(endpoint_type="REST_API", official=True) == TIER_OFFICIAL
    assert classify_structured_tier(endpoint_type="SOCRATA") == TIER_STABLE
    assert classify_structured_tier(endpoint_type="JS_SPA") == TIER_FRAGILE
    api = structured_source_value_score(
        unique_opportunity=40, commercial_yield=20, reliability=0.9, auth_burden=0, tier=TIER_OFFICIAL
    )
    fragile = structured_source_value_score(
        unique_opportunity=5, commercial_yield=1, reliability=0.2, maintenance_burden=0.9, tier=TIER_FRAGILE
    )
    assert api["StructuredSourceValueScore"] > fragile["StructuredSourceValueScore"]


def test_stop_loss_and_parked_fragile():
    stop = apply_engineering_stop_loss(attempts=3, elapsed_s=10.0, stable_path_found=False)
    assert stop["stop"] is True
    assert stop["status"] == PARKED_FRAGILE_SOURCE
    cont = apply_engineering_stop_loss(attempts=1, elapsed_s=1.0, stable_path_found=True, unique_yield=5)
    assert cont["stop"] is False
    assert any(p.get("status") == PARKED_FRAGILE_SOURCE or "PARKED" in str(p.get("status")) for p in PARKED_FRAGILE_SOURCES)


def test_free_access_queue_and_registry():
    assert FREE_STRUCTURED_ACCESS_QUEUE
    assert any("SAM" in x["source"] for x in FREE_STRUCTURED_ACCESS_QUEUE)
    assert STRUCTURED_LIVE_SOURCES
    cands = all_structured_live_candidates()
    assert all(c["adapter_family"] == "live_structured" for c in cands)
    q = expansion_queue()
    assert q and q[0]["recommended_priority"] <= q[-1]["recommended_priority"]
    matrix = state_structured_matrix()
    assert len(matrix) >= 50


def test_structured_fetcher_registered():
    f = get_live_fetcher("live_structured")
    assert isinstance(f, StructuredOpenDataLiveFetcher)
    opps = f.parse_listing(
        SOCRATA_FIXTURE,
        list_url="https://data.montgomerycountymd.gov/resource/eeq6-nnwe.json?$where=status%3D%27Active%27",
        meta={
            "source_id": "structured_socrata_montgomery_md_solicitations",
            "structured_meta": {
                "adapter_kind": "socrata",
                "field_map": {
                    "title": "description",
                    "solicitation_number": "number",
                    "status": "status",
                    "deadline": "closingdate",
                    "buyer": "department",
                    "live_status_values": ["Active"],
                },
                "role": "LIVE",
            },
        },
    )
    assert len(opps) >= 1


def test_no_caps_evidence_gate_no_outreach():
    assert_no_fixed_positive_cap()
    assert STAGE3_NO_ROW_CAP
    assert VALIDATED_QUOTE_TARGET == "VALIDATED_QUOTE_TARGET"


def test_l15_artifacts_and_docs_when_present():
    """If L.15 has been run, required artifacts/docs must exist."""
    required = [
        "l15_structured_source_inventory.json",
        "l15_structured_source_queue.json",
        "l15_free_access_queue.json",
        "l15_source_value.json",
        "l15_fresh_hunt.json",
        "l15_source_contribution.json",
        "l15_history_contribution.json",
        "l15_quote_targets.json",
        "l15_discovery_gaps.json",
        "l15_summary.json",
    ]
    # Soft: only enforce after run — create inventory always via registry
    from phase_l.l15_structured_expansion import build_structured_inventory

    inv = build_structured_inventory()
    assert inv["tier_counts"]["tier1"] >= 1
    assert inv["tier_counts"]["tier2"] >= 1
    # If summary exists, all required must exist
    if (OUT / "l15_summary.json").exists():
        for name in required:
            assert (OUT / name).exists(), name
        for doc in (
            "phase_l15_structured_source_strategy.md",
            "phase_l15_api_feed_inventory.md",
            "phase_l15_state_structured_coverage.md",
            "phase_l15_open_data_sources.md",
            "phase_l15_source_value.md",
            "phase_l15_source_stop_loss.md",
            "phase_l15_fresh_hunt.md",
            "phase_l15_quote_target_delta.md",
            "phase_l15_legacy_cleanup.md",
            "phase_l15_regression.md",
        ):
            assert (DOCS / doc).exists(), doc
        summary = json.loads((OUT / "l15_summary.json").read_text(encoding="utf-8"))
        assert summary["verdict"].startswith("PHASE_L15_")
        assert summary.get("no_outreach") is True
        assert summary.get("evidence_gate_unchanged") is True
        hunt = json.loads((OUT / "l15_fresh_hunt.json").read_text(encoding="utf-8"))
        assert hunt["terminal_status"] in {
            "COMPLETE",
            "COMPLETE_WITH_SOURCE_FAILURES",
            "HUNT_COMPLETE",
            "HUNT_COMPLETE_WITH_FAILURES",
        } or "COMPLETE" in str(hunt["terminal_status"])
