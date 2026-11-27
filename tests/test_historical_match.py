"""Historical match classes, packaging, and pricing-type guards (no live OpenAI)."""

from __future__ import annotations

from m3.research.commercial import build_commercial_evidence
from m3.research.historical_match import (
    MATCH_EXACT_MPN,
    MATCH_EXACT_NSN,
    MATCH_MARKET,
    MATCH_NONE,
    PRICING_AWARDED_UNIT,
    PRICING_CONTRACT_TOTAL,
    RELEVANCE_CONTEXT,
    RELEVANCE_UNUSABLE,
    RELEVANCE_VERIFIED,
    classify_identifier_match,
    normalize_award_record,
    normalize_awards_list,
    packaging_conversion_supported,
)


def test_exact_mpn_and_nsn_match():
    mc, reason = classify_identifier_match(current_mpn="ABC-123", historical_mpn="ABC-123")
    assert mc == MATCH_EXACT_MPN
    assert "Exact manufacturer part number" in reason
    mc, reason = classify_identifier_match(current_nsn="1234-00-567-8901", historical_nsn="1234-00-567-8901")
    assert mc == MATCH_EXACT_NSN


def test_similar_but_different_mpn_is_not_exact():
    mc, reason = classify_identifier_match(current_mpn="ABC-123", historical_mpn="ABC-123A")
    assert mc == MATCH_NONE
    assert "not identical" in reason.lower() or "differ" in reason.lower()


def test_case_vs_each_requires_pack_qty():
    ok, msg = packaging_conversion_supported(
        historical_unit="case", historical_pack_qty=None, target_unit="each"
    )
    assert ok is False
    assert "pack quantity" in msg.lower()
    ok, msg = packaging_conversion_supported(
        historical_unit="case", historical_pack_qty=100, target_unit="each"
    )
    assert ok is True


def test_contract_total_suppresses_unit_price():
    row = normalize_award_record(
        {
            "product": "Assorted supplies",
            "match_class": MATCH_MARKET,
            "match_reason": "Same agency bought supplies",
            "pricing_type": PRICING_CONTRACT_TOTAL,
            "unit_price": "5.00",
            "total_award_value": "250000",
        }
    )
    assert row["unit_price"] is None
    assert row["unit_price_suppressed"] == "5.00"
    assert row["pricing_type"] == PRICING_CONTRACT_TOTAL
    assert row["relevance"] in {RELEVANCE_CONTEXT, RELEVANCE_UNUSABLE}


def test_exact_unit_award_verified():
    row = normalize_award_record(
        {
            "part_number": "XYZ-9",
            "match_class": "exact",
            "match_reason": "Same MPN on bid tab",
            "pricing_type": "awarded_unit_price",
            "unit_price": "12.50",
        },
        current_mpn="XYZ-9",
    )
    assert row["match_class"] == MATCH_EXACT_MPN
    assert row["relevance"] == RELEVANCE_VERIFIED
    assert "match_reason" in row and row["match_reason"]


def test_missing_match_reason_filled():
    row = normalize_award_record({"match_class": MATCH_NONE, "pricing_type": PRICING_AWARDED_UNIT})
    assert row["match_reason"]


def test_market_context_does_not_drive_commercial_revenue():
    ev = build_commercial_evidence(
        quantity=10,
        historical_unit_price=100.0,
        historical_match_type=MATCH_MARKET,
        historical_match_reason="Similar category only",
        historical_relevance=RELEVANCE_CONTEXT,
        catalog_unit_price=40.0,
    )
    assert ev["historical_revenue_benchmark"] is None
    assert ev["historical_match_reason"]


def test_no_historical_is_unknown_not_zero():
    ev = build_commercial_evidence(quantity=5, catalog_unit_price=None)
    assert ev["historical_unit_price"] is None
    assert ev["profitability_status"] == "UNKNOWN"


def test_normalize_awards_list_and_schema_prompt_importable():
    from m3.research.historical_match import HISTORICAL_AWARD_JSON_SCHEMA, historical_award_prompt

    assert "match_reason" in HISTORICAL_AWARD_JSON_SCHEMA
    assert "match_class" in historical_award_prompt(query="test widget", part_number="A1")
    out = normalize_awards_list(
        [{"part_number": "A1", "match_type": "approximate", "pricing_type": "ceiling", "unit_price": "1"}]
    )
    assert out[0]["match_class"] == MATCH_MARKET
    assert out[0]["unit_price"] is None


def test_usaspending_helper_shapes_context_only(monkeypatch):
    from m3.research import historical_usaspending as hus

    class _Resp:
        status = 200

        def read(self):
            return b'{"results":[{"Award ID":"X1","Recipient Name":"ACME","Award Amount":1000,"Description":"widgets","Awarding Agency":"GSA","Start Date":"2024-01-01","End Date":"2024-12-31","NAICS Code":null,"PSC Code":null}]}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(hus.request, "urlopen", lambda *a, **k: _Resp())
    result = hus.search_awards_by_keyword(["widgets"], limit=1)
    assert result["ok"]
    assert result["items"][0]["pricing_type"] == "total_contract_award_amount"
    assert result["items"][0]["unit_price"] is None
    assert "not verified product unit" in result["items"][0]["match_reason"].lower()
