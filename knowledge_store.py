"""Persist product requirements / deal state into GovCon knowledge tables."""

from __future__ import annotations
from application_clock import now_utc

from datetime import datetime, timezone
from typing import Any

from data_integrity import STATUS_UNKNOWN, STATUS_VERIFIED


def persist_product_requirements(
    session: Any,
    contract_id: int,
    product_requirements: dict[str, Any],
    *,
    notice_id: str | None = None,
    solicitation_number: str | None = None,
    schema_version: str = "product-requirements-v1",
) -> int:
    """Upsert durable requirement fields from the product requirements view."""
    from models import ProductRequirement

    skip = {"schema", "status_note"}
    now = now_utc()
    count = 0
    for key, fact in (product_requirements or {}).items():
        if key in skip or not isinstance(fact, dict):
            continue
        status = str(fact.get("status") or STATUS_UNKNOWN)
        row = (
            session.query(ProductRequirement)
            .filter_by(contract_id=contract_id, field_key=key)
            .first()
        )
        if row is None:
            row = ProductRequirement(contract_id=contract_id, field_key=key)
            session.add(row)
        row.notice_id = notice_id
        row.solicitation_number = solicitation_number
        row.field_type = "fact_envelope"
        row.value_json = fact.get("value")
        row.status = status
        row.source_document_id = fact.get("document_id")
        row.evidence_text = fact.get("evidence_text")
        row.evidence_ref = fact.get("source_reference") or fact.get("source_field")
        row.source_type = fact.get("source_type")
        row.schema_version = schema_version
        row.extracted_at = now
        if status == STATUS_VERIFIED:
            row.verified_at = now
        count += 1
    session.flush()
    return count


def upsert_deal_state(session: Any, contract_id: int, payload: dict[str, Any]) -> Any:
    from decimal import Decimal

    from models import DealState

    row = session.query(DealState).filter_by(contract_id=contract_id).first()
    if row is None:
        row = DealState(contract_id=contract_id)
        session.add(row)
    for key in (
        "core_fit",
        "pipeline_stage",
        "decision",
        "match_class",
    ):
        if key in payload:
            setattr(row, key, payload.get(key))
    if "reason_codes" in payload:
        row.reason_codes_json = payload["reason_codes"]
    if "economics" in payload:
        row.economics_json = payload["economics"]
    if "deal_score" in payload:
        row.deal_score_json = payload["deal_score"]
    if "portfolio" in payload:
        row.portfolio_json = payload["portfolio"]
    if "financing_gate" in payload:
        row.financing_gate_json = payload["financing_gate"]
    if "research_plan" in payload:
        row.research_plan_json = payload["research_plan"]
    if "funnel_checkpoint" in payload:
        row.funnel_checkpoint_json = payload["funnel_checkpoint"]
    if "operator_bid_amount" in payload and payload["operator_bid_amount"] is not None:
        row.operator_bid_amount = Decimal(str(payload["operator_bid_amount"]))
    if "ai_cost_usd" in payload and payload["ai_cost_usd"] is not None:
        row.ai_cost_usd = Decimal(str(payload["ai_cost_usd"]))
    session.flush()
    return row


def record_research_event(session: Any, **fields: Any) -> Any:
    from models import ResearchEvent

    ev = ResearchEvent(**{k: v for k, v in fields.items() if hasattr(ResearchEvent, k)})
    session.add(ev)
    session.flush()
    return ev


def load_current_supplier_offers(session: Any, contract_id: int) -> list[dict[str, Any]]:
    """CURRENT offers only — historical rows excluded from deal-usable acquisition cost."""
    from models import SupplierOffer

    rows = (
        session.query(SupplierOffer)
        .filter_by(contract_id=contract_id, temporal_class="CURRENT")
        .filter(SupplierOffer.verification_status == "VERIFIED")
        .all()
    )
    return [
        {
            "id": r.id,
            "unit_price": float(r.unit_price) if r.unit_price is not None else None,
            "source_type": r.source_type,
            "temporal_class": r.temporal_class,
            "quote_date": r.quote_date.isoformat() if r.quote_date else None,
            "expiration_date": r.expiration_date.isoformat() if r.expiration_date else None,
            "verification_status": r.verification_status,
            "code": "CURRENT_SUPPLIER_QUOTE",
            "status": "VERIFIED",
        }
        for r in rows
    ]
