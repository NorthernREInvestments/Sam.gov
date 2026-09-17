"""Commercial readiness ladder + final bid execution gate."""

from __future__ import annotations

from typing import Any

from commercial_verification_constants import (
    CR_AUTH_REQ,
    CR_EXEC_CONFIRMED,
    CR_FUNDING_COND,
    CR_FUNDING_UNVER,
    CR_PARTIAL,
    CR_PLAN_READY,
    CR_PUBLIC,
    CV_FAILED,
    EG_CONDITIONAL,
    EG_NOT_READY,
    EG_READY_FINAL,
    FUND_COND_FEASIBLE,
    FUND_EXHAUSTED,
    FUND_NOT_REQUIRED,
    FUND_VERIFY_REQ,
    FUND_VERIFIED,
    PURSUIT_KEEP,
    PURSUIT_KEEP_UNCERTAIN,
    PURSUIT_STOP_FUNDING,
)


def evaluate_pursuit_vs_execution(
    *,
    economically_attractive: bool = False,
    funding_gate_state: str | None = None,
    transaction_funding_exhausted: bool = False,
    execution_ready: bool = False,
) -> dict[str, Any]:
    """SHOULD WE KEEP PURSUING? vs COULD WE EXECUTE RIGHT NOW?"""
    if transaction_funding_exhausted or funding_gate_state == FUND_EXHAUSTED:
        return {
            "pursuit": PURSUIT_STOP_FUNDING,
            "pursuit_alive": False,
            "execution": EG_NOT_READY,
            "why_execution_not_ready": ["All reasonable funding paths affirmatively exhausted"],
            "deal_cannot_be_done": True,
        }
    if funding_gate_state in {FUND_VERIFY_REQ, "FUNDING_PATH_IDENTIFIED", "FUNDING_RESEARCH_ONLY", "FUNDING_REQUIREMENT_UNKNOWN"}:
        return {
            "pursuit": PURSUIT_KEEP_UNCERTAIN if economically_attractive else PURSUIT_KEEP,
            "pursuit_alive": True,
            "execution": EG_NOT_READY,
            "why_execution_not_ready": [
                "Funding has not yet been transaction-verified — not 'funding unavailable'"
            ],
            "deal_cannot_be_done": False,
            "status_label": "FUNDING VERIFICATION REQUIRED",
        }
    if execution_ready:
        return {
            "pursuit": PURSUIT_KEEP,
            "pursuit_alive": True,
            "execution": EG_READY_FINAL,
            "why_execution_not_ready": [],
            "deal_cannot_be_done": False,
        }
    return {
        "pursuit": PURSUIT_KEEP if economically_attractive else PURSUIT_KEEP_UNCERTAIN,
        "pursuit_alive": True,
        "execution": EG_NOT_READY,
        "why_execution_not_ready": ["Other readiness requirements unmet"],
        "deal_cannot_be_done": False,
    }


def evaluate_commercial_readiness(
    *,
    plan_built: bool = False,
    actions_pending_auth: bool = False,
    verification_in_progress: bool = False,
    inputs_partial: bool = False,
    inputs_verified: bool = False,
    funding_gate_state: str | None = None,
    verification_outcome: str | None = None,
    economically_attractive: bool = False,
) -> dict[str, Any]:
    if verification_outcome == CV_FAILED:
        return {"state": CR_PUBLIC, "note": "commercial_verification_failed", "live_verification_blocked": True}
    if funding_gate_state == FUND_EXHAUSTED:
        return {
            "state": CR_PUBLIC,
            "note": "transaction_funding_exhausted",
            "pursuit_alive": False,
            "live_verification_blocked": True,
        }
    if inputs_verified and funding_gate_state in {FUND_NOT_REQUIRED, FUND_VERIFIED, FUND_COND_FEASIBLE}:
        if funding_gate_state == FUND_COND_FEASIBLE:
            return {"state": CR_FUNDING_COND, "live_verification_blocked": True, "pursuit_alive": True}
        return {"state": CR_EXEC_CONFIRMED, "live_verification_blocked": True, "pursuit_alive": True}
    if inputs_verified and funding_gate_state in {
        FUND_VERIFY_REQ,
        "FUNDING_PATH_IDENTIFIED",
        "FUNDING_RESEARCH_ONLY",
        "FUNDING_REQUIREMENT_UNKNOWN",
    }:
        return {
            "state": CR_FUNDING_UNVER,
            "economically_attractive_but_funding_unverified": economically_attractive,
            "economically_attractive_funding_verification_required": economically_attractive,
            "pursuit_alive": True,
            "live_verification_blocked": True,
        }
    if inputs_partial:
        return {"state": CR_PARTIAL, "live_verification_blocked": True, "pursuit_alive": True}
    if verification_in_progress:
        return {"state": "VERIFICATION_IN_PROGRESS", "live_verification_blocked": True, "pursuit_alive": True}
    if actions_pending_auth:
        return {"state": CR_AUTH_REQ, "live_verification_blocked": True, "pursuit_alive": True}
    if plan_built:
        return {"state": CR_PLAN_READY, "live_verification_blocked": True, "pursuit_alive": True}
    return {"state": CR_PUBLIC, "live_verification_blocked": True, "pursuit_alive": True}


def evaluate_execution_gate(
    *,
    solicitation_open: bool = True,
    deadline_viable: bool = True,
    package_fresh: bool = True,
    amendments_acknowledged: bool = False,
    compliance_passes: bool = False,
    product_compliant: bool = False,
    acquisition_verified_current: bool = False,
    availability_sufficient: bool = False,
    delivery_viable: bool = False,
    freight_known: bool = False,
    funding_required: bool = False,
    funding_feasible: bool = False,
    funding_verification_pending: bool = False,
    profit_floor_preserved: bool = False,
    profit_after_financing_ok: bool = False,
    required_forms_present: bool = False,
    unresolved_certifications_identified: bool = True,
    signatures_identified: bool = True,
    submission_instructions_known: bool = False,
    commercial_evidence_expired: bool = False,
) -> dict[str, Any]:
    """Could this deal be submitted IF operator authorized? Never SUBMITTED."""
    blockers = []
    why_not_ready: list[str] = []
    if not solicitation_open:
        blockers.append("solicitation_not_open")
    if not deadline_viable:
        blockers.append("deadline_not_viable")
    if not package_fresh:
        blockers.append("governing_package_not_fresh")
    if not amendments_acknowledged:
        blockers.append("amendments_not_acknowledged")
    if not compliance_passes:
        blockers.append("compliance_not_passing")
    if not product_compliant:
        blockers.append("product_not_compliant")
    if not acquisition_verified_current:
        blockers.append("acquisition_pricing_not_verified_current")
    if not availability_sufficient:
        blockers.append("availability_insufficient")
    if not delivery_viable:
        blockers.append("delivery_not_viable")
    if not freight_known:
        blockers.append("freight_unknown")
    if funding_required and not funding_feasible:
        if funding_verification_pending:
            blockers.append("funding_not_yet_transaction_verified")
            why_not_ready.append(
                "NOT READY BECAUSE: Funding has not yet been transaction-verified (not 'funding unavailable')."
            )
        else:
            blockers.append("funding_not_feasible")
            why_not_ready.append("NOT READY BECAUSE: Funding feasibility not established.")
    if not profit_floor_preserved:
        blockers.append("profit_floor_not_preserved")
    if funding_required and not profit_after_financing_ok:
        blockers.append("profit_after_financing_not_ok")
    if not required_forms_present:
        blockers.append("required_forms_missing")
    if not unresolved_certifications_identified:
        blockers.append("certifications_not_identified")
    if not signatures_identified:
        blockers.append("signatures_not_identified")
    if not submission_instructions_known:
        blockers.append("submission_instructions_unknown")
    if commercial_evidence_expired:
        blockers.append("commercial_evidence_expired")

    if blockers:
        soft = {"certifications_not_identified", "signatures_identified"}
        hard = [b for b in blockers if b not in soft]
        state = EG_NOT_READY if hard else EG_CONDITIONAL
    else:
        state = EG_READY_FINAL

    return {
        "kind": "ExecutionGate",
        "state": state,
        "blockers": blockers,
        "why_not_ready": why_not_ready,
        "checks_funding_when_required": True,
        "checks_profit_after_financing": True,
        "submitted": False,
        "does_not_say_funding_unavailable_when_unverified": True,
        "question": "COULD THIS DEAL BE SUBMITTED IF OPERATOR AUTHORIZED IT?",
    }
