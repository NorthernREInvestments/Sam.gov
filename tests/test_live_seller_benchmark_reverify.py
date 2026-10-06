"""Tests for live seller benchmark reverify."""

from __future__ import annotations

from live_seller_benchmark_reverify.corpus import build_contradiction_corpus, freeze_baseline
from live_seller_benchmark_reverify.models import (
    BUILD,
    HONEST_BASELINE,
    ORIGINAL_DENOM,
    PUBLIC_NEW_PRICE_REVERIFIED,
    QUOTE_ONLY_CONFIRMED,
)
from live_seller_benchmark_reverify.reverify import classify_contradiction_outcome


def test_build_id():
    assert BUILD == "20261005-m3-live-seller-benchmark-reverify-v1"


def test_baseline_50():
    assert HONEST_BASELINE == 50
    assert ORIGINAL_DENOM == 82


def test_freeze_and_contradiction_corpus():
    b = freeze_baseline()
    assert b["current_valid_prices"] == 50
    c = build_contradiction_corpus()
    assert c["count"] == 26
    assert c["immutable"] is True
    assert len(c["items"]) == 26
    row = c["items"][0]
    assert "benchmark_id" in row
    assert "candidate_urls" in row
    assert "seller_domains_attempted" in row


def test_classify_price_reverified():
    out = classify_contradiction_outcome(
        audits=[],
        prices=[{"price": 12.5, "domain": "quill.com", "url": "https://quill.com/x", "extraction_route": "JSON_LD"}],
        item={"benchmark_id": "x"},
    )
    assert out["outcome"] == PUBLIC_NEW_PRICE_REVERIFIED
    assert out["retain_in_denom"] is True


def test_classify_quote_only():
    out = classify_contradiction_outcome(
        audits=[
            {
                "state": "LIVE_EXACT_PDP_QUOTE_ONLY",
                "domain": "example.com",
                "url": "https://example.com/p",
                "extractable": False,
                "evidence": {},
            }
        ],
        prices=[],
        item={"benchmark_id": "x"},
    )
    assert out["outcome"] == QUOTE_ONLY_CONFIRMED
    assert out["remove_from_denom"] is True
