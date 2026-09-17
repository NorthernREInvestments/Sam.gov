"""Budget-aware catch-up — recover cheaply, kill dead work, research only actionable survivors."""

from __future__ import annotations

from typing import Any

from application_clock import now_utc
from cost_governor_constants import (
    CATCHUP_PRESERVE_ACTIONABLE,
    CATCHUP_REJECT_CANCELLED,
    CATCHUP_REJECT_CLOSED,
    CATCHUP_REJECT_EXPIRED,
    CATCHUP_REJECT_NON_FIT,
    CATCHUP_REJECT_TOO_LATE,
    DISCOVERED_PENDING_RESEARCH,
    MARKET_BEHIND,
    MARKET_CATCHING_UP,
    MARKET_CURRENT,
    MARKET_UNKNOWN,
    SOURCE_FAILED,
    SOURCE_PAUSED_BUDGET,
)
from discovery.deadline_viability import VIABILITY_TOO_LATE, compute_deadline_runway
from discovery_checkpoint import filter_records_for_incremental, incremental_window
from national_discovery_funnel import ResearchBacklog, stage1_ultra_cheap, stage2_transaction_fit
from solicitation_identity import SolicitationInventory


def _utc() -> str:
    return now_utc().isoformat()


def market_coverage_state(
    *,
    sources: list[dict[str, Any]],
    budget_paused_ids: set[str] | None = None,
    catching_up_ids: set[str] | None = None,
    expected_fresh_hours: float = 36.0,
) -> dict[str, Any]:
    """Coverage based on checkpoints — distinct from research backlog completeness."""
    budget_paused_ids = budget_paused_ids or set()
    catching_up_ids = catching_up_ids or set()
    current = []
    behind = []
    failed = []
    paused_budget = []
    oldest = None
    newest = None
    for s in sources:
        sid = s.get("source_id")
        cp = s.get("last_successful_checkpoint")
        if cp:
            if oldest is None or str(cp) < str(oldest):
                oldest = cp
            if newest is None or str(cp) > str(newest):
                newest = cp
        if sid in budget_paused_ids:
            paused_budget.append(sid)
            behind.append(sid)
            continue
        if sid in catching_up_ids:
            behind.append(sid)
            continue
        if s.get("pause_reason") == SOURCE_FAILED or s.get("health_state") in {"DEAD", "QUARANTINED"}:
            failed.append(sid)
            behind.append(sid)
            continue
        if s.get("pause_reason") == SOURCE_PAUSED_BUDGET:
            paused_budget.append(sid)
            behind.append(sid)
            continue
        current.append(sid)

    if catching_up_ids:
        state = MARKET_CATCHING_UP
    elif behind or paused_budget:
        state = MARKET_BEHIND
    elif not sources:
        state = MARKET_UNKNOWN
    else:
        state = MARKET_CURRENT

    return {
        "kind": "MarketCoverageState",
        "state": state,
        "sources_current": current,
        "sources_behind": behind,
        "budget_paused_sources": paused_budget,
        "failed_sources": failed,
        "catch_up_sources": list(catching_up_ids),
        "oldest_successful_checkpoint": oldest,
        "newest_successful_checkpoint": newest,
        "research_backlog_may_remain": True,
        "note": "MARKET_COVERAGE_CURRENT ≠ ALL RESEARCH COMPLETE",
        "claim_fully_current_when_budget_paused": False,
    }


def evaluate_catchup_record(record: dict[str, Any]) -> dict[str, Any]:
    """Cheap CURRENT-date viability — no paid research."""
    status = str(record.get("status") or "").upper()
    if status in {"CANCELLED", "CANCELED"}:
        return {"actionable": False, "reason": CATCHUP_REJECT_CANCELLED, "paid_research": False}
    if status in {"CLOSED", "AWARDED"}:
        return {"actionable": False, "reason": CATCHUP_REJECT_CLOSED, "paid_research": False}
    if status == "EXPIRED":
        return {"actionable": False, "reason": CATCHUP_REJECT_EXPIRED, "paid_research": False}

    deadline = record.get("deadline") or record.get("response_deadline")
    if deadline:
        rw = compute_deadline_runway(response_deadline=str(deadline))
        viability = rw.get("deadline_viability")
        if viability == VIABILITY_TOO_LATE:
            return {
                "actionable": False,
                "reason": CATCHUP_REJECT_TOO_LATE,
                "paid_research": False,
                "deadline_viability": viability,
                "calendar_days_remaining": rw.get("calendar_days_remaining"),
            }
        record = {**record, "deadline_viability": viability}

    s1 = stage1_ultra_cheap(record)
    if not s1["survive"]:
        return {"actionable": False, "reason": CATCHUP_REJECT_NON_FIT, "paid_research": False, "stage1": s1}
    s2 = stage2_transaction_fit({**record, "classification": s1.get("classification")})
    if not s2["survive"]:
        return {"actionable": False, "reason": CATCHUP_REJECT_NON_FIT, "paid_research": False, "stage2": s2}

    return {
        "actionable": True,
        "reason": CATCHUP_PRESERVE_ACTIONABLE,
        "paid_research": False,  # inventory first — paid later by priority
        "inventory_state": DISCOVERED_PENDING_RESEARCH,
        "stage1": s1,
        "stage2": s2,
        "deadline_viability": record.get("deadline_viability"),
    }


def run_catchup_pass(
    *,
    source_id: str,
    last_successful_checkpoint: str,
    missed_records: list[dict[str, Any]],
    backlog: ResearchBacklog | None = None,
    inventory: SolicitationInventory | None = None,
    overlap_hours: int = 36,
) -> dict[str, Any]:
    """
    Recover missed interval cheaply → reevaluate today → kill dead → queue survivors.
    Does NOT deep-research. Does NOT pay.
    """
    backlog = backlog or ResearchBacklog()
    inventory = inventory or SolicitationInventory()
    source = {
        "source_id": source_id,
        "last_successful_checkpoint": last_successful_checkpoint,
        "overlap_hours": overlap_hours,
    }
    window = incremental_window(source)
    recovered = filter_records_for_incremental(missed_records, since=window.get("since"))

    rejected = []
    preserved = []
    for rec in recovered:
        rec = {**rec, "source_id": rec.get("source_id") or source_id}
        ident, change = inventory.upsert(rec)
        decision = evaluate_catchup_record(rec)
        row = {
            "identity_key": ident["identity_key"],
            "record": {k: rec.get(k) for k in rec if k not in {"raw_html", "body"}},
            "change_state": change,
            "catchup_decision": decision,
        }
        if not decision["actionable"]:
            rejected.append(row)
            continue
        preserved.append(row)
        backlog.enqueue(
            ident["identity_key"],
            priority=55.0,
            stage="STAGE_3",
            reason="catchup_actionable_survivor",
            record=rec,
        )

    return {
        "kind": "CatchUpPass",
        "source_id": source_id,
        "window": window,
        "missed_interval": {
            "from": last_successful_checkpoint,
            "to": _utc(),
        },
        "records_recovered": len(recovered),
        "rejected_before_paid_research": len(rejected),
        "actionable_survivors": len(preserved),
        "rejected_sample": [
            {"reason": r["catchup_decision"]["reason"], "deadline": r["record"].get("deadline"), "title": r["record"].get("title")}
            for r in rejected[:20]
        ],
        "preserved_sample": [
            {"deadline": r["record"].get("deadline"), "title": r["record"].get("title"), "reason": r["catchup_decision"]["reason"]}
            for r in preserved[:20]
        ],
        "backlog_queued": len(backlog),
        "deep_research_performed": False,
        "paid_actions": 0,
        "note": "Catch-up inventories cheaply before any paid research",
    }


def sept20_oct1_oct2_scenario(*, frozen_now: str = "2026-10-01T12:00:00+00:00") -> dict[str, Any]:
    """Mandatory business rule fixture — labeled test-only."""
    from datetime import datetime

    from application_clock import FrozenClock, use_clock

    clock = FrozenClock(datetime.fromisoformat(frozen_now))
    with use_clock(clock):
        missed = [
            {
                "title": "Widget Supplies Purchase",
                "solicitation_number": "GAP-2026-001",
                "agency": "Demo Agency",
                "source_id": "state_demo",
                "status": "OPEN",
                "deadline": "2026-10-02",
                "source_modified_at": "2026-09-25T10:00:00+00:00",
                "published_at": "2026-09-25T10:00:00+00:00",
                "test_only": True,
            }
        ]
        result = run_catchup_pass(
            source_id="state_demo",
            last_successful_checkpoint="2026-09-20T12:00:00+00:00",
            missed_records=missed,
        )
        direct = evaluate_catchup_record(missed[0])
        return {
            "kind": "Sept20Oct1Oct2Validation",
            "test_only": True,
            "checkpoint": "2026-09-20",
            "resume_at": "2026-10-01",
            "deadline": "2026-10-02",
            "record_recovered": True,
            "catchup_decision": direct,
            "paid_research_authorized": False,
            "supplier_research": False,
            "financing_research": False,
            "expected_reason": CATCHUP_REJECT_TOO_LATE,
            "pass": direct["reason"] == CATCHUP_REJECT_TOO_LATE and not direct["actionable"],
            "pass_result": result,
        }


def sept21_oct1_oct25_scenario(*, frozen_now: str = "2026-10-01T12:00:00+00:00") -> dict[str, Any]:
    from datetime import datetime

    from application_clock import FrozenClock, use_clock

    clock = FrozenClock(datetime.fromisoformat(frozen_now))
    with use_clock(clock):
        missed = [
            {
                "title": "Industrial Pump Equipment",
                "solicitation_number": "GAP-2026-002",
                "agency": "Demo DOT",
                "source_id": "state_demo",
                "status": "OPEN",
                "deadline": "2026-10-25",
                "source_modified_at": "2026-09-21T10:00:00+00:00",
                "test_only": True,
            }
        ]
        direct = evaluate_catchup_record(missed[0])
        result = run_catchup_pass(
            source_id="state_demo",
            last_successful_checkpoint="2026-09-20T12:00:00+00:00",
            missed_records=missed,
        )
        return {
            "kind": "Sept21Oct1Oct25Validation",
            "test_only": True,
            "catchup_decision": direct,
            "preserved": direct["actionable"],
            "expected_reason": CATCHUP_PRESERVE_ACTIONABLE,
            "pass": direct["actionable"] and result["actionable_survivors"] == 1,
            "backlog_queued": result["backlog_queued"],
        }
