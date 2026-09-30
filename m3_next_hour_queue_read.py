"""BUILD 28 — Next-Hour Operator Queue (evidence-based attention orchestration).

Answers: "What should I spend my next hour working on?"

NOT: win probability, opportunity score, AI ranking, profitability score.
IS: deterministic attention queue over existing actionable work.

Read/presentation only — never mutates opportunities, never creates actions,
never spends AI budget, never auto-creates research missions.
"""

from __future__ import annotations

import logging
from typing import Any

from application_clock import now_utc

log = logging.getLogger("govtracker.m3_next_hour_queue")

BUILD_TAG = "20260919-m3-next-hour-queue-1"

KIND_DECISION = "DECISION_NEEDED"
KIND_DEADLINE = "DEADLINE_REQUIRES_ATTENTION"
KIND_SUPPLIER = "SUPPLIER_ACTION"
KIND_RESEARCH = "RESEARCH_BLOCKER"
KIND_PURSUIT = "PURSUIT_GAP"
KIND_EXECUTION = "EXECUTION_ACTION"

# Operational urgency ranks — lower = work sooner. Not a score.
RANK_CRITICAL_DEADLINE = 0
RANK_BLOCKED = 1
RANK_DECISION = 2
RANK_SUPPLIER = 3
RANK_RESEARCH = 4
RANK_PURSUIT = 5
RANK_EXECUTION = 6
RANK_FOLLOW_UP = 7

URGENCY_LABEL = {
    RANK_CRITICAL_DEADLINE: "CRITICAL",
    RANK_BLOCKED: "BLOCKED",
    RANK_DECISION: "DECISION",
    RANK_SUPPLIER: "SUPPLIER",
    RANK_RESEARCH: "RESEARCH",
    RANK_PURSUIT: "PURSUIT_GAP",
    RANK_EXECUTION: "EXECUTION",
    RANK_FOLLOW_UP: "FOLLOW_UP",
}

NEXT_HOUR_LIMIT = 8
OTHER_ATTENTION_LIMIT = 20


def _utc() -> str:
    return now_utc().isoformat()


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _known(v: Any) -> bool:
    return v not in (None, "", "UNKNOWN")


def _row_deadline_context(row: dict[str, Any]) -> dict[str, Any]:
    """Reuse existing deadline fields — do not re-parse raw deadlines."""
    eval_ = _as_dict(row.get("deadline_evaluation"))
    days = row.get("deadline_runway_days")
    if days is None and eval_.get("calendar_days_remaining") is not None:
        days = eval_.get("calendar_days_remaining")
    viability = row.get("deadline_viability") or eval_.get("viability") or "UNKNOWN"
    actionable = row.get("deadline_actionable")
    if actionable is None:
        actionable = viability in {"RUSH", "GOOD", "PLENTY_OF_TIME"}
    badge = row.get("deadline_badge") or eval_.get("badge") or (
        "DEADLINE UNKNOWN" if not _known(days) and not _known(row.get("deadline")) else "UNKNOWN"
    )
    # Prefer raw Close / response deadline for operator date visibility
    raw = (
        row.get("deadline_raw")
        or row.get("response_deadline")
        or row.get("deadline")
        or eval_.get("operational_deadline")
    )
    display = raw or row.get("deadline_display") or "UNKNOWN"
    runway_label = row.get("deadline_display") or "UNKNOWN"
    bucket = row.get("queue_bucket") or eval_.get("queue_bucket") or "UNKNOWN"
    return {
        "deadline": display if _known(display) else "UNKNOWN",
        "deadline_runway_label": runway_label if _known(runway_label) else "UNKNOWN",
        "deadline_runway_days": days if isinstance(days, (int, float)) else None,
        "deadline_viability": viability if _known(viability) else "UNKNOWN",
        "deadline_actionable": bool(actionable) if actionable is not None else False,
        "deadline_badge": badge,
        "queue_bucket": bucket,
        "deadline_known": isinstance(days, (int, float)) or _known(raw) or _known(row.get("deadline")),
    }


def _urgency_from_deadline(dl: dict[str, Any], *, blocker: str = "generic") -> dict[str, Any]:
    try:
        from discovery.deadline_viability import action_urgency_for_unresolved

        return action_urgency_for_unresolved(
            viability=str(dl.get("deadline_viability") or "UNKNOWN"),
            runway_days=dl.get("deadline_runway_days")
            if isinstance(dl.get("deadline_runway_days"), (int, float))
            else None,
            blocker=blocker,
        )
    except Exception:
        days = dl.get("deadline_runway_days")
        if isinstance(days, (int, float)) and days <= 3:
            return {"urgency": "CRITICAL", "reason": "short_runway", "blocker": blocker}
        if not dl.get("deadline_known"):
            return {"urgency": "HIGH", "reason": "deadline_unknown", "blocker": blocker}
        return {"urgency": "NORMAL", "reason": "normal_window", "blocker": blocker}


def _deal_room_path(opportunity_id: str | None) -> str | None:
    if not _known(opportunity_id):
        return None
    return f"/api/m3/mobile/deal/{opportunity_id}"


def _queue_item(
    *,
    kind: str,
    action: str,
    opportunity_id: Any,
    opportunity_title: Any,
    reason: str,
    evidence: list[Any] | None = None,
    missing: list[str] | None = None,
    deadline_ctx: dict[str, Any] | None = None,
    lifecycle_state: Any = "UNKNOWN",
    pursuit_summary: Any = "UNKNOWN",
    blocking: bool = False,
    action_id: Any = None,
    research_mission_id: Any = None,
    urgency_rank: int,
    action_priority: Any = None,
) -> dict[str, Any]:
    dl = deadline_ctx or {}
    oid = opportunity_id if _known(opportunity_id) else "UNKNOWN"
    days = dl.get("deadline_runway_days")
    return {
        "kind": kind,
        "action": action if _known(action) else "UNKNOWN",
        "opportunity_id": oid,
        "opportunity": (str(opportunity_title)[:120] if _known(opportunity_title) else "UNKNOWN"),
        "why": reason if _known(reason) else "UNKNOWN",
        "evidence_context": evidence or ["UNKNOWN"],
        "missing": missing or [],
        "deadline": dl.get("deadline") or "UNKNOWN",
        "deadline_runway_days": days if days is not None else "UNKNOWN",
        "deadline_viability": dl.get("deadline_viability") or "UNKNOWN",
        "deadline_badge": dl.get("deadline_badge") or "UNKNOWN",
        "lifecycle_state": lifecycle_state if _known(lifecycle_state) else "UNKNOWN",
        "pursuit_readiness": pursuit_summary if _known(pursuit_summary) else "UNKNOWN",
        "blocking": bool(blocking),
        "action_id": action_id if _known(action_id) else None,
        "research_mission_id": research_mission_id if _known(research_mission_id) else None,
        "deal_room_path": _deal_room_path(str(oid) if oid != "UNKNOWN" else None),
        "open_deal_room": True,
        "urgency_rank": urgency_rank,
        "urgency_label": URGENCY_LABEL.get(urgency_rank, "FOLLOW_UP"),
        "action_priority": action_priority if isinstance(action_priority, (int, float)) else None,
        "not_a_score": True,
        "not_a_prediction": True,
        "source_of_truth": "existing_actions_and_opportunity_evidence",
    }


def _pursuit_summary(assessment: dict[str, Any] | None) -> str:
    dims = _as_dict((assessment or {}).get("dimensions"))
    parts = []
    for key, short in (
        ("product_fit", "Product"),
        ("supply_confidence", "Supply"),
        ("economics_visibility", "Economics"),
        ("competition_context", "Competition"),
        ("compliance_execution_fit", "Execution"),
    ):
        st = (_as_dict(dims.get(key)).get("state") or "UNKNOWN")
        if st != "KNOWN":
            parts.append(f"{short} {st}")
    return "; ".join(parts[:3]) if parts else "Dimensions evidenced"


def _lifecycle_state(row: dict[str, Any]) -> str:
    try:
        from m3_opportunity_operating_read import derive_operator_lifecycle

        return str(derive_operator_lifecycle(row).get("current_state") or "UNKNOWN")
    except Exception:
        return str(row.get("lifecycle") or "UNKNOWN")


def _collect_from_actions(
    rows_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    try:
        from m3_action_orchestration_read import (
            AT_COMMUNICATION,
            AT_DECISION_REVIEW,
            AT_EXECUTION,
            AT_FOLLOW_UP,
            AT_RESEARCH,
            AT_VALIDATION,
            ST_BLOCKED,
            ST_CANCELLED,
            ST_COMPLETED,
            list_actions,
        )
    except Exception:
        return items

    done = {ST_COMPLETED, ST_CANCELLED}
    for a in list_actions(limit=120):
        if not isinstance(a, dict):
            continue
        if a.get("status") in done:
            continue
        oid = a.get("related_opportunity")
        row = rows_by_id.get(str(oid) or "") or {}
        dl = _row_deadline_context(row) if row else {
            "deadline": "UNKNOWN",
            "deadline_runway_days": None,
            "deadline_viability": "UNKNOWN",
            "deadline_badge": "DEADLINE UNKNOWN",
            "deadline_known": False,
        }
        title = row.get("title") or a.get("title") or oid
        atype = str(a.get("action_type") or "")
        status = a.get("status")
        why = a.get("why_exists") or a.get("description") or "Action requires attention"
        evidence = [
            f"action_type={atype}",
            f"status={status}",
            f"trigger={a.get('trigger_source') or 'UNKNOWN'}",
        ]
        missing = list(a.get("required_inputs") or [])[:6]
        blocking = status == ST_BLOCKED
        lc = _lifecycle_state(row) if row else "UNKNOWN"

        if blocking:
            kind, rank = KIND_RESEARCH if atype == AT_RESEARCH else KIND_DEADLINE, RANK_BLOCKED
            if atype == AT_COMMUNICATION:
                kind = KIND_SUPPLIER
            elif atype == AT_DECISION_REVIEW:
                kind = KIND_DECISION
            elif atype == AT_EXECUTION:
                kind = KIND_EXECUTION
            items.append(
                _queue_item(
                    kind=kind,
                    action=str(a.get("title") or "Resolve blocked action"),
                    opportunity_id=oid,
                    opportunity_title=title,
                    reason=f"Blocked: {why}",
                    evidence=evidence,
                    missing=missing or ["Resolve blocker before progress"],
                    deadline_ctx=dl,
                    lifecycle_state=lc,
                    blocking=True,
                    action_id=a.get("action_id"),
                    urgency_rank=rank,
                    action_priority=(a.get("priority") if isinstance(a.get("priority"), (int, float)) else None),
                )
            )
            continue

        urg = _urgency_from_deadline(
            dl,
            blocker=(
                "supplier_quote_missing"
                if atype == AT_COMMUNICATION
                else ("document_unresolved" if atype == AT_VALIDATION else "generic")
            ),
        )
        if urg.get("urgency") == "CRITICAL" and dl.get("deadline_known"):
            items.append(
                _queue_item(
                    kind=KIND_DEADLINE,
                    action=str(a.get("title") or "Act before deadline"),
                    opportunity_id=oid,
                    opportunity_title=title,
                    reason=f"Deadline urgency ({urg.get('reason')}): {why}",
                    evidence=evidence + [f"deadline_urgency={urg.get('urgency')}"],
                    missing=missing,
                    deadline_ctx=dl,
                    lifecycle_state=lc,
                    blocking=False,
                    action_id=a.get("action_id"),
                    urgency_rank=RANK_CRITICAL_DEADLINE,
                )
            )

        if atype == AT_DECISION_REVIEW:
            items.append(
                _queue_item(
                    kind=KIND_DECISION,
                    action=str(a.get("title") or "Review decision"),
                    opportunity_id=oid,
                    opportunity_title=title,
                    reason=why,
                    evidence=evidence,
                    missing=missing,
                    deadline_ctx=dl,
                    lifecycle_state=lc,
                    action_id=a.get("action_id"),
                    urgency_rank=RANK_DECISION,
                )
            )
        elif atype == AT_COMMUNICATION:
            items.append(
                _queue_item(
                    kind=KIND_SUPPLIER,
                    action=str(a.get("title") or "Supplier action"),
                    opportunity_id=oid,
                    opportunity_title=title,
                    reason=why,
                    evidence=evidence,
                    missing=missing,
                    deadline_ctx=dl,
                    lifecycle_state=lc,
                    action_id=a.get("action_id"),
                    urgency_rank=RANK_SUPPLIER,
                )
            )
        elif atype in {AT_RESEARCH, AT_VALIDATION}:
            items.append(
                _queue_item(
                    kind=KIND_RESEARCH,
                    action=str(a.get("title") or "Research action"),
                    opportunity_id=oid,
                    opportunity_title=title,
                    reason=why,
                    evidence=evidence,
                    missing=missing,
                    deadline_ctx=dl,
                    lifecycle_state=lc,
                    action_id=a.get("action_id"),
                    urgency_rank=RANK_RESEARCH,
                )
            )
        elif atype == AT_EXECUTION:
            items.append(
                _queue_item(
                    kind=KIND_EXECUTION,
                    action=str(a.get("title") or "Execution action"),
                    opportunity_id=oid,
                    opportunity_title=title,
                    reason=why,
                    evidence=evidence,
                    missing=missing,
                    deadline_ctx=dl,
                    lifecycle_state=lc,
                    action_id=a.get("action_id"),
                    urgency_rank=RANK_EXECUTION,
                )
            )
        elif atype == AT_FOLLOW_UP:
            items.append(
                _queue_item(
                    kind=KIND_RESEARCH,
                    action=str(a.get("title") or "Follow up"),
                    opportunity_id=oid,
                    opportunity_title=title,
                    reason=why,
                    evidence=evidence,
                    missing=missing,
                    deadline_ctx=dl,
                    lifecycle_state=lc,
                    action_id=a.get("action_id"),
                    urgency_rank=RANK_FOLLOW_UP,
                )
            )
    return items


def _collect_deadline_attention(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Surface hard deadline pressure when runway evidence says so — no fabricated work."""
    items: list[dict[str, Any]] = []
    unknown_deadline_count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        dl = _row_deadline_context(row)
        if not dl.get("deadline_known"):
            if unknown_deadline_count >= 3:
                continue
            # Needs deadline review — only if opportunity is otherwise active
            if row.get("canonical_id") and (row.get("cheap_screen_survive") or row.get("title")):
                unknown_deadline_count += 1
                items.append(
                    _queue_item(
                        kind=KIND_DEADLINE,
                        action="Resolve deadline / runway",
                        opportunity_id=row.get("canonical_id"),
                        opportunity_title=row.get("title"),
                        reason="Deadline information is UNKNOWN — cannot prioritize without runway",
                        evidence=["deadline_viability=UNKNOWN", f"queue_bucket={dl.get('queue_bucket')}"],
                        missing=["Confirmed response deadline"],
                        deadline_ctx=dl,
                        lifecycle_state=_lifecycle_state(row),
                        urgency_rank=RANK_CRITICAL_DEADLINE
                        if dl.get("queue_bucket") == "NEEDS_DEADLINE_REVIEW"
                        else RANK_FOLLOW_UP,
                    )
                )
            continue
        days = dl.get("deadline_runway_days")
        urg = _urgency_from_deadline(dl, blocker="generic")
        if urg.get("urgency") != "CRITICAL":
            continue
        # Only add if there is something to do — tie to lifecycle next action text
        lc = _lifecycle_state(row)
        try:
            from m3_opportunity_operating_read import derive_operator_lifecycle

            next_a = (derive_operator_lifecycle(row).get("next_human_action") or {}).get("what")
        except Exception:
            next_a = "Review opportunity before deadline"
        items.append(
            _queue_item(
                kind=KIND_DEADLINE,
                action=str(next_a or "Review opportunity before deadline"),
                opportunity_id=row.get("canonical_id"),
                opportunity_title=row.get("title"),
                reason=f"Hard deadline pressure ({urg.get('reason')}); runway={days} day(s)",
                evidence=[
                    f"deadline_viability={dl.get('deadline_viability')}",
                    f"deadline_runway_days={days}",
                    f"deadline_badge={dl.get('deadline_badge')}",
                ],
                missing=[],
                deadline_ctx=dl,
                lifecycle_state=lc,
                urgency_rank=RANK_CRITICAL_DEADLINE,
            )
        )
    return items


def _collect_decision_ready(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    try:
        from m3_opportunity_operating_read import OP_DECISION_READY, derive_operator_lifecycle
    except Exception:
        return items

    for row in rows:
        if not isinstance(row, dict):
            continue
        lc = derive_operator_lifecycle(row)
        if lc.get("current_state") != OP_DECISION_READY:
            continue
        dl = _row_deadline_context(row)
        next_a = (lc.get("next_human_action") or {}).get("what") or "Make pursue / pass decision"
        why = (lc.get("next_human_action") or {}).get("why") or (
            "Required research appears complete; human decision remains"
        )
        items.append(
            _queue_item(
                kind=KIND_DECISION,
                action=str(next_a),
                opportunity_id=row.get("canonical_id"),
                opportunity_title=row.get("title"),
                reason=why,
                evidence=[f"lifecycle={OP_DECISION_READY}", *(lc.get("known_information") or [])[:3]],
                missing=list(lc.get("missing_information") or [])[:5],
                deadline_ctx=dl,
                lifecycle_state=OP_DECISION_READY,
                urgency_rank=RANK_DECISION,
            )
        )
    return items


def _collect_pursuit_gaps(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read-only pursuit gaps — does not call ensure_actions / persist."""
    items: list[dict[str, Any]] = []
    try:
        from m3_pursuit_readiness_read import ST_UNKNOWN, build_pursuit_readiness_assessment
    except Exception:
        return items

    for row in rows:
        if not isinstance(row, dict) or not row.get("canonical_id"):
            continue
        assessment = build_pursuit_readiness_assessment(row, ensure_actions=False)
        dims = _as_dict(assessment.get("dimensions"))
        dl = _row_deadline_context(row)
        lc = assessment.get("operator_lifecycle") or _lifecycle_state(row)
        pursuit = _pursuit_summary(assessment)

        # Decision-ready from pursuit boards
        overall = _as_dict(assessment.get("overall"))
        blockers = list(overall.get("biggest_blockers") or [])
        next_a = _as_dict(overall.get("recommended_next_human_action"))

        # Critical UNKNOWN dimensions become pursuit gaps / research blockers
        for key, kind, rank in (
            ("product_fit", KIND_PURSUIT, RANK_PURSUIT),
            ("supply_confidence", KIND_SUPPLIER, RANK_SUPPLIER),
            ("economics_visibility", KIND_PURSUIT, RANK_PURSUIT),
            ("compliance_execution_fit", KIND_RESEARCH, RANK_RESEARCH),
        ):
            d = _as_dict(dims.get(key))
            if d.get("state") != ST_UNKNOWN:
                continue
            na = _as_dict(d.get("next_action"))
            missing = list(d.get("unknowns") or [])[:5]
            items.append(
                _queue_item(
                    kind=kind if key != "supply_confidence" else KIND_SUPPLIER,
                    action=str(na.get("what") or next_a.get("what") or "Close pursuit gap"),
                    opportunity_id=row.get("canonical_id"),
                    opportunity_title=row.get("title"),
                    reason=str(d.get("why") or na.get("why") or "Critical pursuit dimension UNKNOWN"),
                    evidence=[f"pursuit_dimension={key}", f"state={d.get('state')}"],
                    missing=missing,
                    deadline_ctx=dl,
                    lifecycle_state=lc,
                    pursuit_summary=pursuit,
                    urgency_rank=rank if key != "supply_confidence" else RANK_SUPPLIER,
                )
            )

        # PARTIAL supply with quote missing → supplier attention
        supply = _as_dict(dims.get("supply_confidence"))
        if supply.get("state") == "PARTIAL":
            unk = [str(u) for u in (supply.get("unknowns") or [])]
            if any("quote" in u.lower() or "pricing" in u.lower() or "commercial" in u.lower() for u in unk):
                na = _as_dict(supply.get("next_action"))
                items.append(
                    _queue_item(
                        kind=KIND_SUPPLIER,
                        action=str(na.get("what") or "Request supplier pricing"),
                        opportunity_id=row.get("canonical_id"),
                        opportunity_title=row.get("title"),
                        reason=str(supply.get("why") or "Commercial terms incomplete"),
                        evidence=["pursuit_dimension=supply_confidence", "state=PARTIAL"],
                        missing=unk[:5],
                        deadline_ctx=dl,
                        lifecycle_state=lc,
                        pursuit_summary=pursuit,
                        urgency_rank=RANK_SUPPLIER,
                    )
                )

        if blockers and not any(
            i.get("opportunity_id") == row.get("canonical_id") and i.get("kind") == KIND_DECISION
            for i in items
        ):
            # Surface overall next only when there are blockers and a concrete next action
            if next_a.get("what") and str(lc) in {"DECISION_READY", "COMMERCIAL_VALIDATION"}:
                items.append(
                    _queue_item(
                        kind=KIND_DECISION if str(lc) == "DECISION_READY" else KIND_PURSUIT,
                        action=str(next_a.get("what")),
                        opportunity_id=row.get("canonical_id"),
                        opportunity_title=row.get("title"),
                        reason=str(next_a.get("why") or overall.get("summary") or "Pursuit blockers remain"),
                        evidence=[f"blocker={b}" for b in blockers[:3]],
                        missing=blockers[:5],
                        deadline_ctx=dl,
                        lifecycle_state=lc,
                        pursuit_summary=pursuit,
                        urgency_rank=RANK_DECISION if str(lc) == "DECISION_READY" else RANK_PURSUIT,
                    )
                )
    return items


def _collect_research_missions(rows_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    try:
        from m3_research_execution_read import list_research_missions
    except Exception:
        return items

    for m in (list_research_missions(limit=40).get("missions") or []):
        if not isinstance(m, dict):
            continue
        if str(m.get("status") or "").upper() in {"COMPLETE", "CANCELLED"}:
            continue
        oid = m.get("related_opportunity") or m.get("opportunity_id")
        row = rows_by_id.get(str(oid) or "") or {}
        dl = _row_deadline_context(row) if row else {
            "deadline": "UNKNOWN",
            "deadline_runway_days": None,
            "deadline_viability": "UNKNOWN",
            "deadline_badge": "DEADLINE UNKNOWN",
            "deadline_known": False,
        }
        missing = list(m.get("required_evidence") or [])[:5]
        items.append(
            _queue_item(
                kind=KIND_RESEARCH,
                action=str(m.get("question") or "Continue research mission"),
                opportunity_id=oid,
                opportunity_title=row.get("title") or m.get("question"),
                reason=str(m.get("reason") or "Active research mission requires attention"),
                evidence=[f"mission_status={m.get('status')}", f"mission_id={m.get('mission_id')}"],
                missing=missing,
                deadline_ctx=dl,
                lifecycle_state=_lifecycle_state(row) if row else "UNKNOWN",
                research_mission_id=m.get("mission_id"),
                action_id=(m.get("linked_action_ids") or [None])[0],
                urgency_rank=RANK_RESEARCH,
            )
        )
    return items


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for it in items:
        aid = it.get("action_id")
        mid = it.get("research_mission_id")
        if aid:
            key = f"act:{aid}"
        elif mid:
            key = f"mission:{mid}"
        else:
            key = f"{it.get('kind')}|{it.get('opportunity_id')}|{it.get('action')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _sort_key(item: dict[str, Any]) -> tuple:
    days = item.get("deadline_runway_days")
    if isinstance(days, (int, float)):
        day_key = (0, float(days))
    else:
        day_key = (1, float("inf"))
    pri = item.get("action_priority")
    pri_key = (0, float(pri)) if isinstance(pri, (int, float)) else (1, float("inf"))
    return (
        int(item.get("urgency_rank") if item.get("urgency_rank") is not None else RANK_FOLLOW_UP),
        day_key,
        pri_key,
        str(item.get("opportunity_id") or ""),
        str(item.get("action") or ""),
    )


def build_next_hour_queue(
    rows: list[dict[str, Any]] | None = None,
    *,
    limit_next: int = NEXT_HOUR_LIMIT,
    limit_other: int = OTHER_ATTENTION_LIMIT,
) -> dict[str, Any]:
    """Deterministic read-only next-hour queue. No mutations. No AI spend."""
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    rows_by_id = {
        str(r.get("canonical_id")): r for r in rows if _known(r.get("canonical_id"))
    }

    raw: list[dict[str, Any]] = []
    raw.extend(_collect_from_actions(rows_by_id))
    raw.extend(_collect_deadline_attention(rows[:40])[:12])
    raw.extend(_collect_decision_ready(rows[:40]))
    raw.extend(_collect_pursuit_gaps(rows[:25])[:40])
    raw.extend(_collect_research_missions(rows_by_id))

    items = _dedupe(raw)
    items.sort(key=_sort_key)

    next_hour = items[: max(0, limit_next)]
    other = items[len(next_hour) : len(next_hour) + max(0, limit_other)]

    empty_urgent = not next_hour
    empty_all = not items

    if empty_all:
        empty_message = "No actionable work right now."
        guidance = "When new opportunities, research missions, or actions appear, they will show here."
    elif empty_urgent:
        empty_message = "No urgent actions right now."
        guidance = "Other attention items are listed below — work the next available actionable item."
    else:
        empty_message = None
        guidance = "Work the first Next Hour item. Open Deal Room for context."

    result = {
        "kind": "M3NextHourQueue",
        "build": BUILD_TAG,
        "question": "What should I spend my next hour working on?",
        "next_hour": next_hour,
        "other_attention": other,
        "counts": {
            "next_hour": len(next_hour),
            "other_attention": len(other),
            "total_actionable": len(items),
        },
        "empty_urgent": empty_urgent,
        "empty_all": empty_all,
        "empty_message": empty_message,
        "guidance": guidance,
        "principles": {
            "attention_not_prediction": True,
            "no_win_probability": True,
            "no_opportunity_score": True,
            "no_profitability_score": True,
            "no_ai_ranking": True,
            "no_duplicate_task_store": True,
            "read_only": True,
            "idempotent": True,
            "does_not_mutate_state": True,
            "does_not_spend_ai": True,
            "does_not_auto_create_actions": True,
            "does_not_auto_create_missions": True,
            "reuses_deadline_viability": True,
            "reuses_pursuit_readiness": True,
            "reuses_action_orchestration": True,
            "reuses_opportunity_operating": True,
            "humans_decide": True,
        },
        "ordering": [
            "1. Hard deadline / critical urgency",
            "2. Blocking action",
            "3. Decision needed",
            "4. Supplier action",
            "5. Research blocker",
            "6. Pursuit gap",
            "7. Execution / follow-up",
            "Tie-break: runway days ASC → action priority → opportunity_id",
        ],
        "generated_at": _utc(),
        "read_only": True,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
        "paid": 0,
    }
    try:
        from m3_operator_loop_read import enrich_next_hour_with_operator_paths

        result = enrich_next_hour_with_operator_paths(result)
    except Exception:
        pass
    return result


def attach_next_hour_to_human_os_home(home: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    out = dict(home) if isinstance(home, dict) else {}
    try:
        queue = build_next_hour_queue(rows)
        out["next_hour_queue"] = queue
        out["next_hour"] = queue.get("next_hour")
        out["other_attention"] = queue.get("other_attention")
        out["what_should_i_work_on"] = (
            (queue.get("next_hour") or [{}])[0].get("action")
            if queue.get("next_hour")
            else queue.get("empty_message")
        )
    except Exception as e:
        log.warning("next-hour attach to home failed: %s", e)
        out["next_hour_queue"] = {
            "kind": "M3NextHourQueue",
            "build": BUILD_TAG,
            "error": "next_hour_unavailable",
            "read_only": True,
        }
    return out


def enrich_command_center_next_hour(
    sections: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    period: str,
) -> dict[str, Any]:
    out = dict(sections or {})
    queue = build_next_hour_queue(rows)
    out["next_hour_queue"] = queue
    if period == "morning":
        out["next_hour"] = queue.get("next_hour")
        out["other_attention"] = queue.get("other_attention")
    else:
        out["remaining_attention"] = queue.get("next_hour")
        out["deferred_attention"] = queue.get("other_attention")
    return out
