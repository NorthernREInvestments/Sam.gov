"""Temporal Evidence Firewall — anti-hindsight controls for historical simulation.

CRITICAL:
- retrieval_date is NOT historical availability.
- POST_BID_* evidence may be stored for scoring but MUST NOT enter pre-bid decision context.
- No information first published after knowledge_cutoff_at may affect simulated pre-bid decisions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from application_clock import CLOCK_HISTORICAL_SIMULATION, clock_mode, knowledge_cutoff_at
from historical_case_constants import (
    POST_BID_ROLES,
    PRE_BID_ROLES,
    ROLE_UNKNOWN,
    TEMPORAL_AVAILABLE_AT_CUTOFF,
    TEMPORAL_AVAILABLE_BEFORE_CUTOFF,
    TEMPORAL_NOT_APPLICABLE,
    TEMPORAL_POST_CUTOFF,
    TEMPORAL_PUBLICATION_DATE_UNKNOWN,
    TEMPORAL_TEMPORAL_CONFLICT,
)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_iso_datetime(value: Any) -> datetime | None:
    if value is None or value == "" or value == "UNKNOWN":
        return None
    if isinstance(value, datetime):
        return _aware(value)
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return _aware(datetime.fromisoformat(s))
    except ValueError:
        # date-only
        try:
            return _aware(datetime.fromisoformat(s + "T00:00:00+00:00"))
        except ValueError:
            return None


def resolve_historical_availability(
    *,
    source_publication_date: Any = None,
    document_effective_date: Any = None,
    historical_availability_date: Any = None,
) -> datetime | None:
    """Prefer explicit historical_availability_date, else publication, else document effective."""
    for candidate in (
        historical_availability_date,
        source_publication_date,
        document_effective_date,
    ):
        dt = parse_iso_datetime(candidate)
        if dt is not None:
            return dt
    return None


def classify_temporal_state(
    knowledge_cutoff: datetime | None,
    *,
    source_publication_date: Any = None,
    document_effective_date: Any = None,
    historical_availability_date: Any = None,
    retrieval_date: Any = None,
    not_applicable: bool = False,
) -> tuple[str, str]:
    """Return (temporal_state, reason). Retrieval date never substitutes for availability."""
    if not_applicable:
        return TEMPORAL_NOT_APPLICABLE, "evidence marked not applicable to temporal cutoff"

    cutoff = _aware(knowledge_cutoff)
    availability = resolve_historical_availability(
        source_publication_date=source_publication_date,
        document_effective_date=document_effective_date,
        historical_availability_date=historical_availability_date,
    )
    pub = parse_iso_datetime(source_publication_date)
    hist = parse_iso_datetime(historical_availability_date)
    retrieval = parse_iso_datetime(retrieval_date)

    if cutoff is None:
        return TEMPORAL_NOT_APPLICABLE, "no knowledge_cutoff_at provided"

    if availability is None:
        return (
            TEMPORAL_PUBLICATION_DATE_UNKNOWN,
            "publication/historical availability date unknown; retrieval_date cannot substitute",
        )

    # Conflict: claimed historical availability before publication without defense
    if pub is not None and hist is not None and hist < pub:
        return (
            TEMPORAL_TEMPORAL_CONFLICT,
            "historical_availability_date precedes source_publication_date without reconciliation",
        )

    # Retrieval after availability is normal (e.g. retrieve 2023 PDF in 2026)
    if retrieval is not None and availability is not None and retrieval < availability:
        return (
            TEMPORAL_TEMPORAL_CONFLICT,
            "retrieval_date precedes claimed historical availability",
        )

    if availability < cutoff:
        return (
            TEMPORAL_AVAILABLE_BEFORE_CUTOFF,
            f"historical availability {availability.isoformat()} before cutoff {cutoff.isoformat()}",
        )
    if availability == cutoff:
        return (
            TEMPORAL_AVAILABLE_AT_CUTOFF,
            f"historical availability equals cutoff {cutoff.isoformat()} (deterministic include)",
        )
    return (
        TEMPORAL_POST_CUTOFF,
        f"historical availability {availability.isoformat()} after cutoff {cutoff.isoformat()}",
    )


def allowed_for_pre_bid_simulation(
    temporal_state: str,
    evidence_role: str,
    *,
    allow_unknown_dates: bool = False,
) -> tuple[bool, str]:
    """POST_BID roles never enter pre-bid engine; only pre-cutoff/at-cutoff temporal states allowed."""
    if evidence_role in POST_BID_ROLES:
        return False, f"role {evidence_role} is post-bid; withheld from pre-bid decision engine"
    if temporal_state == TEMPORAL_POST_CUTOFF:
        return False, "POST_CUTOFF evidence blocked by anti-hindsight rule"
    if temporal_state == TEMPORAL_TEMPORAL_CONFLICT:
        return False, "TEMPORAL_CONFLICT evidence blocked until reconciled"
    if temporal_state == TEMPORAL_PUBLICATION_DATE_UNKNOWN:
        if allow_unknown_dates:
            return False, "PUBLICATION_DATE_UNKNOWN preserved; not auto-allowed"
        return False, "PUBLICATION_DATE_UNKNOWN cannot enter pre-bid context"
    if temporal_state in (TEMPORAL_AVAILABLE_BEFORE_CUTOFF, TEMPORAL_AVAILABLE_AT_CUTOFF):
        if evidence_role in PRE_BID_ROLES or evidence_role == ROLE_UNKNOWN:
            return True, f"temporal {temporal_state} allows pre-bid use"
        return True, f"temporal {temporal_state}; role {evidence_role} treated cautiously but date-ok"
    if temporal_state == TEMPORAL_NOT_APPLICABLE:
        return False, "NOT_APPLICABLE"
    return False, f"unhandled temporal state {temporal_state}"


def make_temporal_evidence(
    *,
    evidence_id: str | None = None,
    title: str,
    evidence_role: str,
    knowledge_cutoff_at: datetime | str | None,
    source_publication_date: Any = None,
    document_effective_date: Any = None,
    retrieval_date: Any = None,
    historical_availability_date: Any = None,
    payload: dict[str, Any] | None = None,
    source_url: str | None = None,
    case_id: str | None = None,
    not_applicable: bool = False,
) -> dict[str, Any]:
    cutoff = parse_iso_datetime(knowledge_cutoff_at)
    state, reason = classify_temporal_state(
        cutoff,
        source_publication_date=source_publication_date,
        document_effective_date=document_effective_date,
        historical_availability_date=historical_availability_date,
        retrieval_date=retrieval_date,
        not_applicable=not_applicable,
    )
    allowed, allow_reason = allowed_for_pre_bid_simulation(state, evidence_role)
    return {
        "kind": "TemporalEvidenceRecord",
        "evidence_id": evidence_id or f"TE-{uuid4().hex[:12]}",
        "case_id": case_id,
        "title": title,
        "evidence_role": evidence_role,
        "source_publication_date": source_publication_date,
        "document_effective_date": document_effective_date,
        "retrieval_date": retrieval_date,
        "historical_availability_date": historical_availability_date
        or source_publication_date
        or document_effective_date,
        "knowledge_cutoff_at": cutoff.isoformat() if cutoff else None,
        "temporal_state": state,
        "allowed_for_simulation": allowed,
        "reason": f"{reason} | {allow_reason}",
        "source_url": source_url,
        "payload": payload or {},
        "namespace": "HISTORICAL_SIMULATION",
        # Explicit: retrieval ≠ availability
        "retrieval_is_not_historical_availability": True,
    }


def anti_hindsight_guard(
    evidence: dict[str, Any],
    *,
    for_pre_bid_decision: bool,
) -> dict[str, Any]:
    """Strict rule: post-cutoff / post-bid cannot affect simulated pre-bid decision."""
    if not for_pre_bid_decision:
        return {"allowed": True, "reason": "scoring/storage context"}
    if not evidence.get("allowed_for_simulation"):
        return {
            "allowed": False,
            "reason": evidence.get("reason") or "blocked by temporal firewall",
            "rule": "NO_POST_CUTOFF_IN_PRE_BID_DECISION",
        }
    if evidence.get("evidence_role") in POST_BID_ROLES:
        return {
            "allowed": False,
            "reason": "POST_BID evidence withheld from pre-bid engine",
            "rule": "NO_POST_BID_IN_PRE_BID_DECISION",
        }
    return {"allowed": True, "reason": "passes anti-hindsight guard"}


def require_knowledge_cutoff_in_historical_mode() -> datetime:
    """HISTORICAL_SIMULATION mode requires an active knowledge cutoff."""
    if clock_mode() != CLOCK_HISTORICAL_SIMULATION:
        raise RuntimeError("require_knowledge_cutoff_in_historical_mode called outside HISTORICAL_SIMULATION")
    cutoff = knowledge_cutoff_at()
    if cutoff is None:
        raise RuntimeError("HISTORICAL_SIMULATION requires knowledge_cutoff_at")
    return cutoff


def filter_pre_bid_context(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only evidence allowed into the simulated pre-bid decision engine."""
    out: list[dict[str, Any]] = []
    for rec in records:
        guard = anti_hindsight_guard(rec, for_pre_bid_decision=True)
        if guard["allowed"]:
            out.append(rec)
    return out


def partition_evidence_for_validation(
    records: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    allowed = []
    blocked = []
    unknown_date = []
    for rec in records:
        if rec.get("temporal_state") == TEMPORAL_PUBLICATION_DATE_UNKNOWN:
            unknown_date.append(rec)
        guard = anti_hindsight_guard(rec, for_pre_bid_decision=True)
        if guard["allowed"]:
            allowed.append(rec)
        else:
            blocked.append(rec)
    return {
        "allowed_pre_bid": allowed,
        "blocked_from_pre_bid": blocked,
        "unknown_date": unknown_date,
    }
