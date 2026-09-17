"""Transactional resale discovery tests — local, no live HTTP by default."""

from __future__ import annotations

from deep_deal_constants import (
    DEAL_COOP_MASTER,
    DEAL_ONE_TIME_PRODUCT,
    TIER_A_IMMEDIATE,
    TIER_B_LIKELY,
    TIER_D_STRATEGIC,
    TIER_F_LOW_FIT,
)
from deep_deal_qualification import classify_deal_type, cheap_second_stage_classify
from transactional_discovery import (
    assign_launch_tier,
    enrich_for_transactional_ranking,
    estimate_value_potential,
    pre_deep_hard_filter,
    rerank_existing_pool,
    run_transactional_discovery,
    select_deep_research_candidates,
    build_source_yield,
)


def test_blades_is_one_time_likely_product():
    row = {
        "title": "Tungsten-Carbide Blades for Snow/Ice Removal",
        "source_id": "state_ia",
        "product_classification": "UNKNOWN",
        "agency": "State of Iowa",
    }
    e = enrich_for_transactional_ranking(row)
    assert e["deal_type"] == DEAL_ONE_TIME_PRODUCT
    assert e["cheap_classification"] == "LIKELY_PRODUCT"
    assert e["launch_tier"] in {TIER_A_IMMEDIATE, TIER_B_LIKELY}


def test_sourcewell_strategic_demotion():
    row = {
        "title": "Heavy and Compact Construction Equipment with Related Attachments and Services",
        "agency": "Sourcewell",
        "source_id": "coop_sourcewell_live",
        "product_classification": "CORE_PRODUCT",
    }
    e = enrich_for_transactional_ranking(row)
    assert e["deal_type"] == DEAL_COOP_MASTER
    assert e["launch_tier"] == TIER_D_STRATEGIC


def test_naspo_strategic_not_immediate():
    row = {
        "title": "NASPO ValuePoint RFP NVP_AS26-68 UT Walk-In Building Supplies",
        "source_id": "state_tx",
        "product_classification": "CORE_PRODUCT",
    }
    e = enrich_for_transactional_ranking(row)
    assert e["launch_tier"] == TIER_D_STRATEGIC


def test_hvac_low_fit():
    row = {
        "title": "DOT D1 Ames Materials Lab HVAC Upgrade",
        "source_id": "state_ia",
        "product_classification": "CORE_PRODUCT",
    }
    e = enrich_for_transactional_ranking(row)
    assert e["launch_tier"] == TIER_F_LOW_FIT


def test_one_time_product_outranks_sourcewell():
    product = enrich_for_transactional_ranking(
        {
            "title": "RFQ Purchase of industrial pumps",
            "description": "Procurement of 12 industrial pumps",
            "source_id": "state_tx",
            "product_classification": "CORE_PRODUCT",
            "deadline_raw": "10/01/2026",
        }
    )
    coop = enrich_for_transactional_ranking(
        {
            "title": "Portable Construction Equipment",
            "agency": "Sourcewell",
            "source_id": "coop_sourcewell_live",
            "product_classification": "CORE_PRODUCT",
            "deadline_raw": "11/03/2026",
        }
    )
    assert product["transactional_priority_score"] > coop["transactional_priority_score"]
    assert product["launch_tier"] in {TIER_A_IMMEDIATE, TIER_B_LIKELY}
    assert coop["launch_tier"] == TIER_D_STRATEGIC


def test_local_rerank_zero_http():
    pool = [
        {
            "title": "Tungsten-Carbide Blades for Snow/Ice Removal",
            "source_id": "state_ia",
            "product_classification": "UNKNOWN",
        },
        {
            "title": "Heavy Construction Equipment",
            "agency": "Sourcewell",
            "source_id": "coop_sourcewell_live",
            "product_classification": "CORE_PRODUCT",
        },
        {
            "title": "Employee Benefits Consultant Services",
            "source_id": "state_ia",
            "product_classification": "UNKNOWN",
        },
    ]
    r = rerank_existing_pool(pool)
    assert r["LIVE_API_REQUESTS"] == 0
    assert r["OpenAI"] == 0
    assert r["tier_ab_count"] >= 1
    assert r["strategic_demoted_count"] >= 1
    assert r["unknown_listing_reduced"] >= 1


def test_expansion_not_triggered_when_enough_candidates():
    # Build synthetic pool with 3+ tier B
    pool = []
    for i in range(4):
        pool.append(
            {
                "title": f"RFQ Purchase of network switches batch {i}",
                "description": "Procurement of IT hardware equipment",
                "source_id": "state_ia",
                "product_classification": "CORE_PRODUCT",
                "external_id": f"x-{i}",
                "solicitation_number": f"RFQ-{i}",
            }
        )
    # Monkey via run with authorize_live=False and temp tiny file
    import json
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "tiny.json"
        p.write_text(json.dumps({"discovery": {"opportunities": pool}}), encoding="utf-8")
        result = run_transactional_discovery(
            authorize_live=False,
            force_openai_offline=True,
            tiny_path=p,
            min_tier_ab=3,
        )
    assert result["expansion"]["expansion_triggered"] is False
    assert result["tier_ab_count"] >= 3
    assert result["LIVE_API_REQUESTS"] == 0


def test_pre_deep_hard_filter_blocks_strategic():
    row = enrich_for_transactional_ranking(
        {
            "title": "Sourcewell Equipment",
            "agency": "Sourcewell",
            "source_id": "coop_sourcewell_live",
            "product_classification": "CORE_PRODUCT",
        }
    )
    f = pre_deep_hard_filter(row)
    assert f["pre_deep_pass"] is False


def test_top3_admission():
    ranked = []
    for i in range(6):
        ranked.append(
            enrich_for_transactional_ranking(
                {
                    "title": f"RFQ Purchase of generators {i}",
                    "source_id": "state_ia",
                    "product_classification": "CORE_PRODUCT",
                    "external_id": f"g-{i}",
                }
            )
        )
    admitted = select_deep_research_candidates(ranked, max_slots=3)
    assert len(admitted) == 3
    assert all(a.get("deep_research_admitted") for a in admitted)


def test_value_potential_no_fabrication():
    v = estimate_value_potential({"title": "Purchase of equipment", "product_classification": "CORE_PRODUCT"})
    assert v["estimated_value_parsed"] is None
    assert v["value_potential"] == "UNCERTAIN_VALUE"


def test_source_yield_report():
    rows = [
        enrich_for_transactional_ranking(
            {"title": "RFQ Purchase of tools", "source_id": "state_ia", "product_classification": "CORE_PRODUCT"}
        ),
        enrich_for_transactional_ranking(
            {
                "title": "Sourcewell Equipment",
                "agency": "Sourcewell",
                "source_id": "coop_sourcewell_live",
                "product_classification": "CORE_PRODUCT",
            }
        ),
    ]
    y = build_source_yield(rows)
    assert any(r["source"] == "state_ia" and r["transactional"] >= 1 for r in y)
    assert any(r["source"] == "coop_sourcewell_live" and r["strategic"] >= 1 for r in y)


def test_run_offline_no_outreach():
    import json
    from pathlib import Path
    import tempfile

    pool = [
        {
            "title": "Tungsten-Carbide Blades for Snow/Ice Removal",
            "source_id": "state_ia",
            "product_classification": "UNKNOWN",
            "detail_url": "https://example.gov/blades",
            "external_id": "blade-1",
        },
        {
            "title": "Planer - Attach 48 Flat Tooth Fine Finish",
            "source_id": "state_ia",
            "product_classification": "UNKNOWN",
            "external_id": "planer-1",
        },
        {
            "title": "Wildflower and Native Grass Seed",
            "source_id": "state_ia",
            "product_classification": "UNKNOWN",
            "external_id": "seed-1",
        },
    ]
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "tiny.json"
        p.write_text(json.dumps({"discovery": {"opportunities": pool}}), encoding="utf-8")
        result = run_transactional_discovery(
            authorize_live=False,
            force_openai_offline=True,
            tiny_path=p,
            min_tier_ab=10,  # would expand if live; offline notes would_trigger
        )
    assert result["supplier_outreach"] == 0
    assert result["lender_outreach"] == 0
    assert result["bid_submissions"] == 0
    assert result["SAM"] == 0
    assert result["tier_ab_count"] >= 1
