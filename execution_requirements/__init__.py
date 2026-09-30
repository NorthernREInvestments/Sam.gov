"""Phase D — Execution + Compliance Intelligence (projection layer).

Aggregates existing extractors into a normalized ExecutionRequirement model.
Does not replace scoring, bid_compliance, DLA clause extraction, or µLab engines.
Does not send email or submit bids.
"""

from __future__ import annotations

from execution_requirements.cash_cycle import build_cash_cycle_handoff
from execution_requirements.checklists import (
    build_invoice_checklist,
    build_post_award_checklist,
    build_submission_checklist,
    build_supplier_confirmation_checklist,
    build_supplier_quote_packet,
)
from execution_requirements.conflicts import detect_requirement_conflicts, apply_amendment_overlay
from execution_requirements.constants import (
    BUILD_TAG,
    CATEGORIES,
    CLAUSE_RELEVANCE,
    REQUIREMENT_STATUSES,
)
from execution_requirements.extract import build_execution_requirements
from execution_requirements.enrichment import enrich_deal_for_operator, owner_ready_banner_allowed
from execution_requirements.profile import build_execution_compliance_profile
from execution_requirements.readiness import evaluate_owner_approval_gate
from execution_requirements.supplier_state import resolve_supplier_execution_state

__all__ = [
    "BUILD_TAG",
    "CATEGORIES",
    "CLAUSE_RELEVANCE",
    "REQUIREMENT_STATUSES",
    "build_execution_requirements",
    "build_execution_compliance_profile",
    "detect_requirement_conflicts",
    "apply_amendment_overlay",
    "build_submission_checklist",
    "build_supplier_confirmation_checklist",
    "build_supplier_quote_packet",
    "build_post_award_checklist",
    "build_invoice_checklist",
    "build_cash_cycle_handoff",
    "evaluate_owner_approval_gate",
    "enrich_deal_for_operator",
    "owner_ready_banner_allowed",
    "resolve_supplier_execution_state",
]
