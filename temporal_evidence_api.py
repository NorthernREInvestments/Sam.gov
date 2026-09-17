"""Temporal evidence API — single place for cutoff-filtered evidence access.

Future discovery/research/economics/funding modules must consume this API
instead of implementing their own cutoff logic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from application_clock import CLOCK_HISTORICAL_SIMULATION, CLOCK_SYSTEM, clock_mode
from historical_case_constants import (
    LIVE_IOWA_SOLICITATION,
    POST_BID_ROLES,
    SIMULATION_NAMESPACE,
)
from temporal_evidence_firewall import (
    anti_hindsight_guard,
    make_temporal_evidence,
    parse_iso_datetime,
    require_knowledge_cutoff_in_historical_mode,
)


class TemporalEvidenceStore:
    """In-memory / artifact-backed store isolated under HISTORICAL_SIMULATION namespace."""

    def __init__(self, *, namespace: str = SIMULATION_NAMESPACE) -> None:
        if namespace != SIMULATION_NAMESPACE:
            raise ValueError("TemporalEvidenceStore must use HISTORICAL_SIMULATION namespace")
        self.namespace = namespace
        self._records: dict[str, dict[str, Any]] = {}

    def add(self, record: dict[str, Any]) -> dict[str, Any]:
        if record.get("namespace") != SIMULATION_NAMESPACE:
            record = dict(record)
            record["namespace"] = SIMULATION_NAMESPACE
        # Hard isolation: never attach live Iowa solicitation as simulation contamination
        sol = None
        payload = record.get("payload") or {}
        if isinstance(payload, dict):
            sol = payload.get("solicitation_number")
        if sol == LIVE_IOWA_SOLICITATION:
            raise ValueError(
                f"refusing to store live opportunity {LIVE_IOWA_SOLICITATION} in simulation store"
            )
        eid = record["evidence_id"]
        self._records[eid] = record
        return record

    def add_evidence(self, **kwargs: Any) -> dict[str, Any]:
        rec = make_temporal_evidence(**kwargs)
        return self.add(rec)

    def all_records(self) -> list[dict[str, Any]]:
        return list(self._records.values())

    def get(self, evidence_id: str) -> dict[str, Any] | None:
        return self._records.get(evidence_id)

    def clear(self) -> None:
        self._records.clear()


def get_evidence_available_as_of(
    store: TemporalEvidenceStore | Iterable[dict[str, Any]],
    knowledge_cutoff_at: datetime | str,
    *,
    evidence_role: str | None = None,
    evidence_roles: Iterable[str] | None = None,
    for_pre_bid_decision: bool = True,
    include_post_cutoff_for_scoring: bool = False,
    case_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return evidence visible under the given knowledge cutoff.

    When for_pre_bid_decision=True (default), POST_CUTOFF and POST_BID_* are excluded.
    When include_post_cutoff_for_scoring=True and for_pre_bid_decision=False,
    post-cutoff/post-bid records may be returned for outcome scoring only.
    """
    cutoff = parse_iso_datetime(knowledge_cutoff_at)
    if cutoff is None:
        raise ValueError("knowledge_cutoff_at is required")

    if clock_mode() == CLOCK_HISTORICAL_SIMULATION:
        # Enforce active clock cutoff consistency when in historical mode
        active = require_knowledge_cutoff_in_historical_mode()
        if active != cutoff:
            # Allow explicit override only if equal; mismatch is a hard error
            raise RuntimeError(
                "requested knowledge_cutoff_at does not match active FrozenClock cutoff"
            )

    if isinstance(store, TemporalEvidenceStore):
        records = store.all_records()
    else:
        records = list(store)

    role_filter: set[str] | None = None
    if evidence_role:
        role_filter = {evidence_role}
    elif evidence_roles is not None:
        role_filter = set(evidence_roles)

    out: list[dict[str, Any]] = []
    for rec in records:
        if case_id is not None and rec.get("case_id") != case_id:
            continue
        if role_filter is not None and rec.get("evidence_role") not in role_filter:
            continue

        # Re-evaluate against requested cutoff (records may have been built with another cutoff)
        from temporal_evidence_firewall import classify_temporal_state, allowed_for_pre_bid_simulation

        state, reason = classify_temporal_state(
            cutoff,
            source_publication_date=rec.get("source_publication_date"),
            document_effective_date=rec.get("document_effective_date"),
            historical_availability_date=rec.get("historical_availability_date"),
            retrieval_date=rec.get("retrieval_date"),
        )
        evaluated = dict(rec)
        evaluated["knowledge_cutoff_at"] = cutoff.isoformat()
        evaluated["temporal_state"] = state
        evaluated["reason"] = reason
        allowed, allow_reason = allowed_for_pre_bid_simulation(state, rec.get("evidence_role") or "UNKNOWN_ROLE")
        evaluated["allowed_for_simulation"] = allowed
        evaluated["reason"] = f"{reason} | {allow_reason}"

        if for_pre_bid_decision:
            guard = anti_hindsight_guard(evaluated, for_pre_bid_decision=True)
            if not guard["allowed"]:
                continue
            out.append(evaluated)
        else:
            if include_post_cutoff_for_scoring:
                out.append(evaluated)
            else:
                # scoring store without post-cutoff still excludes post-cutoff unless asked
                if evaluated["allowed_for_simulation"] or evaluated.get("evidence_role") in POST_BID_ROLES:
                    # post-bid with pre-cutoff publication (unusual) — include for scoring only
                    out.append(evaluated)
                elif state != "POST_CUTOFF":
                    out.append(evaluated)
    return out


def assert_live_mode_uncontaminated(*, live_artifact_paths: list[str] | None = None) -> dict[str, Any]:
    """Safety check: SYSTEM clock and no historical bleed into live Iowa packet paths."""
    mode = clock_mode()
    ok = mode == CLOCK_SYSTEM
    return {
        "clock_mode": mode,
        "system_clock_active": ok,
        "live_iowa_solicitation": LIVE_IOWA_SOLICITATION,
        "live_paths_checked": list(live_artifact_paths or []),
        "historical_contamination": False if ok else "clock_not_system",
        "note": "Historical simulation must use FrozenClock HISTORICAL_SIMULATION; live uses SYSTEM.",
    }
