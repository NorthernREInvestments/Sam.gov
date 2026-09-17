"""Durable funding plan persistence — sync engine output to Postgres."""

from __future__ import annotations
from application_clock import now_utc

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


def _utc() -> datetime:
    return now_utc()


def load_persisted_funding(session: Any, contract_id: int) -> dict[str, Any] | None:
    from models import FundingGap, FundingPlan, FundingStrategyCandidate

    row = session.query(FundingPlan).filter_by(contract_id=contract_id).first()
    if not row:
        return None
    strategies = (
        session.query(FundingStrategyCandidate)
        .filter_by(funding_plan_id=row.id)
        .order_by(FundingStrategyCandidate.rank_score.asc())
        .all()
    )
    gaps = session.query(FundingGap).filter_by(funding_plan_id=row.id, resolved=False).all()
    return {
        "id": row.id,
        "pre_bid_status": row.pre_bid_status,
        "award_confirmation_status": row.award_confirmation_status,
        "capital_required": float(row.capital_required) if row.capital_required is not None else None,
        "capital_required_status": row.capital_required_status,
        "capital_timing": row.capital_timing,
        "selected_structure": row.selected_structure,
        "personal_cash_required": row.personal_cash_required,
        "personal_credit_required": row.personal_credit_required,
        "pg_required": row.pg_required,
        "supplier_payment_requirement": row.supplier_payment_requirement,
        "freight_funding_requirement": row.freight_funding_requirement,
        "government_payment_assumption": row.government_payment_assumption,
        "funding_cost_status": row.funding_cost_status,
        "unresolved_gaps": row.unresolved_gaps_json or [],
        "evidence": row.evidence_json or {},
        "cash_cycle": row.cash_cycle_json,
        "economics_scenarios": row.economics_scenarios_json,
        "strategies": [
            {
                "id": s.strategy_id,
                "label": s.label,
                "status": s.status,
                "rank_score": s.rank_score,
                "is_fact": s.is_fact,
                "evidence": s.evidence_json,
            }
            for s in strategies
        ],
        "gaps": [
            {
                "gap_key": g.gap_key,
                "description": g.description,
                "status": g.status,
                "must_know_before_bid": g.must_know_before_bid,
            }
            for g in gaps
        ],
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def sync_funding_plan(
    session: Any,
    *,
    contract_id: int,
    funding_plan_view: dict[str, Any],
    pursuits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Persist computed funding plan; return durable row summary."""
    from models import FundingGap, FundingPlan, FundingStrategyCandidate

    pre_bid = funding_plan_view.get("pre_bid_viability") or {}
    award = funding_plan_view.get("award_confirmation") or {}
    econ = funding_plan_view.get("economics_scenarios") or {}

    acq = None
    acq_status = "UNKNOWN"
    for q in funding_plan_view.get("_quotes") or []:
        if (q.get("validation") or {}).get("status") == "QUOTE_VALID":
            acq = q.get("total") or q.get("extended_price")
            if acq is not None:
                acq_status = "CALCULATED"
                break

    pg = personal_credit = personal_cash = None
    for p in pursuits or []:
        if p.get("pg_required") is True:
            pg = True
        elif p.get("pg_required") is False and pg is None:
            pg = False
        if p.get("personal_credit_required") is True:
            personal_credit = True
        elif p.get("personal_credit_required") is False and personal_credit is None:
            personal_credit = False
        if p.get("borrower_cash_required") is True:
            personal_cash = True
        elif p.get("borrower_cash_required") is False and personal_cash is None:
            personal_cash = False

    row = session.query(FundingPlan).filter_by(contract_id=contract_id).first()
    if row is None:
        row = FundingPlan(contract_id=contract_id)
        session.add(row)
        session.flush()

    row.pre_bid_status = funding_plan_view.get("pre_bid_status") or pre_bid.get("status") or "NOT_RESEARCHED"
    row.award_confirmation_status = award.get("status") or "PENDING"
    row.capital_required = Decimal(str(acq)) if acq is not None else None
    row.capital_required_status = acq_status
    row.capital_timing = None
    row.selected_structure = None
    row.personal_cash_required = personal_cash
    row.personal_credit_required = personal_credit
    row.pg_required = pg
    row.supplier_payment_requirement = None
    row.freight_funding_requirement = None
    row.government_payment_assumption = None
    row.funding_cost_status = econ.get("status") or "UNKNOWN"
    row.unresolved_gaps_json = list(pre_bid.get("unknowns") or []) + list(pre_bid.get("blockers") or [])
    row.evidence_json = {
        "pre_bid_viability": pre_bid,
        "award_confirmation": award,
        "persisted_at": _utc().isoformat(),
    }
    row.cash_cycle_json = funding_plan_view.get("cash_cycle")
    row.economics_scenarios_json = econ
    row.updated_at = _utc()

    session.query(FundingStrategyCandidate).filter_by(funding_plan_id=row.id).delete()
    for s in funding_plan_view.get("strategies") or []:
        session.add(
            FundingStrategyCandidate(
                funding_plan_id=row.id,
                strategy_id=str(s.get("id") or s.get("strategy_id") or "unknown"),
                label=s.get("label"),
                status=s.get("status") or "POSSIBLE_STRATEGY",
                rank_score=s.get("rank_score"),
                is_fact=bool(s.get("is_fact")),
                evidence_json={"source": "FUNDING_ENGINE"},
            )
        )

    session.query(FundingGap).filter_by(funding_plan_id=row.id).delete()
    for idx, gap in enumerate(row.unresolved_gaps_json or []):
        desc = gap if isinstance(gap, str) else str(gap)
        session.add(
            FundingGap(
                funding_plan_id=row.id,
                gap_key=f"gap_{idx}",
                description=desc,
                status="UNKNOWN",
                must_know_before_bid=True,
                resolved=False,
                evidence_json={"source": "FUNDING_ENGINE"},
            )
        )

    session.flush()
    return {"id": row.id, "contract_id": contract_id, "pre_bid_status": row.pre_bid_status, "LIVE_API_REQUESTS": 0}
