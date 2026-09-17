"""HistoricalOutcomeVault — post-bid outcomes isolated until decision freeze.

Pre-bid simulation pipeline MUST NOT access vault contents.
Scoring may unlock only after PreBidSimulationDecision is frozen + hash verified.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from historical_benchmark_constants import LIVE_IOWA_2975
from historical_benchmark_models import UNKNOWN, utc_now_iso


class OutcomeVaultLockedError(RuntimeError):
    """Raised when pre-bid code attempts to read the vault before freeze."""


class OutcomeVaultIntegrityError(RuntimeError):
    """Raised when unlock attempted without valid frozen decision hash."""


class HistoricalOutcomeVault:
    """Logically isolated post-bid outcome store."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._unlocked_for: dict[str, str] = {}  # case_id -> decision_hash
        self._reveal_timestamps: dict[str, str] = {}

    def store(self, case_id: str, outcome: dict[str, Any]) -> dict[str, Any]:
        if case_id == LIVE_IOWA_2975 or outcome.get("solicitation_number") == LIVE_IOWA_2975:
            raise ValueError("refusing to store live Iowa 2975 in outcome vault")
        rec = {
            "kind": "HistoricalOutcomeRecord",
            "benchmark_case_id": case_id,
            "awardee": outcome.get("awardee", UNKNOWN),
            "award_amount": outcome.get("award_amount", UNKNOWN),
            "awarded_line_items": outcome.get("awarded_line_items", UNKNOWN),
            "winning_unit_prices": outcome.get("winning_unit_prices", UNKNOWN),
            "bid_tab": outcome.get("bid_tab", UNKNOWN),
            "actual_competitors": outcome.get("actual_competitors", UNKNOWN),
            "award_date": outcome.get("award_date", UNKNOWN),
            "post_award_modifications": outcome.get("post_award_modifications", UNKNOWN),
            "delivery_performance_evidence": outcome.get("delivery_performance_evidence", UNKNOWN),
            "payment_evidence": outcome.get("payment_evidence", UNKNOWN),
            "creator_case_study_claims": outcome.get("creator_case_study_claims", UNKNOWN),
            "provenance": list(outcome.get("provenance") or []),
            "vault_only": True,
            "accessible_to_prebid": False,
        }
        self._records[case_id] = rec
        # Ensure locked
        self._unlocked_for.pop(case_id, None)
        return deepcopy(rec)

    def is_unlocked(self, case_id: str) -> bool:
        return case_id in self._unlocked_for

    def peek_locked_meta(self, case_id: str) -> dict[str, Any]:
        """Safe metadata without revealing outcome fields."""
        exists = case_id in self._records
        return {
            "benchmark_case_id": case_id,
            "exists": exists,
            "unlocked": self.is_unlocked(case_id),
            "accessible_to_prebid": False,
        }

    def get_for_prebid(self, case_id: str) -> None:
        """Explicit pre-bid access path — always denied."""
        raise OutcomeVaultLockedError(
            f"HistoricalOutcomeVault inaccessible to pre-bid pipeline for {case_id}"
        )

    def unlock_for_scoring(self, case_id: str, *, decision_hash: str, expected_hash: str) -> dict[str, Any]:
        if decision_hash != expected_hash:
            raise OutcomeVaultIntegrityError("decision hash mismatch — refuse outcome reveal")
        if case_id not in self._records:
            raise KeyError(f"no outcome stored for {case_id}")
        self._unlocked_for[case_id] = decision_hash
        self._reveal_timestamps[case_id] = utc_now_iso()
        return self.get_for_scoring(case_id)

    def get_for_scoring(self, case_id: str) -> dict[str, Any]:
        if not self.is_unlocked(case_id):
            raise OutcomeVaultLockedError(f"vault still locked for {case_id}; freeze+unlock required")
        out = deepcopy(self._records[case_id])
        out["outcome_reveal_timestamp"] = self._reveal_timestamps.get(case_id)
        out["unlocked_with_hash"] = self._unlocked_for[case_id]
        return out

    def export_all_locked(self) -> dict[str, Any]:
        """Export for artifact storage — scoring consumer must still respect unlock protocol."""
        return {
            "kind": "HistoricalOutcomeVaultExport",
            "note": "Post-bid only. Must not be merged into pre-bid evidence manifests.",
            "records": {cid: deepcopy(rec) for cid, rec in self._records.items()},
            "unlocked_cases": list(self._unlocked_for.keys()),
            "reveal_timestamps": dict(self._reveal_timestamps),
        }
