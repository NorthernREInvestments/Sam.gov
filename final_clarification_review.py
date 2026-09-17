"""Final clarification second-pass — required before CO even after MISSING_CONFIRMED_LOCAL."""

from __future__ import annotations

import json
import re
from typing import Any

from missing_info import FACT_SOLICITATION, MISSING_CONFIRMED_LOCAL
from package_manifest import FINAL_CLARIFICATION_REVIEW_REQUIRED, PACKAGE_COMPLETE
from co_clarification import draft_co_question, evaluate_co_clarification_gate

SECOND_PASS_NOT_RUN = "NOT_RUN"
SECOND_PASS_SKIPPED_BUDGET = "SKIPPED_BUDGET"
SECOND_PASS_COMPLETE_NO_FINDING = "COMPLETE_NO_FINDING"
SECOND_PASS_COMPLETE_FOUND_CANDIDATE = "COMPLETE_FOUND_CANDIDATE"
SECOND_PASS_VALIDATION_FAILED = "VALIDATION_FAILED"


def needs_final_clarification_review(
    *,
    fact_class: str,
    package_status: str,
    deterministic_status: str,
    possible_matches_resolved: bool,
) -> bool:
    if fact_class != FACT_SOLICITATION:
        return False
    if package_status != PACKAGE_COMPLETE:
        return False
    if deterministic_status != MISSING_CONFIRMED_LOCAL:
        return False
    if not possible_matches_resolved:
        return False
    return True


def validate_ai_finding_against_corpus(
    *,
    claimed_value: str | None,
    claimed_evidence: str | None,
    corpus_text: str,
) -> dict[str, Any]:
    """AI-found answers are ASSESSMENT until exact evidence validates in local corpus."""
    if not claimed_value or not claimed_evidence:
        return {
            "validated": False,
            "status": "ASSESSMENT",
            "reason": "missing_value_or_evidence",
        }
    # Evidence snippet must appear in corpus (normalized whitespace)
    norm_corpus = re.sub(r"\s+", " ", corpus_text or "").lower()
    norm_ev = re.sub(r"\s+", " ", claimed_evidence).lower()
    if len(norm_ev) < 12 or norm_ev not in norm_corpus:
        return {
            "validated": False,
            "status": "ASSESSMENT",
            "reason": "evidence_not_found_in_local_corpus",
            "ai_status": SECOND_PASS_VALIDATION_FAILED,
        }
    return {
        "validated": True,
        "status": "VERIFIED",
        "reason": "exact_evidence_in_local_corpus",
        "value": claimed_value,
        "evidence": claimed_evidence,
    }


def build_second_pass_prompt(*, fact_description: str, corpus_excerpt: str) -> str:
    return (
        f"We believe the following fact may be missing from the solicitation: {fact_description}.\n"
        "Search this supplied solicitation evidence specifically for the answer, including "
        "indirect/table/configuration expressions. If you find an answer, return JSON with "
        "keys found (bool), value (string|null), evidence_snippet (string|null exact quote), "
        "notes (string). If you cannot find it, set found=false and do not infer a value.\n\n"
        f"SOLICITATION EVIDENCE:\n{corpus_excerpt[:120000]}"
    )


def run_targeted_luna_second_pass(
    *,
    fact_description: str,
    corpus_text: str,
    notice_id: str | None = None,
    max_cost_usd: float = 0.015,
) -> dict[str, Any]:
    """One targeted Luna call. Stops if estimated cost exceeds budget slice."""
    from ai_pricing import estimate_pre_call_cost_usd
    from openai_runtime import create_response, get_last_response_meta, text_part

    prompt = build_second_pass_prompt(
        fact_description=fact_description,
        corpus_excerpt=corpus_text,
    )
    est = estimate_pre_call_cost_usd(
        model="gpt-5.6-luna",
        input_chars=len(prompt) + 200,
        max_output_tokens=800,
        web_search=False,
    )
    if est > max_cost_usd:
        return {
            "executed": False,
            "status": SECOND_PASS_SKIPPED_BUDGET,
            "estimated_cost_usd": est,
            "reason": "estimated_cost_exceeds_slice",
            "LIVE_API_REQUESTS": 0,
        }

    raw = create_response(
        task="final_clarification_second_pass",
        instructions=(
            "Return ONLY JSON. Never invent solicitation facts. "
            "If absent, found=false. Evidence snippets must be exact quotes."
        ),
        content=[text_part(prompt)],
        max_output_tokens=800,
        web_search=False,
        notice_id=notice_id,
        funnel_stage=3,
        use_cache=True,
        model="gpt-5.6-luna",
        model_tier="cheap",
    )
    meta = get_last_response_meta()
    parsed: dict[str, Any] = {}
    try:
        from openai_runtime import extract_json_object

        parsed = extract_json_object(raw) or {}
    except Exception:
        parsed = {}

    found = bool(parsed.get("found"))
    value = parsed.get("value")
    evidence = parsed.get("evidence_snippet")
    validation = validate_ai_finding_against_corpus(
        claimed_value=str(value) if value is not None else None,
        claimed_evidence=str(evidence) if evidence is not None else None,
        corpus_text=corpus_text,
    )
    cost = meta.get("estimated_cost_usd") or meta.get("actual_cost_usd") or meta.get("cost_usd")
    return {
        "executed": True,
        "status": (
            SECOND_PASS_COMPLETE_FOUND_CANDIDATE
            if found and validation.get("validated")
            else SECOND_PASS_COMPLETE_NO_FINDING
            if not found
            else SECOND_PASS_VALIDATION_FAILED
        ),
        "raw_parsed": parsed,
        "validation": validation,
        "model": "gpt-5.6-luna",
        "cost_usd": cost,
        "tokens": meta.get("tokens") or meta.get("usage"),
        "request_id": meta.get("request_id") or meta.get("id"),
        "LIVE_API_REQUESTS": 1 if not meta.get("cache_hit") else 0,
        "cache_hit": bool(meta.get("cache_hit")),
        "ai_finding_is_assessment_until_validated": True,
        "meta": {k: meta.get(k) for k in ("estimated_cost_usd", "cache_hit", "model")},
    }


def build_co_escalation_report(
    *,
    missing_item: str,
    why_needed: str,
    package_status: str,
    deterministic_complete: bool,
    possible_match_review_complete: bool,
    final_second_pass_status: str,
    documents_checked: list[str],
    terms_checked: list[str],
    closest_matches: list[str],
    answer_found: bool,
    confidence_absent: str,
    safe_to_ask_co: bool,
    draft_question: str | None,
) -> dict[str, Any]:
    return {
        "title": "CLARIFICATION REVIEW COMPLETE",
        "missing_item": missing_item,
        "why_needed": why_needed,
        "PACKAGE_STATUS": package_status,
        "DETERMINISTIC_SEARCH": "COMPLETE" if deterministic_complete else "INCOMPLETE",
        "POSSIBLE_MATCH_REVIEW": "COMPLETE" if possible_match_review_complete else "INCOMPLETE",
        "FINAL_SECOND_PASS": final_second_pass_status,
        "DOCUMENTS_CHECKED": documents_checked,
        "TERMS_ALTERNATES_CHECKED": terms_checked,
        "CLOSEST_MATCHES": closest_matches or ["NONE"],
        "ANSWER_FOUND": "YES" if answer_found else "NO",
        "CONFIDENCE_ABSENT": confidence_absent,
        "SAFE_TO_ASK_CO": "YES" if safe_to_ask_co else "NO",
        "DRAFT_QUESTION": draft_question,
        "status_gate": FINAL_CLARIFICATION_REVIEW_REQUIRED
        if package_status == PACKAGE_COMPLETE
        and deterministic_complete
        and possible_match_review_complete
        and final_second_pass_status == SECOND_PASS_NOT_RUN
        else None,
    }
