"""Scale evidence → profit pipeline unit tests."""

from __future__ import annotations

from scale_evidence_profit.bid_price_index import _norm, lookup_matches
from scale_evidence_profit.models import BUILD, BOTH_SIDES_READY
from scale_evidence_profit.opportunity import aggregate_opportunity, classify_pipeline, compute_basket_economics


def test_build_constant():
    assert BUILD.startswith("20261003-m3-scale-evidence-to-profit")


def test_norm_pn():
    assert _norm("394-4593") == "3944593"


def test_lookup_exact_pn():
    idx = {
        "records": [
            {
                "government_code": "go-metro",
                "project_id": 1,
                "part_number": "3944593",
                "pn_norm": "3944593",
                "bid_unit_price": 3.3,
                "line_description": "SCREW",
                "all_priced_vendors": [{"unit_price": 3.3, "vendor": "Acme"}],
            }
        ],
        "by_pn": {"3944593": [0]},
        "by_model": {},
    }
    hits = lookup_matches(idx, part_number="3944593", government_code="go-metro")
    assert hits
    assert hits[0]["match_grade"] == "EXACT_PN"


def test_aggregate_majority():
    lines = []
    for i in range(10):
        lines.append(
            {
                "line_status": BOTH_SIDES_READY if i < 6 else "GOV_ONLY",
                "identity": {"quantity": 1},
                "government_value": {
                    "status": "FOUND",
                    "evidence": {"awarded_unit_price": 100.0},
                },
                "public_cost": {
                    "status": "FOUND",
                    "evidence": {"unit_price": 60.0},
                },
            }
        )
    agg = aggregate_opportunity("opengov:x:1", lines, total_purchasing_lines=10)
    assert agg["both_sides_lines"] == 6
    assert agg["material_coverage_pct"] >= 50
    assert agg["basket_ready"] is True


def test_economics_and_pipeline():
    lines = [
        {
            "line_status": BOTH_SIDES_READY,
            "identity": {"quantity": 2, "description": "part"},
            "government_value": {"status": "FOUND", "evidence": {"awarded_unit_price": 100.0}},
            "public_cost": {"status": "FOUND", "evidence": {"unit_price": 40.0}},
        }
    ] * 5
    agg = aggregate_opportunity("oid", lines, total_purchasing_lines=5)
    econ = compute_basket_economics(lines)
    assert econ["expected_profit"] is not None and econ["expected_profit"] > 0
    from scale_evidence_profit.opportunity import classify_execution

    ex = classify_execution(agg, econ)
    pipe = classify_pipeline(agg, econ, ex)
    assert pipe["profit_status"] in {"LIKELY_PROFITABLE", "PROVEN_PROFITABLE", "POSSIBLE_PROFIT"}
