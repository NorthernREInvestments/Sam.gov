"""Structured models for historical case leads, claims, timelines, and outcomes."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from historical_case_constants import (
    STATUS_SOURCE_CLAIM,
    STATUS_UNKNOWN,
    STATUS_VERIFIED,
)


UNKNOWN = "UNKNOWN"


def _u(value: Any) -> Any:
    if value is None or value == "":
        return UNKNOWN
    return value


def source_claim(
    field: str,
    value: Any,
    *,
    source_name: str,
    source_file: str | None = None,
    source_location: str | None = None,
    speaker: str | None = None,
    excerpt: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Material claim from a transcript/creator — never auto-promoted to VERIFIED."""
    return {
        "kind": "SourceClaim",
        "field": field,
        "value": _u(value),
        "claim_status": STATUS_SOURCE_CLAIM,
        "source_name": source_name,
        "source_file": source_file,
        "source_location": source_location,
        "speaker": speaker,
        "transcript_excerpt_reference": excerpt,
        "notes": notes,
        "verified_fact": None,
    }


def verified_fact(
    field: str,
    value: Any,
    *,
    evidence_id: str | None = None,
    source_url: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": "VerifiedFact",
        "field": field,
        "value": _u(value),
        "claim_status": STATUS_VERIFIED,
        "evidence_id": evidence_id,
        "source_url": source_url,
        "notes": notes,
    }


def claim_vs_verified(
    *,
    source_claim_record: dict[str, Any] | None,
    verified: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep SOURCE_CLAIM distinct from independently verified evidence."""
    out = {
        "source_claim": source_claim_record,
        "verified_fact": verified,
        "resolved_value": UNKNOWN,
        "resolved_status": STATUS_UNKNOWN,
    }
    if verified is not None and verified.get("value") not in (None, "", UNKNOWN):
        out["resolved_value"] = verified["value"]
        out["resolved_status"] = STATUS_VERIFIED
    elif source_claim_record is not None and source_claim_record.get("value") not in (
        None,
        "",
        UNKNOWN,
    ):
        out["resolved_value"] = source_claim_record["value"]
        out["resolved_status"] = STATUS_SOURCE_CLAIM
    return out


def empty_claimed_fields() -> dict[str, Any]:
    keys = [
        "claimed_agency",
        "claimed_subagency",
        "claimed_buying_office",
        "claimed_product",
        "claimed_quantity",
        "claimed_solicitation_number",
        "claimed_contract_number",
        "claimed_award_number",
        "claimed_vendor",
        "claimed_award_amount",
        "claimed_supplier",
        "claimed_supplier_cost",
        "claimed_profit",
        "claimed_margin",
        "claimed_bid_count",
        "claimed_competitors",
        "claimed_financing_method",
        "claimed_payment_timing",
        "claimed_contract_type",
        "claimed_set_aside",
        "claimed_location",
        "claimed_date",
        "other_identifying_clues",
    ]
    return {k: UNKNOWN for k in keys}


def historical_case_lead(
    *,
    case_id: str | None = None,
    source_name: str,
    source_type: str,
    source_file: str | None = None,
    source_location: str | None = None,
    speaker: str | None = None,
    video_title: str | None = None,
    source_publication_date: str | None = None,
    transcript_excerpt_reference: str | None = None,
    case_classification: str,
    claimed: dict[str, Any] | None = None,
    source_claims: list[dict[str, Any]] | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    fields = empty_claimed_fields()
    if claimed:
        for k, v in claimed.items():
            if k in fields:
                fields[k] = _u(v)
            elif k == "other_identifying_clues":
                fields[k] = v if v not in (None, "") else UNKNOWN
    return {
        "kind": "HistoricalCaseLead",
        "case_id": case_id or f"HCL-{uuid4().hex[:10]}",
        "source_name": source_name,
        "source_type": source_type,
        "source_file": source_file,
        "source_location": source_location,
        "speaker": speaker,
        "video_title": video_title,
        "source_publication_date": _u(source_publication_date),
        "transcript_excerpt_reference": transcript_excerpt_reference,
        "case_classification": case_classification,
        **fields,
        "source_claims": list(source_claims or []),
        "notes": notes,
        "namespace": "HISTORICAL_SIMULATION",
    }


def timeline_event(
    event_type: str,
    event_date: Any,
    *,
    provenance: str | None = None,
    evidence_id: str | None = None,
    status: str = STATUS_UNKNOWN,
    notes: str | None = None,
) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "event_date": _u(event_date),
        "status": status if event_date not in (None, "", UNKNOWN) else STATUS_UNKNOWN,
        "provenance": provenance,
        "evidence_id": evidence_id,
        "notes": notes,
    }


def historical_case_timeline(
    case_id: str,
    *,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    types = [
        "earliest_public_signal",
        "presolicitation_source_sought",
        "solicitation_posting",
        "amendments",
        "question_deadline",
        "bid_deadline",
        "award_date",
        "contract_start",
        "delivery",
        "acceptance",
        "invoice",
        "payment",
    ]
    by_type = {t: timeline_event(t, UNKNOWN) for t in types}
    for ev in events or []:
        et = ev.get("event_type")
        if et in by_type:
            by_type[et] = ev
    return {
        "kind": "HistoricalCaseTimeline",
        "case_id": case_id,
        "events": by_type,
        "provenance_preserved": True,
    }


def economic_outcome_field(value: Any = UNKNOWN, status: str = STATUS_UNKNOWN) -> dict[str, Any]:
    return {"value": _u(value), "status": status}


def historical_economic_outcome(case_id: str) -> dict[str, Any]:
    """Post-simulation scoring only — must stay behind temporal firewall pre-bid."""
    keys = [
        "historical_government_revenue",
        "historical_award_amount",
        "historical_quantity",
        "historical_unit_price",
        "historical_supplier",
        "historical_supplier_cost",
        "historical_freight",
        "historical_financing_cost",
        "historical_transaction_expenses",
        "historical_actual_profit",
        "historical_estimated_profit",
    ]
    return {
        "kind": "HistoricalEconomicOutcome",
        "case_id": case_id,
        "for_post_simulation_scoring_only": True,
        "allowed_in_pre_bid_decision": False,
        "fields": {k: economic_outcome_field() for k in keys},
    }


def set_outcome_field(
    outcome: dict[str, Any],
    key: str,
    value: Any,
    status: str,
) -> None:
    if key not in outcome["fields"]:
        raise KeyError(key)
    if status == STATUS_SOURCE_CLAIM:
        # Normalize: source-claim-only outcome fields use SOURCE_CLAIM_ONLY label
        from historical_case_constants import STATUS_SOURCE_CLAIM_ONLY

        status = STATUS_SOURCE_CLAIM_ONLY
    outcome["fields"][key] = economic_outcome_field(value, status)


def research_quality_summary(
    *,
    identity: str,
    timeline: str,
    pre_bid_evidence: str,
    outcome_evidence: str,
    economic_reconstruction: str,
) -> dict[str, Any]:
    return {
        "IDENTITY_CONFIDENCE": identity,
        "TIMELINE_CONFIDENCE": timeline,
        "PRE_BID_EVIDENCE_QUALITY": pre_bid_evidence,
        "OUTCOME_EVIDENCE_QUALITY": outcome_evidence,
        "ECONOMIC_RECONSTRUCTION_QUALITY": economic_reconstruction,
        "note": "Separate dimensions — not collapsed into a single deceptive score.",
    }


def simulation_cutoff_record(
    *,
    simulation_as_of: datetime | str | None,
    confidence: str,
    reason: str,
    source_evidence: list[str] | None = None,
    time_before_deadline: str | None = None,
    rule_id: str | None = None,
) -> dict[str, Any]:
    as_of = UNKNOWN
    if isinstance(simulation_as_of, datetime):
        as_of = simulation_as_of.isoformat()
    elif simulation_as_of not in (None, ""):
        as_of = str(simulation_as_of)
    return {
        "simulation_as_of": as_of,
        "confidence": confidence,
        "reason": reason,
        "source_evidence": list(source_evidence or []),
        "time_before_deadline": _u(time_before_deadline),
        "rule_id": rule_id,
    }


def future_backtest_metric_scaffold() -> dict[str, Any]:
    """Prepare metric slots for future backtests — not executed here."""
    return {
        "DISCOVERY": None,
        "CLASSIFICATION": None,
        "DOCUMENTS": None,
        "PRODUCT": None,
        "SUPPLIER": None,
        "ECONOMICS": None,
        "PROFIT": None,
        "FUNDING": None,
        "TIMING": None,
        "DECISION": None,
        "OUTCOME": None,
        "forbidden": "WOULD_HAVE_WON is never a valid counterfactual claim",
        "allowed_counterfactuals": [
            "WOULD_HAVE_FOUND",
            "WOULD_HAVE_QUALIFIED",
            "WOULD_HAVE_CONTINUED",
            "WOULD_HAVE_REJECTED",
            "INSUFFICIENT_EVIDENCE",
        ],
    }
