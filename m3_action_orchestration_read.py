"""BUILD 21 — Action Orchestration Layer (read models).

Converts known needs, unknowns, decisions, and intelligence events into
controlled, traceable actions — NOT a generic task manager.

Flow:
DECISION → ACTION → EXECUTION → RESULT → MEMORY

Does NOT score, rank, invent facts, replace retrieval/master records,
let AI modify verified intelligence, or silently promote assumptions.
UNKNOWN stays UNKNOWN. Evidence and lineage mandatory.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from application_clock import now_utc

BUILD_TAG = "20260919-m3-action-orchestration-1"

# Action states
ST_CREATED = "CREATED"
ST_READY = "READY"
ST_BLOCKED = "BLOCKED"
ST_IN_PROGRESS = "IN_PROGRESS"
ST_WAITING = "WAITING"
ST_COMPLETED = "COMPLETED"
ST_CANCELLED = "CANCELLED"
ST_FAILED = "FAILED"
ST_UNKNOWN = "UNKNOWN"

ACTION_STATES = (
    ST_CREATED,
    ST_READY,
    ST_BLOCKED,
    ST_IN_PROGRESS,
    ST_WAITING,
    ST_COMPLETED,
    ST_CANCELLED,
    ST_FAILED,
    ST_UNKNOWN,
)

# Action types
AT_RESEARCH = "RESEARCH"
AT_VALIDATION = "VALIDATION"
AT_COMMUNICATION = "COMMUNICATION"
AT_DECISION_REVIEW = "DECISION_REVIEW"
AT_EXECUTION = "EXECUTION"
AT_DOCUMENTATION = "DOCUMENTATION"
AT_MONITORING = "MONITORING"
AT_FOLLOW_UP = "FOLLOW_UP"

ACTION_TYPES = (
    AT_RESEARCH,
    AT_VALIDATION,
    AT_COMMUNICATION,
    AT_DECISION_REVIEW,
    AT_EXECUTION,
    AT_DOCUMENTATION,
    AT_MONITORING,
    AT_FOLLOW_UP,
)

# Executors
EX_HUMAN = "human"
EX_AI_AGENT = "ai_agent"
EX_AUTOMATED = "automated_process"
EX_EXTERNAL = "external_integration"

# Triggers
TRIGGERS = (
    "new_intelligence_discovered",
    "unknown_created",
    "evidence_expired",
    "conflict_detected",
    "supplier_response_received",
    "contract_change_detected",
    "product_lifecycle_change_detected",
    "decision_requires_review",
    "workflow_milestone_reached",
)

ACTION_INDEX_KEY = "m3_actions_v1"
DEP_INDEX_KEY = "m3_action_dependencies_v1"
OUTCOME_INDEX_KEY = "m3_action_outcomes_v1"
WORKFLOW_INDEX_KEY = "m3_action_workflows_v1"
EXECUTOR_INDEX_KEY = "m3_action_executors_v1"
HISTORY_INDEX_KEY = "m3_action_history_v1"

# Type definitions: required inputs, completion, allowed executors
ACTION_TYPE_DEFS: dict[str, dict[str, Any]] = {
    AT_RESEARCH: {
        "required_inputs": ["question", "related_entities", "potential_sources"],
        "completion_requirements": [
            "sources_checked_documented",
            "findings_or_unknowns_recorded",
            "evidence_references_captured",
        ],
        "allowed_executors": [EX_HUMAN, EX_AI_AGENT, EX_AUTOMATED],
    },
    AT_VALIDATION: {
        "required_inputs": ["claim_or_field", "evidence_packet_or_sources"],
        "completion_requirements": [
            "validation_status_recorded",
            "evidence_linked",
            "unknowns_explicit",
        ],
        "allowed_executors": [EX_HUMAN],  # humans validate; AI may assist research only
    },
    AT_COMMUNICATION: {
        "required_inputs": ["recipient_or_channel", "message_purpose", "related_entities"],
        "completion_requirements": [
            "communication_attempt_documented",
            "response_or_waiting_status",
            "evidence_of_contact_or_unknown",
        ],
        "allowed_executors": [EX_HUMAN, EX_EXTERNAL],
    },
    AT_DECISION_REVIEW: {
        "required_inputs": ["decision_question", "evidence_packet", "options_or_unknowns"],
        "completion_requirements": [
            "decision_recorded_or_deferred",
            "evidence_reviewed_listed",
            "action_taken_documented",
        ],
        "allowed_executors": [EX_HUMAN],
    },
    AT_EXECUTION: {
        "required_inputs": ["contract_or_opportunity", "execution_stage", "prerequisites_met_or_unknown"],
        "completion_requirements": [
            "execution_evidence_linked",
            "stage_status_updated_with_evidence",
            "blockers_or_completion_recorded",
        ],
        "allowed_executors": [EX_HUMAN, EX_EXTERNAL, EX_AUTOMATED],
    },
    AT_DOCUMENTATION: {
        "required_inputs": ["object_to_document", "required_fields"],
        "completion_requirements": [
            "documentation_complete_or_gaps_listed",
            "evidence_attached",
        ],
        "allowed_executors": [EX_HUMAN, EX_AI_AGENT, EX_AUTOMATED],
    },
    AT_MONITORING: {
        "required_inputs": ["monitored_object", "signal_to_watch"],
        "completion_requirements": [
            "observation_recorded",
            "change_or_no_change_evidenced",
        ],
        "allowed_executors": [EX_HUMAN, EX_AUTOMATED, EX_AI_AGENT],
    },
    AT_FOLLOW_UP: {
        "required_inputs": ["prior_action_or_event", "follow_up_purpose"],
        "completion_requirements": [
            "follow_up_result_documented",
            "open_or_closed_status",
        ],
        "allowed_executors": [EX_HUMAN, EX_AUTOMATED, EX_EXTERNAL],
    },
}

WORKFLOW_TEMPLATES: dict[str, dict[str, Any]] = {
    "PRODUCT_OPPORTUNITY_REVIEW": {
        "title": "Product Opportunity Review",
        "steps": [
            {"action_type": AT_RESEARCH, "title": "Identify product", "depends_on": []},
            {"action_type": AT_RESEARCH, "title": "Research suppliers", "depends_on": ["Identify product"]},
            {"action_type": AT_VALIDATION, "title": "Validate acquisition path", "depends_on": ["Identify product"]},
            {"action_type": AT_RESEARCH, "title": "Gather economics", "depends_on": ["Identify product", "Research suppliers"]},
            {"action_type": AT_EXECUTION, "title": "Execution review", "depends_on": ["Validate acquisition path", "Gather economics"]},
            {"action_type": AT_DECISION_REVIEW, "title": "Pursuit decision", "depends_on": ["Execution review"]},
        ],
    },
    "SUPPLIER_QUALIFICATION": {
        "title": "Supplier Qualification",
        "steps": [
            {"action_type": AT_VALIDATION, "title": "Verify supplier identity", "depends_on": []},
            {"action_type": AT_RESEARCH, "title": "Identify products", "depends_on": ["Verify supplier identity"]},
            {"action_type": AT_RESEARCH, "title": "Collect commercial information", "depends_on": ["Verify supplier identity"]},
            {"action_type": AT_DOCUMENTATION, "title": "Document unknowns", "depends_on": ["Identify products", "Collect commercial information"]},
        ],
    },
    "CONTRACT_EXECUTION": {
        "title": "Contract Execution",
        "steps": [
            {"action_type": AT_DOCUMENTATION, "title": "Record award", "depends_on": []},
            {"action_type": AT_COMMUNICATION, "title": "Supplier confirmation", "depends_on": ["Record award"]},
            {"action_type": AT_EXECUTION, "title": "Fulfillment planning", "depends_on": ["Supplier confirmation"]},
            {"action_type": AT_MONITORING, "title": "Delivery tracking", "depends_on": ["Fulfillment planning"]},
            {"action_type": AT_VALIDATION, "title": "Acceptance", "depends_on": ["Delivery tracking"]},
            {"action_type": AT_EXECUTION, "title": "Payment", "depends_on": ["Acceptance"]},
            {"action_type": AT_DOCUMENTATION, "title": "Capture lessons", "depends_on": ["Payment"]},
        ],
    },
}


def _utc() -> str:
    return now_utc().isoformat()


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _known(v: Any) -> bool:
    return v not in (None, "", "UNKNOWN")


def _aid(seed: str) -> str:
    return f"act:{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:12]}"


def _load_index(key: str) -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == key).one_or_none()
            if row and row.value:
                data = json.loads(row.value)
                if isinstance(data, dict):
                    data.setdefault("by_key", {})
                    data.setdefault("entries", [])
                    return data
        finally:
            db.close()
    except Exception:
        pass
    return {"by_key": {}, "entries": []}


def _save_index(key: str, payload: dict[str, Any]) -> bool:
    """Persist index. Retries once on contention — silent total failure would flake readiness."""
    for attempt in range(2):
        try:
            from database import SessionLocal
            from models import AppSetting

            payload = dict(payload)
            payload["updated_at"] = _utc()
            payload["build"] = BUILD_TAG
            raw = json.dumps(payload, default=str)
            db = SessionLocal()
            try:
                row = db.query(AppSetting).filter(AppSetting.key == key).one_or_none()
                if row:
                    row.value = raw
                else:
                    db.add(AppSetting(key=key, value=raw))
                db.commit()
                return True
            finally:
                db.close()
        except Exception:
            if attempt == 0:
                continue
    return False


def _upsert_action(action: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    """Merge action into index. Re-load before write to reduce lost-update flakes under suite load."""
    idx = _load_index(ACTION_INDEX_KEY)
    by = _as_dict(idx.get("by_key"))
    by[action["action_id"]] = action
    idx["by_key"] = by
    entries = [e for e in (idx.get("entries") or []) if isinstance(e, dict) and e.get("action_id") != action["action_id"]]
    entries.append(action)
    idx["entries"] = entries[-800:]
    if persist:
        ok = _save_index(ACTION_INDEX_KEY, idx)
        if not ok:
            # One more attempt with fresh merge
            idx2 = _load_index(ACTION_INDEX_KEY)
            by2 = _as_dict(idx2.get("by_key"))
            by2[action["action_id"]] = action
            idx2["by_key"] = by2
            entries2 = [
                e
                for e in (idx2.get("entries") or [])
                if isinstance(e, dict) and e.get("action_id") != action["action_id"]
            ]
            entries2.append(action)
            idx2["entries"] = entries2[-800:]
            _save_index(ACTION_INDEX_KEY, idx2)
    return action


def get_action(action_id: str) -> dict[str, Any] | None:
    by = _as_dict(_load_index(ACTION_INDEX_KEY).get("by_key"))
    a = by.get(action_id)
    return a if isinstance(a, dict) else None


def list_actions(
    *,
    opportunity_id: str | None = None,
    status: str | None = None,
    action_type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    entries = [e for e in (_load_index(ACTION_INDEX_KEY).get("entries") or []) if isinstance(e, dict)]
    if opportunity_id:
        entries = [e for e in entries if e.get("related_opportunity") == opportunity_id]
    if status:
        entries = [e for e in entries if e.get("status") == status]
    if action_type:
        entries = [e for e in entries if e.get("action_type") == action_type]
    entries.sort(key=lambda e: str(e.get("timestamps", {}).get("updated_at") or e.get("timestamps", {}).get("created_at") or ""), reverse=True)
    return entries[:limit]


# ---------------------------------------------------------------------------
# 1–2 Action record + type defs
# ---------------------------------------------------------------------------
def action_type_catalog() -> dict[str, Any]:
    return {
        "kind": "M3ActionTypeCatalog",
        "types": [
            {"action_type": t, **ACTION_TYPE_DEFS[t]}
            for t in ACTION_TYPES
        ],
        "states": list(ACTION_STATES),
        "ai_may_modify_verified_intelligence": False,
    }


def create_action(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    """No action without context — trigger + why required."""
    title = str(payload.get("title") or "").strip()
    trigger = payload.get("trigger_source") or payload.get("trigger")
    why = payload.get("why") or payload.get("description")
    if not title:
        raise ValueError("title required")
    if not _known(trigger):
        raise ValueError("trigger_source required — no action without context")
    if not _known(why):
        raise ValueError("description/why required — no action without context")

    atype = str(payload.get("action_type") or AT_RESEARCH).upper()
    if atype not in ACTION_TYPES:
        atype = AT_RESEARCH
    typedef = ACTION_TYPE_DEFS[atype]
    status = str(payload.get("status") or ST_CREATED).upper()
    if status not in ACTION_STATES:
        status = ST_CREATED

    action_id = payload.get("action_id") or _aid(f"{title}|{trigger}|{_utc()}")
    now = _utc()
    action = {
        "kind": "M3Action",
        "action_id": action_id,
        "action_type": atype,
        "title": title,
        "description": why,
        "why_exists": why,
        "trigger_source": trigger,
        "related_entities": payload.get("related_entities") or ["UNKNOWN"],
        "related_opportunity": payload.get("related_opportunity") or payload.get("opportunity_id") or "UNKNOWN",
        "related_decision": payload.get("related_decision") or "UNKNOWN",
        "required_inputs": payload.get("required_inputs") or list(typedef["required_inputs"]),
        "dependencies": payload.get("dependencies") or [],
        "executor": payload.get("executor")
        or {
            "executor_type": EX_HUMAN,
            "permissions": ["execute_assigned_actions"],
            "allowed_actions": [atype],
            "required_approval": atype in {AT_VALIDATION, AT_DECISION_REVIEW, AT_EXECUTION},
            "may_modify_verified_intelligence": False,
        },
        "status": status,
        "evidence_requirements": payload.get("evidence_requirements")
        or list(typedef["completion_requirements"]),
        "completion_criteria": payload.get("completion_criteria")
        or list(typedef["completion_requirements"]),
        "result": payload.get("result") or "UNKNOWN",
        "outcome": payload.get("outcome") or "UNKNOWN",
        "timestamps": {
            "created_at": payload.get("created_at") or now,
            "updated_at": now,
            "started_at": payload.get("started_at") or "UNKNOWN",
            "completed_at": payload.get("completed_at") or "UNKNOWN",
        },
        "lineage": {
            "parent_event": trigger,
            "evidence_caused_by": payload.get("evidence_caused_by") or payload.get("evidence") or "UNKNOWN",
            "created_by": payload.get("created_by") or "system",
            "version": "1",
            "build": BUILD_TAG,
        },
        "evidence_links": {
            "evidence_packets": payload.get("evidence_packets") or [],
            "retrieval_sessions": payload.get("retrieval_sessions") or [],
            "master_records": payload.get("master_records") or [],
            "decisions": payload.get("decisions") or [],
            "analysis_records": payload.get("analysis_records") or [],
        },
        "blocked_by": [],
        "fabricated": False,
        "numeric_score": None,
        "ai_decides": False,
    }
    # Apply dependency blocking if prerequisites listed
    deps = action["dependencies"]
    if deps:
        blocking = []
        for d in deps:
            dep_id = d if isinstance(d, str) else (d.get("prerequisite_action_id") if isinstance(d, dict) else None)
            if not dep_id:
                continue
            prereq = get_action(str(dep_id))
            if not prereq or prereq.get("status") != ST_COMPLETED:
                blocking.append(str(dep_id))
        if blocking:
            action["blocked_by"] = blocking
            action["status"] = ST_BLOCKED
        elif action["status"] == ST_CREATED:
            action["status"] = ST_READY
    return _upsert_action(action, persist=persist)


# ---------------------------------------------------------------------------
# 3 Trigger engine
# ---------------------------------------------------------------------------
_TRIGGER_TEMPLATES: dict[str, dict[str, Any]] = {
    "evidence_expired": {
        "action_type": AT_COMMUNICATION,
        "title": "Request updated supplier quote",
        "why": "Supplier quote or evidence expired — refresh required before pricing/execution",
    },
    "unknown_created": {
        "action_type": AT_RESEARCH,
        "title": "Resolve intelligence unknown",
        "why": "Unknown created — research required to convert UNKNOWN with evidence or keep UNKNOWN",
    },
    "conflict_detected": {
        "action_type": AT_DECISION_REVIEW,
        "title": "Resolve data conflict",
        "why": "Conflicting values detected — no auto-winner; review with evidence",
    },
    "supplier_response_received": {
        "action_type": AT_VALIDATION,
        "title": "Validate supplier response",
        "why": "Supplier response received — document evidence and update commercial terms history",
    },
    "contract_change_detected": {
        "action_type": AT_DOCUMENTATION,
        "title": "Record contract modification",
        "why": "Contract change detected — capture modification with evidence",
    },
    "product_lifecycle_change_detected": {
        "action_type": AT_MONITORING,
        "title": "Review product lifecycle change",
        "why": "Product lifecycle signal changed — verify impact with evidence",
    },
    "decision_requires_review": {
        "action_type": AT_DECISION_REVIEW,
        "title": "Decision review required",
        "why": "Decision requires human review — AI does not decide",
    },
    "new_intelligence_discovered": {
        "action_type": AT_DOCUMENTATION,
        "title": "Capture new intelligence",
        "why": "New intelligence discovered — link to master record / evidence packet",
    },
    "workflow_milestone_reached": {
        "action_type": AT_FOLLOW_UP,
        "title": "Advance workflow milestone",
        "why": "Workflow milestone reached — execute next template step if prerequisites met",
    },
}


def trigger_action_from_event(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    event = str(payload.get("event") or payload.get("trigger") or "").strip()
    if event not in TRIGGERS and event not in _TRIGGER_TEMPLATES:
        # allow custom but require why/title
        event = event or "unknown_created"
    tmpl = _TRIGGER_TEMPLATES.get(event) or {
        "action_type": AT_FOLLOW_UP,
        "title": payload.get("title") or "Follow up on event",
        "why": payload.get("why") or f"Event triggered: {event}",
    }
    # Specialize unknown_created when unknown_label provided
    title = payload.get("title") or tmpl["title"]
    why = payload.get("why") or tmpl["why"]
    if event == "unknown_created" and payload.get("unknown_label"):
        label = payload["unknown_label"]
        title = f"Verify {label}"
        why = f"UNKNOWN: {label} — create research/validation action; do not invent status"
    if event == "evidence_expired" and payload.get("what") == "supplier_quote":
        title = "Request updated supplier quote"
        why = "Supplier quote expires — EVENT creates action to request updated quote"

    return create_action(
        {
            "action_type": payload.get("action_type") or tmpl["action_type"],
            "title": title,
            "description": why,
            "trigger_source": event,
            "evidence_caused_by": payload.get("evidence") or payload.get("evidence_caused_by") or "UNKNOWN",
            "related_opportunity": payload.get("related_opportunity") or payload.get("opportunity_id"),
            "related_entities": payload.get("related_entities") or ["UNKNOWN"],
            "related_decision": payload.get("related_decision"),
            "evidence_packets": payload.get("evidence_packets") or [],
            "created_by": payload.get("created_by") or "trigger_engine",
        },
        persist=persist,
    )


def derive_actions_from_row(row: dict[str, Any], *, persist: bool = False) -> list[dict[str, Any]]:
    """Event-like derivation from opportunity state — does not invent completion."""
    actions = []
    cid = row.get("canonical_id")
    # Knowledge gaps → research actions
    try:
        from m3_intelligence_retrieval_read import detect_knowledge_gaps

        gaps = detect_knowledge_gaps(row)
        for g in (gaps.get("gaps") or [])[:5]:
            actions.append(
                trigger_action_from_event(
                    {
                        "event": "unknown_created",
                        "unknown_label": g.get("missing_information"),
                        "why": f"{g.get('impact')} — {g.get('research_action')}",
                        "opportunity_id": cid,
                        "evidence": "knowledge_gap_detection",
                        "related_entities": g.get("possible_sources") or ["UNKNOWN"],
                    },
                    persist=persist,
                )
            )
    except Exception:
        pass

    # Quote expiration → communication
    try:
        from m3_supplier_capital_ops_read import build_supplier_commercial_terms

        terms = build_supplier_commercial_terms(row)
        for s in terms.get("suppliers") or []:
            q = _as_dict(s.get("quote"))
            if q.get("status") == "EXPIRED" or (
                _known(_as_dict(q.get("expiration")).get("value")) and q.get("status") == "EXPIRED"
            ):
                actions.append(
                    trigger_action_from_event(
                        {
                            "event": "evidence_expired",
                            "what": "supplier_quote",
                            "opportunity_id": cid,
                            "related_entities": [s.get("supplier_name") or "UNKNOWN"],
                            "evidence": f"quote_expiration:{_as_dict(q.get('expiration')).get('value')}",
                        },
                        persist=persist,
                    )
                )
    except Exception:
        pass

    return actions


# ---------------------------------------------------------------------------
# 4 Dependency engine
# ---------------------------------------------------------------------------
def add_dependency(
    *,
    action_id: str,
    prerequisite_action_id: str,
    relationship: str = "BLOCKS",
    persist: bool = True,
) -> dict[str, Any]:
    dep = {
        "kind": "M3ActionDependency",
        "action_id": action_id,
        "prerequisite_action_id": prerequisite_action_id,
        "relationship": relationship,  # BLOCKS / REQUIRES
        "created_at": _utc(),
        "note": "No assumptions — prerequisite must be COMPLETED with evidence",
    }
    if persist:
        idx = _load_index(DEP_INDEX_KEY)
        entries = list(idx.get("entries") or [])
        entries.append(dep)
        idx["entries"] = entries[-500:]
        _save_index(DEP_INDEX_KEY, idx)

    action = get_action(action_id)
    if action:
        deps = list(action.get("dependencies") or [])
        if prerequisite_action_id not in deps:
            deps.append(prerequisite_action_id)
        action["dependencies"] = deps
        prereq = get_action(prerequisite_action_id)
        if not prereq or prereq.get("status") != ST_COMPLETED:
            blocked = list(action.get("blocked_by") or [])
            if prerequisite_action_id not in blocked:
                blocked.append(prerequisite_action_id)
            action["blocked_by"] = blocked
            if action.get("status") not in {ST_COMPLETED, ST_CANCELLED, ST_FAILED}:
                action["status"] = ST_BLOCKED
        _upsert_action(action, persist=persist)
    return dep


def evaluate_action_readiness(action_id: str, *, persist: bool = True) -> dict[str, Any]:
    action = get_action(action_id)
    if not action:
        return {"action_id": action_id, "status": ST_UNKNOWN, "error": "not_found"}
    # Operator-declared blocks are preserved (not cleared by dependency engine)
    operator_block = action.get("operator_block") if isinstance(action.get("operator_block"), dict) else None
    if operator_block and operator_block.get("active"):
        action["status"] = ST_BLOCKED
        blocked = list(action.get("blocked_by") or [])
        tag = f"operator:{operator_block.get('reason') or 'blocked'}"
        if tag not in blocked:
            blocked.append(tag)
        action["blocked_by"] = blocked
        action["timestamps"] = dict(action.get("timestamps") or {})
        action["timestamps"]["updated_at"] = _utc()
        _upsert_action(action, persist=persist)
        return {
            "action_id": action_id,
            "status": action["status"],
            "blocked_by": blocked,
            "ready": False,
            "operator_block": True,
        }

    # Operator needs-evidence waits are preserved
    if action.get("operator_needs_evidence") and action.get("status") not in {
        ST_COMPLETED,
        ST_CANCELLED,
        ST_FAILED,
    }:
        action["status"] = ST_WAITING
        action["timestamps"] = dict(action.get("timestamps") or {})
        action["timestamps"]["updated_at"] = _utc()
        _upsert_action(action, persist=persist)
        return {
            "action_id": action_id,
            "status": action["status"],
            "blocked_by": list(action.get("blocked_by") or []),
            "ready": False,
            "operator_needs_evidence": True,
        }

    blocking = []
    for d in action.get("dependencies") or []:
        dep_id = d if isinstance(d, str) else d.get("prerequisite_action_id")
        prereq = get_action(str(dep_id)) if dep_id else None
        if not prereq or prereq.get("status") != ST_COMPLETED:
            blocking.append(str(dep_id))
    action["blocked_by"] = blocking
    if blocking and action.get("status") not in {ST_COMPLETED, ST_CANCELLED, ST_FAILED}:
        action["status"] = ST_BLOCKED
    elif not blocking and action.get("status") in {ST_BLOCKED, ST_CREATED}:
        action["status"] = ST_READY
    action["timestamps"] = dict(action.get("timestamps") or {})
    action["timestamps"]["updated_at"] = _utc()
    _upsert_action(action, persist=persist)
    return {
        "action_id": action_id,
        "status": action["status"],
        "blocked_by": blocking,
        "ready": action["status"] == ST_READY,
        "examples": {
            "cannot_price_until_product_identity": "price opportunity blocked until product identity confirmed",
            "cannot_commit_until_supplier_validated": "commit execution blocked until supplier path validated",
        },
    }


def append_action_history(
    *,
    action_id: str,
    previous_status: Any,
    new_status: Any,
    actor: str = "human",
    note: Any = "UNKNOWN",
    evidence: Any = "UNKNOWN",
    operator_intent: Any = "UNKNOWN",
    persist: bool = True,
) -> dict[str, Any]:
    """Audit trail for action state changes — reuses AppSetting index pattern."""
    entry = {
        "kind": "M3ActionHistoryEntry",
        "history_id": _aid(f"hist|{action_id}|{previous_status}|{new_status}|{_utc()}"),
        "action_id": action_id,
        "previous_status": previous_status if previous_status not in (None, "") else "UNKNOWN",
        "new_status": new_status if new_status not in (None, "") else "UNKNOWN",
        "actor": actor if _known(actor) else "UNKNOWN",
        "note": note if note not in (None, "") else "UNKNOWN",
        "evidence": evidence if evidence not in (None, "") else "UNKNOWN",
        "operator_intent": operator_intent if operator_intent not in (None, "") else "UNKNOWN",
        "changed_at": _utc(),
        "build": BUILD_TAG,
        "fabricated": False,
    }
    action = get_action(action_id)
    if action:
        hist = list(action.get("history") or [])
        hist.append(entry)
        action["history"] = hist[-40:]
        action["timestamps"] = dict(action.get("timestamps") or {})
        action["timestamps"]["updated_at"] = _utc()
        _upsert_action(action, persist=persist)
    if persist:
        idx = _load_index(HISTORY_INDEX_KEY)
        entries = list(idx.get("entries") or [])
        entries.append(entry)
        idx["entries"] = entries[-800:]
        _save_index(HISTORY_INDEX_KEY, idx)
    return entry


def list_action_history(*, action_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    entries = [e for e in (_load_index(HISTORY_INDEX_KEY).get("entries") or []) if isinstance(e, dict)]
    if action_id:
        entries = [e for e in entries if e.get("action_id") == action_id]
    entries.sort(key=lambda e: str(e.get("changed_at") or ""), reverse=True)
    return entries[:limit]


# ---------------------------------------------------------------------------
# 5 Executor model
# ---------------------------------------------------------------------------
def assign_executor(
    *,
    action_id: str,
    executor_type: str,
    permissions: list[str] | None = None,
    required_approval: bool | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    action = get_action(action_id)
    if not action:
        raise ValueError("action not found")
    et = executor_type if executor_type in {EX_HUMAN, EX_AI_AGENT, EX_AUTOMATED, EX_EXTERNAL} else EX_HUMAN
    may_modify = False  # AI agents must not directly modify verified intelligence
    if et == EX_AI_AGENT:
        may_modify = False
        if action.get("action_type") in {AT_VALIDATION, AT_DECISION_REVIEW}:
            raise ValueError("AI agent cannot be sole executor for VALIDATION or DECISION_REVIEW")

    typedef = ACTION_TYPE_DEFS.get(action.get("action_type"), ACTION_TYPE_DEFS[AT_RESEARCH])
    if et not in typedef.get("allowed_executors", [EX_HUMAN]):
        raise ValueError(f"executor {et} not allowed for action type {action.get('action_type')}")

    executor = {
        "executor_type": et,
        "permissions": permissions or ["execute_assigned_actions"],
        "allowed_actions": [action.get("action_type")],
        "required_approval": required_approval
        if required_approval is not None
        else action.get("action_type") in {AT_VALIDATION, AT_DECISION_REVIEW, AT_EXECUTION},
        "may_modify_verified_intelligence": may_modify,
        "assigned_at": _utc(),
    }
    action["executor"] = executor
    action["timestamps"] = dict(action.get("timestamps") or {})
    action["timestamps"]["updated_at"] = _utc()
    _upsert_action(action, persist=persist)

    rec = {"kind": "M3ExecutorAssignment", "action_id": action_id, "executor": executor}
    if persist:
        idx = _load_index(EXECUTOR_INDEX_KEY)
        entries = list(idx.get("entries") or [])
        entries.append(rec)
        idx["entries"] = entries[-300:]
        _save_index(EXECUTOR_INDEX_KEY, idx)
    return rec


# ---------------------------------------------------------------------------
# 6–7 Completion + evidence linking
# ---------------------------------------------------------------------------
def link_action_evidence(
    *,
    action_id: str,
    evidence_packets: list[str] | None = None,
    retrieval_sessions: list[str] | None = None,
    master_records: list[str] | None = None,
    decisions: list[str] | None = None,
    analysis_records: list[str] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    action = get_action(action_id)
    if not action:
        raise ValueError("action not found")
    links = dict(action.get("evidence_links") or {})
    for key, vals in (
        ("evidence_packets", evidence_packets),
        ("retrieval_sessions", retrieval_sessions),
        ("master_records", master_records),
        ("decisions", decisions),
        ("analysis_records", analysis_records),
    ):
        if vals:
            links[key] = list(dict.fromkeys(list(links.get(key) or []) + list(vals)))
    action["evidence_links"] = links
    action["timestamps"] = dict(action.get("timestamps") or {})
    action["timestamps"]["updated_at"] = _utc()
    return _upsert_action(action, persist=persist)


def record_action_result(
    *,
    action_id: str,
    result: Any,
    evidence: Any = "UNKNOWN",
    status: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    action = get_action(action_id)
    if not action:
        raise ValueError("action not found")
    action["result"] = result if result is not None else "UNKNOWN"
    action["lineage"] = dict(action.get("lineage") or {})
    action["lineage"]["result_evidence"] = evidence if evidence not in (None, "") else "UNKNOWN"
    if status:
        st = status.upper()
        if st in ACTION_STATES:
            action["status"] = st
    elif action.get("status") in {ST_READY, ST_CREATED, ST_BLOCKED}:
        action["status"] = ST_IN_PROGRESS
        action["timestamps"] = dict(action.get("timestamps") or {})
        action["timestamps"]["started_at"] = _utc()
    action["timestamps"] = dict(action.get("timestamps") or {})
    action["timestamps"]["updated_at"] = _utc()
    return _upsert_action(action, persist=persist)


def complete_action(
    *,
    action_id: str,
    result: Any = None,
    evidence: Any = "UNKNOWN",
    criteria_met: list[str] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    action = get_action(action_id)
    if not action:
        raise ValueError("action not found")
    evaluate_action_readiness(action_id, persist=persist)
    action = get_action(action_id) or action
    if action.get("status") == ST_BLOCKED:
        raise ValueError("action still BLOCKED by incomplete prerequisites — no assumptions")

    if not _known(evidence):
        raise ValueError("evidence required to complete action")

    required = list(action.get("completion_criteria") or [])
    met = list(criteria_met or [])
    missing = [c for c in required if c not in met]
    # Allow completion when evidence documents remaining unknowns explicitly
    unknowns_ok = any(
        x in met
        for x in (
            "unknowns_recorded",
            "unknowns_explicit",
            "findings_or_unknowns_recorded",
            "documentation_complete_or_gaps_listed",
        )
    )
    if missing and not unknowns_ok and not met:
        # If no criteria claimed met, require at least documenting unknowns in evidence note
        if "unknown" not in str(evidence).lower() and "complete" not in str(evidence).lower():
            raise ValueError(f"completion criteria not evidenced: {missing}")

    action["result"] = result if result is not None else action.get("result") or "UNKNOWN"
    action["status"] = ST_COMPLETED
    action["completion_criteria_met"] = met if met else (required if not missing else met)
    action["completion_criteria_missing"] = missing
    action["timestamps"] = dict(action.get("timestamps") or {})
    action["timestamps"]["completed_at"] = _utc()
    action["timestamps"]["updated_at"] = _utc()
    action["lineage"] = dict(action.get("lineage") or {})
    action["lineage"]["completion_evidence"] = evidence
    _upsert_action(action, persist=persist)

    for other in list_actions(limit=200):
        deps = other.get("dependencies") or []
        dep_ids = []
        for d in deps:
            if isinstance(d, str):
                dep_ids.append(d)
            elif isinstance(d, dict) and d.get("prerequisite_action_id"):
                dep_ids.append(d["prerequisite_action_id"])
        if action_id in dep_ids:
            evaluate_action_readiness(other["action_id"], persist=persist)

    return {
        "kind": "M3ActionCompletion",
        "action": action,
        "trace": {
            "question": action.get("why_exists") or action.get("description"),
            "evidence": evidence,
            "action": action.get("title"),
            "result": action.get("result"),
        },
    }


# ---------------------------------------------------------------------------
# 8 Outcome memory
# ---------------------------------------------------------------------------
def capture_action_outcome(
    payload: dict[str, Any],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    entry = {
        "kind": "M3ActionOutcomeMemory",
        "action_id": payload.get("action_id") or "UNKNOWN",
        "expected_outcome": payload.get("expected_outcome") or "UNKNOWN",
        "actual_outcome": payload.get("actual_outcome") or "UNKNOWN",
        "variance": payload.get("variance") or "UNKNOWN",
        "lesson_created": payload.get("lesson_created") or "UNKNOWN",
        "related_future_applicability": payload.get("related_future_applicability") or ["UNKNOWN"],
        "evidence": payload.get("evidence") if payload.get("evidence") not in (None, "") else "UNKNOWN",
        "recorded_at": _utc(),
        "fabricated_score": False,
    }
    # Update action outcome field
    if _known(payload.get("action_id")):
        action = get_action(str(payload["action_id"]))
        if action:
            action["outcome"] = entry
            action["timestamps"] = dict(action.get("timestamps") or {})
            action["timestamps"]["updated_at"] = _utc()
            _upsert_action(action, persist=persist)
    if persist:
        idx = _load_index(OUTCOME_INDEX_KEY)
        entries = list(idx.get("entries") or [])
        entries.append(entry)
        idx["entries"] = entries[-400:]
        _save_index(OUTCOME_INDEX_KEY, idx)
    return entry


# ---------------------------------------------------------------------------
# 9 Workflow templates
# ---------------------------------------------------------------------------
def start_workflow(
    *,
    template_key: str,
    opportunity_id: str | None = None,
    related_entities: list[Any] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    tmpl = WORKFLOW_TEMPLATES.get(template_key)
    if not tmpl:
        raise ValueError(f"unknown workflow template: {template_key}")
    created = []
    title_to_id: dict[str, str] = {}
    for step in tmpl["steps"]:
        dep_ids = [title_to_id[t] for t in (step.get("depends_on") or []) if t in title_to_id]
        action = create_action(
            {
                "action_type": step["action_type"],
                "title": step["title"],
                "description": f"Workflow {template_key}: {step['title']}",
                "trigger_source": "workflow_milestone_reached",
                "related_opportunity": opportunity_id,
                "related_entities": related_entities or ["UNKNOWN"],
                "dependencies": dep_ids,
                "evidence_caused_by": f"workflow:{template_key}",
                "created_by": "workflow_engine",
            },
            persist=persist,
        )
        title_to_id[step["title"]] = action["action_id"]
        for dep_id in dep_ids:
            add_dependency(action_id=action["action_id"], prerequisite_action_id=dep_id, persist=persist)
        created.append(action)

    record = {
        "kind": "M3ActionWorkflow",
        "template_key": template_key,
        "title": tmpl["title"],
        "opportunity_id": opportunity_id or "UNKNOWN",
        "action_ids": [a["action_id"] for a in created],
        "started_at": _utc(),
        "build": BUILD_TAG,
    }
    if persist:
        idx = _load_index(WORKFLOW_INDEX_KEY)
        entries = list(idx.get("entries") or [])
        entries.append(record)
        idx["entries"] = entries[-200:]
        _save_index(WORKFLOW_INDEX_KEY, idx)
    return record


def list_workflow_templates() -> dict[str, Any]:
    return {
        "kind": "M3ActionWorkflowTemplateSet",
        "templates": [
            {"key": k, "title": v["title"], "steps": v["steps"]}
            for k, v in WORKFLOW_TEMPLATES.items()
        ],
    }


# ---------------------------------------------------------------------------
# Profile + deal room + command center
# ---------------------------------------------------------------------------
def build_action_orchestration_profile(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    cid = row.get("canonical_id")
    derived = derive_actions_from_row(row, persist=False)
    existing = list_actions(opportunity_id=str(cid), limit=30) if cid else []
    return {
        "kind": "M3ActionOrchestrationProfile",
        "build": BUILD_TAG,
        "opportunity_id": cid or "UNKNOWN",
        "action_types": action_type_catalog(),
        "workflow_templates": list_workflow_templates(),
        "derived_actions": derived[:15],
        "actions": existing,
        "flow": [
            "EXTERNAL WORLD",
            "CONNECTORS / SOURCES",
            "RETRIEVAL INTELLIGENCE",
            "MASTER RECORD GOVERNANCE",
            "KNOWLEDGE GRAPH",
            "PRODUCT INTELLIGENCE",
            "SUPPLIER INTELLIGENCE",
            "BUYER INTELLIGENCE",
            "ACQUISITION INTELLIGENCE",
            "ECONOMIC INTELLIGENCE",
            "EXECUTION INTELLIGENCE",
            "STRATEGIC INTELLIGENCE",
            "DECISION SUPPORT",
            "ACTION ORCHESTRATION",
            "EXECUTION",
            "LEARNING MEMORY",
        ],
        "questions": [
            "Why does this action exist?",
            "What triggered it?",
            "What evidence caused it?",
            "What information is required?",
            "Who/what performs it?",
            "What happened afterward?",
        ],
        "ai_decides": False,
        "no_numeric_scores": True,
        "no_rankings": True,
        "does_not_replace_retrieval": True,
        "does_not_replace_master_records": True,
        "facts_only": True,
        "unknown_preserved": True,
        "engines_unchanged": True,
        "scoring_unchanged": True,
        "generated_at": _utc(),
        "read_only": True,
    }


def attach_action_orchestration_to_deal_room(deal: dict[str, Any], *, row: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(deal, dict):
        return deal
    out = dict(deal)
    try:
        out["action_orchestration"] = build_action_orchestration_profile(
            row or {"canonical_id": deal.get("canonical_id")}
        )
    except Exception:
        out["action_orchestration"] = {
            "kind": "M3ActionOrchestrationProfile",
            "build": BUILD_TAG,
            "error": "action_orchestration_unavailable",
            "read_only": True,
        }
    return out


def enrich_command_center_actions(
    sections: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    period: str,
) -> dict[str, Any]:
    out = dict(sections or {})
    blocked = []
    unresolved_unknowns = list(out.get("missing_evidence") or [])
    pending_decisions = list(out.get("pending_decisions") or [])
    supplier_needed = []
    expired = []
    execution_actions = []
    completed = []
    new_intel = []
    resolved_unknowns = []
    lessons = []

    all_actions = list_actions(limit=100)
    for a in all_actions:
        st = a.get("status")
        item = {
            "action_id": a.get("action_id"),
            "title": a.get("title"),
            "status": st,
            "type": a.get("action_type"),
            "opportunity_id": a.get("related_opportunity"),
            "why": a.get("why_exists"),
        }
        if st == ST_BLOCKED:
            blocked.append(item)
        if st == ST_COMPLETED:
            completed.append(item)
        if a.get("action_type") == AT_EXECUTION and st not in {ST_COMPLETED, ST_CANCELLED}:
            execution_actions.append(item)
        if a.get("action_type") == AT_COMMUNICATION and st not in {ST_COMPLETED, ST_CANCELLED}:
            supplier_needed.append(item)
        if a.get("trigger_source") == "evidence_expired":
            expired.append(item)
        if a.get("action_type") == AT_DECISION_REVIEW and st not in {ST_COMPLETED, ST_CANCELLED}:
            pending_decisions.append(item)

    for e in (_load_index(OUTCOME_INDEX_KEY).get("entries") or [])[-30:]:
        if isinstance(e, dict):
            lessons.append(
                {
                    "action_id": e.get("action_id"),
                    "lesson": e.get("lesson_created"),
                    "variance": e.get("variance"),
                }
            )

    for r in rows[:30]:
        if not isinstance(r, dict):
            continue
        for a in derive_actions_from_row(r, persist=False)[:3]:
            if a.get("trigger_source") == "unknown_created":
                unresolved_unknowns.append(
                    {
                        "opportunity_id": r.get("canonical_id"),
                        "title": (r.get("title") or "")[:80],
                        "missing": a.get("title"),
                        "action": a.get("why_exists"),
                    }
                )

    if period == "morning":
        out["blocked_actions"] = blocked[:15]
        out["unresolved_unknowns"] = unresolved_unknowns[:15]
        out["pending_decisions"] = pending_decisions[:15]
        out["supplier_responses_needed"] = supplier_needed[:15]
        out["expired_evidence"] = expired[:15]
        out["execution_actions"] = execution_actions[:15]
    else:
        out["completed_actions"] = (list(out.get("completed_actions") or []) + completed)[:15]
        out["new_intelligence_created"] = new_intel[:15]
        out["resolved_unknowns"] = resolved_unknowns[:15]
        out["lessons_captured"] = lessons[:15]
    return out
