"""Shared research framework — one engine, many objectives."""

from m3.research.engine import (
    cancel_research_run,
    run_contract_discovery,
    run_historical_award_research,
    run_supplier_discovery,
)

__all__ = [
    "run_contract_discovery",
    "run_supplier_discovery",
    "run_historical_award_research",
    "cancel_research_run",
]
