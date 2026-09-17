"""Benchmark case models, quality-tier assessment, manifests, integrity."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from historical_benchmark_constants import (
    BENCHMARK_VERSION,
    FORBIDDEN_DISP,
    TIER_BRONZE,
    TIER_GOLD,
    TIER_NOT_READY,
    TIER_SILVER,
    TIER_VALIDATION_ONLY,
)

UNKNOWN = "UNKNOWN"


def _u(v: Any) -> Any:
    if v is None or v == "":
        return UNKNOWN
    return v


def benchmark_case(
    *,
    benchmark_case_id: str,
    agency: Any = UNKNOWN,
    subagency: Any = UNKNOWN,
    buying_office: Any = UNKNOWN,
    solicitation_number: Any = UNKNOWN,
    contract_award_number: Any = UNKNOWN,
    product_description: Any = UNKNOWN,
    quantities: Any = UNKNOWN,
    posting_date: Any = UNKNOWN,
    question_deadline: Any = UNKNOWN,
    bid_deadline: Any = UNKNOWN,
    award_date: Any = UNKNOWN,
    awardee: Any = UNKNOWN,
    award_amount: Any = UNKNOWN,
    procurement_method: Any = UNKNOWN,
    set_aside: Any = UNKNOWN,
    delivery_destination: Any = UNKNOWN,
    delivery_requirements: Any = UNKNOWN,
    payment_terms: Any = UNKNOWN,
    bid_count: Any = UNKNOWN,
    historical_competitors: Any = UNKNOWN,
    product_category: Any = UNKNOWN,
    buyer_level: Any = UNKNOWN,  # federal | state | local
    provenance: list[dict[str, Any]] | None = None,
    synthetic_dates_used: bool = False,
    notes: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": "HistoricalBenchmarkCase",
        "benchmark_version": BENCHMARK_VERSION,
        "benchmark_case_id": benchmark_case_id,
        "agency": _u(agency),
        "subagency": _u(subagency),
        "buying_office": _u(buying_office),
        "solicitation_number": _u(solicitation_number),
        "contract_award_number": _u(contract_award_number),
        "product_description": _u(product_description),
        "quantities": _u(quantities),
        "posting_date": _u(posting_date),
        "question_deadline": _u(question_deadline),
        "bid_deadline": _u(bid_deadline),
        "award_date": _u(award_date),
        "awardee": _u(awardee),
        "award_amount": _u(award_amount),
        "procurement_method": _u(procurement_method),
        "set_aside": _u(set_aside),
        "delivery_destination": _u(delivery_destination),
        "delivery_requirements": _u(delivery_requirements),
        "payment_terms": _u(payment_terms),
        "bid_count": _u(bid_count),
        "historical_competitors": _u(historical_competitors),
        "product_category": _u(product_category),
        "buyer_level": _u(buyer_level),
        "provenance": list(provenance or []),
        "synthetic_dates_used": bool(synthetic_dates_used),
        "notes": notes,
        "namespace": "HISTORICAL_SIMULATION",
    }


def assess_benchmark_tier(case: dict[str, Any], *, prebid_recoverable: bool = False) -> dict[str, Any]:
    """Assign GOLD/SILVER/BRONZE/NOT_READY/VALIDATION_ONLY — no fake numeric score."""
    if case.get("synthetic_dates_used"):
        return {
            "tier": TIER_VALIDATION_ONLY,
            "reason": "Synthetic validation dates present — cannot qualify as GOLD/SILVER/BRONZE",
        }

    def known(k: str) -> bool:
        return case.get(k) not in (None, "", UNKNOWN)

    sol = known("solicitation_number")
    agency = known("agency")
    posting = known("posting_date")
    deadline = known("bid_deadline")
    award = known("award_date") or known("award_amount")
    awardee = known("awardee")
    amount = known("award_amount")
    product = known("product_description")
    temporal_sep = posting and deadline and (award or awardee)

    reasons: list[str] = []
    if sol:
        reasons.append("verified solicitation identity")
    if agency:
        reasons.append("verified agency")
    if posting and deadline:
        reasons.append("verified posting/deadline")
    if prebid_recoverable:
        reasons.append("recoverable pre-bid requirement/specification")
    if award and awardee:
        reasons.append("verified award outcome + awardee")
    if amount:
        reasons.append("verified award amount")
    if product:
        reasons.append("clear tangible-product description")
    if temporal_sep:
        reasons.append("temporal separation possible")

    missing = []
    for label, ok in [
        ("solicitation", sol),
        ("agency", agency),
        ("posting", posting),
        ("deadline", deadline),
        ("prebid_spec", prebid_recoverable),
        ("awardee", awardee),
        ("amount", amount),
        ("product", product),
    ]:
        if not ok:
            missing.append(label)

    if (
        sol
        and agency
        and posting
        and deadline
        and prebid_recoverable
        and awardee
        and amount
        and product
        and temporal_sep
    ):
        return {"tier": TIER_GOLD, "reason": "; ".join(reasons), "missing": missing}

    # SILVER: meaningful simulation; may lack supplier cost / full bid tab / complete attachments
    # or posting/deadline when award + solicitation identity are verified
    if sol and agency and product and awardee and amount:
        return {
            "tier": TIER_SILVER,
            "reason": "; ".join(reasons)
            + " | SILVER: award+solicitation identity verified; pre-bid package and/or dates may be incomplete",
            "missing": missing,
        }

    if sol and agency and product and (deadline or posting) and (awardee or amount) and (
        prebid_recoverable or (posting and deadline)
    ):
        return {
            "tier": TIER_SILVER,
            "reason": "; ".join(reasons) + " | SILVER: meaningful simulation with one+ gaps",
            "missing": missing,
        }

    if agency and product and (awardee or amount):
        return {
            "tier": TIER_BRONZE,
            "reason": "; ".join(reasons)
            + " | BRONZE: award-centric product purchase; solicitation/pre-bid package weak",
            "missing": missing,
        }

    if sol and product and (agency or awardee):
        return {
            "tier": TIER_BRONZE,
            "reason": "; ".join(reasons) + " | BRONZE: partial subsystem testing only",
            "missing": missing,
        }

    if not sol and not product:
        return {"tier": TIER_NOT_READY, "reason": "Insufficient identity for benchmark", "missing": missing}

    return {"tier": TIER_NOT_READY, "reason": "Does not meet BRONZE floor", "missing": missing}


def prebid_evidence_manifest(
    case_id: str,
    evidence_ids: list[str],
    *,
    knowledge_cutoff_at: str,
    notes: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": "PreBidEvidenceManifest",
        "benchmark_case_id": case_id,
        "knowledge_cutoff_at": knowledge_cutoff_at,
        "evidence_ids": list(evidence_ids),
        "contains_post_bid": False,
        "notes": notes,
    }


def postbid_evidence_manifest(case_id: str, evidence_ids: list[str]) -> dict[str, Any]:
    return {
        "kind": "PostBidEvidenceManifest",
        "benchmark_case_id": case_id,
        "evidence_ids": list(evidence_ids),
        "vault_only": True,
    }


def integrity_record(
    *,
    case_id: str,
    tier: str,
    simulation_mode: str,
    simulation_start_at: str,
    decision_hash: str | None,
    outcome_reveal_timestamp: str | None,
    git_commit: str | None,
    m3_configuration: dict[str, Any] | None = None,
    operator_funding_profile_version: str = "CURRENT_OPERATOR_FUNDING_OVERLAY_v1",
    case_provenance: list[dict[str, Any]] | None = None,
    prebid_manifest_id: str | None = None,
    postbid_manifest_id: str | None = None,
    temporal_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "benchmark_version": BENCHMARK_VERSION,
        "benchmark_case_id": case_id,
        "tier": tier,
        "case_provenance": case_provenance or [],
        "prebid_manifest_id": prebid_manifest_id,
        "postbid_manifest_id": postbid_manifest_id,
        "temporal_validation": temporal_validation or {},
        "simulation_mode": simulation_mode,
        "simulation_start_at": simulation_start_at,
        "decision_hash": decision_hash,
        "outcome_reveal_timestamp": outcome_reveal_timestamp,
        "software_version_git_commit": git_commit,
        "m3_configuration": m3_configuration or {"funding_overlay": "CURRENT_OPERATOR_FUNDING_OVERLAY"},
        "operator_funding_profile_version": operator_funding_profile_version,
        "immutable_version": True,
        "forbidden_disposition": FORBIDDEN_DISP,
    }


def new_evidence_id(prefix: str = "BE") -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
