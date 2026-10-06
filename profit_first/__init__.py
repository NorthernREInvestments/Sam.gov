"""Profit-first product discovery and economics routing.

Governing principle: find live tangible-product opportunities with executable
profit after freight, financing, and required costs — not category hunting.
"""

from __future__ import annotations

from profit_first.router import (
    evaluate_opportunity_profit,
    owner_profit_card,
    research_next_priority,
)
from profit_first.reevaluate import reevaluate_canonical_population
from profit_first.telemetry import profit_funnel_telemetry

__all__ = [
    "evaluate_opportunity_profit",
    "owner_profit_card",
    "research_next_priority",
    "reevaluate_canonical_population",
    "profit_funnel_telemetry",
]
