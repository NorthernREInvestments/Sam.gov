"""Per-contract proposal pipeline status for dashboard incomplete tracking."""

from __future__ import annotations

from typing import Any

from models import Contract, ContractSub, Proposal
from proposal_service import QUOTED_STATUSES

STAGE_LABELS: dict[str, str] = {
    "none": "",
    "needs_sub_quote": "Needs sub quote",
    "needs_proposal": "Ready to bid",
    "proposal_incomplete": "Proposal incomplete",
    "draft_ready": "Draft ready",
    "ready": "Ready to submit",
    "submitted": "Submitted",
    "won": "Won",
    "lost": "Lost",
}

OWNER_REQUIRED = [
    ("address_line_1", "Business address"),
    ("city", "City"),
    ("zip", "ZIP code"),
    ("uei", "UEI number"),
    ("cage_code", "CAGE code"),
    ("ein", "EIN"),
    ("sam_expiration", "SAM registration expiration"),
]

SOLICITATION_REQUIRED = [
    ("contracting_officer_name", "Contracting Officer name"),
    ("contracting_officer_email", "Contracting Officer email"),
    ("submission_method", "Submission method"),
]


def _owner_gaps() -> list[dict[str, str]]:
    from settings_store import get_owner_settings

    owner = get_owner_settings()
    gaps: list[dict[str, str]] = []
    for key, label in OWNER_REQUIRED:
        if not str(owner.get(key) or "").strip():
            gaps.append({"code": f"owner_{key}", "label": label, "where": "settings"})
    return gaps


def _solicitation_gaps(analysis: dict[str, Any]) -> list[dict[str, str]]:
    sol = analysis.get("solicitation_meta") if isinstance(analysis.get("solicitation_meta"), dict) else {}
    gaps: list[dict[str, str]] = []
    for key, label in SOLICITATION_REQUIRED:
        val = sol.get(key) or analysis.get(key)
        if not str(val or "").strip():
            gaps.append({"code": key, "label": label, "where": "solicitation"})
    return gaps


def compute_workflow_status(contract: Contract, session) -> dict[str, Any]:
    analysis = contract.analysis if isinstance(contract.analysis, dict) else {}
    if analysis.get("pursue") is not True:
        return {
            "stage": "none",
            "label": "",
            "incomplete": False,
            "items": [],
            "proposal_id": None,
            "quoted_sub_count": 0,
        }

    items: list[dict[str, str]] = []
    items.extend(_owner_gaps())

    quoted_count = (
        session.query(ContractSub)
        .filter(
            ContractSub.contract_id == contract.id,
            ContractSub.status.in_(QUOTED_STATUSES),
            ContractSub.quote_amount.isnot(None),
        )
        .count()
    )

    proposal = (
        session.query(Proposal)
        .filter_by(contract_id=contract.id)
        .order_by(Proposal.date_updated.desc())
        .first()
    )

    stage = "needs_sub_quote"
    if quoted_count == 0:
        items.insert(0, {"code": "sub_quote", "label": "Record a sub quote", "where": "subs"})
    elif not proposal:
        stage = "needs_proposal"
        items.insert(0, {"code": "proposal", "label": "Configure bid and generate proposal", "where": "pursue"})
        items.extend(_solicitation_gaps(analysis))
    elif proposal.status in ("won", "lost", "submitted"):
        stage = proposal.status
    elif proposal.missing_fields:
        stage = "proposal_incomplete"
        for mf in proposal.missing_fields or []:
            if isinstance(mf, dict):
                items.append(
                    {
                        "code": str(mf.get("field") or ""),
                        "label": str(mf.get("label") or "Missing field"),
                        "where": str(mf.get("where") or "proposal"),
                    }
                )
    elif proposal.status in ("draft", "ready"):
        stage = "draft_ready" if proposal.status == "draft" else "ready"
    else:
        stage = "needs_proposal"

    if stage in ("needs_proposal", "proposal_incomplete", "draft_ready", "ready"):
        for gap in _solicitation_gaps(analysis):
            if not any(i.get("code") == gap["code"] for i in items):
                items.append(gap)

    if proposal and any(i.get("where") in ("settings", "solicitation") for i in items):
        if stage in ("draft_ready", "ready", "needs_proposal"):
            stage = "proposal_incomplete"

    incomplete = stage in ("needs_sub_quote", "needs_proposal", "proposal_incomplete") or bool(
        _owner_gaps()
    )

    return {
        "stage": stage,
        "label": STAGE_LABELS.get(stage, stage.replace("_", " ").title()),
        "incomplete": incomplete,
        "items": items,
        "proposal_id": proposal.id if proposal else None,
        "quoted_sub_count": quoted_count,
        "already_bid": stage in ("submitted", "won"),
        "do_not_rebid": stage in ("submitted", "won"),
    }


def compute_card_pipeline(contract: Contract, session) -> dict[str, Any]:
    """Visual intake + bid steps for dashboard cards."""
    from sam_enrich import is_scrape_complete

    analysis = contract.analysis if isinstance(contract.analysis, dict) else {}
    raw = contract.sam_raw if isinstance(contract.sam_raw, dict) else {}
    wf = compute_workflow_status(contract, session)

    intake_steps = [
        {
            "key": "attachments",
            "label": "Attachments",
            "state": "done" if is_scrape_complete(raw) else "pending",
        },
        {
            "key": "analysis",
            "label": "Claude analysis",
            "state": "done" if analysis.get("screening_stage") == "full" else "pending",
        },
    ]

    bid_steps: list[dict[str, str]] = []
    if analysis.get("pursue") is True:
        stage = wf.get("stage") or "none"
        bid_steps = [
            {
                "key": "sub_quote",
                "label": "Sub quote",
                "state": "done" if stage not in ("needs_sub_quote", "none") else "pending",
            },
            {
                "key": "proposal",
                "label": "Proposal",
                "state": "done"
                if stage in ("draft_ready", "ready", "submitted", "won", "lost", "proposal_incomplete")
                else "pending",
            },
            {
                "key": "submitted",
                "label": "Submitted",
                "state": "done" if stage in ("submitted", "won", "lost") else "pending",
            },
        ]

    return {
        "intake": intake_steps,
        "bid": bid_steps,
        "do_not_rebid": wf.get("do_not_rebid", False),
        "already_bid_label": wf.get("label") if wf.get("do_not_rebid") else "",
    }
