"""Financing Intelligence package — verified lender rules → capital stacks → opportunity decisions."""

from financing_intelligence.assess import (
    assess_opportunity_financing,
    attach_financing_to_opportunity_row,
    blocked_profit_summary,
)
from financing_intelligence.capital import (
    capital_snapshot,
    confirm_reservation,
    propose_reservation,
    release_reservation,
    update_capital,
)
from financing_intelligence.constants import BUILD
from financing_intelligence.facts import decide_fact, list_facts
from financing_intelligence.notes_extract import ingest_call_notes
from financing_intelligence.outcomes import historical_outcome_patterns, list_outcomes, record_outcome
from financing_intelligence.service import dashboard, ui_page
from financing_intelligence.sources import list_sources, upsert_source
from financing_intelligence.stack_engine import build_capital_stacks

__all__ = [
    "BUILD",
    "assess_opportunity_financing",
    "attach_financing_to_opportunity_row",
    "blocked_profit_summary",
    "build_capital_stacks",
    "capital_snapshot",
    "confirm_reservation",
    "dashboard",
    "decide_fact",
    "historical_outcome_patterns",
    "ingest_call_notes",
    "list_facts",
    "list_outcomes",
    "list_sources",
    "propose_reservation",
    "record_outcome",
    "release_reservation",
    "ui_page",
    "update_capital",
    "upsert_source",
]
