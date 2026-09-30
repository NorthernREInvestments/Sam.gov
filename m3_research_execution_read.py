"""BUILD 25 — Research Mission Execution Layer (read models).

Connects unknowns to controlled evidence collection.

Unknown → Research Mission → Research Tasks → Evidence Collection
  → Validation → Record Update → Memory

NOT an autonomous AI agent. No unlimited searching, browsing loops,
uncontrolled research, auto outreach, or automatic decisions.

AI remains requested, cost-controlled, explainable via stages 0–5.
Does not create a second source of truth — views/links over existing layers.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from application_clock import now_utc
from cost_governor_constants import COST_FREE, TIER_ORDER

BUILD_TAG = "20260919-m3-research-execution-1"

ST_CREATED = "CREATED"
ST_READY = "READY"
ST_ACTIVE = "ACTIVE"
ST_WAITING = "WAITING"
ST_EVIDENCE_FOUND = "EVIDENCE_FOUND"
ST_REVIEW = "REVIEW"
ST_COMPLETE = "COMPLETE"
ST_BLOCKED = "BLOCKED"

MISSION_STATES = (
    ST_CREATED,
    ST_READY,
    ST_ACTIVE,
    ST_WAITING,
    ST_EVIDENCE_FOUND,
    ST_REVIEW,
    ST_COMPLETE,
    ST_BLOCKED,
)

SOURCE_TYPES = (
    "Government record",
    "Manufacturer",
    "Distributor",
    "Supplier",
    "Document",
    "Human input",
    "Unknown",
)

MISSION_INDEX = "m3_research_missions_exec_v1"
TASK_INDEX = "m3_research_tasks_exec_v1"
SOURCE_INDEX = "m3_research_sources_exec_v1"
OUTCOME_INDEX = "m3_research_outcomes_exec_v1"

# Map supply-style unknowns → mission templates
_UNKNOWN_MISSION_TEMPLATES = (
    (
        "supplier",
        {
            "question": "Find suppliers for product",
            "reason": "Supplier missing from supply path",
            "required_evidence": ["Supplier relationship evidence", "Catalog or quote"],
            "tasks": [
                {
                    "question": "Find supplier",
                    "source_type": "Distributor",
                    "instructions": "Identify distributors/dealers with evidenced product relationship",
                    "evidence_required": ["Supplier relationship evidence"],
                }
            ],
        },
    ),
    (
        "manufacturer",
        {
            "question": "Find manufacturer for product",
            "reason": "Manufacturer missing from supply path",
            "required_evidence": ["OEM documentation"],
            "tasks": [
                {
                    "question": "Find manufacturer",
                    "source_type": "Manufacturer",
                    "instructions": "Locate OEM documentation linking product to manufacturer",
                    "evidence_required": ["OEM documentation"],
                }
            ],
        },
    ),
    (
        "pricing",
        {
            "question": "Obtain commercial pricing evidence",
            "reason": "Commercial evidence missing",
            "required_evidence": ["Supplier Quote"],
            "tasks": [
                {
                    "question": "Collect supplier quote evidence",
                    "source_type": "Supplier",
                    "instructions": "Document quote or catalog price with source and date — no automatic outreach",
                    "evidence_required": ["Supplier Quote"],
                }
            ],
        },
    ),
    (
        "quote",
        {
            "question": "Obtain commercial pricing evidence",
            "reason": "Commercial evidence missing",
            "required_evidence": ["Supplier Quote"],
            "tasks": [
                {
                    "question": "Collect supplier quote evidence",
                    "source_type": "Supplier",
                    "instructions": "Document quote with source and date — human-controlled",
                    "evidence_required": ["Supplier Quote"],
                }
            ],
        },
    ),
    (
        "delivery",
        {
            "question": "Confirm delivery / lead time",
            "reason": "Execution requirement unknown",
            "required_evidence": ["Lead time confirmation"],
            "tasks": [
                {
                    "question": "Confirm lead time",
                    "source_type": "Supplier",
                    "instructions": "Record lead time evidence; do not invent availability",
                    "evidence_required": ["Lead time confirmation"],
                }
            ],
        },
    ),
    (
        "product",
        {
            "question": "Confirm product identity",
            "reason": "Product identity not evidenced",
            "required_evidence": ["NSN/part/document"],
            "tasks": [
                {
                    "question": "Confirm product identity",
                    "source_type": "Government record",
                    "instructions": "Capture NSN, part number, or packaging evidence",
                    "evidence_required": ["NSN/part/document"],
                }
            ],
        },
    ),
)


def _utc() -> str:
    return now_utc().isoformat()


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _known(v: Any) -> bool:
    return v not in (None, "", "UNKNOWN")


def _sid(prefix: str, seed: str) -> str:
    return f"{prefix}:{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:12]}"


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
        return False


def _upsert(index_key: str, id_field: str, record: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    idx = _load_index(index_key)
    by = _as_dict(idx.get("by_key"))
    rid = record[id_field]
    by[rid] = record
    idx["by_key"] = by
    entries = [e for e in (idx.get("entries") or []) if isinstance(e, dict) and e.get(id_field) != rid]
    entries.append(record)
    idx["entries"] = entries[-500:]
    if persist:
        _save_index(index_key, idx)
    return record


def _get(index_key: str, rid: str) -> dict[str, Any] | None:
    by = _as_dict(_load_index(index_key).get("by_key"))
    r = by.get(rid)
    return r if isinstance(r, dict) else None


def _list_entries(index_key: str, *, limit: int = 50) -> list[dict[str, Any]]:
    entries = [e for e in (_load_index(index_key).get("entries") or []) if isinstance(e, dict)]
    entries.sort(key=lambda e: str(e.get("updated_at") or e.get("created_at") or ""), reverse=True)
    return entries[:limit]


# ---------------------------------------------------------------------------
# Research Mission
# ---------------------------------------------------------------------------
def create_research_mission(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    question = str(payload.get("question") or "").strip()
    reason = payload.get("reason") or payload.get("why")
    if not question:
        raise ValueError("question required")
    if not _known(reason):
        raise ValueError("reason required — why this research matters")

    required = payload.get("required_evidence") or payload.get("evidence_required")
    if not required:
        raise ValueError("required_evidence required")
    if isinstance(required, str):
        required = [required]

    status = str(payload.get("status") or ST_CREATED).upper()
    if status not in MISSION_STATES:
        status = ST_CREATED

    now = _utc()
    mid = payload.get("mission_id") or _sid("rmx", f"{question}|{payload.get('opportunity_id')}|{now}")
    record = {
        "kind": "M3ResearchMissionExec",
        "mission_id": mid,
        "related_opportunity": payload.get("related_opportunity") or payload.get("opportunity_id") or "UNKNOWN",
        "related_product": payload.get("related_product") or payload.get("product") or "UNKNOWN",
        "related_supplier": payload.get("related_supplier") or payload.get("supplier") or "UNKNOWN",
        "question": question,
        "reason": reason,
        "required_evidence": required,
        "status": status,
        "created_from": payload.get("created_from") or "manual",
        "outcome": payload.get("outcome") if _known(payload.get("outcome")) else "UNKNOWN",
        "unknowns_resolved": payload.get("unknowns_resolved") or [],
        "linked_action_ids": payload.get("linked_action_ids") or [],
        "task_ids": payload.get("task_ids") or [],
        "ai_autonomous": False,
        "ai_stage_default": 0,
        "ai_stage5_requires_approval": True,
        "no_automatic_outreach": True,
        "created_at": now,
        "updated_at": now,
        "build": BUILD_TAG,
    }
    out = _upsert(MISSION_INDEX, "mission_id", record, persist=persist)

    # Optional starter tasks
    for t in payload.get("tasks") or []:
        if isinstance(t, dict):
            task = create_research_task(
                {**t, "mission_id": mid, "owner": t.get("owner") or "human"},
                persist=persist,
            )
            out["task_ids"] = list(out.get("task_ids") or []) + [task["task_id"]]
    if out.get("task_ids") and persist:
        _upsert(MISSION_INDEX, "mission_id", out, persist=True)

    # Link to Action Orchestration when requested
    if persist and payload.get("create_action", True):
        action_id = _link_action_for_mission(out, persist=persist)
        if action_id:
            out["linked_action_ids"] = list(dict.fromkeys(list(out.get("linked_action_ids") or []) + [action_id]))
            _upsert(MISSION_INDEX, "mission_id", out, persist=True)

    return out


def get_research_mission(mission_id: str) -> dict[str, Any] | None:
    return _get(MISSION_INDEX, mission_id)


def list_research_missions(
    *,
    opportunity_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    entries = _list_entries(MISSION_INDEX, limit=200)
    if opportunity_id:
        entries = [e for e in entries if e.get("related_opportunity") == opportunity_id]
    if status:
        entries = [e for e in entries if e.get("status") == status]
    return {
        "kind": "M3ResearchMissionExecList",
        "missions": entries[:limit],
        "no_autonomous_agent": True,
        "build": BUILD_TAG,
    }


def _link_action_for_mission(mission: dict[str, Any], *, persist: bool) -> str | None:
    try:
        from m3_action_orchestration_read import create_action

        action = create_action(
            {
                "action_type": "RESEARCH",
                "title": mission.get("question"),
                "description": mission.get("reason"),
                "trigger_source": "unknown_created",
                "related_opportunity": mission.get("related_opportunity"),
                "evidence_caused_by": f"research_mission:{mission.get('mission_id')}",
                "evidence_requirements": mission.get("required_evidence") or [],
                "completion_criteria": list(mission.get("required_evidence") or []) + ["unknowns_recorded"],
                "created_by": "research_execution",
            },
            persist=persist,
        )
        return action.get("action_id")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Research Task
# ---------------------------------------------------------------------------
def create_research_task(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    mission_id = payload.get("mission_id")
    question = str(payload.get("question") or "").strip()
    if not _known(mission_id):
        raise ValueError("mission_id required")
    if not question:
        raise ValueError("question required")

    evidence_required = payload.get("evidence_required") or payload.get("required_evidence")
    if not evidence_required:
        raise ValueError("evidence_required required")
    if isinstance(evidence_required, str):
        evidence_required = [evidence_required]

    source_type = payload.get("source_type") or "Unknown"
    if source_type not in SOURCE_TYPES:
        source_type = "Unknown"

    status = str(payload.get("status") or ST_CREATED).upper()
    if status not in MISSION_STATES:
        status = ST_CREATED

    now = _utc()
    tid = payload.get("task_id") or _sid("rtx", f"{mission_id}|{question}|{now}")
    record = {
        "kind": "M3ResearchTaskExec",
        "task_id": tid,
        "mission_id": mission_id,
        "question": question,
        "source_type": source_type,
        "instructions": payload.get("instructions")
        or "Collect required evidence; keep UNKNOWN explicit; no autonomous browsing",
        "evidence_required": evidence_required,
        "owner": payload.get("owner") or "human",
        "status": status,
        "ai_may_assist": bool(payload.get("ai_may_assist", False)),
        "ai_max_stage": int(payload.get("ai_max_stage") or 1),
        "created_at": now,
        "updated_at": now,
        "build": BUILD_TAG,
    }
    out = _upsert(TASK_INDEX, "task_id", record, persist=persist)

    # Attach task id onto mission
    mission = get_research_mission(str(mission_id))
    if mission and persist:
        tids = list(mission.get("task_ids") or [])
        if tid not in tids:
            tids.append(tid)
            mission["task_ids"] = tids
            if mission.get("status") == ST_CREATED:
                mission["status"] = ST_READY
            mission["updated_at"] = _utc()
            _upsert(MISSION_INDEX, "mission_id", mission, persist=True)
    return out


def get_research_task(task_id: str) -> dict[str, Any] | None:
    return _get(TASK_INDEX, task_id)


# ---------------------------------------------------------------------------
# Research Source Record
# ---------------------------------------------------------------------------
def create_research_source(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    source = payload.get("source")
    if not _known(source):
        raise ValueError("source required")
    mission_id = payload.get("related_mission") or payload.get("mission_id")
    if not _known(mission_id):
        raise ValueError("related_mission required")

    source_type = payload.get("source_type") or "Unknown"
    if source_type not in SOURCE_TYPES:
        source_type = "Unknown"

    now = _utc()
    sid = payload.get("source_id") or _sid("rsx", f"{source}|{mission_id}|{now}")
    record = {
        "kind": "M3ResearchSourceExec",
        "source_id": sid,
        "source": source,
        "source_type": source_type,
        "related_mission": mission_id,
        "evidence_created": payload.get("evidence_created") or [],
        "reliability_notes": payload.get("reliability_notes")
        if _known(payload.get("reliability_notes"))
        else "UNKNOWN",
        "created_at": now,
        "updated_at": now,
        "build": BUILD_TAG,
    }
    if isinstance(record["evidence_created"], str):
        record["evidence_created"] = [record["evidence_created"]]

    out = _upsert(SOURCE_INDEX, "source_id", record, persist=persist)

    # Advance mission when evidence appears
    mission = get_research_mission(str(mission_id))
    if mission and record["evidence_created"] and persist:
        if mission.get("status") in {ST_CREATED, ST_READY, ST_ACTIVE, ST_WAITING}:
            mission["status"] = ST_EVIDENCE_FOUND
            mission["updated_at"] = _utc()
            _upsert(MISSION_INDEX, "mission_id", mission, persist=True)
    return out


# ---------------------------------------------------------------------------
# Research Outcome
# ---------------------------------------------------------------------------
def create_research_outcome(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    mission_id = payload.get("mission_id")
    if not _known(mission_id):
        raise ValueError("mission_id required")

    now = _utc()
    oid = payload.get("outcome_id") or _sid("rox", f"{mission_id}|{now}")
    knowns = payload.get("knowns") or []
    unknowns = payload.get("unknowns_remaining")
    if unknowns is None:
        unknowns = ["UNKNOWN"]
    if isinstance(knowns, str):
        knowns = [knowns]
    if isinstance(unknowns, str):
        unknowns = [unknowns]

    record = {
        "kind": "M3ResearchOutcomeExec",
        "outcome_id": oid,
        "mission_id": mission_id,
        "knowns": knowns or ["UNKNOWN"],
        "unknowns_remaining": unknowns,
        "evidence_added": payload.get("evidence_added") or [],
        "records_updated": payload.get("records_updated") or [],
        "lessons": payload.get("lessons") if _known(payload.get("lessons")) else "UNKNOWN",
        "created_at": now,
        "updated_at": now,
        "ai_decided": False,
        "build": BUILD_TAG,
    }
    if isinstance(record["evidence_added"], str):
        record["evidence_added"] = [record["evidence_added"]]
    if isinstance(record["records_updated"], str):
        record["records_updated"] = [record["records_updated"]]

    out = _upsert(OUTCOME_INDEX, "outcome_id", record, persist=persist)

    mission = get_research_mission(str(mission_id))
    if mission and persist:
        resolved = [k for k in knowns if _known(k) and k != "UNKNOWN"]
        mission["outcome"] = out
        mission["unknowns_resolved"] = list(mission.get("unknowns_resolved") or []) + resolved
        remaining = [u for u in unknowns if str(u).upper() != "UNKNOWN" and _known(u)]
        if remaining:
            mission["status"] = ST_REVIEW
        else:
            mission["status"] = ST_COMPLETE
        mission["updated_at"] = _utc()
        _upsert(MISSION_INDEX, "mission_id", mission, persist=True)
    return out


# ---------------------------------------------------------------------------
# Supply Intelligence integration — unknowns → missions
# ---------------------------------------------------------------------------
def create_missions_from_supply_unknowns(
    *,
    opportunity_id: str,
    unknowns: list[Any] | None = None,
    product: Any = "UNKNOWN",
    supplier: Any = "UNKNOWN",
    persist: bool = True,
) -> list[dict[str, Any]]:
    created = []
    for u in unknowns or []:
        text = str(u).lower()
        if text in ("unknown", "") or "none listed" in text:
            continue
        tmpl = None
        for key, spec in _UNKNOWN_MISSION_TEMPLATES:
            if key in text:
                tmpl = spec
                break
        if not tmpl:
            tmpl = {
                "question": f"Resolve: {str(u)[:100]}",
                "reason": "Supply intelligence gap",
                "required_evidence": ["Documented finding or explicit UNKNOWN"],
                "tasks": [
                    {
                        "question": str(u)[:100],
                        "source_type": "Unknown",
                        "instructions": "Collect evidence; do not invent facts",
                        "evidence_required": ["Documented finding or explicit UNKNOWN"],
                    }
                ],
            }
        mission = create_research_mission(
            {
                "question": tmpl["question"],
                "reason": tmpl["reason"],
                "required_evidence": tmpl["required_evidence"],
                "opportunity_id": opportunity_id,
                "related_product": product,
                "related_supplier": supplier,
                "created_from": f"supply_unknown:{u}",
                "tasks": tmpl.get("tasks") or [],
                "status": ST_READY,
            },
            persist=persist,
        )
        created.append(mission)
    return created


def missions_from_opportunity_row(row: dict[str, Any] | None, *, persist: bool = False) -> list[dict[str, Any]]:
    """Derive mission suggestions from supply status — persist only when requested."""
    row = row if isinstance(row, dict) else {}
    cid = row.get("canonical_id")
    if not _known(cid):
        return []
    try:
        from m3_supply_intelligence_read import derive_supply_from_row

        view = derive_supply_from_row(row)
        st = view.get("supply_status") or {}
        unknowns = st.get("unknowns_remaining") or []
        product = (view.get("derived_product") or {}).get("name") or "UNKNOWN"
        supplier = (view.get("derived_suppliers") or ["UNKNOWN"])[0]
        return create_missions_from_supply_unknowns(
            opportunity_id=str(cid),
            unknowns=unknowns,
            product=product,
            supplier=supplier,
            persist=persist,
        )
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Human OS Research Workspace enrichment
# ---------------------------------------------------------------------------
def research_workspace_for_mission(mission_id: str | None = None, *, opportunity_id: str | None = None) -> dict[str, Any]:
    mission = get_research_mission(mission_id) if mission_id else None
    if not mission and opportunity_id:
        listed = list_research_missions(opportunity_id=opportunity_id, limit=1)
        missions = listed.get("missions") or []
        mission = missions[0] if missions else None

    if not mission:
        return {
            "kind": "M3ResearchWorkspaceExec",
            "question": "UNKNOWN",
            "why_it_matters": "UNKNOWN",
            "evidence_needed": ["UNKNOWN"],
            "tasks": [],
            "progress": "No research mission yet",
            "unknowns_remaining": ["UNKNOWN"],
            "build": BUILD_TAG,
        }

    tasks = []
    for tid in mission.get("task_ids") or []:
        t = get_research_task(str(tid))
        if t:
            tasks.append(
                {
                    "task_id": t.get("task_id"),
                    "question": t.get("question"),
                    "evidence_required": t.get("evidence_required"),
                    "status": t.get("status"),
                    "owner": t.get("owner"),
                }
            )

    outcomes = [o for o in _list_entries(OUTCOME_INDEX, limit=20) if o.get("mission_id") == mission.get("mission_id")]
    unknowns_remaining = ["UNKNOWN"]
    if outcomes:
        unknowns_remaining = outcomes[0].get("unknowns_remaining") or ["UNKNOWN"]

    return {
        "kind": "M3ResearchWorkspaceExec",
        "mission_id": mission.get("mission_id"),
        "question": mission.get("question"),
        "why_it_matters": mission.get("reason"),
        "evidence_needed": mission.get("required_evidence") or ["UNKNOWN"],
        "tasks": tasks,
        "progress": mission.get("status"),
        "unknowns_remaining": unknowns_remaining,
        "linked_actions": mission.get("linked_action_ids") or [],
        "build": BUILD_TAG,
    }


# ---------------------------------------------------------------------------
# Profile / attach / command center
# ---------------------------------------------------------------------------
def build_research_execution_profile(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    cid = row.get("canonical_id")
    missions = list_research_missions(opportunity_id=str(cid), limit=15) if cid else list_research_missions(limit=10)
    suggested = missions_from_opportunity_row(row, persist=False) if cid else []
    return {
        "kind": "M3ResearchExecutionProfile",
        "build": BUILD_TAG,
        "opportunity_id": cid or "UNKNOWN",
        "workflow": [
            "Unknown",
            "Research Mission",
            "Research Tasks",
            "Evidence Collection",
            "Validation",
            "Record Update",
            "Memory",
        ],
        "missions": missions.get("missions") or [],
        "suggested_from_supply": [
            {"question": m.get("question"), "reason": m.get("reason"), "required_evidence": m.get("required_evidence")}
            for m in suggested[:5]
        ],
        "research_workspace": research_workspace_for_mission(opportunity_id=str(cid) if cid else None),
        "ai_controls": {
            "autonomous_agent": False,
            "stages": {0: "rules", 1: "cheap extraction", 2: "document understanding", 3: "targeted research", 4: "complex analysis", 5: "explicit approval only"},
            "cost_governor_tiers": list(TIER_ORDER),
            "default_cost_state": COST_FREE,
            "no_unlimited_search": True,
            "no_automatic_outreach": True,
        },
        "not_a_second_source_of_truth": True,
        "integrates_supply_intelligence": True,
        "integrates_action_orchestration": True,
        "integrates_human_os": True,
        "unknown_preserved": True,
        "engines_unchanged": True,
        "generated_at": _utc(),
        "read_only": True,
    }


def attach_research_execution_to_deal_room(
    deal: dict[str, Any], *, row: dict[str, Any] | None = None
) -> dict[str, Any]:
    if not isinstance(deal, dict):
        return deal
    out = dict(deal)
    try:
        out["research_execution"] = build_research_execution_profile(
            row or {"canonical_id": deal.get("canonical_id")}
        )
    except Exception:
        out["research_execution"] = {
            "kind": "M3ResearchExecutionProfile",
            "build": BUILD_TAG,
            "error": "research_execution_unavailable",
            "read_only": True,
        }
    return out


def enrich_command_center_research_execution(
    sections: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    period: str,
) -> dict[str, Any]:
    out = dict(sections or {})
    missions = _list_entries(MISSION_INDEX, limit=40)
    outcomes = _list_entries(OUTCOME_INDEX, limit=20)
    sources = _list_entries(SOURCE_INDEX, limit=20)

    waiting = []
    evidence_needed = []
    blocked = []
    completed = []
    evidence_created = []
    knowledge_added = []

    for m in missions:
        item = {
            "mission_id": m.get("mission_id"),
            "question": m.get("question"),
            "status": m.get("status"),
            "opportunity_id": m.get("related_opportunity"),
            "reason": m.get("reason"),
        }
        st = m.get("status")
        if st in {ST_CREATED, ST_READY, ST_WAITING, ST_ACTIVE}:
            waiting.append(item)
            evidence_needed.append(
                {
                    "mission_id": m.get("mission_id"),
                    "evidence": m.get("required_evidence"),
                    "question": m.get("question"),
                }
            )
        if st == ST_BLOCKED:
            blocked.append(item)
        if st == ST_COMPLETE:
            completed.append(item)

    for s in sources:
        if s.get("evidence_created"):
            evidence_created.append(
                {
                    "source": s.get("source"),
                    "evidence": s.get("evidence_created"),
                    "mission_id": s.get("related_mission"),
                }
            )

    for o in outcomes:
        knowledge_added.append(
            {
                "mission_id": o.get("mission_id"),
                "knowns": o.get("knowns"),
                "lessons": o.get("lessons"),
            }
        )

    # Suggest from active rows (read-only hints)
    for r in (rows or [])[:10]:
        if not isinstance(r, dict):
            continue
        for m in missions_from_opportunity_row(r, persist=False)[:2]:
            waiting.append(
                {
                    "opportunity_id": r.get("canonical_id"),
                    "question": m.get("question"),
                    "status": "SUGGESTED",
                    "reason": m.get("reason"),
                }
            )

    if period == "morning":
        out["research_missions_waiting"] = waiting[:15]
        out["research_evidence_needed"] = evidence_needed[:15]
        out["blocked_research_missions"] = blocked[:15]
    else:
        out["research_missions_completed"] = completed[:15]
        out["research_evidence_created"] = evidence_created[:15]
        out["research_knowledge_added"] = knowledge_added[:15]
    return out
