"""Operator workflow state-transition legality (projection checks — not a workflow engine).

Phase F: document and test legal vs illegal transitions against current M3 behavior.
Does not add RBAC or autonomous state mutation.
"""

from __future__ import annotations

from typing import Any

from operator_workflow.constants import (
    OW_AWARDED,
    OW_BID_PREPARATION,
    OW_DEEP_RESEARCH,
    OW_DELIVERED,
    OW_DISCOVERED,
    OW_FINANCE_REVIEW,
    OW_HISTORY,
    OW_ORDERING,
    OW_PAID,
    OW_QUALIFIED,
    OW_SUBMITTED,
    OW_SUPPLIER_VALIDATION,
    STATE_RANK,
)

# Forward transitions that are normally legal when evidence supports them
_FORWARD_OK = {
    (OW_DISCOVERED, OW_QUALIFIED),
    (OW_QUALIFIED, OW_DEEP_RESEARCH),
    (OW_DEEP_RESEARCH, OW_SUPPLIER_VALIDATION),
    (OW_SUPPLIER_VALIDATION, OW_FINANCE_REVIEW),
    (OW_FINANCE_REVIEW, OW_BID_PREPARATION),
    (OW_BID_PREPARATION, OW_SUBMITTED),  # requires explicit submission evidence
    (OW_SUBMITTED, OW_AWARDED),
    (OW_AWARDED, OW_ORDERING),
    (OW_ORDERING, OW_DELIVERED),
    (OW_DELIVERED, OW_PAID),  # normally via acceptance/invoice — see notes
    (OW_PAID, OW_HISTORY),
}

# Transitions that require explicit override / audited evidence
_REQUIRES_OVERRIDE = {
    (OW_HISTORY, OW_DISCOVERED),
    (OW_HISTORY, OW_QUALIFIED),
    (OW_HISTORY, OW_BID_PREPARATION),  # REJECTED/EXPIRED → ACTIVE
    ("EXPIRED", OW_BID_PREPARATION),
    ("REJECTED", OW_QUALIFIED),
    ("REJECTED", OW_BID_PREPARATION),
}


def evaluate_transition(
    from_state: str,
    to_state: str,
    *,
    submission_evidenced: bool = False,
    acceptance_evidenced: bool = False,
    invoice_evidenced: bool = False,
    override_audited: bool = False,
    renewed_solicitation: bool = False,
) -> dict[str, Any]:
    """
    Return legality of a proposed operator-state transition.

    This is a check/helper for Phase F torture tests — it does not mutate deals.
    """
    src = str(from_state or "").upper()
    dst = str(to_state or "").upper()
    # Normalize READY alias used in legacy Deal_state
    if dst == "READY":
        dst = OW_BID_PREPARATION
    if src == "READY":
        src = OW_BID_PREPARATION

    notes: list[str] = []
    if src == dst:
        return {"legal": True, "requires_override": False, "notes": ["No-op transition"], "from": src, "to": dst}

    # EXPIRED → READY/BID_PREP invalid without renewed solicitation / override
    if src in {"EXPIRED", "CLOSED_EXPIRED"} and dst in {OW_BID_PREPARATION, OW_SUBMITTED}:
        ok = bool(override_audited or renewed_solicitation)
        return {
            "legal": ok,
            "requires_override": not ok,
            "notes": [
                "EXPIRED → READY/BID_PREPARATION invalid without renewed solicitation or audited override"
            ],
            "from": src,
            "to": dst,
        }

    # REJECTED → ACTIVE requires audited override
    if src in {"REJECTED", "REJECTED_CHEAP_SCREEN", OW_HISTORY} and dst not in {OW_HISTORY, OW_PAID}:
        if STATE_RANK.get(dst, 0) < STATE_RANK.get(OW_SUBMITTED, 6) or dst in {
            OW_DISCOVERED,
            OW_QUALIFIED,
            OW_DEEP_RESEARCH,
            OW_SUPPLIER_VALIDATION,
            OW_FINANCE_REVIEW,
            OW_BID_PREPARATION,
        }:
            ok = bool(override_audited)
            return {
                "legal": ok,
                "requires_override": True,
                "notes": ["REJECTED/HISTORY → active requires audited override"],
                "from": src,
                "to": dst,
            }

    # BID_PREPARATION → SUBMITTED only with submission evidence
    if src == OW_BID_PREPARATION and dst == OW_SUBMITTED:
        ok = bool(submission_evidenced)
        return {
            "legal": ok,
            "requires_override": False,
            "notes": [
                "READY/BID_PREPARATION → SUBMITTED valid only after explicit submission action/evidence"
                if ok
                else "Submission not evidenced — cannot mark SUBMITTED"
            ],
            "from": src,
            "to": dst,
        }

    # DELIVERED → PAID should not skip acceptance/invoice unless path differs
    if src == OW_DELIVERED and dst == OW_PAID:
        ok = bool(acceptance_evidenced and invoice_evidenced) or override_audited
        notes.append(
            "DELIVERED → PAID normally requires acceptance + invoice evidence"
            if not ok
            else "Acceptance and invoice evidenced (or audited override)"
        )
        return {"legal": ok, "requires_override": bool(override_audited and not (acceptance_evidenced and invoice_evidenced)), "notes": notes, "from": src, "to": dst}

    # AWARDED → ORDERING is valid
    if src == OW_AWARDED and dst == OW_ORDERING:
        return {"legal": True, "requires_override": False, "notes": ["Awarded → ordering is valid"], "from": src, "to": dst}

    pair = (src, dst)
    if pair in _FORWARD_OK:
        return {"legal": True, "requires_override": False, "notes": ["Forward ladder transition"], "from": src, "to": dst}

    # Same-rank or backward without override: conservative illegal
    sr = STATE_RANK.get(src)
    dr = STATE_RANK.get(dst)
    if sr is not None and dr is not None and dr < sr and not override_audited:
        return {
            "legal": False,
            "requires_override": True,
            "notes": ["Backward transition requires audited override"],
            "from": src,
            "to": dst,
        }
    if sr is not None and dr is not None and dr > sr + 1 and not override_audited:
        return {
            "legal": False,
            "requires_override": True,
            "notes": ["Skipping multiple stages requires audited override / evidence"],
            "from": src,
            "to": dst,
        }

    return {
        "legal": bool(override_audited),
        "requires_override": True,
        "notes": notes or ["Transition not on standard ladder — requires explicit override"],
        "from": src,
        "to": dst,
    }
