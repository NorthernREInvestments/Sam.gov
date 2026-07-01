"""Per-contract proposal pipeline status for dashboard incomplete tracking."""

from __future__ import annotations

from typing import Any

from models import Contract, ContractSub, Proposal, SubContact
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
        session.query(SubContact)
        .filter(
            SubContact.contract_id == contract.id,
            SubContact.quote_received.is_(True),
            SubContact.quote_amount.isnot(None),
        )
        .count()
    )
    if quoted_count == 0:
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


WORKFLOW_STAGE_KEYS = ("found", "analyzed", "subs", "proposal", "submitted")
WORKFLOW_STAGE_LABELS = ("Found", "Analyzed", "Subs", "Proposal", "Submitted")
PERFORMANCE_STATUSES = frozenset({"awarded", "active", "option_year", "stop_work", "completed"})


def _contract_filter_category(contract: Contract, session) -> str:
    """Dashboard status filter bucket."""
    if contract.status in ("active", "option_year", "stop_work"):
        return "active"
    if contract.status in ("awarded", "won"):
        return "awarded"
    if contract.status == "submitted" or (contract.analysis or {}).get("status") == "submitted":
        pass
    wf = compute_workflow_status(contract, session)
    stage = wf.get("stage") or "none"
    if stage in ("submitted", "won") or contract.status == "submitted":
        return "submitted"
    if stage in ("needs_proposal", "proposal_incomplete", "draft_ready", "ready"):
        return "ready_to_bid"
    if stage == "needs_sub_quote" or wf.get("quoted_sub_count", 0) == 0:
        analysis = contract.analysis if isinstance(contract.analysis, dict) else {}
        if analysis.get("pursue") is True:
            return "needs_subs"
    if contract.status in PERFORMANCE_STATUSES:
        return "awarded" if contract.status == "awarded" else "active"
    return "other"


def matches_status_filter(contract: Contract, session, status_filter: str | None) -> bool:
    if not status_filter or status_filter.lower() in ("all", ""):
        return True
    cat = _contract_filter_category(contract, session)
    key = status_filter.lower().replace(" ", "_")
    mapping = {
        "needs_subs": "needs_subs",
        "ready_to_bid": "ready_to_bid",
        "submitted": "submitted",
        "awarded": "awarded",
        "active": "active",
    }
    return cat == mapping.get(key, key)


def matches_set_aside_filter(contract: Contract, set_aside_filter: str | None) -> bool:
    if not set_aside_filter or set_aside_filter.lower() in ("all", ""):
        return True
    raw = (contract.set_aside or "").lower()
    if not raw:
        return set_aside_filter.lower() in ("all", "")
    key = set_aside_filter.lower().replace("-", "").replace(" ", "")
    patterns = {
        "totalsmallbusiness": ("total small business", "small business set-aside", "small business"),
        "8a": ("8(a)", "8a"),
        "hubzone": ("hubzone", "hub zone"),
        "wosb": ("wosb", "women-owned", "woman owned"),
        "sdvosb": ("sdvosb", "service-disabled", "service disabled veteran"),
    }
    needles = patterns.get(key, (set_aside_filter.lower(),))
    return any(n in raw for n in needles)


def compute_workflow_progress(contract: Contract, session) -> dict[str, Any]:
    """Five-stage progress for compact dashboard cards."""
    from sam_enrich import is_scrape_complete

    analysis = contract.analysis if isinstance(contract.analysis, dict) else {}
    raw = contract.sam_raw if isinstance(contract.sam_raw, dict) else {}
    wf = compute_workflow_status(contract, session)
    stage = wf.get("stage") or "none"

    found_done = True
    analyzed_done = bool(
        analysis.get("screening_stage") == "full"
        and (analysis.get("plain_english_summary") or analysis.get("executive_summary"))
    ) or bool(analysis.get("score") is not None and is_scrape_complete(raw))
    subs_done = wf.get("quoted_sub_count", 0) > 0 or stage not in ("needs_sub_quote", "none")
    proposal_done = stage in (
        "draft_ready",
        "ready",
        "submitted",
        "won",
        "lost",
        "proposal_incomplete",
    ) or bool(wf.get("proposal_id"))
    submitted_done = (
        stage in ("submitted", "won")
        or contract.status in ("submitted", "awarded", "active", "option_year", "stop_work", "completed", "won")
    )

    if contract.status in PERFORMANCE_STATUSES or contract.status == "won":
        analyzed_done = subs_done = proposal_done = submitted_done = True

    done_flags = [found_done, analyzed_done, subs_done, proposal_done, submitted_done]
    current_index = 0
    for i, done in enumerate(done_flags):
        if not done:
            current_index = i
            break
    else:
        current_index = len(done_flags) - 1

    stages: list[dict[str, str]] = []
    for i, (key, label, done) in enumerate(zip(WORKFLOW_STAGE_KEYS, WORKFLOW_STAGE_LABELS, done_flags)):
        if done:
            state = "done"
        elif i == current_index:
            state = "current"
        else:
            state = "future"
        stages.append({"key": key, "label": label, "state": state})

    status_message = _dashboard_status_message(contract, wf)
    primary_action = _dashboard_primary_action(contract, wf)

    return {
        "stages": stages,
        "current_index": current_index,
        "status_message": status_message,
        "primary_action": primary_action,
        "filter_category": _contract_filter_category(contract, session),
    }


def _dashboard_status_message(contract: Contract, wf: dict[str, Any]) -> str:
    if contract.status in ("active", "option_year", "stop_work"):
        return "Contract won — performance active"
    if contract.status in ("awarded", "won"):
        return "Contract awarded"
    if contract.status == "completed":
        return "Contract completed"
    if contract.status == "not_awarded":
        return "Bid was not selected"
    if contract.status == "submitted":
        return "Bid submitted — waiting for decision"
    stage = wf.get("stage") or ""
    if stage == "needs_sub_quote" or wf.get("quoted_sub_count", 0) == 0:
        return "Need to find subs and get quotes"
    if stage in ("needs_proposal", "proposal_incomplete"):
        return "Ready to write proposal"
    if stage == "draft_ready":
        return "Proposal draft ready for review"
    if stage == "ready":
        return "Ready to submit bid"
    if stage == "submitted":
        return "Bid submitted — waiting for decision"
    if stage == "won":
        return "Contract awarded"
    if stage == "lost":
        return "Bid was not selected"
    analysis = contract.analysis if isinstance(contract.analysis, dict) else {}
    if analysis.get("pursue") is not True:
        return "Reviewing fit"
    return "New opportunity found"


def _dashboard_primary_action(contract: Contract, wf: dict[str, Any]) -> dict[str, str]:
    if contract.status in ("awarded", "active", "option_year", "stop_work"):
        return {"label": "View Performance", "action": "performance"}
    stage = wf.get("stage") or "none"
    if stage == "needs_sub_quote" or wf.get("quoted_sub_count", 0) == 0:
        return {"label": "Find Subs", "action": "find_subs"}
    if stage in ("needs_proposal", "proposal_incomplete", "draft_ready"):
        return {"label": "View Proposal", "action": "proposal"}
    if stage == "ready":
        return {"label": "Open Checklist", "action": "checklist"}
    if stage in ("submitted", "won"):
        return {"label": "View Contract", "action": "overview"}
    return {"label": "View Proposal", "action": "proposal"}
