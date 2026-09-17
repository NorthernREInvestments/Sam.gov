"""Controlled OpenAI advisory layer for deep deal research.

OpenAI is ADVISORY. Deterministic hard constraints remain authoritative.
Degrades honestly when API key unavailable.
"""

from __future__ import annotations

import json
import os
from typing import Any

from deep_deal_constants import EV_PROPOSED_AI, HARD_MAX_OPENAI, TARGET_OPENAI


class OpenAIBudget:
    def __init__(self, *, hard_max: int = HARD_MAX_OPENAI, target: int = TARGET_OPENAI):
        self.hard_max = hard_max
        self.target = target
        self.used = 0
        self.skipped: list[str] = []

    def can_call(self) -> bool:
        return self.used < self.hard_max

    def record(self, n: int = 1) -> None:
        self.used += n

    def to_dict(self) -> dict[str, Any]:
        return {
            "openai_requests_used": self.used,
            "openai_target": self.target,
            "openai_hard_max": self.hard_max,
            "openai_skipped": list(self.skipped),
            "OpenAI": self.used,
        }


def openai_available() -> bool:
    return bool((os.getenv("OPENAI_API_KEY") or "").strip())


def propose_ai_fact(
    *,
    field: str,
    value: Any,
    source_excerpt: str | None = None,
    confidence: str = "LOW",
) -> dict[str, Any]:
    """AI fact — PROPOSED_AI_UNCONFIRMED until confirmed. Never overwrites verified."""
    return {
        "field": field,
        "value": value,
        "verification_status": EV_PROPOSED_AI,
        "confidence": confidence,
        "source_excerpt": (source_excerpt or "")[:500],
        "requires_confirmation": True,
        "auto_verified": False,
    }


def merge_ai_proposals_safely(
    *,
    existing_facts: dict[str, Any],
    existing_provenance: dict[str, Any],
    proposals: list[dict[str, Any]],
) -> dict[str, Any]:
    """Do not overwrite VERIFIED_* with AI proposals."""
    protected = {"VERIFIED_PUBLIC", "VERIFIED_CALL", "VERIFIED_DOCUMENT", "VERIFIED_QUOTE", "VERIFIED_BY_CALL"}
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    facts = dict(existing_facts or {})
    prov = dict(existing_provenance or {})

    for p in proposals or []:
        field = p.get("field")
        if not field:
            continue
        prior = prov.get(field) or {}
        prior_status = str(prior.get("verification_status") or "").upper()
        if prior_status in protected or prior_status.startswith("VERIFIED"):
            rejected.append({**p, "reject_reason": "would_overwrite_verified_evidence"})
            continue
        facts[field] = p.get("value")
        prov[field] = {
            "verification_status": EV_PROPOSED_AI,
            "confidence": p.get("confidence"),
            "source_excerpt": p.get("source_excerpt"),
            "requires_confirmation": True,
        }
        accepted.append(p)

    return {
        "facts": facts,
        "provenance": prov,
        "accepted_proposals": accepted,
        "rejected_proposals": rejected,
        "ai_proposals_auto_verified": False,
    }


def build_openai_advisory_prompt(
    *,
    opportunity: dict[str, Any],
    document_excerpt: str | None = None,
    task: str = "summarize_requirements",
) -> dict[str, Any]:
    """Build advisory prompt packet — does not call API."""
    return {
        "task": task,
        "rules": [
            "Do not waive hard constraints ($0 cash, no PG, no personal credit)",
            "Do not convert UNKNOWN to VERIFIED",
            "Do not invent line items, prices, or eligibility",
            "Mark every extracted fact as PROPOSED_AI_UNCONFIRMED",
            "Flag compliance blockers explicitly",
        ],
        "opportunity": {
            "title": opportunity.get("title"),
            "agency": opportunity.get("agency"),
            "solicitation": opportunity.get("solicitation_number") or opportunity.get("solicitation_id"),
            "source_url": opportunity.get("source_url") or opportunity.get("detail_url"),
        },
        "document_excerpt": (document_excerpt or "")[:12000],
        "output_schema": {
            "summary": None,
            "proposed_facts": [],
            "compliance_flags": [],
            "missing_information": [],
            "operator_next_step": None,
            "why_blocked": None,
        },
        "OpenAI": 0,
    }


def run_openai_advisory(
    *,
    opportunity: dict[str, Any],
    document_excerpt: str | None = None,
    task: str = "summarize_requirements",
    budget: OpenAIBudget | None = None,
    force_offline: bool = False,
) -> dict[str, Any]:
    """
    Controlled OpenAI call for deep research.
    Degrades to offline stub when unavailable or budget exhausted.
    """
    budget = budget or OpenAIBudget()
    packet = build_openai_advisory_prompt(
        opportunity=opportunity,
        document_excerpt=document_excerpt,
        task=task,
    )

    if force_offline or not openai_available():
        budget.skipped.append("openai_unavailable_or_offline")
        return {
            "status": "DEGRADED_OFFLINE",
            "reason": "OPENAI_API_KEY_missing_or_force_offline",
            "advisory": {
                "summary": None,
                "proposed_facts": [],
                "compliance_flags": [],
                "missing_information": ["OpenAI unavailable — deterministic extraction only"],
                "operator_next_step": "Continue with document review and deterministic gates",
                "why_blocked": None,
            },
            "prompt_packet": packet,
            "OpenAI": 0,
            "budget": budget.to_dict(),
        }

    if not budget.can_call():
        budget.skipped.append("hard_max_reached")
        return {
            "status": "BUDGET_EXHAUSTED",
            "reason": "openai_hard_max",
            "advisory": {
                "summary": None,
                "proposed_facts": [],
                "missing_information": ["OpenAI budget exhausted"],
                "operator_next_step": "Proceed with deterministic path only",
            },
            "OpenAI": 0,
            "budget": budget.to_dict(),
        }

    try:
        from openai_runtime import create_response

        system = (
            "You are an advisory assistant for government product-resale bid qualification. "
            "Return JSON only. Never claim facts are verified. Never waive $0 cash / no PG / no personal credit."
        )
        user = json.dumps(packet)
        resp = create_response(
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        budget.record(1)
        text = ""
        if isinstance(resp, dict):
            text = resp.get("output_text") or resp.get("text") or ""
        else:
            text = getattr(resp, "output_text", None) or str(resp)

        advisory: dict[str, Any]
        try:
            advisory = json.loads(text)
        except Exception:
            advisory = {
                "summary": text[:2000] if text else None,
                "proposed_facts": [],
                "missing_information": ["model_response_not_json"],
            }

        # Force AI facts into proposed state
        proposed = []
        for f in advisory.get("proposed_facts") or []:
            if isinstance(f, dict):
                proposed.append(
                    propose_ai_fact(
                        field=f.get("field") or f.get("name") or "unknown",
                        value=f.get("value"),
                        source_excerpt=f.get("source_excerpt") or f.get("evidence"),
                        confidence=f.get("confidence") or "LOW",
                    )
                )
        advisory["proposed_facts"] = proposed

        return {
            "status": "OK",
            "advisory": advisory,
            "prompt_packet": packet,
            "OpenAI": 1,
            "budget": budget.to_dict(),
        }
    except Exception as exc:
        budget.skipped.append(f"openai_error:{type(exc).__name__}")
        return {
            "status": "DEGRADED_ERROR",
            "reason": str(exc),
            "advisory": {
                "summary": None,
                "proposed_facts": [],
                "missing_information": [f"OpenAI error: {exc}"],
                "operator_next_step": "Continue deterministic path",
            },
            "OpenAI": 0,
            "budget": budget.to_dict(),
        }


def interpret_call_notes_with_ai(
    *,
    raw_notes: str,
    note_type: str = "supplier",
    budget: OpenAIBudget | None = None,
    force_offline: bool = True,
) -> dict[str, Any]:
    """Supplier/lender call-note interpretation — default offline for tests."""
    budget = budget or OpenAIBudget()
    if force_offline or not openai_available() or not budget.can_call():
        return {
            "status": "DEGRADED_OFFLINE",
            "raw_notes": raw_notes,
            "proposed_facts": [],
            "note_type": note_type,
            "OpenAI": 0,
        }
    return run_openai_advisory(
        opportunity={"title": f"{note_type} call notes"},
        document_excerpt=raw_notes,
        task=f"interpret_{note_type}_call_notes",
        budget=budget,
    )
