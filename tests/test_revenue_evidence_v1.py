"""Tests for revenue evidence hierarchy + buyer-first matching."""

from __future__ import annotations

from revenue_evidence.comparability import score_comparability
from revenue_evidence.models import BUILD, EXACT_MATCH, STRONG_COMPARABLE
from revenue_evidence.buyer import aliases_for
from revenue_evidence.resolver import incumbent_sensitivity


def test_build():
    assert BUILD == "20261004-m3-revenue-evidence-v1"


def test_go_metro_aliases():
    a = aliases_for("go-metro")
    assert any("SORTA" == x for x in a)
    assert any("metro" in x.lower() for x in a)


def test_exact_pn_comparability():
    c = score_comparability(
        buyer_match=True,
        current={"part_number": "82-48412-003", "quantity": 2, "uom": "EA"},
        prior={"part_number": "82-48412-003", "quantity": 2, "uom": "EA", "project_title": "RFQ 5455"},
        match_grade="EXACT_PN",
    )
    assert c["comparability"] in {EXACT_MATCH, STRONG_COMPARABLE}


def test_buyer_mismatch_not_comparable():
    c = score_comparability(
        buyer_match=False,
        current={"part_number": "ABC"},
        prior={"part_number": "ABC"},
        match_grade="EXACT_PN",
    )
    assert c["comparability"] == "NOT_COMPARABLE"


def test_incumbent_sensitivity_floor():
    s = incumbent_sensitivity(prior_award_value=10000, current_acquisition_cost=8000, owner_floor=2500)
    assert len(s["scenarios"]) == 5
    match = s["scenarios"][0]
    assert match["expected_profit"] == 2000.0
    assert match["below_owner_floor"] is True
