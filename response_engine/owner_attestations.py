"""R3 owner attestations — never auto-answer. Versioned audit trail."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from application_clock import now_utc

from response_engine.company_profile_r3 import load_attestation_library, save_attestation_library
from response_engine.models import new_id
from response_engine.r3_constants import BUILD, OWNER_CONFIRMATION_REQUIRED, UNKNOWN

ROOT = Path(__file__).resolve().parents[1]
ATTESTATION_LOG = ROOT / "data" / "owner_attestation_log.json"


def _utc() -> str:
    return now_utc().isoformat()


def new_owner_attestation(
    *,
    response_project_id: str,
    requirement_id: str | None,
    question: str,
    proposed_answer: str | None = None,
    supporting_evidence: list[Any] | None = None,
    legal_note: str | None = None,
    source: str | None = None,
    reconfirmation: str = "per_solicitation",
) -> dict[str, Any]:
    return {
        "kind": "OwnerAttestation",
        "attestation_id": new_id("ATT"),
        "response_project_id": response_project_id,
        "requirement_id": requirement_id,
        "question": question,
        "proposed_answer": proposed_answer,  # never pre-selected as confirmed
        "supporting_evidence": supporting_evidence or [],
        "legal_significance_note": legal_note,
        "owner_confirmed": False,
        "confirmed_by": None,
        "confirmed_at": None,
        "answer": None,
        "reconfirmation_rule": reconfirmation,
        "source": source,
        "build": BUILD,
        "created_at": _utc(),
    }


def confirm_attestation(
    attestation: dict[str, Any],
    *,
    answer: str,
    confirmed_by: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Explicit owner click only. Preserves prior answers in log. Mutates and returns attestation."""
    prior = {
        "attestation_id": attestation.get("attestation_id"),
        "prior_answer": attestation.get("answer"),
        "prior_confirmed_at": attestation.get("confirmed_at"),
        "prior_confirmed_by": attestation.get("confirmed_by"),
    }
    attestation["owner_confirmed"] = True
    attestation["answer"] = answer  # YES | NO | NEED_REVIEW
    attestation["confirmed_by"] = confirmed_by
    attestation["confirmed_at"] = _utc()
    attestation["confirm_reason"] = reason
    _append_log({**dict(attestation), "prior": prior, "event": "CONFIRM"})
    return attestation


def suggest_reuse(
    question: str,
    *,
    scope: str = "company",
    allow_reuse: bool = True,
) -> dict[str, Any] | None:
    """Propose prior library answer — never auto-submit."""
    if not allow_reuse:
        return None
    lib = load_attestation_library()
    qn = question.strip().lower()
    for e in lib.get("entries") or []:
        if str(e.get("question") or "").strip().lower() == qn:
            if e.get("reconfirmation_rule") == "never_reuse":
                return None
            if e.get("stale") is True:
                return None
            return {
                "suggested": True,
                "auto_applied": False,
                "entry": e,
                "note": "Prior owner answer available — confirm again if required",
            }
    return None


def store_library_entry(
    *,
    question: str,
    answer: str,
    owner: str,
    scope: str = "company",
    reconfirmation_rule: str = "annual",
    source: str | None = None,
) -> dict[str, Any]:
    lib = load_attestation_library()
    entry = {
        "entry_id": new_id("LIB"),
        "question": question,
        "answer": answer,
        "scope": scope,
        "effective_date": _utc(),
        "expiration": None,
        "reconfirmation_rule": reconfirmation_rule,
        "source": source,
        "owner": owner,
        "stale": False,
    }
    lib.setdefault("entries", []).append(entry)
    save_attestation_library(lib)
    return entry


def _append_log(row: dict[str, Any]) -> None:
    log: dict[str, Any] = {"events": []}
    if ATTESTATION_LOG.exists():
        try:
            log = json.loads(ATTESTATION_LOG.read_text(encoding="utf-8"))
        except Exception:
            log = {"events": []}
    log.setdefault("events", []).append(row)
    log["events"] = log["events"][-500:]
    ATTESTATION_LOG.parent.mkdir(parents=True, exist_ok=True)
    ATTESTATION_LOG.write_text(json.dumps(log, indent=2, default=str), encoding="utf-8")


def attestation_matrix_row(att: dict[str, Any]) -> dict[str, Any]:
    """Confirmed ≠ silent legal PASS rewrite; NEED_REVIEW stays review."""
    if att.get("owner_confirmed") and att.get("answer") == "NEED_REVIEW":
        status = "REVIEW_REQUIRED"
    elif att.get("owner_confirmed") and att.get("answer") in ("YES", "NO"):
        status = "OWNER_CONFIRMED"
    else:
        status = OWNER_CONFIRMATION_REQUIRED
    return {
        "requirement": att.get("question"),
        "applies": "YES",
        "answer": att.get("answer") or UNKNOWN,
        "evidence": att.get("supporting_evidence"),
        "timing": "AT_SUBMISSION",
        "status": status,
        "attestation_id": att.get("attestation_id"),
    }
