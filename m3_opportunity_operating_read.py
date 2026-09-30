"""BUILD 26 — Opportunity Operating Integration (lifecycle + Deal Room assembly).

Connects existing M3 modules into one opportunity-centered operating workflow.

Does NOT rebuild intelligence modules, create parallel stores, scoring,
win prediction, autonomous decisions, or bypass AI cost controls.

Operator lifecycle (deterministic, evidence-based):
NEW -> SCREENED -> UNDERSTANDING -> PRODUCT_RESEARCH -> SUPPLY_RESEARCH
  -> COMMERCIAL_VALIDATION -> DECISION_READY -> EXECUTION -> COMPLETE -> LEARNED
"""

from __future__ import annotations

import logging
from typing import Any

from application_clock import now_utc

log = logging.getLogger("govtracker.m3_opportunity_operating")

BUILD_TAG = "20260919-m3-opportunity-operating-1"

OP_NEW = "NEW"
OP_SCREENED = "SCREENED"
OP_UNDERSTANDING = "UNDERSTANDING"
OP_PRODUCT_RESEARCH = "PRODUCT_RESEARCH"
OP_SUPPLY_RESEARCH = "SUPPLY_RESEARCH"
OP_COMMERCIAL_VALIDATION = "COMMERCIAL_VALIDATION"
OP_DECISION_READY = "DECISION_READY"
OP_EXECUTION = "EXECUTION"
OP_COMPLETE = "COMPLETE"
OP_LEARNED = "LEARNED"

OPERATOR_LIFECYCLE_STATES = (
    OP_NEW,
    OP_SCREENED,
    OP_UNDERSTANDING,
    OP_PRODUCT_RESEARCH,
    OP_SUPPLY_RESEARCH,
    OP_COMMERCIAL_VALIDATION,
    OP_DECISION_READY,
    OP_EXECUTION,
    OP_COMPLETE,
    OP_LEARNED,
)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    OP_NEW: frozenset({OP_SCREENED, OP_UNDERSTANDING}),
    OP_SCREENED: frozenset({OP_UNDERSTANDING, OP_PRODUCT_RESEARCH}),
    OP_UNDERSTANDING: frozenset({OP_PRODUCT_RESEARCH, OP_SUPPLY_RESEARCH}),
    OP_PRODUCT_RESEARCH: frozenset({OP_SUPPLY_RESEARCH, OP_COMMERCIAL_VALIDATION}),
    OP_SUPPLY_RESEARCH: frozenset({OP_COMMERCIAL_VALIDATION, OP_DECISION_READY}),
    OP_COMMERCIAL_VALIDATION: frozenset({OP_DECISION_READY, OP_SUPPLY_RESEARCH}),
    OP_DECISION_READY: frozenset({OP_EXECUTION, OP_SUPPLY_RESEARCH, OP_COMMERCIAL_VALIDATION}),
    OP_EXECUTION: frozenset({OP_COMPLETE, OP_DECISION_READY}),
    OP_COMPLETE: frozenset({OP_LEARNED}),
    OP_LEARNED: frozenset(),
}


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
    return {
        "claim": claim if _known(claim) else "UNKNOWN",
        "evidence": evidence if _known(evidence) or isinstance(evidence, (list, dict)) else "UNKNOWN",
        "source": source if _known(source) else "UNKNOWN",
        "date": date or "UNKNOWN",
        "context": context if _known(context) else "UNKNOWN",
        "unsupported_conclusion": False,
    }


def _has_product_evidence(row: dict[str, Any]) -> bool:
    nsn = _as_dict(_as_dict(_as_dict(row.get("dla_product_structure")).get("fields")).get("nsn")).get("value")
    if _known(nsn):
        return True
    if row.get("bom") or row.get("line_items"):
        return True
    pc = str(row.get("product_classification") or "").upper()
    return bool(pc and pc not in {"UNKNOWN", "NONE", ""})


def _has_supply_evidence(row: dict[str, Any]) -> bool:
    if _as_dict(row.get("supplier_product_graph")).get("edges"):
        return True
    try:
        from m3_supply_intelligence_read import derive_supply_from_row

        return bool((derive_supply_from_row(row).get("supply_status") or {}).get("supplier_identified"))
    except Exception:
        return False


def _has_commercial_evidence(row: dict[str, Any]) -> bool:
    econ = _as_dict(row.get("transaction_economics") or row.get("economics"))
    if row.get("bid_pricing") or any(
        _known(econ.get(k)) for k in ("acquisition", "revenue", "supported_profit", "expected_profit")
    ):
        return True
    try:
        from m3_supply_intelligence_read import derive_supply_from_row

        return bool((derive_supply_from_row(row).get("supply_status") or {}).get("commercial_evidence_available"))
    except Exception:
        return False


def _has_execution_signal(row: dict[str, Any]) -> bool:
    if _known(row.get("award_id") or row.get("contract_number")):
        return True
    status = str(row.get("status") or row.get("current_status") or "").upper()
    return status in {"AWARDED"} or bool(row.get("execution_in_progress") or row.get("draft_bid_ready"))


def _has_lessons(row: dict[str, Any]) -> bool:
    if row.get("lessons") or row.get("learning_captured"):
        return True
    try:
        from m3_action_orchestration_read import list_actions

        for a in list_actions(opportunity_id=str(row.get("canonical_id") or ""), limit=20):
            oc = a.get("outcome")
            if isinstance(oc, dict) and _known(oc.get("lesson_created")):
                return True
    except Exception:
        pass
    return False


def _is_screened(row: dict[str, Any]) -> bool:
    if row.get("cheap_screen_survive") is True:
        return True
    stage = str(row.get("pipeline_stage") or "")
    if stage and stage not in {"", "UNKNOWN", "DISCOVERED"}:
        return True
    lc = str(row.get("lifecycle") or "")
    if lc and lc not in {"DISCOVERED", "NEW", ""}:
        return True
    return bool(row.get("canonical_id") and row.get("title"))


def _is_understanding(row: dict[str, Any]) -> bool:
    if row.get("documents") or row.get("package_acquired") or row.get("governing_documents"):
        return True
    if row.get("requirements_parsed"):
        return True
    desc = str(row.get("description") or "")
    return _known(desc) and len(desc) > 40


def _next_human_action(state: str, missing: list[str], blockers: list[dict[str, Any]]) -> dict[str, Any]:
    if blockers:
        b = blockers[0]
        return {
            "what": b.get("what") or "Resolve blocker",
            "why": b.get("why") or "Blocks progress",
            "evidence_required": "Document resolution with source and date",
        }
    mapping = {
        OP_NEW: ("Review new opportunity", "Confirm it belongs in the pipeline", "Screening notes"),
        OP_SCREENED: (
            "Open documents / requirements",
            "Need to understand what is being bought",
            "Solicitation package evidence",
        ),
        OP_UNDERSTANDING: (
            "Identify the product",
            "Cannot source without product identity",
            "NSN/part/spec evidence",
        ),
        OP_PRODUCT_RESEARCH: (
            "Find supplier pathway",
            "Product identified but acquisition path unknown",
            "Supplier capability confirmation",
        ),
        OP_SUPPLY_RESEARCH: (
            "Collect commercial evidence",
            "Need quote/pricing before decision",
            "Supplier Quote",
        ),
        OP_COMMERCIAL_VALIDATION: (
            "Validate economics with evidence",
            "Commercial figures must be sourced",
            "Quote + cost evidence",
        ),
        OP_DECISION_READY: (
            "Make pursue / pass decision",
            "Humans decide — AI does not",
            "Decision note with evidence reviewed",
        ),
        OP_EXECUTION: (
            "Track execution milestones",
            "Keep commitments evidenced",
            "Delivery/acceptance evidence",
        ),
        OP_COMPLETE: ("Capture lessons", "Improve future work", "Lesson record"),
        OP_LEARNED: ("No further action required", "Lessons already captured", "N/A"),
    }
    what, why, ev = mapping.get(state, ("Review opportunity", "Unclear state", "Evidence"))
    if missing and state not in {OP_LEARNED, OP_COMPLETE}:
        why = f"{why} · Missing: {missing[0]}"
    return {"what": what, "why": why, "evidence_required": ev}


def derive_operator_lifecycle(row: dict[str, Any] | None) -> dict[str, Any]:
    """Deterministic operator lifecycle from evidence — AI does not advance state."""
    row = row if isinstance(row, dict) else {}
    cid = row.get("canonical_id") or "UNKNOWN"
    knowns: list[str] = []
    missing: list[str] = []
    blockers: list[dict[str, Any]] = []
    reasons: list[str] = []

    if _known(row.get("title")):
        knowns.append(f"title={row.get('title')}")
    else:
        missing.append("Opportunity title")
    if _known(row.get("agency") or row.get("buyer")):
        knowns.append(f"agency={row.get('agency') or row.get('buyer')}")
    else:
        missing.append("Agency / buyer")
    if _known(row.get("deadline")):
        knowns.append(f"deadline={row.get('deadline')}")
    else:
        missing.append("Deadline")

    screened = _is_screened(row)
    understanding = _is_understanding(row)
    product = _has_product_evidence(row)
    supply = _has_supply_evidence(row)
    commercial = _has_commercial_evidence(row)
    execution = _has_execution_signal(row)
    lessons = _has_lessons(row)
    status_u = str(row.get("status") or row.get("current_status") or "").upper()

    if status_u in {"CLOSED", "CANCELLED", "CANCELED", "EXPIRED"} and lessons:
        state = OP_LEARNED
        reasons.append("Opportunity closed/cancelled and lessons captured")
    elif status_u in {"CLOSED", "CANCELLED", "CANCELED", "EXPIRED"}:
        state = OP_COMPLETE
        reasons.append("Opportunity closed or cancelled")
    elif lessons and execution:
        state = OP_LEARNED
        reasons.append("Execution signal present and lessons captured")
    elif execution:
        state = OP_EXECUTION
        reasons.append("Award/contract or execution-in-progress evidenced")
    elif commercial and supply and product:
        state = OP_DECISION_READY
        reasons.append("Product, supply path, and commercial evidence present — human decision required")
    elif commercial or (supply and product and row.get("bid_pricing")):
        state = OP_COMMERCIAL_VALIDATION
        reasons.append("Commercial validation in progress or partially evidenced")
    elif product and not supply:
        state = OP_SUPPLY_RESEARCH
        reasons.append("Product identified; supplier path missing")
        missing.append("Supplier / supply path")
        blockers.append(
            {
                "what": "Find supplier pathway",
                "why": "Product identified but acquisition path unknown",
            }
        )
        if "Commercial evidence (quote/pricing)" not in missing:
            missing.append("Commercial evidence (quote/pricing)")
    elif product and supply and not commercial:
        state = OP_SUPPLY_RESEARCH
        reasons.append("Product and suppliers evidenced; commercial evidence still missing")
        missing.append("Commercial evidence (quote/pricing)")
        blockers.append({"what": "Obtain commercial evidence", "why": "Pricing/quote unknown"})
    elif understanding and not product:
        state = OP_PRODUCT_RESEARCH
        reasons.append("Opportunity understood enough to identify product; product identity incomplete")
        missing.append("Product identity evidence")
        blockers.append(
            {
                "what": "Confirm product identity",
                "why": "Cannot research supply without product",
            }
        )
    elif screened and not understanding:
        state = OP_UNDERSTANDING
        reasons.append("Screened into pipeline; documents/requirements not yet assembled")
        missing.append("Package / requirements understanding")
    elif screened:
        state = OP_UNDERSTANDING
        reasons.append("Screened and understanding underway")
    else:
        state = OP_NEW
        reasons.append("Newly discovered or insufficient screening evidence")

    if product:
        knowns.append("product_identity_evidenced")
    elif "Product identity evidence" not in missing and state not in {OP_NEW, OP_SCREENED}:
        missing.append("Product identity evidence")
    if supply:
        knowns.append("supply_path_evidenced")
    if commercial:
        knowns.append("commercial_evidence_present")

    try:
        from m3_action_orchestration_read import ST_BLOCKED, list_actions

        for a in list_actions(opportunity_id=str(cid) if _known(cid) else None, limit=15):
            if a.get("status") == ST_BLOCKED:
                blockers.append(
                    {
                        "what": a.get("title"),
                        "why": a.get("why_exists") or "Blocked action",
                        "action_id": a.get("action_id"),
                    }
                )
    except Exception:
        pass

    return {
        "kind": "M3OpportunityLifecycle",
        "opportunity_id": cid,
        "current_state": state,
        "why_in_this_state": reasons or ["Insufficient evidence to explain — remains conservative"],
        "known_information": knowns or ["UNKNOWN"],
        "missing_information": missing or [],
        "blocking_actions": blockers[:12],
        "next_human_action": _next_human_action(state, missing, blockers),
        "deterministic": True,
        "ai_does_not_advance_state": True,
        "maps_existing_evidence_only": True,
        "does_not_replace_m3_lifecycle": True,
        "subsystem_lifecycle": row.get("lifecycle") or "UNKNOWN",
        "evidence_discipline": "Claim → Evidence → Source → Date → Context",
        "states": list(OPERATOR_LIFECYCLE_STATES),
        "build": BUILD_TAG,
        "generated_at": _utc(),
    }


def validate_lifecycle_transition(from_state: str, to_state: str) -> dict[str, Any]:
    fs = str(from_state or "").upper()
    ts = str(to_state or "").upper()
    if fs not in OPERATOR_LIFECYCLE_STATES:
        return {"valid": False, "error": f"unknown from_state: {from_state}", "allowed": []}
    if ts not in OPERATOR_LIFECYCLE_STATES:
        return {
            "valid": False,
            "error": f"unknown to_state: {to_state}",
            "allowed": list(ALLOWED_TRANSITIONS.get(fs, ())),
        }
    allowed = ALLOWED_TRANSITIONS.get(fs, frozenset())
    return {
        "valid": ts in allowed or fs == ts,
        "from_state": fs,
        "to_state": ts,
        "allowed": list(allowed),
        "note": "Lifecycle is derived from evidence; manual jumps still require matching evidence",
        "build": BUILD_TAG,
    }


def ensure_research_missions_for_lifecycle(
    row: dict[str, Any] | None, *, persist: bool = False
) -> list[dict[str, Any]]:
    row = row if isinstance(row, dict) else {}
    lc = derive_operator_lifecycle(row)
    cid = lc.get("opportunity_id")
    if not _known(cid):
        return []
    unknowns = list(lc.get("missing_information") or [])
    for b in lc.get("blocking_actions") or []:
        if b.get("what"):
            unknowns.append(str(b.get("what")))
    try:
        from m3_research_execution_read import create_missions_from_supply_unknowns

        return create_missions_from_supply_unknowns(
            opportunity_id=str(cid),
            unknowns=unknowns,
            product=row.get("title") or "UNKNOWN",
            supplier="UNKNOWN",
            persist=persist,
        )
    except Exception as e:
        log.debug("research mission ensure skipped: %s", e)
        return []


def assemble_deal_room_operating_view(
    deal: dict[str, Any] | None,
    *,
    row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble operator Deal Room from existing attachments — no duplicate stores."""
    deal = deal if isinstance(deal, dict) else {}
    row = row if isinstance(row, dict) else {"canonical_id": deal.get("canonical_id")}
    overview = _as_dict(deal.get("overview"))
    requirements = _as_dict(deal.get("requirements"))
    economics = _as_dict(deal.get("economics"))
    funding = _as_dict(deal.get("funding"))
    compliance = _as_dict(deal.get("compliance") or deal.get("bid_compliance"))
    supply = _as_dict(deal.get("supply_intelligence"))
    supply_view = _as_dict(supply.get("opportunity_view"))
    supply_status = _as_dict(supply_view.get("supply_status"))
    research = _as_dict(deal.get("research_execution"))
    actions = _as_dict(deal.get("action_orchestration"))
    execution = _as_dict(deal.get("execution_os") or deal.get("contract_lifecycle"))
    econ_learn = _as_dict(deal.get("economic_learning"))
    lifecycle = derive_operator_lifecycle(row)

    history_lessons: list[dict[str, Any]] = []
    try:
        from m3_action_orchestration_read import OUTCOME_INDEX_KEY, _load_index

        for e in (_load_index(OUTCOME_INDEX_KEY).get("entries") or [])[-10:]:
            if isinstance(e, dict):
                history_lessons.append(
                    {"lesson": e.get("lesson_created"), "action_id": e.get("action_id")}
                )
    except Exception:
        pass

    return {
        "kind": "M3DealRoomOperatingView",
        "build": BUILD_TAG,
        "opportunity_id": row.get("canonical_id") or deal.get("canonical_id") or "UNKNOWN",
        "lifecycle": lifecycle,
        "opportunity": {
            "agency": overview.get("buyer") or row.get("agency") or row.get("buyer") or "UNKNOWN",
            "requirement": overview.get("title") or row.get("title") or "UNKNOWN",
            "deadline": overview.get("deadline")
            or row.get("deadline_display")
            or row.get("deadline_raw")
            or row.get("response_deadline")
            or row.get("deadline")
            or "UNKNOWN",
            "deadline_viability": overview.get("deadline_viability")
            or row.get("deadline_viability")
            or "UNKNOWN",
            "deadline_runway_days": overview.get("deadline_runway_days")
            or row.get("deadline_runway_days"),
            "documents": row.get("documents") or requirements.get("package_access") or "UNKNOWN",
        },
        "product": {
            "identified_products": supply_view.get("stored_products")
            or [{"name": overview.get("title") or row.get("title") or "UNKNOWN"}],
            "manufacturers": [
                {"name": (supply_view.get("derived_product") or {}).get("manufacturer") or "UNKNOWN"}
            ],
            "specifications": requirements.get("bom_lines") or [],
            "evidence": [
                evidence_envelope(
                    claim="Product context from opportunity",
                    evidence=row.get("title") or "UNKNOWN",
                    source=row.get("source_id") or "opportunity_row",
                    context=str(row.get("canonical_id") or "UNKNOWN"),
                )
            ],
        },
        "supply": {
            "suppliers": supply_view.get("derived_suppliers")
            or supply_view.get("stored_suppliers")
            or ["UNKNOWN"],
            "supply_paths": supply_view.get("stored_paths") or [],
            "missing_supply_information": supply_status.get("unknowns_remaining")
            or lifecycle.get("missing_information")
            or [],
            "supply_status": supply_status,
        },
        "economics": {
            "pricing": economics.get("revenue") or "UNKNOWN",
            "quotes": supply_view.get("stored_evidence") or [],
            "costs": {
                "acquisition": economics.get("acquisition_evidence") or "UNKNOWN",
                "freight": economics.get("freight") or "UNKNOWN",
                "financing": economics.get("financing") or "UNKNOWN",
            },
            "cash_requirements": funding.get("capital_requirement") or "UNKNOWN",
            "unknowns": funding.get("unknowns")
            or (["economics UNKNOWN"] if not _known(economics.get("expected_profit")) else []),
            "learning": econ_learn.get("kind"),
        },
        "compliance": {
            "requirements": compliance if compliance else ["UNKNOWN"],
            "missing_evidence": (
                (compliance.get("missing") if isinstance(compliance, dict) else None)
                or requirements.get("missing_information")
                or []
            ),
        },
        "decision": {
            "known": lifecycle.get("known_information"),
            "unknown": lifecycle.get("missing_information"),
            "available_actions": [
                lifecycle.get("next_human_action"),
                *[
                    {"what": a.get("title"), "status": a.get("status")}
                    for a in (actions.get("actions") or actions.get("derived_actions") or [])[:5]
                ],
            ],
            "humans_decide": True,
            "ai_decides": False,
        },
        "execution": {
            "tasks": (actions.get("actions") or [])[:10],
            "milestones": execution.get("stages") or execution.get("milestones") or ["UNKNOWN"],
            "status": lifecycle.get("current_state"),
        },
        "history": {
            "research": (research.get("missions") or research.get("suggested_from_supply") or [])[:10],
            "decisions": [],
            "lessons": history_lessons,
        },
        "operator_questions": [
            "What is this?",
            "What do we know?",
            "What is missing?",
            "What do I do next?",
        ],
        "assembles_existing_layers_only": True,
        "no_duplicate_sources": True,
        "no_scores": True,
        "no_win_prediction": True,
        "generated_at": _utc(),
        "read_only": True,
    }


def build_opportunity_operating_profile(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    lifecycle = derive_operator_lifecycle(row)
    suggestions = ensure_research_missions_for_lifecycle(row, persist=False)
    return {
        "kind": "M3OpportunityOperatingProfile",
        "build": BUILD_TAG,
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "lifecycle": lifecycle,
        "suggested_research_missions": [
            {
                "question": m.get("question"),
                "reason": m.get("reason"),
                "required_evidence": m.get("required_evidence"),
            }
            for m in suggestions[:5]
        ],
        "workflow_goal": [
            "Find opportunity",
            "Open one workspace",
            "Understand it",
            "See known / missing",
            "Complete research",
            "Review supply",
            "Review economics",
            "Human decision",
            "Execution",
            "Capture lessons",
        ],
        "not_a_parallel_system": True,
        "reuses_action_orchestration": True,
        "reuses_research_execution": True,
        "reuses_supply_intelligence": True,
        "reuses_human_os": True,
        "ai_cost_controls_intact": True,
        "engines_unchanged": True,
        "generated_at": _utc(),
        "read_only": True,
    }


def attach_opportunity_operating_to_deal_room(
    deal: dict[str, Any], *, row: dict[str, Any] | None = None
) -> dict[str, Any]:
    if not isinstance(deal, dict):
        return deal
    out = dict(deal)
    try:
        r = row or {"canonical_id": deal.get("canonical_id")}
        out["opportunity_lifecycle"] = derive_operator_lifecycle(r)
        out["opportunity_operating"] = build_opportunity_operating_profile(r)
        out["operating"] = assemble_deal_room_operating_view(out, row=r)
    except Exception as e:
        log.warning("opportunity operating attach failed: %s", e)
        out["operating"] = {
            "kind": "M3DealRoomOperatingView",
            "build": BUILD_TAG,
            "error": "operating_view_unavailable",
            "read_only": True,
        }
    return out


def build_operator_workflow_boards(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    today_attention: list[dict[str, Any]] = []
    research: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    execution: list[dict[str, Any]] = []

    for r in rows[:40]:
        lc = derive_operator_lifecycle(r)
        item = {
            "opportunity_id": r.get("canonical_id"),
            "title": (r.get("title") or "")[:100],
            "state": lc.get("current_state"),
            "next": (lc.get("next_human_action") or {}).get("what"),
            "why": (lc.get("next_human_action") or {}).get("why"),
        }
        st = lc.get("current_state")
        if st in {
            OP_NEW,
            OP_SCREENED,
            OP_UNDERSTANDING,
            OP_PRODUCT_RESEARCH,
            OP_SUPPLY_RESEARCH,
            OP_COMMERCIAL_VALIDATION,
        }:
            today_attention.append(item)
        if st in {OP_PRODUCT_RESEARCH, OP_SUPPLY_RESEARCH, OP_UNDERSTANDING}:
            research.append(item)
        if st == OP_DECISION_READY:
            decisions.append(item)
        if st in {OP_EXECUTION, OP_COMPLETE}:
            execution.append(item)

    try:
        from m3_research_execution_read import list_research_missions

        for m in list_research_missions(limit=20).get("missions") or []:
            if m.get("status") != "COMPLETE":
                research.append(
                    {
                        "opportunity_id": m.get("related_opportunity"),
                        "title": m.get("question"),
                        "state": m.get("status"),
                        "next": "Continue research mission",
                        "why": m.get("reason"),
                    }
                )
    except Exception:
        pass

    try:
        from m3_action_orchestration_read import AT_DECISION_REVIEW, list_actions

        for a in list_actions(action_type=AT_DECISION_REVIEW, limit=15):
            decisions.append(
                {
                    "opportunity_id": a.get("related_opportunity"),
                    "title": a.get("title"),
                    "state": a.get("status"),
                    "next": a.get("title"),
                    "why": a.get("why_exists"),
                }
            )
    except Exception:
        pass

    return {
        "kind": "M3OperatorWorkflowBoards",
        "today": {
            "opportunities_needing_attention": today_attention[:15],
            "decisions_waiting": decisions[:10],
            "supplier_responses": [],
            "blocked_items": [
                i for i in today_attention if "missing" in str(i.get("why") or "").lower()
            ][:10],
        },
        "research": {"active_research_missions": research[:15], "unknowns": []},
        "decisions": {
            "opportunities_ready_for_review": [d for d in decisions if d.get("state") == OP_DECISION_READY][
                :15
            ]
            or decisions[:15]
        },
        "execution": {"active_transactions": execution[:15]},
        "beginner_simple": True,
        "question": "What do I do next?",
        "build": BUILD_TAG,
        "generated_at": _utc(),
    }


def enrich_command_center_operating(
    sections: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    period: str,
) -> dict[str, Any]:
    out = dict(sections or {})
    boards = build_operator_workflow_boards(rows)
    if period == "morning":
        out["opportunities_needing_attention"] = boards["today"]["opportunities_needing_attention"]
        out["operator_decisions_waiting"] = boards["today"]["decisions_waiting"]
        out["operator_research_active"] = boards["research"]["active_research_missions"]
        out["operator_blocked"] = boards["today"]["blocked_items"]
    else:
        learned = []
        for r in (rows or [])[:20]:
            if not isinstance(r, dict):
                continue
            lc = derive_operator_lifecycle(r)
            if lc.get("current_state") in {OP_COMPLETE, OP_LEARNED}:
                learned.append(
                    {
                        "opportunity_id": r.get("canonical_id"),
                        "title": r.get("title"),
                        "state": lc.get("current_state"),
                    }
                )
        out["operator_completed_or_learned"] = learned[:15]
        out["operator_execution_active"] = boards["execution"]["active_transactions"]
    out["operator_workflow_boards"] = boards
    return out
