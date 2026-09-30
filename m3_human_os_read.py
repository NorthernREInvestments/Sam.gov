"""BUILD 23 — M3 Human Operating System (read models / operator UX).

Complex intelligence engine → simple human workflow.

Purpose: a person with no government contracting experience can learn and
operate M3 in 15–30 minutes. Hides knowledge graphs, retrieval internals,
governance jargon, and AI staging behind plain-language workspaces.

Does NOT replace databases, invent facts, score, rank, or decide for humans.
AI assists via staged escalation (0–5); humans decide. UNKNOWN stays UNKNOWN.

Views over existing layers — does not duplicate entity truth.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from application_clock import now_utc
from cost_governor_constants import (
    ACTION_AI_COMPLETION,
    COST_FREE,
    COST_REUSED_EVIDENCE,
    TIER_ORDER,
)

BUILD_TAG = "20260919-m3-human-operating-system-1"

WORKFLOW_STEPS = (
    {"step": 1, "key": "find", "label": "Find opportunities", "plain": "See what the government is buying that might fit."},
    {"step": 2, "key": "understand", "label": "Understand the opportunity", "plain": "Learn what is being asked for and who wants it."},
    {"step": 3, "key": "research", "label": "Research suppliers/products", "plain": "Find who can supply the product and what we know about it."},
    {"step": 4, "key": "evidence", "label": "Review evidence", "plain": "Check the facts and sources before deciding."},
    {"step": 5, "key": "decide", "label": "Make decision", "plain": "A person chooses what to do next — M3 never decides alone."},
    {"step": 6, "key": "execute", "label": "Execute", "plain": "Carry out commitments and track progress."},
    {"step": 7, "key": "learn", "label": "Capture lessons", "plain": "Record what happened so the next opportunity is easier."},
)

ROLE_BEGINNER = "Beginner User"
ROLE_RESEARCHER = "Researcher"
ROLE_REVIEWER = "Reviewer"
ROLE_MANAGER = "Manager"
ROLE_ADMIN = "Administrator"
USER_ROLES = (ROLE_BEGINNER, ROLE_RESEARCHER, ROLE_REVIEWER, ROLE_MANAGER, ROLE_ADMIN)

AI_STAGES = {
    0: {"name": "Deterministic rules", "uses_ai": False, "note": "No AI — rules and existing evidence only"},
    1: {"name": "Low-cost triage", "uses_ai": True, "note": "Cheap AI at scale"},
    2: {"name": "Targeted extraction", "uses_ai": True, "note": "Pull structured facts from documents"},
    3: {"name": "Focused research", "uses_ai": True, "note": "Directed research with optional web"},
    4: {"name": "Deep analysis", "uses_ai": True, "note": "Heavier reasoning for complex cases"},
    5: {"name": "Premium reasoning", "uses_ai": True, "note": "Highest-value cases only — explicit user action required"},
}

AI_USER_ACTIONS = {
    "analyze_this": {"label": "Analyze this", "default_stage": 1, "max_auto_stage": 2, "escalation_point": "Stage 3+ needs explicit research intent"},
    "find_missing_information": {"label": "Find missing information", "default_stage": 0, "max_auto_stage": 1, "escalation_point": "Gaps first from deterministic scan; AI only if cache miss"},
    "explain_this_opportunity": {"label": "Explain this opportunity", "default_stage": 1, "max_auto_stage": 2, "escalation_point": "Stage 4 only if evidence packet is large/complex"},
    "show_me_what_matters": {"label": "Show me what matters", "default_stage": 0, "max_auto_stage": 1, "escalation_point": "Prefer Stage 0 attention list; AI optional summary"},
    "prepare_review": {"label": "Prepare review", "default_stage": 1, "max_auto_stage": 3, "escalation_point": "Stage 3 if sources need retrieval"},
    "find_next_steps": {"label": "Find next steps", "default_stage": 0, "max_auto_stage": 1, "escalation_point": "Deterministic action orchestration first"},
}

BEGINNER_GLOSSARY = {
    "Master Record Conflict": {
        "plain": "There are two different pieces of information about this supplier. Review which one is correct.",
        "why_matters": "Acting on the wrong detail can waste time or create a bad offer.",
    },
    "Knowledge Gap": {
        "plain": "We are missing information needed to move forward.",
        "why_matters": "Without this answer, later steps stay blocked.",
    },
    "Evidence Packet": {
        "plain": "A folder of facts and sources gathered for this question.",
        "why_matters": "Decisions should rest on evidence, not guesses.",
    },
    "Action Orchestration": {
        "plain": "The list of work items M3 created from what we know and don’t know.",
        "why_matters": "Shows what needs done next and why.",
    },
    "Strategic Intelligence": {
        "plain": "Patterns we’ve noticed across opportunities — observations, not predictions.",
        "why_matters": "Helps you see the bigger picture without scoring or ranking.",
    },
    "UNKNOWN": {
        "plain": "We do not have this answer yet — and we will not invent one.",
        "why_matters": "Honest gaps become tasks instead of fake certainty.",
    },
    "Retrieval Session": {
        "plain": "A search for answers in trusted outside systems.",
        "why_matters": "M3 looks up information; it does not store the whole world.",
    },
    "Capability Gap": {
        "plain": "Something we have not yet demonstrated we can do.",
        "why_matters": "Points to research that may unlock more opportunities.",
    },
}

REVIEW_TYPES = (
    "Opportunity Review",
    "Supplier Review",
    "Product Review",
    "Economic Review",
    "Execution Review",
)

ONBOARDING_STEPS = (
    {"n": 1, "title": "Opportunities appear here", "body": "Government buy opportunities show up on Home. You don’t need to hunt databases."},
    {"n": 2, "title": "M3 gathers evidence", "body": "M3 finds facts from trusted sources and keeps track of what is still unknown."},
    {"n": 3, "title": "Unknowns become tasks", "body": "Missing information turns into clear tasks — what to do, why it matters, what evidence is needed."},
    {"n": 4, "title": "Humans make decisions", "body": "AI can help organize and research. People choose what to pursue."},
    {"n": 5, "title": "Lessons improve future work", "body": "When work finishes, capture what you learned so the next opportunity is easier."},
)

COST_INDEX = "m3_human_os_ai_cost_v1"
ONBOARD_INDEX = "m3_human_os_onboarding_v1"


def _utc() -> str:
    return now_utc().isoformat()


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _known(v: Any) -> bool:
    return v not in (None, "", "UNKNOWN")


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


def plain_label(technical: str) -> dict[str, Any]:
    entry = BEGINNER_GLOSSARY.get(technical)
    if entry:
        return {"technical": technical, "plain": entry["plain"], "why_matters": entry["why_matters"]}
    return {"technical": technical, "plain": technical, "why_matters": "Review the evidence before acting."}


def build_home_today(
    rows: list[dict[str, Any]] | None = None,
    *,
    period: str = "morning",
) -> dict[str, Any]:
    rows = rows or []
    actions_attention: list[dict[str, Any]] = []
    research_needed: list[dict[str, Any]] = []
    decisions_waiting: list[dict[str, Any]] = []
    new_opps: list[dict[str, Any]] = []
    important_changes: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []

    try:
        from m3_action_orchestration_read import AT_DECISION_REVIEW, AT_RESEARCH, ST_BLOCKED, list_actions

        for a in list_actions(limit=40):
            item = {
                "what": a.get("title"),
                "why": a.get("why_exists") or a.get("description"),
                "opportunity": a.get("related_opportunity"),
                "status": a.get("status"),
                "action_id": a.get("action_id"),
            }
            if a.get("status") == ST_BLOCKED:
                blocked.append(item)
            elif a.get("action_type") == AT_DECISION_REVIEW:
                decisions_waiting.append(item)
            elif a.get("action_type") == AT_RESEARCH:
                research_needed.append(item)
            else:
                actions_attention.append(item)
    except Exception:
        pass

    for r in rows[:20]:
        if not isinstance(r, dict):
            continue
        cid = r.get("canonical_id")
        title = (r.get("title") or "")[:100] or "Opportunity"
        new_opps.append({"what": title, "opportunity": cid, "why": "Active in pipeline"})
        spg = _as_dict(r.get("supplier_product_graph"))
        if not (spg.get("edges") or []):
            research_needed.append(
                {
                    "what": "Find who can supply this product",
                    "why": "We do not yet have supplier evidence for this opportunity.",
                    "opportunity": cid,
                    "status": "OPEN",
                }
            )

    if period == "evening":
        return {
            "kind": "M3HomeToday",
            "period": "evening",
            "headline": "End of day",
            "questions": [
                "What did we finish?",
                "What new information did we create?",
                "What did we learn?",
                "What is still open?",
            ],
            "completed_work": actions_attention[:8],
            "new_intelligence": important_changes[:8],
            "lessons_learned": [],
            "outstanding_issues": blocked[:10] + research_needed[:5],
            "plain_language": True,
            "hides_architecture": True,
            "build": BUILD_TAG,
        }

    return {
        "kind": "M3HomeToday",
        "period": "morning",
        "headline": "Today",
        "questions": [
            "What needs my attention?",
            "What do I do next?",
            "Why am I doing it?",
            "What evidence supports this?",
            "What did we learn?",
        ],
        "actions_requiring_attention": actions_attention[:12],
        "research_needing_completion": research_needed[:12],
        "decisions_waiting": decisions_waiting[:12],
        "new_opportunities": new_opps[:12],
        "important_changes": important_changes[:12],
        "blocked_items": blocked[:12],
        "plain_language": True,
        "hides_architecture": True,
        "no_raw_intelligence_objects": True,
        "build": BUILD_TAG,
        "generated_at": _utc(),
    }


def infer_workflow_step(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    has_id = _known(row.get("canonical_id"))
    has_product = bool(_as_dict(_as_dict(row.get("dla_product_structure")).get("fields")).get("nsn")) or _known(
        row.get("title")
    )
    has_supplier = bool((_as_dict(row.get("supplier_product_graph")).get("edges") or []))
    step = 1
    if has_id:
        step = 2
    if has_product:
        step = 3
    if has_supplier:
        step = 4
    if _known(row.get("award_id") or row.get("contract_number")):
        step = 6
    current = WORKFLOW_STEPS[min(step, 7) - 1]
    return {
        "kind": "M3GuidedWorkflow",
        "current_step": current["step"],
        "current_key": current["key"],
        "current_label": current["label"],
        "plain": current["plain"],
        "all_steps": list(WORKFLOW_STEPS),
        "you_are_here": f"Step {current['step']} of 7 — {current['label']}",
        "build": BUILD_TAG,
    }


def build_opportunity_workspace(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    cid = row.get("canonical_id") or "UNKNOWN"
    title = row.get("title") or "UNKNOWN"
    buyer = row.get("buyer") or row.get("agency") or "UNKNOWN"
    nsn = _as_dict(_as_dict(_as_dict(row.get("dla_product_structure")).get("fields")).get("nsn")).get("value") or "UNKNOWN"
    suppliers = []
    for e in (_as_dict(row.get("supplier_product_graph")).get("edges") or [])[:6]:
        if isinstance(e, dict) and _known(e.get("supplier_name") or e.get("company")):
            suppliers.append(e.get("supplier_name") or e.get("company"))
    if not suppliers:
        suppliers = ["UNKNOWN"]

    deadline_ctx = None
    try:
        from deadline_runtime import deadline_context_for_operator

        deadline_ctx = deadline_context_for_operator(row)
    except Exception:
        deadline_ctx = {
            "deadline": row.get("deadline") or "UNKNOWN",
            "deadline_known": bool(row.get("deadline")),
        }

    known = [
        x
        for x in [
            f"Opportunity: {title}" if _known(title) else None,
            f"Buyer: {buyer}" if _known(buyer) else None,
            f"Product NSN: {nsn}" if _known(nsn) else None,
            (
                f"Response deadline: {deadline_ctx.get('deadline')}"
                if deadline_ctx and _known(deadline_ctx.get("deadline"))
                else None
            ),
        ]
        if x
    ]
    unknowns = []
    if not _known(nsn):
        unknowns.append("What exact product is involved?")
    if suppliers == ["UNKNOWN"]:
        unknowns.append("Who can supply it?")
    if not (deadline_ctx or {}).get("deadline_known"):
        unknowns.append("What is the response / closing deadline?")
    if not unknowns:
        unknowns = ["Check for remaining gaps before deciding"]

    next_actions = []
    try:
        from m3_action_orchestration_read import list_actions

        for a in list_actions(opportunity_id=str(cid), limit=5):
            next_actions.append({"what": a.get("title"), "why": a.get("why_exists"), "status": a.get("status")})
    except Exception:
        pass
    if not next_actions and suppliers == ["UNKNOWN"]:
        next_actions.append(
            {
                "what": "Research suppliers/products",
                "why": "We cannot move forward without supplier evidence.",
                "status": "SUGGESTED",
            }
        )

    supply_status = None
    try:
        from m3_supply_intelligence_read import supply_status_for_human_os

        supply_status = supply_status_for_human_os(row)
    except Exception:
        supply_status = None

    return {
        "kind": "M3OpportunityWorkspace",
        "opportunity_id": cid,
        "what_is_this": title,
        "who_needs_it": buyer,
        "what_product": nsn,
        "who_can_supply": suppliers,
        "deadline_context": deadline_ctx,
        "what_we_know": known or ["UNKNOWN"],
        "what_we_do_not_know": unknowns,
        "what_needs_to_happen_next": next_actions,
        "supply_status": supply_status,
        "workflow": infer_workflow_step(row),
        "view_only": True,
        "does_not_duplicate_data": True,
        "build": BUILD_TAG,
    }


def build_supplier_workspace(payload: dict[str, Any] | None = None, *, row: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    row = row if isinstance(row, dict) else {}
    name = payload.get("supplier_name") or "UNKNOWN"
    products = list(payload.get("products") or [])
    evidence = list(payload.get("evidence") or [])
    questions = list(payload.get("questions") or [])
    if row:
        for e in (_as_dict(row.get("supplier_product_graph")).get("edges") or [])[:8]:
            if isinstance(e, dict):
                sn = e.get("supplier_name") or e.get("company")
                if _known(sn) and (name == "UNKNOWN" or sn == name):
                    name = sn
                    evidence.append("supplier_product_graph")
                    if e.get("product") or e.get("nsn"):
                        products.append(e.get("product") or e.get("nsn"))
    if not products:
        products = ["UNKNOWN"]
        questions.append("What products are connected to this supplier?")
    if not evidence:
        evidence = ["UNKNOWN"]
        questions.append("What evidence confirms this supplier?")

    actions = payload.get("actions") or (
        [
            {
                "what": "Confirm supplier availability",
                "why": "This opportunity cannot move forward until we know whether the supplier can provide the product.",
            }
        ]
        if name != "UNKNOWN"
        else [{"what": "Identify a supplier", "why": "No supplier is linked yet."}]
    )

    supply_view = None
    try:
        from m3_supply_intelligence_read import supplier_workspace_supply_view

        supply_view = supplier_workspace_supply_view(payload.get("supplier_id"), row=row)
    except Exception:
        supply_view = None

    return {
        "kind": "M3SupplierWorkspace",
        "who_is_this_supplier": name,
        "what_products_connected": products,
        "what_evidence_exists": evidence,
        "what_questions_remain": questions or ["UNKNOWN"],
        "what_actions_needed": actions,
        "supply_intelligence": supply_view,
        "view_only": True,
        "build": BUILD_TAG,
    }


def build_research_workspace(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    exec_view = None
    try:
        from m3_research_execution_read import research_workspace_for_mission

        exec_view = research_workspace_for_mission(
            payload.get("mission_id"),
            opportunity_id=payload.get("opportunity_id"),
        )
    except Exception:
        exec_view = None

    exec_q = (exec_view or {}).get("question")
    if not _known(exec_q):
        exec_q = None

    base = {
        "kind": "M3ResearchWorkspace",
        "research_question": exec_q or payload.get("question") or "UNKNOWN",
        "why_it_matters": (exec_view or {}).get("why_it_matters")
        if _known((exec_view or {}).get("why_it_matters"))
        else (payload.get("reason") or "UNKNOWN"),
        "evidence_needed": (exec_view or {}).get("evidence_needed")
        if (exec_view or {}).get("evidence_needed")
        else (payload.get("evidence_needed") or ["UNKNOWN"]),
        "tasks": (exec_view or {}).get("tasks") or payload.get("tasks") or [],
        "progress": (exec_view or {}).get("progress")
        if _known((exec_view or {}).get("progress")) and (exec_view or {}).get("progress") != "No research mission yet"
        else (payload.get("progress") or "UNKNOWN"),
        "sources_checked": payload.get("sources_checked") or ["UNKNOWN"],
        "evidence_found": payload.get("evidence_found") or ["UNKNOWN"],
        "unknowns": payload.get("unknowns")
        if payload.get("unknowns") is not None
        else ((exec_view or {}).get("unknowns_remaining") if (exec_view or {}).get("unknowns_remaining") is not None else ["UNKNOWN"]),
        "unknowns_remaining": (exec_view or {}).get("unknowns_remaining")
        if (exec_view or {}).get("unknowns_remaining") is not None
        else (payload.get("unknowns") if payload.get("unknowns") is not None else ["UNKNOWN"]),
        "next_step": payload.get("next_step") or "Document findings or keep UNKNOWN explicit",
        "research_execution": exec_view,
        "view_only": True,
        "build": BUILD_TAG,
    }
    return base


def build_decision_workspace(payload: dict[str, Any] | None = None, *, row: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    row = row if isinstance(row, dict) else {}
    facts = list(payload.get("facts") or [])
    evidence = list(payload.get("evidence") or [])
    unknowns = list(payload.get("unknowns") or [])
    conflicts = list(payload.get("conflicts") or [])
    if row and not facts:
        if _known(row.get("title")):
            facts.append(f"Title: {row.get('title')}")
        if _known(row.get("buyer")):
            facts.append(f"Buyer: {row.get('buyer')}")
    if not unknowns:
        unknowns = ["UNKNOWN"]

    supply_decision = None
    try:
        from m3_supply_intelligence_read import decision_workspace_supply_view

        supply_decision = decision_workspace_supply_view(row, path_id=payload.get("path_id"))
    except Exception:
        supply_decision = None

    return {
        "kind": "M3DecisionWorkspace",
        "facts": facts or ["UNKNOWN"],
        "evidence": evidence or ["UNKNOWN"],
        "unknowns": unknowns,
        "conflicts": conflicts or [],
        "required_human_decisions": payload.get("required_human_decisions")
        or ["A person must choose whether to pursue, research more, or stop — AI does not decide."],
        "supply_path_view": supply_decision,
        "ai_decides": False,
        "humans_decide": True,
        "view_only": True,
        "build": BUILD_TAG,
    }


def build_execution_workspace(payload: dict[str, Any] | None = None, *, row: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    row = row if isinstance(row, dict) else {}
    commitments = list(payload.get("commitments") or [])
    if row and not commitments:
        if _known(row.get("award_id") or row.get("contract_number")):
            commitments.append({"what": "Contract/award present", "id": row.get("award_id") or row.get("contract_number")})
        else:
            commitments.append({"what": "UNKNOWN", "note": "No award commitment evidenced yet"})
    return {
        "kind": "M3ExecutionWorkspace",
        "current_commitments": commitments or [{"what": "UNKNOWN"}],
        "milestones": payload.get("milestones") or ["UNKNOWN"],
        "issues": payload.get("issues") or [],
        "required_actions": payload.get("required_actions")
        or [{"what": "Confirm next execution milestone", "why": "Keep delivery and acceptance evidence current."}],
        "view_only": True,
        "build": BUILD_TAG,
    }


def beginner_mode_pack() -> dict[str, Any]:
    return {
        "kind": "M3BeginnerMode",
        "enabled_by_default_for": ROLE_BEGINNER,
        "glossary": [{**plain_label(k), "key": k} for k in BEGINNER_GLOSSARY],
        "tooltips": {
            "Home": "Start here every day — what needs attention.",
            "Opportunity": "A government purchase request you might fulfill.",
            "Task": "Something a person needs to do, with a clear reason.",
            "Evidence": "Facts with sources — not guesses.",
            "Decision": "A human choice. M3 prepares; people decide.",
        },
        "examples": [plain_label("Master Record Conflict"), plain_label("Knowledge Gap")],
        "build": BUILD_TAG,
    }


def humanize_task(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "M3HumanTask",
        "action_id": action.get("action_id"),
        "what_needs_done": action.get("title") or "Task",
        "why_it_matters": action.get("why_exists")
        or action.get("description")
        or "This work was created from something we know or something we still need.",
        "related_opportunity": action.get("related_opportunity") or "UNKNOWN",
        "required_evidence": action.get("evidence_requirements") or action.get("completion_criteria") or ["UNKNOWN"],
        "who_owns_it": _as_dict(action.get("executor")).get("executor_type") or "human",
        "completion_state": action.get("status") or "UNKNOWN",
        "from_action_orchestration": True,
        "build": BUILD_TAG,
    }


def list_human_tasks(*, opportunity_id: str | None = None, limit: int = 30) -> dict[str, Any]:
    tasks = []
    try:
        from m3_action_orchestration_read import list_actions

        for a in list_actions(opportunity_id=opportunity_id, limit=limit):
            tasks.append(humanize_task(a))
    except Exception:
        pass
    return {
        "kind": "M3HumanTaskList",
        "tasks": tasks,
        "source": "action_orchestration",
        "does_not_duplicate_actions": True,
        "build": BUILD_TAG,
    }


def build_human_review(
    review_type: str, *, payload: dict[str, Any] | None = None, row: dict[str, Any] | None = None
) -> dict[str, Any]:
    payload = payload or {}
    if review_type not in REVIEW_TYPES:
        review_type = "Opportunity Review"
    question = payload.get("question") or {
        "Opportunity Review": "Should we continue researching this opportunity?",
        "Supplier Review": "Is this supplier a viable path for this product?",
        "Product Review": "Is the product identity clear enough to proceed?",
        "Economic Review": "Do we have enough evidenced economics to continue?",
        "Execution Review": "Are commitments and milestones on track?",
    }.get(review_type, "What should we do next?")

    known = list(payload.get("known") or [])
    unknown = payload.get("unknown") if payload.get("unknown") is not None else ["UNKNOWN"]
    evidence = payload.get("evidence") or ["UNKNOWN"]
    if row and not known:
        ws = build_opportunity_workspace(row)
        known = ws.get("what_we_know") or []
        unknown = ws.get("what_we_do_not_know") or ["UNKNOWN"]

    return {
        "kind": "M3HumanReview",
        "review_type": review_type,
        "question": question,
        "evidence": evidence if isinstance(evidence, list) else [evidence],
        "known": known if isinstance(known, list) else [known],
        "unknown": unknown if isinstance(unknown, list) else [unknown],
        "action": payload.get("action") or "UNKNOWN",
        "outcome": payload.get("outcome") or "UNKNOWN",
        "flow": ["Question", "Evidence", "Known", "Unknown", "Action", "Outcome"],
        "ai_decides": False,
        "humans_decide": True,
        "no_scores": True,
        "build": BUILD_TAG,
    }


def plan_ai_user_action(
    action_key: str,
    *,
    opportunity_id: str | None = None,
    force_stage: int | None = None,
    allow_stage5: bool = False,
    prefer_cache: bool = True,
) -> dict[str, Any]:
    spec = AI_USER_ACTIONS.get(action_key)
    if not spec:
        raise ValueError(f"unknown AI action: {action_key}. Allowed: {list(AI_USER_ACTIONS)}")

    stage = force_stage if force_stage is not None else spec["default_stage"]
    stage = max(0, min(5, int(stage)))
    blocked_premium = False
    if stage == 5 and not allow_stage5:
        stage = min(stage, spec["max_auto_stage"])
        blocked_premium = True

    uses_ai = AI_STAGES[stage]["uses_ai"]
    cost_state = COST_REUSED_EVIDENCE if (prefer_cache and stage >= 1) else (COST_FREE if stage == 0 else "PENDING_AUTHORIZATION")

    return {
        "kind": "M3HumanAiActionPlan",
        "action_key": action_key,
        "label": spec["label"],
        "opportunity_id": opportunity_id or "UNKNOWN",
        "funnel_stage": stage,
        "stage_info": AI_STAGES[stage],
        "uses_ai": uses_ai,
        "model_routing": {
            "stage": stage,
            "max_auto_stage": spec["max_auto_stage"],
            "stage5_requires_explicit_user_action": True,
            "premium_blocked_without_allow": blocked_premium,
        },
        "caching": {"prefer_cache": prefer_cache, "policy": "reuse_first" if prefer_cache else "bypass"},
        "cost_tracking": {
            "action_type": ACTION_AI_COMPLETION if uses_ai else "DETERMINISTIC",
            "cost_state": cost_state,
            "tier_order_ref": list(TIER_ORDER),
        },
        "reusable_intelligence": True,
        "escalation_point": spec["escalation_point"],
        "executes_ai_call": False,
        "unnecessary_ai_disallowed": True,
        "humans_decide": True,
        "build": BUILD_TAG,
        "planned_at": _utc(),
    }


def record_ai_workflow_cost(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    entry = {
        "kind": "M3HumanOsAiCost",
        "task": payload.get("task") or payload.get("action_key") or "UNKNOWN",
        "opportunity_id": payload.get("opportunity_id") or "UNKNOWN",
        "funnel_stage": payload.get("funnel_stage"),
        "estimated_cost_usd": payload.get("estimated_cost_usd"),
        "cache_hit": bool(payload.get("cache_hit")),
        "escalation_reason": payload.get("escalation_reason") or "UNKNOWN",
        "cost_state": payload.get("cost_state") or COST_FREE,
        "recorded_at": _utc(),
        "build": BUILD_TAG,
    }
    if persist:
        idx = _load_index(COST_INDEX)
        entries = list(idx.get("entries") or [])
        entries.append(entry)
        idx["entries"] = entries[-400:]
        _save_index(COST_INDEX, idx)
    return entry


def onboarding_walkthrough() -> dict[str, Any]:
    return {
        "kind": "M3OnboardingWalkthrough",
        "goal_minutes": "15–30",
        "steps": list(ONBOARDING_STEPS),
        "core_questions": [
            "What needs my attention?",
            "What do I do next?",
            "Why am I doing it?",
            "What evidence supports this?",
            "What did we learn?",
        ],
        "design_principle": "Complex intelligence engine. Simple human workflow.",
        "not_a_database": True,
        "build": BUILD_TAG,
    }


def complete_onboarding_step(step_n: int, *, user_id: str = "local", persist: bool = True) -> dict[str, Any]:
    rec = {
        "kind": "M3OnboardingProgress",
        "user_id": user_id,
        "completed_step": step_n,
        "completed_at": _utc(),
        "build": BUILD_TAG,
    }
    if persist:
        idx = _load_index(ONBOARD_INDEX)
        by = dict(idx.get("by_key") or {})
        by[f"{user_id}:{step_n}"] = rec
        idx["by_key"] = by
        entries = list(idx.get("entries") or [])
        entries.append(rec)
        idx["entries"] = entries[-200:]
        _save_index(ONBOARD_INDEX, idx)
    return rec


def list_user_roles() -> dict[str, Any]:
    return {
        "kind": "M3UserRoles",
        "roles": [
            {"role": ROLE_BEGINNER, "sees": ["Home", "Beginner Mode", "Guided workflow", "Simple tasks"]},
            {"role": ROLE_RESEARCHER, "sees": ["Research workspace", "Evidence", "AI assist actions"]},
            {"role": ROLE_REVIEWER, "sees": ["Decision workspace", "Review workflows", "Conflicts"]},
            {"role": ROLE_MANAGER, "sees": ["Morning/Evening Command Center", "Cost awareness", "Lessons"]},
            {"role": ROLE_ADMIN, "sees": ["All operator surfaces", "Build version"]},
        ],
        "build": BUILD_TAG,
    }


def build_human_os_profile(row: dict[str, Any] | None = None, *, role: str = ROLE_BEGINNER) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    rows = [row] if row else []
    operator_boards = None
    pursuit_boards = None
    try:
        from m3_opportunity_operating_read import build_operator_workflow_boards

        operator_boards = build_operator_workflow_boards(rows)
    except Exception:
        operator_boards = None
    try:
        from m3_pursuit_readiness_read import build_pursuit_operator_boards

        pursuit_boards = build_pursuit_operator_boards(rows)
    except Exception:
        pursuit_boards = None
    next_hour_queue = None
    try:
        from m3_next_hour_queue_read import build_next_hour_queue

        next_hour_queue = build_next_hour_queue(rows)
    except Exception:
        next_hour_queue = None
    operator_loop_boards = None
    try:
        from m3_operator_loop_read import build_operator_loop_boards

        operator_loop_boards = build_operator_loop_boards(rows)
    except Exception:
        operator_loop_boards = None
    return {
        "kind": "M3HumanOperatingSystemProfile",
        "build": BUILD_TAG,
        "role": role if role in USER_ROLES else ROLE_BEGINNER,
        "home_today": build_home_today(rows, period="morning"),
        "workflow": infer_workflow_step(row),
        "opportunity_workspace": build_opportunity_workspace(row) if row.get("canonical_id") else None,
        "operator_workflow": operator_boards,
        "pursuit_readiness_boards": pursuit_boards,
        "next_hour_queue": next_hour_queue,
        "operator_loop_boards": operator_loop_boards,
        "beginner_mode": beginner_mode_pack() if (role == ROLE_BEGINNER or True) else {"enabled": False},
        "ai_actions": [{"key": k, "label": v["label"], "default_stage": v["default_stage"]} for k, v in AI_USER_ACTIONS.items()],
        "ai_stages": AI_STAGES,
        "onboarding": onboarding_walkthrough(),
        "roles": list_user_roles(),
        "not_a_database_replacement": True,
        "hides_internal_complexity": True,
        "facts_only": True,
        "unknown_preserved": True,
        "ai_assists_humans_decide": True,
        "staged_ai_escalation_preserved": True,
        "no_scores": True,
        "no_rankings": True,
        "no_predictions": True,
        "engines_unchanged": True,
        "generated_at": _utc(),
        "read_only": True,
    }


def attach_human_os_to_deal_room(deal: dict[str, Any], *, row: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(deal, dict):
        return deal
    out = dict(deal)
    try:
        out["human_os"] = build_human_os_profile(row or {"canonical_id": deal.get("canonical_id")})
    except Exception:
        out["human_os"] = {"kind": "M3HumanOperatingSystemProfile", "build": BUILD_TAG, "error": "human_os_unavailable", "read_only": True}
    return out


def enrich_command_center_human_os(
    sections: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    period: str,
) -> dict[str, Any]:
    out = dict(sections or {})
    today = build_home_today(rows, period=period)
    if period == "morning":
        out["what_changed"] = today.get("important_changes") or []
        out["what_needs_attention"] = today.get("actions_requiring_attention") or []
        out["what_is_blocked"] = today.get("blocked_items") or []
        out["what_decisions_waiting"] = today.get("decisions_waiting") or []
        out["human_os_today"] = today
    else:
        out["completed_work"] = today.get("completed_work") or []
        out["new_intelligence_created"] = today.get("new_intelligence") or []
        out["lessons_learned"] = today.get("lessons_learned") or []
        out["outstanding_issues"] = today.get("outstanding_issues") or []
        out["human_os_today"] = today
    return out
