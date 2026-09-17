"""Cost-governor integration helpers for tracked deals and paid entry points."""

from __future__ import annotations

from typing import Any

from authoritative_freshness_gate import evaluate_authoritative_freshness_gate
from cost_governor import CostGovernor, get_cost_governor
from cost_governor_constants import (
    TRACKED_CHANGE_ANALYSIS_BLOCKED_BY_BUDGET,
    TIER_1_ACTIVE,
)
from national_discovery_constants import NOT_READY_UNREVIEWED_CHANGE, READY_FOR_SUBMISSION
from tracked_solicitation import TrackedSolicitationStore


def authorize_tracked_change_analysis(
    governor: CostGovernor,
    *,
    deal_id: str,
    change_id: str,
    question: str,
    estimated_max_cost: float = 0.25,
) -> dict[str, Any]:
    auth = governor.authorize(
        {
            "provider": "openai",
            "action_type": "AI_COMPLETION",
            "deal_id": deal_id,
            "tracked": True,
            "material_change": True,
            "priority_tier": TIER_1_ACTIVE,
            "question": question,
            "could_change_decision": True,
            "estimated_max_cost": estimated_max_cost,
            "estimated_input_tokens": 2000,
            "estimated_output_tokens": 800,
            "idempotency_key": change_id,
            "content_version": change_id,
        }
    )
    if not auth.get("authorized"):
        return {
            **auth,
            "tracked_state": TRACKED_CHANGE_ANALYSIS_BLOCKED_BY_BUDGET,
            "visual_alert": {
                "badge_text": "TRACKED DEAL RESEARCH BLOCKED",
                "icon": "stop",
                "css_class": "budget-badge-exhausted change-alert-critical",
                "text_label_required": True,
                "aria_label": "Tracked deal research blocked by budget",
            },
        }
    return auth


def apply_tracked_budget_block(
    store: TrackedSolicitationStore,
    *,
    deal_id: str,
    change: dict[str, Any],
    auth: dict[str, Any],
) -> dict[str, Any]:
    tracked = store.get(deal_id) or store.promote(deal_id)
    if not auth.get("authorized"):
        tracked["deal_state"] = dict(tracked.get("deal_state") or {})
        tracked["deal_state"]["budget_block"] = {
            "status": TRACKED_CHANGE_ANALYSIS_BLOCKED_BY_BUDGET,
            "change_id": change.get("change_id"),
            "reason": auth.get("reason"),
        }
        tracked["readiness"] = NOT_READY_UNREVIEWED_CHANGE
        store._tracked[deal_id] = tracked
        # Keep change unreviewed
        return {"tracked": tracked, "blocked": True}
    return {"tracked": tracked, "blocked": False}


def freshness_with_budget_block(
    *,
    unreviewed_changes: list[dict[str, Any]],
    budget_blocked_analysis: bool,
    last_check_at: str | None,
) -> dict[str, Any]:
    gate = evaluate_authoritative_freshness_gate(
        last_authoritative_check_at=last_check_at,
        current_version_confirmed=True,
        amendment_set_confirmed=not budget_blocked_analysis,
        unreviewed_material_changes=unreviewed_changes,
    )
    if budget_blocked_analysis:
        gate["passed"] = False
        gate["blockers"] = list(gate.get("blockers") or []) + ["tracked_change_analysis_blocked_by_budget"]
        gate["readiness"] = NOT_READY_UNREVIEWED_CHANGE
        gate["would_allow_ready_for_submission"] = False
    return gate


def wrap_openai_paid_request(request: dict[str, Any], governor: CostGovernor | None = None) -> dict[str, Any]:
    """Entry-point helper — openai_runtime / other providers consult this before paid calls."""
    gov = governor or get_cost_governor()
    return gov.authorize(request)
