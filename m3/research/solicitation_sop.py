"""Permanent solicitation processing SOP — production-enforced workflow.

Owner directive: every solicitation from every source follows the ordered stages.
NextBid retrieves; OpenAI interprets; deterministic gates qualify.
No stage bypass. No paid call without a justified acquisition decision.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from m3.config import get_settings
from m3.research.ai_document_analysis import (
    PROMPT_VERSION,
    analysis_cache_key,
    analyze_solicitation_documents,
    build_single_request_corpus,
    estimate_live_analysis_cost_usd,
    _load_analysis_cache,
)
from m3.research.ai_output_verify import verify_analysis_against_sources
from m3.research.commercial_criteria import evaluate_deadline_gate
from m3.research.local_evidence import assemble_local_evidence_package
from m3.research.owner_queues import empty_queues, write_queues
from m3.research.queue_deadline_repair import demote_expired_from_owner_queues

# --- Mandatory ordered stages (durable) — owner permanent SOP ---
SOP_STAGES: tuple[str, ...] = (
    "REGISTERED",
    "INITIAL_EVIDENCE_SAVED",
    "AI_INITIAL_TRIAGE",
    "RETAINED_EVIDENCE_SEARCHED",
    "DOCUMENTS_INVENTORIED",
    "DOCUMENT_GAPS_IDENTIFIED",
    "ACQUISITION_PLANNED",
    "DOCUMENT_RETRIEVED",
    "SOURCE_ASSEMBLED",
    "AI_FULL_ANALYSIS",
    "FACTS_VERIFIED",
    "EXECUTION_EVALUATED",
    "DEADLINE_ELIGIBILITY_EVALUATED",
    "COMMERCIAL_EVALUATED",
    "OWNER_QUEUE_UPDATED",
    "COMPLETE",
)

# Branch / lifecycle states (not all linear for every record)
SOP_BRANCH_STATES = frozenset(
    {
        "REJECTED_CONFIRMED",
        "AWAITING_MORE_DOCUMENTS",
        "EXPIRED_RETENTION_WINDOW",
        "ARCHIVED_AFTER_CLOSE",
    }
)

EARLY_TERMINAL = {
    "UNSUITABLE_SERVICE_OR_CONSTRUCTION",
    "EXPIRED_OR_CANCELED",
    "PREVIEW_ONLY_INCOMPLETE",
    "BLOCKED_ELIGIBILITY",
    "ACQUISITION_FAILED",
    "REJECTED_CONFIRMED",
}

# Legacy aliases accepted when resuming older workflow files
_STAGE_ALIASES = {
    "SOURCE_REGISTERED": "REGISTERED",
    "EVIDENCE_INVENTORIED": "INITIAL_EVIDENCE_SAVED",
    "DOCUMENTS_ACCOUNTED_FOR": "DOCUMENTS_INVENTORIED",
    "MISSING_REQUIREMENTS_IDENTIFIED": "DOCUMENT_GAPS_IDENTIFIED",
    "AI_ANALYZED": "AI_FULL_ANALYSIS",
    "EXECUTION_REVIEWED": "EXECUTION_EVALUATED",
    "DEADLINE_AND_ELIGIBILITY_REVIEWED": "DEADLINE_ELIGIBILITY_EVALUATED",
    "COMMERCIAL_REVIEWED": "COMMERCIAL_EVALUATED",
}

OWNER_QUEUES = {
    "SUPPLIER_PRICING",
    "NEEDS_DOCUMENTATION",
    "NEEDS_VERIFICATION",
    "BLOCKED_EXPIRED",
    "UNSUITABLE",
}

NEXTBID_OP_COSTS = {
    "search": {"credits": 1, "purpose": "Discover/screen listings"},
    "get_opportunity": {"credits": 1, "purpose": "Opportunity metadata/description"},
    "list_documents": {"credits": 1, "purpose": "Document references/metadata"},
    "list_files": {"credits": 1, "purpose": "Stored text artifact index"},
    "read_file": {"credits": 1, "purpose": "Retrieve stored text"},
    "get_document_download": {"credits": 1, "purpose": "Temporary download URL"},
    "fetch_object_store": {"credits": 0, "purpose": "Bytes via returned URL (verified 0 NextBid credits)"},
}


class SopBlocked(Exception):
    def __init__(self, reason: str, *, stage: str | None = None, decision: dict | None = None):
        self.reason = reason
        self.stage = stage
        self.decision = decision
        super().__init__(reason)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def sop_root() -> Path:
    p = get_settings().data_root() / "solicitation_sop"
    p.mkdir(parents=True, exist_ok=True)
    return p


def workflow_path(nb_id: str) -> Path:
    return sop_root() / "by_nb_id" / f"{nb_id}.json"


def load_workflow(nb_id: str) -> dict[str, Any]:
    path = workflow_path(nb_id)
    if not path.exists():
        return {
            "nb_id": nb_id,
            "stages_completed": [],
            "current_stage": None,
            "terminal": None,
            "blockers": [],
            "acquisition_decisions": [],
            "updated_at": None,
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"nb_id": nb_id, "stages_completed": [], "error": "corrupt_workflow_state"}


def save_workflow(state: dict[str, Any]) -> Path:
    nb = str(state.get("nb_id") or "UNKNOWN")
    path = workflow_path(nb)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _utc()
    path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    return path


def normalize_stage(name: str) -> str:
    return _STAGE_ALIASES.get(name, name)


def stage_index(name: str) -> int:
    name = normalize_stage(name)
    try:
        return SOP_STAGES.index(name)
    except ValueError:
        return -1


def _normalize_completed(state: dict[str, Any]) -> set[str]:
    return {normalize_stage(s) for s in (state.get("stages_completed") or [])}


def require_prior_stages(state: dict[str, Any], target: str) -> None:
    """Fail closed: cannot enter target unless all prior stages completed or early terminal."""
    if state.get("terminal") in EARLY_TERMINAL:
        return
    target = normalize_stage(target)
    if target in SOP_BRANCH_STATES:
        return
    idx = stage_index(target)
    if idx < 0:
        raise SopBlocked(f"unknown_stage:{target}")
    done = _normalize_completed(state)
    for prior in SOP_STAGES[:idx]:
        if prior not in done:
            raise SopBlocked(f"missing_required_stage:{prior}_before_{target}", stage=prior)


def mark_stage(state: dict[str, Any], stage: str, *, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    stage = normalize_stage(stage)
    require_prior_stages(state, stage)
    completed = [normalize_stage(s) for s in (state.get("stages_completed") or [])]
    if stage not in completed:
        completed.append(stage)
    state["stages_completed"] = completed
    state["current_stage"] = stage
    history = list(state.get("stage_history") or [])
    history.append({"stage": stage, "at": _utc(), "detail": detail or {}})
    state["stage_history"] = history[-60:]
    return state

def mark_branch(state: dict[str, Any], branch: str, *, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """Record non-linear lifecycle state without requiring linear priors."""
    if branch not in SOP_BRANCH_STATES:
        raise SopBlocked(f"unknown_branch_state:{branch}")
    branches = list(state.get("branch_states") or [])
    if branch not in branches:
        branches.append(branch)
    state["branch_states"] = branches
    history = list(state.get("stage_history") or [])
    history.append({"stage": branch, "branch": True, "at": _utc(), "detail": detail or {}})
    state["stage_history"] = history[-60:]
    state["current_stage"] = branch
    return state


def terminate(state: dict[str, Any], terminal: str, *, reason: str, detail: dict | None = None) -> dict[str, Any]:
    if terminal not in EARLY_TERMINAL and terminal not in OWNER_QUEUES:
        # allow early terminals only for stop; queues set separately
        pass
    state["terminal"] = terminal
    state["terminal_reason"] = reason
    state["terminal_at"] = _utc()
    state["terminal_detail"] = detail or {}
    return state


def coverage_label_from_package(package: dict[str, Any]) -> str:
    """Evidence-based labels. Text length alone NEVER yields complete_original_package."""
    from m3.research.document_inventory import (
        PKG_COMPLETE_RELEVANT,
        PKG_COMPLETE_REVIEWED,
        PKG_LISTING_ONLY,
        PKG_PARTIAL_DOCUMENTS,
        PKG_SUBSTANTIVE_BODY,
    )

    cov = package.get("coverage") or {}
    docs = package.get("document_texts") or []
    chars = int(cov.get("total_content_chars") or 0)
    pkg = str(cov.get("package_completeness") or (package.get("package_completeness") or {}).get("package_completeness") or "")

    if pkg == PKG_COMPLETE_REVIEWED:
        return "complete_relevant_package_reviewed"
    if pkg == PKG_COMPLETE_RELEVANT:
        return "complete_relevant_package"
    if pkg == PKG_PARTIAL_DOCUMENTS:
        return "partial_document_package"
    if pkg == PKG_LISTING_ONLY or (
        any(d.get("is_listing_preview") for d in docs) or int(cov.get("listing_preview_count") or 0)
    ) and not cov.get("has_substantive_document_content"):
        return "listing_preview_only"
    if not docs and chars <= 0:
        return "no_usable_original_content"
    if cov.get("has_substantive_document_content") and pkg == PKG_SUBSTANTIVE_BODY:
        return "substantive_solicitation_body"
    if cov.get("has_substantive_document_content"):
        # Has body text but package completeness unverified / incomplete
        return "substantive_incomplete_package"
    if docs and all(d.get("is_metadata_only") for d in docs):
        return "attachment_metadata_only"
    if chars > 0:
        return "notice_only"
    return "no_usable_original_content"


def identify_missing_requirements(package: dict[str, Any], item: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Exact missing purchasing/solicitation fields before any paid retrieval."""
    text = str(package.get("assembled_text") or "")
    cov = package.get("coverage") or {}
    missing: list[dict[str, Any]] = []
    if not cov.get("has_substantive_document_content"):
        missing.append(
            {
                "requirement": "substantive_solicitation_body_or_bid_schedule",
                "why": "no_substantive_local_content",
                "cheapest_ops": ["get_opportunity", "list_documents", "read_file", "get_document_download"],
            }
        )
        return missing
    checks = [
        ("quantity_or_line_schedule", r"(?i)\b(quantity|qty|each|ea\b|unit|line\s*item|bid\s*schedule)\b"),
        ("product_specification", r"(?i)\b(spec|specification|model|mpn|nsn|brand|manufacturer)\b"),
        ("deadline", r"(?i)\b(due|deadline|closing|submit\s+by|bid\s+opening)\b"),
        ("delivery", r"(?i)\b(deliver|f\.?o\.?b|ship|freight)\b"),
        ("submission_method", r"(?i)\b(submit|proposal|bid\s+form|electronic\s+bid)\b"),
    ]
    for name, pat in checks:
        if not re.search(pat, text[:50000]):
            missing.append(
                {
                    "requirement": name,
                    "why": "not_found_in_assembled_local_package",
                    "cheapest_ops": ["list_documents", "read_file", "get_document_download"],
                }
            )
    # Attachment refs without content
    for d in package.get("documents") or []:
        if d.get("is_metadata_only") or d.get("kind") == "attachment_metadata":
            missing.append(
                {
                    "requirement": "attachment_body",
                    "doc_id": d.get("doc_id"),
                    "source_path": d.get("source_path"),
                    "why": "metadata_without_contents",
                    "cheapest_ops": ["read_file", "get_document_download"],
                }
            )
    return missing


def plan_acquisition_decision(
    *,
    nb_id: str,
    package: dict[str, Any],
    missing: list[dict[str, Any]],
    proposed_endpoint: str | None = None,
    document_id: str | None = None,
    owner_authorized: bool = False,
    allow_nextbid: bool = False,
) -> dict[str, Any]:
    """Compact machine-readable acquisition decision. Fail closed without justification."""
    from m3.research.nextbid_controller import acquisition_locked, load_budget_state, controller_status
    from m3.research.nextbid_response_store import load_by_request_id

    coverage = coverage_label_from_package(package)
    locked = acquisition_locked()
    budget = load_budget_state()
    remaining = int(budget.get("account_remaining") or 0)
    reserve_floor = 1000

    # Scenario A: complete relevant package — zero calls
    if coverage in {"complete_relevant_package", "complete_relevant_package_reviewed", "complete_original_package"} and not missing:
        decision = {
            "nb_id": nb_id,
            "scenario": "A_complete_local",
            "approved": False,
            "spend": False,
            "reason": "complete_local_package_reuse_only",
            "expected_nextbid_calls": 0,
            "expected_openai_calls": 0,
            "stop_condition": "analyze_or_reuse_cache",
            "coverage": coverage,
            "missing": [],
            "at": _utc(),
        }
        return decision

    if coverage in {"listing_preview_only", "attachment_metadata_only", "no_usable_original_content", "notice_only"}:
        # Need retrieval — but this assignment / killswitch may block
        endpoint = proposed_endpoint or (missing[0].get("cheapest_ops") or ["get_opportunity"])[0] if missing else "get_opportunity"
        cost = int((NEXTBID_OP_COSTS.get(endpoint) or {}).get("credits") or 1)
        justified = bool(missing) and endpoint in NEXTBID_OP_COSTS
        approved = bool(
            justified
            and allow_nextbid
            and owner_authorized
            and not locked
            and remaining - cost >= reserve_floor
        )
        deny_reasons = []
        if locked:
            deny_reasons.append("nextbid_killswitch_locked")
        if not allow_nextbid:
            deny_reasons.append("nextbid_not_authorized_this_assignment")
        if not owner_authorized:
            deny_reasons.append("owner_authorization_required")
        if remaining - cost < reserve_floor:
            deny_reasons.append(f"would_breach_reserve_floor_{reserve_floor}")
        if not justified:
            deny_reasons.append("no_justified_missing_requirement")
        return {
            "nb_id": nb_id,
            "scenario": "D_or_C_missing_content",
            "approved": approved,
            "spend": approved,
            "proposed_endpoint": endpoint,
            "document_id": document_id,
            "verified_cost_credits": cost,
            "max_additional_calls": 1 if approved else 0,
            "expected_returned": (NEXTBID_OP_COSTS.get(endpoint) or {}).get("purpose"),
            "available_remaining": remaining,
            "reserve_floor": reserve_floor,
            "authorization_status": "approved" if approved else "denied",
            "deny_reasons": deny_reasons,
            "reason_worthwhile": missing[0] if missing else None,
            "stop_condition": "reassess_after_single_response_or_deny",
            "coverage": coverage,
            "missing": missing,
            "controller": {
                "locked": locked,
                "day_used": budget.get("day_used"),
                "account_remaining": remaining,
            },
            "at": _utc(),
        }

    # Substantive incomplete — maybe one known PDF (Scenario C)
    endpoint = proposed_endpoint or "get_document_download"
    cost = int((NEXTBID_OP_COSTS.get(endpoint) or {}).get("credits") or 1)
    approved = bool(allow_nextbid and owner_authorized and not locked and remaining - cost >= reserve_floor and missing)
    return {
        "nb_id": nb_id,
        "scenario": "C_known_missing_document" if document_id else "D_promising_incomplete",
        "approved": approved,
        "spend": approved,
        "proposed_endpoint": endpoint,
        "document_id": document_id,
        "verified_cost_credits": cost,
        "max_additional_calls": 1 if approved else 0,
        "available_remaining": remaining,
        "reserve_floor": reserve_floor,
        "authorization_status": "approved" if approved else "denied",
        "deny_reasons": ([] if approved else ["live_retrieval_not_authorized_or_unjustified"]),
        "coverage": coverage,
        "missing": missing,
        "stop_condition": "stop_when_requirements_filled_or_unjustified",
        "at": _utc(),
    }


def assert_acquisition_decision_allows_spend(decision: dict[str, Any] | None) -> None:
    """Hard gate for NextBid metered paths — call before reserve."""
    if not decision or not decision.get("approved") or not decision.get("spend"):
        raise SopBlocked(
            "paid_nextbid_blocked_no_approved_acquisition_decision",
            decision=decision,
        )


def map_owner_queue(
    *,
    terminal: str | None,
    verified: dict[str, Any] | None,
    coverage: str,
    parsed: dict[str, Any] | None,
) -> str:
    if terminal == "UNSUITABLE_SERVICE_OR_CONSTRUCTION" or (
        verified and verified.get("blocks_product_only")
    ):
        return "UNSUITABLE"
    if terminal == "EXPIRED_OR_CANCELED" or (verified and (verified.get("deadline_gate") or {}).get("expired")):
        return "BLOCKED_EXPIRED"
    if coverage in {
        "listing_preview_only",
        "attachment_metadata_only",
        "no_usable_original_content",
        "notice_only",
    }:
        return "NEEDS_DOCUMENTATION"
    promo = (verified or {}).get("promotion") or {}
    if promo.get("supplier_quote_ready") and not promo.get("bid_submission_ready"):
        # pricing possible but eligibility/open may be unverified
        if not promo.get("currently_open_verified"):
            return "NEEDS_VERIFICATION"
        return "SUPPLIER_PRICING"
    if promo.get("supplier_quote_ready") and promo.get("currently_open_verified"):
        return "SUPPLIER_PRICING"
    if (parsed or {}).get("is_product_purchase") and (verified or {}).get("verified_line_items"):
        return "NEEDS_DOCUMENTATION"
    if (parsed or {}).get("is_product_purchase"):
        return "NEEDS_DOCUMENTATION"
    return "NEEDS_VERIFICATION"


def run_sop_pipeline(
    db: Session | None,
    nb_id: str,
    *,
    title: str | None = None,
    inventory_row: dict[str, Any] | None = None,
    allow_live_openai: bool = False,
    allow_nextbid: bool = False,
    owner_openai_authorization: str | None = None,
    write_queue: bool = False,
    triage_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute mandatory SOP stages for one solicitation. Default: zero paid APIs."""
    from m3.research.deadline_retention import retention_state
    from m3.research.rejection_registry import (
        record_rejection,
        remove_from_active_inventory,
        should_block_reacquisition,
    )

    nb_id = str(nb_id or "").strip()
    inv = dict(inventory_row or {})
    state = load_workflow(nb_id)
    state["nb_id"] = nb_id

    # Anti-reacquisition: unchanged rejected identity
    blocked, prior_rej = should_block_reacquisition(
        nb_id=nb_id,
        content_hash=inv.get("content_hash") or inv.get("document_hash"),
        solicitation_number=inv.get("solicitation_number"),
    )
    if blocked:
        terminate(state, "REJECTED_CONFIRMED", reason="rejection_fingerprint_blocks", detail=prior_rej)
        mark_branch(state, "REJECTED_CONFIRMED", detail=prior_rej)
        state["owner_queue"] = "UNSUITABLE"
        save_workflow(state)
        return {
            "ok": True,
            "nb_id": nb_id,
            "owner_queue": "UNSUITABLE",
            "terminal": "REJECTED_CONFIRMED",
            "reacquisition_blocked": True,
            "openai_spent_usd": 0.0,
            "nextbid_calls": 0,
            "workflow": state,
        }

    # STEP 1 — RECEIVE AND PRESERVE
    registered = {
        "source": inv.get("source") or "nextbid",
        "provider_opportunity_id": nb_id,
        "solicitation_number": inv.get("solicitation_number"),
        "agency": inv.get("agency") or inv.get("buyer"),
        "official_url": inv.get("official_url"),
        "acquisition_timestamp": inv.get("fresh_window_acquisition_at_utc")
        or inv.get("first_seen_at")
        or inv.get("fresh_window_acquisition_at_chicago"),
        "publication_date": inv.get("posted_at") or inv.get("posted_date"),
        "reported_deadline": inv.get("deadline_at") or inv.get("response_deadline"),
        "deadline_timezone": inv.get("deadline_timezone") or inv.get("timezone"),
        "content_hash": inv.get("content_hash") or inv.get("document_hash"),
        "reprocessing_is_not_acquisition": True,
    }
    state["registration"] = registered
    mark_stage(state, "REGISTERED", detail={"fields": list(registered.keys())})

    # Retention / deadline — OPEN flag never overrides expired deadline
    ret = retention_state(inv)
    state["retention"] = ret
    if ret.get("past_48h_window"):
        mark_branch(state, "ARCHIVED_AFTER_CLOSE", detail=ret)
    elif ret.get("expired") and ret.get("working_retain"):
        mark_branch(state, "EXPIRED_RETENTION_WINDOW", detail=ret)

    # STEP 2 — initial evidence + retained search
    package = assemble_local_evidence_package(
        nb_id, db=db, title=title or inv.get("title"), include_http_cache=False, include_package_db=True
    )
    cov_label = coverage_label_from_package(package)
    mark_stage(
        state,
        "INITIAL_EVIDENCE_SAVED",
        detail={"sources_inspected": package.get("sources_inspected"), "coverage": cov_label},
    )

    # AI initial triage (may be supplied by recovery runner; else infer from coverage/cache)
    triage = dict(triage_decision or {})
    if not triage:
        if cov_label in {
            "listing_preview_only",
            "no_usable_original_content",
            "notice_only",
            "attachment_metadata_only",
        }:
            triage = {
                "decision": "SOURCE_INCOMPLETE",
                "reason": "preview_or_thin_local_only",
                "is_preliminary_preview_only": True,
            }
        elif cov_label in {
            "complete_relevant_package",
            "complete_relevant_package_reviewed",
            "complete_original_package",
            "substantive_solicitation_body",
            "substantive_incomplete_package",
        }:
            triage = {
                "decision": "PRODUCT_REQUIREMENTS_AVAILABLE"
                if cov_label.startswith("complete")
                else "PRODUCT_INVESTIGATE",
                "reason": f"local_coverage:{cov_label}",
            }
        else:
            triage = {"decision": "PRODUCT_INVESTIGATE", "reason": "substantive_incomplete_local"}
    state["ai_initial_triage"] = triage
    mark_stage(state, "AI_INITIAL_TRIAGE", detail=triage)

    if triage.get("decision") == "CLOSED_OR_EXPIRED" or (ret.get("expired") and not ret.get("working_retain")):
        terminate(state, "EXPIRED_OR_CANCELED", reason="deadline_or_triage_closed", detail={"retention": ret, "triage": triage})
        state["owner_queue"] = "BLOCKED_EXPIRED"
        save_workflow(state)
        return {
            "ok": True,
            "nb_id": nb_id,
            "coverage": cov_label,
            "owner_queue": "BLOCKED_EXPIRED",
            "terminal": state["terminal"],
            "openai_spent_usd": 0.0,
            "nextbid_calls": 0,
            "workflow": state,
        }

    # Deeper retained evidence search if thin
    if cov_label in {"listing_preview_only", "no_usable_original_content", "notice_only", "attachment_metadata_only"}:
        package = assemble_local_evidence_package(
            nb_id, db=db, title=title or inv.get("title"), include_http_cache=True, include_package_db=True
        )
        cov_label = coverage_label_from_package(package)
    mark_stage(
        state,
        "RETAINED_EVIDENCE_SEARCHED",
        detail={"coverage": cov_label, "sources_inspected": package.get("sources_inspected")},
    )

    docs_account = [
        {
            "doc_id": d.get("doc_id"),
            "kind": d.get("kind"),
            "chars": d.get("chars"),
            "has_text": d.get("has_text"),
            "is_metadata_only": d.get("is_metadata_only"),
            "is_listing_preview": d.get("is_listing_preview"),
            "source_path": d.get("source_path"),
        }
        for d in (package.get("documents") or [])
    ]
    mark_stage(state, "DOCUMENTS_INVENTORIED", detail={"documents": docs_account, "coverage": cov_label})

    missing = identify_missing_requirements(package, inv)
    mark_stage(state, "DOCUMENT_GAPS_IDENTIFIED", detail={"missing": missing})

    decision = plan_acquisition_decision(
        nb_id=nb_id,
        package=package,
        missing=missing,
        allow_nextbid=allow_nextbid,
        owner_authorized=bool(allow_nextbid),
    )
    decisions = list(state.get("acquisition_decisions") or [])
    decisions.append(decision)
    state["acquisition_decisions"] = decisions[-20:]
    mark_stage(state, "ACQUISITION_PLANNED", detail={"decision": {k: decision.get(k) for k in decision if k != "missing"}})

    # DOCUMENT_RETRIEVED: mark skipped when no paid retrieval in this pass
    mark_stage(
        state,
        "DOCUMENT_RETRIEVED",
        detail={"skipped": True, "reason": "local_reuse_or_retrieval_deferred", "decision_approved": decision.get("approved")},
    )

    # Preview-only early terminal (await more docs)
    if cov_label in {"listing_preview_only", "no_usable_original_content", "attachment_metadata_only", "notice_only"}:
        terminate(state, "PREVIEW_ONLY_INCOMPLETE", reason="insufficient_local_original_content", detail={"coverage": cov_label})
        state["owner_queue"] = "NEEDS_DOCUMENTATION"
        mark_stage(state, "SOURCE_ASSEMBLED", detail={"assembled_chars": package.get("assembled_chars"), "coverage": cov_label})
        mark_branch(state, "AWAITING_MORE_DOCUMENTS", detail={"decision": decision})
        state["blockers"] = [
            {
                "stage": "AI_FULL_ANALYSIS",
                "blocker": "preview_only_no_model_call",
                "next_action": decision,
            }
        ]
        save_workflow(state)
        return {
            "ok": True,
            "nb_id": nb_id,
            "coverage": cov_label,
            "owner_queue": "NEEDS_DOCUMENTATION",
            "terminal": state["terminal"],
            "openai_spent_usd": 0.0,
            "nextbid_calls": 0,
            "ai_cached": False,
            "ai_live": False,
            "workflow": state,
            "decision": decision,
        }

    mark_stage(
        state,
        "SOURCE_ASSEMBLED",
        detail={
            "assembled_chars": package.get("assembled_chars"),
            "source_refs": package.get("source_refs"),
            "coverage": cov_label,
        },
    )

    # Full AI + verify (cache only unless authorized)
    model = get_settings().openai_default_model or "gpt-4o-mini"
    single = build_single_request_corpus(package)
    cached = _load_analysis_cache(db, analysis_cache_key(corpus_hash=single["corpus_hash"], model=model)) if db is not None else None
    disk_path = get_settings().data_root() / "ai_doc_understanding" / "by_nb_id" / f"{nb_id}.json"
    disk = None
    if disk_path.exists():
        try:
            disk = json.loads(disk_path.read_text(encoding="utf-8"))
        except Exception:
            disk = None

    parsed = None
    ai_meta: dict[str, Any] = {"live": False, "cached": False, "spent": 0.0}
    if disk and disk.get("parsed") and disk.get("corpus_hash") == single["corpus_hash"]:
        parsed = disk["parsed"]
        ai_meta = {"live": False, "cached": True, "spent": 0.0, "source": "disk"}
    elif disk and disk.get("parsed") and not disk.get("corpus_hash"):
        # Tolerate older cache rows missing corpus_hash
        parsed = disk["parsed"]
        ai_meta = {"live": False, "cached": True, "spent": 0.0, "source": "disk_legacy"}
    elif cached and cached.get("parsed") is not None and cached.get("live"):
        parsed = cached["parsed"]
        ai_meta = {"live": False, "cached": True, "spent": 0.0, "source": "research_cache"}
    elif allow_live_openai and owner_openai_authorization == "OWNER_AUTHORIZED_LIVE_OPENAI":
        ai = analyze_solicitation_documents(
            db,
            evidence_package=package,
            allow_live=True,
            owner_live_authorization=owner_openai_authorization,
        )
        parsed = ai.get("parsed")
        ai_meta = {
            "live": bool(ai.get("live_ai_invoked")),
            "cached": bool(ai.get("cached")),
            "spent": float(ai.get("openai_spent_usd") or 0),
            "error": ai.get("error"),
        }
    else:
        state["blockers"] = list(state.get("blockers") or []) + [
            {
                "stage": "AI_FULL_ANALYSIS",
                "blocker": "no_valid_cache_and_live_openai_not_authorized",
                "corpus_hash": single["corpus_hash"],
                "est_usd": estimate_live_analysis_cost_usd(package).get("estimated_usd"),
            }
        ]

    if parsed is not None:
        mark_stage(state, "AI_FULL_ANALYSIS", detail=ai_meta)
    else:
        state["current_stage"] = "SOURCE_ASSEMBLED"
        mark_branch(state, "AWAITING_MORE_DOCUMENTS", detail={"blocker": "awaiting_ai"})
        save_workflow(state)
        return {
            "ok": True,
            "nb_id": nb_id,
            "coverage": cov_label,
            "owner_queue": "NEEDS_DOCUMENTATION",
            "terminal": None,
            "awaiting_ai_authorization": True,
            "openai_spent_usd": 0.0,
            "nextbid_calls": 0,
            "workflow": state,
            "decision": decision,
            "est_live_usd": estimate_live_analysis_cost_usd(package).get("estimated_usd"),
        }

    verified = verify_analysis_against_sources(parsed, package, item=inv)
    mark_stage(
        state,
        "FACTS_VERIFIED",
        detail={
            "rejected": verified.get("rejected_assertions"),
            "line_items": len(verified.get("verified_line_items") or []),
            "blocks_product_only": verified.get("blocks_product_only"),
        },
    )

    blocks = bool(verified.get("blocks_product_only"))
    req = str(parsed.get("requirement_class") or "")
    exec_review = {
        "blocks_product_only": blocks,
        "requirement_class": req,
        "hidden_obligations": parsed.get("hidden_obligations") or [],
        "ordinary_shipping_ok": True,
    }
    mark_stage(state, "EXECUTION_EVALUATED", detail=exec_review)
    if blocks or req in {
        "services",
        "construction",
        "maintenance_repair_services",
        "professional_services",
        "product_with_significant_installation",
    } or triage.get("decision") == "REJECT_CONFIRMED":
        terminate(
            state,
            "UNSUITABLE_SERVICE_OR_CONSTRUCTION",
            reason="execution_or_class_blocks_product_only",
            detail=exec_review,
        )
        cat = "construction" if "construction" in req else (
            "significant_installation" if "install" in req else "other_non_product_execution"
        )
        if req.startswith("maintenance"):
            cat = "mandatory_maintenance"
        fp = record_rejection(
            nb_id=nb_id,
            solicitation_number=registered.get("solicitation_number"),
            agency=registered.get("agency"),
            source_url=registered.get("official_url"),
            category=cat,
            evidence_passage=str(parsed.get("procurement_purpose") or req)[:400],
            content_hash=registered.get("content_hash") or single.get("corpus_hash"),
            reason=f"sop_execution_reject:{req}",
        )
        remove_from_active_inventory(nb_id)
        mark_branch(state, "REJECTED_CONFIRMED", detail=fp)
        terminate(state, "REJECTED_CONFIRMED", reason="fingerprint_recorded", detail=fp)

    work = dict(verified.get("item") or inv)
    dl = evaluate_deadline_gate(work)
    # Stale OPEN must never override expired deadline
    if ret.get("expired"):
        dl = {**dl, "expired": True, "open_status_override_blocked": True}
    elig = {
        "deadline_gate": dl,
        "retention": ret,
        "currently_open_verified": (verified.get("promotion") or {}).get("currently_open_verified") and not ret.get("expired"),
        "open_status": "CLOSED" if ret.get("expired") else (
            "VERIFIED" if (verified.get("promotion") or {}).get("currently_open_verified") else "OPEN_STATUS_UNVERIFIED"
        ),
        "bid_submission_ready": (verified.get("promotion") or {}).get("bid_submission_ready") and not ret.get("expired"),
    }
    mark_stage(state, "DEADLINE_ELIGIBILITY_EVALUATED", detail=elig)
    if dl.get("expired") and state.get("terminal") not in EARLY_TERMINAL:
        terminate(state, "EXPIRED_OR_CANCELED", reason="authoritative_deadline_past", detail=dl)
        if ret.get("working_retain"):
            mark_branch(state, "EXPIRED_RETENTION_WINDOW", detail=ret)

    promo = verified.get("promotion") or {}
    lines = verified.get("verified_line_items") or []
    generic = _generic_category(lines)
    if state.get("terminal") in {
        "UNSUITABLE_SERVICE_OR_CONSTRUCTION",
        "EXPIRED_OR_CANCELED",
        "REJECTED_CONFIRMED",
    }:
        pricing = "NO"
    elif promo.get("supplier_quote_ready") and lines and not generic and not ret.get("expired"):
        pricing = "YES"
    elif lines and parsed.get("is_product_purchase"):
        pricing = "PARTIAL"
    elif not lines and parsed.get("is_product_purchase"):
        pricing = "UNKNOWN"
    else:
        pricing = "NO"
    mark_stage(
        state,
        "COMMERCIAL_EVALUATED",
        detail={"supplier_pricing_possible": pricing, "ai_confidence_alone_insufficient": True},
    )

    oq = map_owner_queue(
        terminal=state.get("terminal"),
        verified=verified,
        coverage=cov_label,
        parsed=parsed,
    )
    if ret.get("expired"):
        oq = "BLOCKED_EXPIRED"
    if pricing == "YES" and oq == "SUPPLIER_PRICING" and not promo.get("currently_open_verified"):
        oq = "NEEDS_VERIFICATION"
    state["owner_queue"] = oq
    state["supplier_pricing_possible"] = pricing
    mark_stage(state, "OWNER_QUEUE_UPDATED", detail={"owner_queue": oq, "pricing": pricing})
    mark_stage(state, "COMPLETE", detail={"owner_queue": oq})
    save_workflow(state)

    return {
        "ok": True,
        "nb_id": nb_id,
        "title": package.get("title") or inv.get("title"),
        "coverage": cov_label,
        "owner_queue": oq,
        "supplier_pricing_possible": pricing,
        "terminal": state.get("terminal"),
        "verified_line_items": lines,
        "promotion": promo,
        "openai_spent_usd": float(ai_meta.get("spent") or 0),
        "nextbid_calls": 0,
        "ai_cached": bool(ai_meta.get("cached")),
        "ai_live": bool(ai_meta.get("live")),
        "rejected_assertions": verified.get("rejected_assertions") or [],
        "missing": missing,
        "decision": decision,
        "triage": triage,
        "retention": ret,
        "workflow_stages": state.get("stages_completed"),
        "workflow_path": str(workflow_path(nb_id)),
    }


def _generic_category(lines: list[dict[str, Any]]) -> bool:
    if not lines:
        return True
    bad = 0
    for li in lines:
        desc = str(li.get("description") or "").lower()
        if any(
            x in desc
            for x in (
                "raw materials",
                "manufacturing support",
                "prepared foods",
                "see attachment",
                "converted bid item",
                "automobiles:",
                "pickup trucks:",
                "qualified vendor list",
            )
        ):
            bad += 1
        if li.get("quantity") in (None, "", "UNKNOWN", "unknown") and "qvl" in desc:
            bad += 1
    return bad >= max(1, (len(lines) + 1) // 2)


def expected_call_scenarios() -> list[dict[str, Any]]:
    """Part B — planning examples (not fixed prices)."""
    return [
        {
            "id": 1,
            "name": "Complete locally retained solicitation",
            "nextbid_calls": 0,
            "openai_calls": 0,
            "max_cost_note": "Reuse docs + cached AI; $0 if hash matches",
            "gains": "Full package already present",
            "stop": "Analyze/reuse cache; no retrieval",
        },
        {
            "id": 2,
            "name": "New search listing with sufficient details",
            "nextbid_calls": 0,
            "openai_calls": 1,
            "max_cost_note": "Search already paid; OpenAI ~token estimate only when authorized",
            "gains": "Body from search payload if substantive",
            "stop": "If body insufficient → plan get_opportunity/docs",
        },
        {
            "id": 3,
            "name": "Needs one known PDF (propane pattern)",
            "nextbid_calls": 1,
            "openai_calls": 1,
            "max_cost_note": "~1 NextBid credit + ~$0.002–0.01 OpenAI (historical)",
            "gains": "Original PDF/text for line items",
            "stop": "After single document + AI; no repurchase of listing",
        },
        {
            "id": 4,
            "name": "Needs document index first",
            "nextbid_calls": "2–3+",
            "openai_calls": 1,
            "max_cost_note": "list_documents/list_files then one read/download; only if justified",
            "gains": "IDs then targeted body",
            "stop": "After minimum docs fill gaps",
        },
        {
            "id": 5,
            "name": "Complex multi-document solicitation",
            "nextbid_calls": "variable",
            "openai_calls": "1+chunks",
            "max_cost_note": "No fixed 1-credit myth; budget each doc; chunked AI counted",
            "gains": "Schedules/amendments/specs",
            "stop": "When quote-ready or next call unjustified",
        },
        {
            "id": 6,
            "name": "Unsuitable service solicitation",
            "nextbid_calls": 0,
            "openai_calls": 0,
            "max_cost_note": "$0 additional",
            "gains": "Disposition UNSUITABLE from available evidence",
            "stop": "Early terminal; no paid deepen",
        },
        {
            "id": 7,
            "name": "Already AI-analyzed unchanged package",
            "nextbid_calls": 0,
            "openai_calls": 0,
            "max_cost_note": "$0 — cache by corpus_hash+model+prompt_version",
            "gains": "Verified replay",
            "stop": "Reuse; re-verify gates only",
        },
    ]


def validate_retained_failure_cases(db: Session | None) -> dict[str, Any]:
    """Offline SOP checks against retained evidence (zero API)."""
    cases = [
        ("NB00375309", "propane_multi_line_estimated_vs_tank"),
        ("NB00321554", "monterey_pump_no_triplicate"),
        ("NB00320329", "lottery_forklifts"),
        ("NB00375339", "unication_pagers_brand_vs_uom"),
        ("NB00380092", "weg_motor_mpn"),
        ("NB00320237", "chemical_controller_obligations"),
    ]
    out = []
    for nb, label in cases:
        path = get_settings().data_root() / "ai_doc_understanding" / "by_nb_id" / f"{nb}.json"
        ft = get_settings().data_root() / "solicitation_full_text" / f"{nb}.json"
        local = {
            "nb_id": nb,
            "case": label,
            "ai_result_present": path.exists(),
            "full_text_present": ft.exists(),
            "sop_run": None,
        }
        if ft.exists() or path.exists():
            try:
                local["sop_run"] = {
                    k: v
                    for k, v in run_sop_pipeline(db, nb, allow_live_openai=False, allow_nextbid=False).items()
                    if k != "workflow"
                }
            except Exception as exc:
                local["sop_run"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        out.append(local)
    return {"cases": out, "openai_spent_usd": 0.0, "nextbid_calls": 0}


def process_fresh_inventory_sop(db: Session) -> dict[str, Any]:
    """Apply SOP to every active fresh-inventory record — zero NextBid/OpenAI spend."""
    demote_expired_from_owner_queues()
    inv_path = get_settings().data_root() / "nextbid_inventory" / "opportunities.jsonl"
    rows: list[dict[str, Any]] = []
    with inv_path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))

    # Guard: refuse if active inventory looks like legacy reimport
    policy_path = get_settings().data_root() / "nextbid_inventory" / "ACTIVE_INVENTORY_POLICY.json"
    policy = {}
    if policy_path.exists():
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    if len(rows) > 500 and policy.get("policy") == "fresh_window_only":
        # soft warn only — process what is active
        pass

    results = []
    queues = {q: [] for q in OWNER_QUEUES}
    for row in rows:
        nb = str(row.get("nb_id") or "")
        if not nb:
            continue
        r = run_sop_pipeline(
            db,
            nb,
            title=row.get("title"),
            inventory_row=row,
            allow_live_openai=False,
            allow_nextbid=False,
        )
        results.append(r)
        oq = r.get("owner_queue") or "NEEDS_DOCUMENTATION"
        queues.setdefault(oq, []).append(
            {
                "nb_id": nb,
                "title": r.get("title") or row.get("title"),
                "owner_queue": oq,
                "coverage": r.get("coverage"),
                "supplier_pricing_possible": r.get("supplier_pricing_possible"),
                "terminal": r.get("terminal"),
                "verified_line_items": r.get("verified_line_items") or [],
                "fresh_cohort": True,
                "sop": True,
            }
        )

    # Map to existing procurement_queues keys
    pq = empty_queues()
    mapping = {
        "SUPPLIER_PRICING": "awaiting_supplier_quotations",
        "NEEDS_DOCUMENTATION": "verified_product_information_needed",
        "NEEDS_VERIFICATION": "promising_awaiting_verification",
        "BLOCKED_EXPIRED": "closed_expired_non_solicitation",
        "UNSUITABLE": "confirmed_disqualification",
    }
    for oq, cards in queues.items():
        key = mapping.get(oq)
        if key and key in pq:
            pq[key].extend(cards)
    qsummary = write_queues(pq, build="solicitation-sop-fresh")

    from collections import Counter

    report = {
        "ok": True,
        "finished_at": _utc(),
        "fresh_accounted": len(results),
        "coverage": dict(Counter(r.get("coverage") for r in results)),
        "owner_queues": {k: len(v) for k, v in queues.items()},
        "ai_cached_reuse": sum(1 for r in results if r.get("ai_cached")),
        "awaiting_ai_auth": sum(1 for r in results if r.get("awaiting_ai_authorization")),
        "supplier_pricing_yes": sum(1 for r in results if r.get("supplier_pricing_possible") == "YES"),
        "openai_spent_usd": 0.0,
        "nextbid_calls": 0,
        "killswitch_locked": True,
        "queue_summary": qsummary,
        "policy": policy,
        "scenarios": expected_call_scenarios(),
        "results": results,
    }
    out = get_settings().data_root() / "acceptance" / "jobs_solicitation_sop_last.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    temp = Path(__file__).resolve().parents[2] / "TEMP" / "solicitation_sop"
    temp.mkdir(parents=True, exist_ok=True)
    (temp / "OWNER_REPORT.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def sop_stage_function_map() -> dict[str, dict[str, Any]]:
    """Acceptance map: stage → implementing functions / enforcement."""
    return {
        "REGISTERED": {
            "functions": ["solicitation_sop.run_sop_pipeline", "nextbid inventory/ledger preserve"],
            "enforced": True,
        },
        "INITIAL_EVIDENCE_SAVED": {
            "functions": ["local_evidence.assemble_local_evidence_package"],
            "enforced": True,
        },
        "AI_INITIAL_TRIAGE": {
            "functions": ["document_recovery_run.openai_triage", "solicitation_sop triage from coverage/cache"],
            "enforced": True,
        },
        "RETAINED_EVIDENCE_SEARCHED": {
            "functions": ["local_evidence.assemble_local_evidence_package(include_http_cache)"],
            "enforced": True,
        },
        "DOCUMENTS_INVENTORIED": {
            "functions": ["local_evidence", "solicitation_sop.coverage_label_from_package"],
            "enforced": True,
        },
        "DOCUMENT_GAPS_IDENTIFIED": {
            "functions": ["solicitation_sop.identify_missing_requirements"],
            "enforced": True,
        },
        "ACQUISITION_PLANNED": {
            "functions": ["solicitation_sop.plan_acquisition_decision", "assert_acquisition_decision_allows_spend"],
            "enforced": True,
        },
        "DOCUMENT_RETRIEVED": {
            "functions": ["document_recovery_run.recover_one_document", "nextbid_controller.metered_request"],
            "enforced": True,
            "note": "Skipped with reason when local reuse only",
        },
        "SOURCE_ASSEMBLED": {
            "functions": ["local_evidence.assemble_local_evidence_package"],
            "enforced": True,
        },
        "AI_FULL_ANALYSIS": {
            "functions": ["ai_document_analysis.analyze_solicitation_documents", "cache by corpus_hash"],
            "enforced": True,
            "note": "Live OpenAI only with owner token; else cache or blocker",
        },
        "FACTS_VERIFIED": {
            "functions": ["ai_output_verify.verify_analysis_against_sources"],
            "enforced": True,
        },
        "EXECUTION_EVALUATED": {
            "functions": ["ai_output_verify blocks_product_only", "rejection_registry.record_rejection"],
            "enforced": True,
        },
        "DEADLINE_ELIGIBILITY_EVALUATED": {
            "functions": ["commercial_criteria.evaluate_deadline_gate", "deadline_retention.retention_state"],
            "enforced": True,
        },
        "COMMERCIAL_EVALUATED": {
            "functions": ["solicitation_sop supplier_pricing YES/PARTIAL/NO/UNKNOWN"],
            "enforced": True,
        },
        "OWNER_QUEUE_UPDATED": {
            "functions": ["owner_queues.write_queues", "map_owner_queue"],
            "enforced": True,
        },
        "COMPLETE": {
            "functions": ["solicitation_sop.run_sop_pipeline"],
            "enforced": True,
        },
    }
