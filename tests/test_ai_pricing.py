"""Official OpenAI pricing table tests — no live API."""

from __future__ import annotations

import ai_pricing


def test_luna_official_rates():
    r = ai_pricing.rates_for_model("gpt-5.6-luna")
    assert r["input_per_mtok"] == 0.20
    assert r["cached_input_per_mtok"] == 0.02
    assert r["output_per_mtok"] == 1.20


def test_terra_official_rates():
    r = ai_pricing.rates_for_model("gpt-5.6-terra")
    assert r["input_per_mtok"] == 2.00
    assert r["cached_input_per_mtok"] == 0.20
    assert r["output_per_mtok"] == 12.00


def test_sol_official_rates():
    r = ai_pricing.rates_for_model("gpt-5.6-sol")
    assert r["input_per_mtok"] == 4.00
    assert r["cached_input_per_mtok"] == 0.40
    assert r["output_per_mtok"] == 20.00


def test_luna_prior_call_cost_no_reasoning_double_count():
    """Exact prior live call usage must total ~$0.0003934 — reasoning not double-charged."""
    cost = ai_pricing.estimate_call_cost_usd(
        model="gpt-5.6-luna",
        input_tokens=767,
        cached_input_tokens=0,
        output_tokens=200,
        reasoning_tokens=106,  # diagnostic only
        web_search=False,
    )
    assert abs(cost - 0.0003934) < 1e-7
    parts = ai_pricing.breakdown_call_cost_usd(
        model="gpt-5.6-luna",
        input_tokens=767,
        cached_input_tokens=0,
        output_tokens=200,
    )
    assert abs(parts["input_cost"] - 0.0001534) < 1e-7
    assert parts["cached_input_cost"] == 0.0
    assert abs(parts["output_cost"] - 0.00024) < 1e-7
    assert abs(parts["total_cost"] - 0.0003934) < 1e-7


def test_cached_input_cheaper_than_full():
    full = ai_pricing.estimate_call_cost_usd(
        model="gpt-5.6-luna", input_tokens=1_000_000, cached_input_tokens=0, output_tokens=0
    )
    cached = ai_pricing.estimate_call_cost_usd(
        model="gpt-5.6-luna", input_tokens=1_000_000, cached_input_tokens=1_000_000, output_tokens=0
    )
    assert full == 0.2
    assert cached == 0.02
