"""Deal economics — profit target status, gaps, simulations, overrides."""

from __future__ import annotations

from m3_commercial_engine import PRICE_LEVEL_2_PUBLIC, PRICE_LEVEL_4_UNKNOWN, SCORE_HIGH
from m3_deal_economics import (
    STATUS_BELOW,
    STATUS_EXCEEDS,
    STATUS_MEETS,
    STATUS_UNKNOWN,
    STATUS_UNVIABLE,
    STATUS_WITHIN,
    analyze_deal_economics_top,
    apply_operator_override,
    build_deal_economics,
    build_deal_economics_profile,
    classify_profit_target_status,
    deal_room_economics_section,
    simulate_cost_change,
)
from m3_pipeline_store import M3PipelineStore


def _opp(**kwargs):
    base = {
        "canonical_id": kwargs.get("canonical_id", "de-1"),
        "title": "Dell Storage",
        "estimated_value": 100000,
        "supplier_intelligence": {
            "Supply_chain": {"supplier_confidence": SCORE_HIGH},
            "Pricing_evidence": {"primary_level": PRICE_LEVEL_4_UNKNOWN, "items": []},
        },
        "commercial_intelligence": {"COMMERCIAL_OPPORTUNITY_SCORE": "MEDIUM"},
        "competitive_intelligence": {"FIRST_DEAL": {"FIRST_DEAL_SCORE": 70}},
        "execution_intelligence": {"EXECUTION": {"EXECUTION_SCORE": 80}},
    }
    base.update(kwargs)
    return base


def test_unknown_pricing_status():
    prof = build_deal_economics_profile(_opp())
    assert prof["PROFIT_TARGET_STATUS"] == STATUS_UNKNOWN
    assert prof["Current_acquisition_cost"] == "UNKNOWN"
    assert prof["Next_Action"] == "Find supplier pricing"
    assert prof["Target_acquisition_cost"] == 90000.0  # 100k - 10k target


def test_known_pricing_and_statuses():
    assert classify_profit_target_status(projected_profit=20000, target=10000, has_cost=True) == STATUS_EXCEEDS
    assert classify_profit_target_status(projected_profit=10000, target=10000, has_cost=True) == STATUS_MEETS
    assert classify_profit_target_status(projected_profit=7000, target=10000, has_cost=True) == STATUS_WITHIN
    assert classify_profit_target_status(projected_profit=3000, target=10000, has_cost=True) == STATUS_BELOW
    assert classify_profit_target_status(projected_profit=-100, target=10000, has_cost=True) == STATUS_UNVIABLE
    assert classify_profit_target_status(projected_profit=None, target=10000, has_cost=False) == STATUS_UNKNOWN


def test_price_variance_and_gap():
    row = _opp(
        supplier_intelligence={
            "Supply_chain": {"supplier_confidence": SCORE_HIGH},
            "Pricing_evidence": {"primary_level": PRICE_LEVEL_2_PUBLIC, "items": []},
            "cost_detail": {"best_price": 95000},
            "ACQUISITION_COST_CONFIDENCE": SCORE_HIGH,
        }
    )
    prof = build_deal_economics_profile(row)
    assert prof["PROFIT_TARGET_STATUS"] == STATUS_BELOW  # profit 5000 < 60% of 10k? 5000 < 6000 = BELOW
    assert prof["Projected_profit"] == 5000.0
    gap = prof["Target_acquisition_gap"]
    assert gap["required_acquisition_cost"] == 90000.0
    assert gap["current_cost"] == 95000.0
    assert gap["difference"] == 5000.0
    assert gap["required_improvement_pct"] == round(5000 / 95000 * 100, 2)


def test_meets_and_exceeds_with_known_cost():
    row = _opp(
        supplier_intelligence={
            "Pricing_evidence": {"primary_level": PRICE_LEVEL_2_PUBLIC},
            "cost_detail": {"best_price": 85000},
            "Supply_chain": {"supplier_confidence": SCORE_HIGH},
        }
    )
    prof = build_deal_economics_profile(row)
    assert prof["Projected_profit"] == 15000.0
    assert prof["PROFIT_TARGET_STATUS"] == STATUS_EXCEEDS  # 15k == 1.5x of 10k
    meet = build_deal_economics_profile(
        _opp(
            supplier_intelligence={
                "Pricing_evidence": {"primary_level": PRICE_LEVEL_2_PUBLIC},
                "cost_detail": {"best_price": 88000},
                "Supply_chain": {"supplier_confidence": SCORE_HIGH},
            }
        )
    )
    assert meet["Projected_profit"] == 12000.0
    assert meet["PROFIT_TARGET_STATUS"] == STATUS_MEETS


def test_simulation_what_if():
    sim = simulate_cost_change(_opp(), 88000)
    assert sim["Projected_profit"] == 12000.0
    assert sim["PROFIT_TARGET_STATUS"] == STATUS_MEETS
    assert "simulation_only" in sim["notes"][0]


def test_strategic_override_does_not_hide_economics():
    row = _opp(
        supplier_intelligence={
            "Pricing_evidence": {"primary_level": PRICE_LEVEL_2_PUBLIC},
            "cost_detail": {"best_price": 96000},
            "Supply_chain": {"supplier_confidence": SCORE_HIGH},
        },
        operator_economics={
            "accept_below_target": True,
            "strategic_value": "High",
            "strategic_notes": "First completed government transaction",
        },
    )
    prof = build_deal_economics_profile(row)
    out = apply_operator_override(prof, row)
    assert out["PROFIT_TARGET_STATUS"] == STATUS_BELOW
    assert out["operator_override"]["economics_still_shown"] is True
    assert out["operator_override"]["notes"] == "First completed government transaction"
    assert "strategic" in (out.get("strategic_context") or "")


def test_unknown_stays_in_ranking(tmp_path):
    store = M3PipelineStore(path=tmp_path / "e.json", durable=False)
    store._rows["a"] = _opp(canonical_id="a")  # unknown pricing
    store._rows["b"] = _opp(
        canonical_id="b",
        title="Cisco Switches",
        estimated_value=50000,
        supplier_intelligence={
            "Pricing_evidence": {"primary_level": PRICE_LEVEL_2_PUBLIC},
            "cost_detail": {"best_price": 30000},
            "Supply_chain": {"supplier_confidence": SCORE_HIGH},
        },
    )
    store.save()
    out = analyze_deal_economics_top(store, limit=5)
    assert out["analyzed"] >= 2
    assert out["unknown_pricing"] >= 1
    assert out["paid"] == 0
    ids = [r["canonical_id"] for r in out["ALL_SCORED"]]
    assert "a" in ids  # unknown not removed
    sec = deal_room_economics_section(store.get("a") or _opp(canonical_id="a"))
    assert sec["Profit_Target_Status"] == STATUS_UNKNOWN
    assert sec["Next_Action"] == "Find supplier pricing"
