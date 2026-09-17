"""Immutable PreBidSimulationDecision freeze + integrity hashing."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from historical_benchmark_constants import FORBIDDEN_DISP
from historical_benchmark_models import utc_now_iso


class DecisionIntegrityError(RuntimeError):
    pass


class FrozenDecisionMutationError(RuntimeError):
    pass


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def hash_decision_payload(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def build_prebid_decision(
    *,
    case_id: str,
    simulation_start_at: str,
    knowledge_cutoff_at: str,
    discovered: Any,
    classification: dict[str, Any],
    requirements_status: str,
    supplier_status: str,
    economics_status: str,
    estimated_economics: dict[str, Any] | None,
    funding_status: dict[str, Any] | None,
    deadline_status: dict[str, Any] | None,
    deal_readiness: Any,
    next_operator_action: Any,
    disposition: str,
    operator_actions: list[str] | None = None,
    backtest_mode: str,
    errors: list[str] | None = None,
    stage_results: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if disposition == FORBIDDEN_DISP:
        raise ValueError(f"{FORBIDDEN_DISP} is forbidden")
    return {
        "kind": "PreBidSimulationDecision",
        "case_id": case_id,
        "simulation_start_at": simulation_start_at,
        "knowledge_cutoff_at": knowledge_cutoff_at,
        "backtest_mode": backtest_mode,
        "discovered": discovered,
        "classification": classification,
        "requirements_status": requirements_status,
        "supplier_status": supplier_status,
        "economics_status": economics_status,
        "estimated_economics": estimated_economics or {},
        "funding_status": funding_status or {},
        "deadline_status": deadline_status or {},
        "deal_readiness": deal_readiness,
        "next_operator_action": next_operator_action,
        "disposition": disposition,
        "operator_actions": list(operator_actions or []),
        "errors": list(errors or []),
        "stage_results": stage_results or {},
        "frozen": False,
        "decision_hash": None,
        "frozen_at": None,
    }


class FrozenPreBidDecision:
    """Immutable wrapper — mutations after freeze raise."""

    def __init__(self, decision: dict[str, Any]) -> None:
        payload = deepcopy(decision)
        if payload.get("disposition") == FORBIDDEN_DISP:
            raise ValueError(f"{FORBIDDEN_DISP} is forbidden")
        # Hash excludes frozen metadata
        to_hash = {
            k: v
            for k, v in payload.items()
            if k not in {"frozen", "decision_hash", "frozen_at"}
        }
        digest = hash_decision_payload(to_hash)
        payload["frozen"] = True
        payload["decision_hash"] = digest
        payload["frozen_at"] = utc_now_iso()
        self._payload = payload
        self._hash = digest

    @property
    def hash(self) -> str:
        return self._hash

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(self._payload)

    def __setitem__(self, key: str, value: Any) -> None:
        raise FrozenDecisionMutationError("frozen PreBidSimulationDecision is immutable")

    def mutate(self, **_: Any) -> None:
        raise FrozenDecisionMutationError("frozen PreBidSimulationDecision is immutable")


def freeze_prebid_decision(decision: dict[str, Any]) -> FrozenPreBidDecision:
    return FrozenPreBidDecision(decision)


def verify_decision_hash(frozen: FrozenPreBidDecision | dict[str, Any], expected_hash: str | None = None) -> bool:
    if isinstance(frozen, FrozenPreBidDecision):
        payload = frozen.to_dict()
        digest = frozen.hash
    else:
        payload = frozen
        digest = payload.get("decision_hash")
    to_hash = {k: v for k, v in payload.items() if k not in {"frozen", "decision_hash", "frozen_at"}}
    recomputed = hash_decision_payload(to_hash)
    if recomputed != digest:
        raise DecisionIntegrityError("decision hash verification failed — possible tampering")
    if expected_hash is not None and expected_hash != digest:
        raise DecisionIntegrityError("decision hash does not match expected")
    return True


def detect_tamper(frozen_dict: dict[str, Any], *, tampered_field: str, tampered_value: Any) -> bool:
    """Return True if tampering would be detected by verify_decision_hash."""
    tampered = deepcopy(frozen_dict)
    tampered[tampered_field] = tampered_value
    try:
        verify_decision_hash(tampered)
        return False
    except DecisionIntegrityError:
        return True
