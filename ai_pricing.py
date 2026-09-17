"""Centralized OpenAI pricing — do not scatter pricing math elsewhere."""

from __future__ import annotations

import json
import os
from typing import Any

from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent / ".env")

# USD per 1M tokens. Official OpenAI standard text-token prices (overridable via AI_MODEL_PRICING_JSON).
_DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "gpt-5.6-luna": {
        "input_per_mtok": 0.20,
        "cached_input_per_mtok": 0.02,
        "output_per_mtok": 1.20,
        "reasoning_per_mtok": 1.20,
        "web_search_per_call": 0.01,
    },
    "gpt-5.6-terra": {
        "input_per_mtok": 2.00,
        "cached_input_per_mtok": 0.20,
        "output_per_mtok": 12.00,
        "reasoning_per_mtok": 12.00,
        "web_search_per_call": 0.025,
    },
    "gpt-5.6-sol": {
        "input_per_mtok": 4.00,
        "cached_input_per_mtok": 0.40,
        "output_per_mtok": 20.00,
        "reasoning_per_mtok": 20.00,
        "web_search_per_call": 0.025,
    },
    # Legacy fallbacks
    "gpt-4.1": {
        "input_per_mtok": 2.00,
        "cached_input_per_mtok": 0.50,
        "output_per_mtok": 8.00,
        "reasoning_per_mtok": 8.00,
        "web_search_per_call": 0.025,
    },
    "default": {
        "input_per_mtok": 1.00,
        "cached_input_per_mtok": 0.10,
        "output_per_mtok": 5.00,
        "reasoning_per_mtok": 5.00,
        "web_search_per_call": 0.02,
    },
}


def _pricing_table() -> dict[str, dict[str, float]]:
    raw = (os.getenv("AI_MODEL_PRICING_JSON") or "").strip()
    table = {k: dict(v) for k, v in _DEFAULT_PRICING.items()}
    if raw:
        try:
            override = json.loads(raw)
            if isinstance(override, dict):
                for model, rates in override.items():
                    if isinstance(rates, dict):
                        table[str(model)] = {**table.get(str(model), table["default"]), **rates}
        except json.JSONDecodeError:
            pass
    return table


def rates_for_model(model: str) -> dict[str, float]:
    table = _pricing_table()
    key = (model or "").strip()
    if key in table:
        return table[key]
    # Prefix match (e.g. gpt-5.6-luna-2026-...)
    for name, rates in table.items():
        if name != "default" and key.startswith(name):
            return rates
    return table["default"]


def estimate_tokens_from_chars(chars: int) -> int:
    """Rough estimate: ~4 chars per token for English text."""
    return max(0, int((chars + 3) / 4))


def estimate_call_cost_usd(
    *,
    model: str,
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    output_tokens: int = 0,
    reasoning_tokens: int = 0,
    web_search: bool = False,
) -> float:
    """
    Dollar cost for a call.
    reasoning_tokens is diagnostic only — NOT charged separately when included in output_tokens.
    """
    parts = breakdown_call_cost_usd(
        model=model,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        web_search=web_search,
    )
    return parts["total_cost"]


def breakdown_call_cost_usd(
    *,
    model: str,
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    output_tokens: int = 0,
    web_search: bool = False,
) -> dict[str, float]:
    rates = rates_for_model(model)
    uncached = max(0, int(input_tokens) - int(cached_input_tokens))
    cached = max(0, int(cached_input_tokens))
    out = max(0, int(output_tokens))
    input_cost = uncached * float(rates["input_per_mtok"]) / 1_000_000
    cached_cost = cached * float(rates["cached_input_per_mtok"]) / 1_000_000
    output_cost = out * float(rates["output_per_mtok"]) / 1_000_000
    web_cost = float(rates.get("web_search_per_call") or 0.0) if web_search else 0.0
    total = input_cost + cached_cost + output_cost + web_cost
    return {
        "uncached_input_tokens": float(uncached),
        "input_cost": round(input_cost, 8),
        "cached_input_cost": round(cached_cost, 8),
        "output_cost": round(output_cost, 8),
        "web_search_cost": round(web_cost, 8),
        "total_cost": round(total, 8),
    }


def estimate_pre_call_cost_usd(
    *,
    model: str,
    input_chars: int,
    max_output_tokens: int,
    web_search: bool = False,
) -> float:
    """Conservative pre-call estimate using full output budget."""
    input_tokens = estimate_tokens_from_chars(input_chars)
    return estimate_call_cost_usd(
        model=model,
        input_tokens=input_tokens,
        output_tokens=max_output_tokens,
        web_search=web_search,
    )


def extract_usage_tokens(response: Any) -> dict[str, int | None]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {
            "input_tokens": None,
            "cached_input_tokens": None,
            "output_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": None,
        }
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    total_tokens = getattr(usage, "total_tokens", None)
    cached = None
    details = getattr(usage, "input_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", None)
    if cached is None:
        cached = getattr(usage, "cached_input_tokens", None)
    reasoning = None
    out_details = getattr(usage, "output_tokens_details", None)
    if out_details is not None:
        reasoning = getattr(out_details, "reasoning_tokens", None)
    if reasoning is None:
        reasoning = getattr(usage, "reasoning_tokens", None)
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = int(input_tokens) + int(output_tokens)
    return {
        "input_tokens": int(input_tokens) if input_tokens is not None else None,
        "cached_input_tokens": int(cached) if cached is not None else None,
        "output_tokens": int(output_tokens) if output_tokens is not None else None,
        "reasoning_tokens": int(reasoning) if reasoning is not None else None,
        "total_tokens": int(total_tokens) if total_tokens is not None else None,
    }
