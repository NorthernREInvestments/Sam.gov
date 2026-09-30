"""BUILD 29 — Production Operator Loop (action completion + evidence closure).

Closes: ATTENTION → ACTION → EVIDENCE/RESULT → UPDATED KNOWLEDGE → NEXT ACTION

Extends Action Orchestration + existing evidence stores.
Does NOT create a second task system, invent evidence, auto-advance readiness,
auto-contact suppliers, spend AI budget, or predict wins.
"""

from __future__ import annotations

import logging
from typing import Any

from application_clock import now_utc

log = logging.getLogger("govtracker.m3_operator_loop")

BUILD_TAG = "20260919-m3-operator-loop-1"

INTENT_DONE = "DONE"
INTENT_BLOCKED = "BLOCKED"
INTENT_NEEDS_EVIDENCE = "NEEDS_EVIDENCE"


def _utc() -> str:
    return now_utc().isoformat()


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _known(v: Any) -> bool:
    return v not in (None, "", "UNKNOWN")


def evidence_envelope(
    *,
    claim: str,
    evidence: Any = "UNKNOWN",
    source: Any = "UNKNOWN",
    date: Any = None,
    context: Any = "UNKNOWN",
) -> dict[str, Any]:
    ev_ok = _known(evidence) or isinstance(evidence, (list, dict))
    return {
        "claim": claim if _known(claim) else "UNKNOWN",
        "evidence": evidence if ev_ok else "UNKNOWN",
        "source": source if _known(source) else "UNKNOWN",
        "date": date or _utc(),
        "context": context if _known(context) else "UNKNOWN",
        "unsupported_conclusion": not ev_ok,
        "fabricated": False,
    }


def build_action_loop_view(action_id: str) -> dict[str, Any]:
    from m3_action_orchestration_read import get_action, list_action_history

    action = get_action(action_id)
    if not action:
        return {
            "kind": "M3OperatorActionLoopView",
            "build": BUILD_TAG,
            "error": "action_not_found",
            "action_id": action_id,
        }
    links = _as_dict(action.get("evidence_links"))
    packets = list(links.get("evidence_packets") or [])
    blocker = _as_dict(action.get("operator_block"))
    needs = bool(action.get("operator_needs_evidence"))
    required = list(action.get("evidence_requirements") or action.get("completion_criteria") or [])
    attached = packets + list(links.get("master_records") or [])
    history = list(action.get("history") or []) or list_action_history(action_id=action_id, limit=20)

    next_step = "Continue work"
    if action.get("status") == "COMPLETED":
        next_step = "Reassess opportunity — evidence determines readiness"
    elif needs or action.get("status") == "WAITING":
        next_step = "Record required evidence"
    elif blocker.get("active") or action.get("status") == "BLOCKED":
        next_step = "Resolve blocker or document why blocked"
    elif not attached and required:
        next_step = "Attach evidence before marking DONE"

    return {
        "kind": "M3OperatorActionLoopView",
        "build": BUILD_TAG,
        "action_id": action_id,
        "what_needs_done": action.get("title") or "UNKNOWN",
        "current_state": action.get("status") or "UNKNOWN",
        "why": action.get("why_exists") or action.get("description") or "UNKNOWN",
        "opportunity_id": action.get("related_opportunity") or "UNKNOWN",
        "evidence_requirement": required or ["Documented finding or explicit UNKNOWN"],
        "evidence_already_attached": attached or [],
        "blocker": blocker if blocker.get("active") else None,
        "operator_needs_evidence": needs,
        "result": action.get("result") or "UNKNOWN",
        "history": history[:20],
        "next_action": next_step,
        "operator_paths": {
            "done": "/api/m3/operator/action/done",
            "blocked": "/api/m3/operator/action/blocked",
            "needs_evidence": "/api/m3/operator/action/needs-evidence",
            "record_evidence": "/api/m3/operator/action/evidence",
        },
        "deal_room_path": (
            f"/api/m3/mobile/deal/{action.get('related_opportunity')}"
            if _known(action.get("related_opportunity"))
            else None
        ),
        "principles": {
            "evidence_determines_knowledge": True,
            "done_does_not_auto_advance_readiness": True,
            "no_fabricated_evidence": True,
            "no_ai_spend": True,
            "no_automatic_outreach": True,
            "humans_decide": True,
        },
        "generated_at": _utc(),
        "read_only": True,
    }


def operator_mark_done(
    payload: dict[str, Any] | None,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """DONE — requires evidence; does not fabricate; does not auto-advance readiness."""
    from m3_action_orchestration_read import (
        ST_COMPLETED,
        append_action_history,
        complete_action,
        get_action,
    )

    payload = payload or {}
    action_id = str(payload.get("action_id") or "").strip()
    if not action_id:
        raise ValueError("action_id required")
    action = get_action(action_id)
    if not action:
        raise ValueError("action not found")

    evidence = payload.get("evidence")
    if not _known(evidence):
        # Do not falsely complete — steer to needs-evidence
        return {
            "kind": "M3OperatorLoopResult",
            "intent": INTENT_DONE,
            "accepted": False,
            "requires": INTENT_NEEDS_EVIDENCE,
            "reason": "Evidence required to mark DONE — completion without evidence is not allowed",
            "action_id": action_id,
            "action": action,
            "loop": build_action_loop_view(action_id),
            "build": BUILD_TAG,
            "OpenAI": 0,
            "paid": 0,
        }

    # Clear operator wait/block flags before complete
    prev = action.get("status")
    action["operator_needs_evidence"] = False
    if isinstance(action.get("operator_block"), dict):
        action["operator_block"] = {**action["operator_block"], "active": False, "cleared_at": _utc()}
    from m3_action_orchestration_read import _upsert_action

    _upsert_action(action, persist=persist)

    try:
        completion = complete_action(
            action_id=action_id,
            result=payload.get("result") or payload.get("note") or "Operator marked DONE",
            evidence=evidence,
            criteria_met=payload.get("criteria_met"),
            persist=persist,
        )
    except ValueError as e:
        # Dependency still blocked or criteria missing
        return {
            "kind": "M3OperatorLoopResult",
            "intent": INTENT_DONE,
            "accepted": False,
            "error": str(e),
            "action_id": action_id,
            "loop": build_action_loop_view(action_id),
            "build": BUILD_TAG,
        }

    append_action_history(
        action_id=action_id,
        previous_status=prev,
        new_status=ST_COMPLETED,
        actor=str(payload.get("actor") or "human"),
        note=payload.get("note") or payload.get("result") or "DONE",
        evidence=evidence,
        operator_intent=INTENT_DONE,
        persist=persist,
    )
    updated = get_action(action_id) or completion.get("action")
    return {
        "kind": "M3OperatorLoopResult",
        "intent": INTENT_DONE,
        "accepted": True,
        "action_id": action_id,
        "action": updated,
        "completion": completion,
        "loop": build_action_loop_view(action_id),
        "readiness_note": "Pursuit Readiness is not auto-advanced; evidence determines knowledge on next read",
        "build": BUILD_TAG,
        "OpenAI": 0,
        "paid": 0,
        "automatic_outreach": False,
    }


def operator_mark_blocked(
    payload: dict[str, Any] | None,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    from m3_action_orchestration_read import (
        ST_BLOCKED,
        append_action_history,
        get_action,
        _upsert_action,
    )

    payload = payload or {}
    action_id = str(payload.get("action_id") or "").strip()
    reason = payload.get("blocker") or payload.get("reason")
    if not action_id:
        raise ValueError("action_id required")
    if not _known(reason):
        raise ValueError("blocker/reason required")
    action = get_action(action_id)
    if not action:
        raise ValueError("action not found")

    prev = action.get("status")
    note = payload.get("note") or "UNKNOWN"
    evidence = payload.get("evidence") if payload.get("evidence") not in (None, "") else "UNKNOWN"
    block = {
        "active": True,
        "reason": reason,
        "note": note,
        "evidence": evidence,
        "opportunity_id": action.get("related_opportunity") or payload.get("opportunity_id") or "UNKNOWN",
        "action_id": action_id,
        "recorded_at": _utc(),
        "actor": str(payload.get("actor") or "human"),
    }
    action["operator_block"] = block
    action["operator_needs_evidence"] = False
    action["status"] = ST_BLOCKED
    blocked = list(action.get("blocked_by") or [])
    tag = f"operator:{reason}"
    if tag not in blocked:
        blocked.append(tag)
    action["blocked_by"] = blocked
    action["timestamps"] = dict(action.get("timestamps") or {})
    action["timestamps"]["updated_at"] = _utc()
    _upsert_action(action, persist=persist)
    append_action_history(
        action_id=action_id,
        previous_status=prev,
        new_status=ST_BLOCKED,
        actor=str(payload.get("actor") or "human"),
        note=f"BLOCKED: {reason}",
        evidence=evidence,
        operator_intent=INTENT_BLOCKED,
        persist=persist,
    )
    return {
        "kind": "M3OperatorLoopResult",
        "intent": INTENT_BLOCKED,
        "accepted": True,
        "action_id": action_id,
        "action": get_action(action_id),
        "blocker": block,
        "loop": build_action_loop_view(action_id),
        "visible_in": ["next_hour", "deal_room", "human_os", "pursuit_readiness_context"],
        "build": BUILD_TAG,
        "OpenAI": 0,
        "paid": 0,
        "automatic_outreach": False,
    }


def operator_mark_needs_evidence(
    payload: dict[str, Any] | None,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    from m3_action_orchestration_read import (
        ST_WAITING,
        append_action_history,
        get_action,
        _upsert_action,
    )

    payload = payload or {}
    action_id = str(payload.get("action_id") or "").strip()
    if not action_id:
        raise ValueError("action_id required")
    action = get_action(action_id)
    if not action:
        raise ValueError("action not found")

    prev = action.get("status")
    required = payload.get("required_evidence") or payload.get("evidence_requirements")
    if isinstance(required, str):
        required = [required]
    if not isinstance(required, list) or not required:
        required = list(action.get("evidence_requirements") or []) or [
            "Documented finding with source and date"
        ]
    note = payload.get("note") or payload.get("why") or "Evidence required before DONE"
    action["operator_needs_evidence"] = True
    action["evidence_requirements"] = required
    action["status"] = ST_WAITING
    # Clear active operator block if pivoting to evidence need
    if isinstance(action.get("operator_block"), dict) and action["operator_block"].get("active"):
        action["operator_block"] = {**action["operator_block"], "active": False, "cleared_at": _utc()}
    action["timestamps"] = dict(action.get("timestamps") or {})
    action["timestamps"]["updated_at"] = _utc()
    action["result"] = action.get("result") if _known(action.get("result")) else "WAITING_ON_EVIDENCE"
    _upsert_action(action, persist=persist)
    append_action_history(
        action_id=action_id,
        previous_status=prev,
        new_status=ST_WAITING,
        actor=str(payload.get("actor") or "human"),
        note=note,
        evidence="UNKNOWN",
        operator_intent=INTENT_NEEDS_EVIDENCE,
        persist=persist,
    )
    return {
        "kind": "M3OperatorLoopResult",
        "intent": INTENT_NEEDS_EVIDENCE,
        "accepted": True,
        "action_id": action_id,
        "action": get_action(action_id),
        "required_evidence": required,
        "loop": build_action_loop_view(action_id),
        "build": BUILD_TAG,
        "OpenAI": 0,
        "paid": 0,
        "automatic_outreach": False,
    }


def operator_record_evidence(
    payload: dict[str, Any] | None,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """Record evidence via existing stores and link to the action — no new evidence DB."""
    from m3_action_orchestration_read import (
        append_action_history,
        get_action,
        link_action_evidence,
    )

    payload = payload or {}
    action_id = str(payload.get("action_id") or "").strip()
    if not action_id:
        raise ValueError("action_id required")
    action = get_action(action_id)
    if not action:
        raise ValueError("action not found")

    claim = payload.get("claim") or payload.get("context") or action.get("title") or "Operator evidence"
    source = payload.get("source")
    evidence_body = payload.get("evidence") or payload.get("notes") or payload.get("value")
    if not _known(source):
        raise ValueError("source required — evidence without source is not accepted")
    if not _known(evidence_body):
        raise ValueError("evidence required — do not invent facts")

    oid = (
        payload.get("opportunity_id")
        or action.get("related_opportunity")
        or "UNKNOWN"
    )
    envelope = evidence_envelope(
        claim=str(claim),
        evidence=evidence_body,
        source=source,
        date=payload.get("date"),
        context=str(oid),
    )

    created_refs: list[str] = []
    commercial = None
    # Prefer supply commercial evidence when pricing/quote-like
    etype = str(payload.get("evidence_type") or "Unknown")
    text = f"{claim} {evidence_body} {etype}".lower()
    if any(k in text for k in ("quote", "price", "pricing", "cost", "commercial")):
        try:
            from m3_supply_intelligence_read import EVIDENCE_TYPES, create_commercial_evidence

            mapped = "Supplier Quote"
            if "catalog" in text:
                mapped = "Catalog"
            elif "manufacturer" in text:
                mapped = "Manufacturer Pricing"
            elif "distributor" in text:
                mapped = "Distributor Listing"
            if etype in EVIDENCE_TYPES:
                mapped = etype
            commercial = create_commercial_evidence(
                {
                    "evidence_type": mapped,
                    "source": source,
                    "product": payload.get("product") or action.get("title") or "UNKNOWN",
                    "supplier": payload.get("supplier") or "UNKNOWN",
                    "price": payload.get("price") if payload.get("price") not in (None, "") else evidence_body,
                    "quantity": payload.get("quantity"),
                    "date": envelope["date"],
                    "lead_time": payload.get("lead_time"),
                    "terms": payload.get("terms"),
                    "notes": payload.get("notes") or str(evidence_body)[:500],
                    "claim": claim,
                    "opportunity_id": oid,
                },
                persist=persist,
            )
            created_refs.append(commercial.get("evidence_id"))
        except Exception as e:
            log.debug("commercial evidence create skipped: %s", e)

    # Always create a lightweight packet id for lineage via action evidence_links
    packet_id = payload.get("evidence_packet_id") or f"optev:{action_id}:{hash(str(envelope)) & 0xFFFFFFFF:08x}"
    if packet_id not in created_refs:
        created_refs.append(packet_id)

    linked = link_action_evidence(
        action_id=action_id,
        evidence_packets=created_refs,
        persist=persist,
    )
    # Store envelope on action for Deal Room display (not a parallel store)
    linked = get_action(action_id) or linked
    envelopes = list(linked.get("operator_evidence") or [])
    # Dedupe by claim+source+evidence
    sig = f"{envelope['claim']}|{envelope['source']}|{envelope['evidence']}"
    existing_sigs = {
        f"{e.get('claim')}|{e.get('source')}|{e.get('evidence')}"
        for e in envelopes
        if isinstance(e, dict)
    }
    duplicate = sig in existing_sigs
    if not duplicate:
        envelopes.append({**envelope, "evidence_id": created_refs[0], "packet_id": packet_id})
        linked["operator_evidence"] = envelopes[-30:]
        from m3_action_orchestration_read import _upsert_action

        # If waiting on evidence and now has attachment, clear needs flag but do not auto-DONE
        if linked.get("operator_needs_evidence"):
            linked["operator_needs_evidence"] = False
            # Stay WAITING/IN_PROGRESS until explicit DONE
            if linked.get("status") == "WAITING":
                linked["status"] = "IN_PROGRESS"
        linked["timestamps"] = dict(linked.get("timestamps") or {})
        linked["timestamps"]["updated_at"] = _utc()
        _upsert_action(linked, persist=persist)

    append_action_history(
        action_id=action_id,
        previous_status=action.get("status"),
        new_status=(get_action(action_id) or {}).get("status") or action.get("status"),
        actor=str(payload.get("actor") or "human"),
        note="Evidence recorded" if not duplicate else "Duplicate evidence ignored",
        evidence=envelope,
        operator_intent="RECORD_EVIDENCE",
        persist=persist,
    )

    # Readiness reassessment is read-time only
    readiness = None
    try:
        from m3_pursuit_readiness_read import build_pursuit_readiness_assessment
        from m3_pipeline_store import M3PipelineStore
        from m3_discovery_service import restore_pipeline_store_from_db

        store = M3PipelineStore()
        try:
            restore_pipeline_store_from_db(store)
        except Exception:
            pass
        row = store.get(str(oid)) if _known(oid) else None
        if row:
            readiness = build_pursuit_readiness_assessment(row, ensure_actions=False)
    except Exception:
        readiness = None

    return {
        "kind": "M3OperatorLoopResult",
        "intent": "RECORD_EVIDENCE",
        "accepted": True,
        "duplicate": duplicate,
        "action_id": action_id,
        "evidence": envelope,
        "evidence_refs": created_refs,
        "commercial_evidence": commercial,
        "action": get_action(action_id),
        "loop": build_action_loop_view(action_id),
        "pursuit_readiness_after": {
            "supply_state": ((_as_dict((_as_dict(readiness).get("dimensions") or {}).get("supply_confidence")).get("state"))
            if readiness
            else "UNKNOWN"),
            "note": "State reflects evidence on next assessment — DONE alone does not advance readiness",
        }
        if readiness
        else {"note": "Opportunity row unavailable for reassessment"},
        "build": BUILD_TAG,
        "OpenAI": 0,
        "paid": 0,
        "automatic_outreach": False,
    }


def enrich_next_hour_with_operator_paths(queue: dict[str, Any]) -> dict[str, Any]:
    out = dict(queue) if isinstance(queue, dict) else {}
    for key in ("next_hour", "other_attention"):
        items = []
        for it in out.get(key) or []:
            if not isinstance(it, dict):
                continue
            item = dict(it)
            aid = item.get("action_id")
            if _known(aid):
                item["operator_paths"] = {
                    "done": "/api/m3/operator/action/done",
                    "blocked": "/api/m3/operator/action/blocked",
                    "needs_evidence": "/api/m3/operator/action/needs-evidence",
                    "record_evidence": "/api/m3/operator/action/evidence",
                    "loop": f"/api/m3/operator/action/{aid}/loop",
                }
                item["operator_intents"] = [INTENT_DONE, INTENT_BLOCKED, INTENT_NEEDS_EVIDENCE]
            items.append(item)
        out[key] = items
    out["operator_loop_build"] = BUILD_TAG
    return out


def build_operator_loop_boards(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Human OS TODAY / DECISIONS from Action Orchestration — no new queues."""
    from m3_action_orchestration_read import (
        ST_BLOCKED,
        ST_COMPLETED,
        ST_WAITING,
        list_actions,
    )

    active: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    evidence_needed: list[dict[str, Any]] = []
    for a in list_actions(limit=80):
        if a.get("status") == ST_COMPLETED:
            continue
        item = {
            "action_id": a.get("action_id"),
            "title": a.get("title"),
            "status": a.get("status"),
            "opportunity_id": a.get("related_opportunity"),
            "why": a.get("why_exists"),
        }
        if a.get("status") == ST_BLOCKED or (_as_dict(a.get("operator_block")).get("active")):
            blocked.append({**item, "blocker": _as_dict(a.get("operator_block")).get("reason")})
        elif a.get("operator_needs_evidence") or a.get("status") == ST_WAITING:
            evidence_needed.append(
                {
                    **item,
                    "required_evidence": a.get("evidence_requirements") or [],
                }
            )
        else:
            active.append(item)

    # Decisions: opportunities with recent evidence history that may need review
    decisions: list[dict[str, Any]] = []
    try:
        from m3_action_orchestration_read import list_action_history
        from m3_opportunity_operating_read import OP_DECISION_READY, derive_operator_lifecycle

        seen_oids: set[str] = set()
        for h in list_action_history(limit=40):
            if h.get("operator_intent") not in {"RECORD_EVIDENCE", INTENT_DONE}:
                continue
            aid = h.get("action_id")
            from m3_action_orchestration_read import get_action

            act = get_action(str(aid)) if aid else None
            oid = (act or {}).get("related_opportunity")
            if not _known(oid) or oid in seen_oids:
                continue
            seen_oids.add(str(oid))
            decisions.append(
                {
                    "opportunity_id": oid,
                    "why": "Evidence or completion changed — review may be needed",
                    "action_id": aid,
                    "changed_at": h.get("changed_at"),
                }
            )
        for r in (rows or [])[:30]:
            if not isinstance(r, dict):
                continue
            lc = derive_operator_lifecycle(r)
            if lc.get("current_state") == OP_DECISION_READY:
                oid = r.get("canonical_id")
                if _known(oid) and oid not in seen_oids:
                    decisions.append(
                        {
                            "opportunity_id": oid,
                            "title": r.get("title"),
                            "why": "Lifecycle DECISION_READY",
                            "state": OP_DECISION_READY,
                        }
                    )
    except Exception:
        pass

    return {
        "kind": "M3OperatorLoopBoards",
        "today": {
            "active_actions": active[:15],
            "blocked_actions": blocked[:15],
            "evidence_needed_actions": evidence_needed[:15],
        },
        "decisions": {"opportunities_needing_review": decisions[:15]},
        "not_a_new_queue": True,
        "reuses_action_orchestration": True,
        "reuses_next_hour": True,
        "build": BUILD_TAG,
        "generated_at": _utc(),
    }


def attach_operator_loop_to_deal_room(
    deal: dict[str, Any], *, row: dict[str, Any] | None = None
) -> dict[str, Any]:
    if not isinstance(deal, dict):
        return deal
    out = dict(deal)
    try:
        from m3_action_orchestration_read import list_actions

        oid = (row or {}).get("canonical_id") or deal.get("canonical_id")
        actions = list_actions(opportunity_id=str(oid), limit=20) if _known(oid) else []
        loops = []
        for a in actions[:10]:
            if a.get("action_id"):
                loops.append(build_action_loop_view(str(a["action_id"])))
        out["operator_loop"] = {
            "kind": "M3OperatorLoopDealRoom",
            "build": BUILD_TAG,
            "opportunity_id": oid or "UNKNOWN",
            "actions": loops,
            "question": "What was I asked to do → what did I do → what changed → what next?",
            "read_only": True,
        }
    except Exception as e:
        log.warning("operator loop deal attach failed: %s", e)
        out["operator_loop"] = {
            "kind": "M3OperatorLoopDealRoom",
            "build": BUILD_TAG,
            "error": "operator_loop_unavailable",
            "read_only": True,
        }
    return out


def enrich_command_center_operator_loop(
    sections: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    period: str,
) -> dict[str, Any]:
    out = dict(sections or {})
    boards = build_operator_loop_boards(rows)
    if period == "morning":
        out["operator_active_actions"] = boards["today"]["active_actions"]
        out["operator_blocked_actions"] = boards["today"]["blocked_actions"]
        out["operator_evidence_needed"] = boards["today"]["evidence_needed_actions"]
        out["operator_decisions_after_evidence"] = boards["decisions"]["opportunities_needing_review"]
    else:
        out["operator_blocked_outstanding"] = boards["today"]["blocked_actions"]
        out["operator_evidence_still_needed"] = boards["today"]["evidence_needed_actions"]
    out["operator_loop_boards"] = boards
    return out
