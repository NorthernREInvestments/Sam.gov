"""Persist commercial quote / financing / proposed-bid mutations (zero external)."""

from __future__ import annotations
from application_clock import now_utc, today_local

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from commercial_economics import (
    evaluate_financing_entry,
    recalculate_commercial_economics,
    record_proposed_bid,
)
from commercial_quotes import (
    bom_memory_per_server,
    bom_storage_per_server,
    normalize_quote_entry,
    quote_document_record,
    validate_quote_against_bom,
)
from economic_integrity import (
    COST_CALCULATED,
    COST_NOT_APPLICABLE,
    COST_REQUIRED_UNKNOWN,
    COST_VERIFIED,
)
from quote_validation import QUOTE_VALID


def _parse_date(val: Any) -> date | None:
    if val is None or val == "" or val == "UNKNOWN":
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    try:
        return date.fromisoformat(str(val)[:10])
    except ValueError:
        return None


def _bom_from_deal(deal: Any) -> list[dict[str, Any]]:
    checkpoint = (deal.funnel_checkpoint_json if deal else None) or {}
    bom = checkpoint.get("bom") if isinstance(checkpoint.get("bom"), list) else []
    return bom


def save_operator_quote(
    session: Any,
    *,
    contract_id: int,
    raw: dict[str, Any],
) -> dict[str, Any]:
    from models import DealState, SupplierOffer

    deal = session.query(DealState).filter_by(contract_id=contract_id).first()
    bom = _bom_from_deal(deal)
    normalized = normalize_quote_entry(raw)
    validation = validate_quote_against_bom(
        normalized,
        required_bom=bom,
        required_quantity=int(raw.get("required_quantity") or 14),
        required_memory_per_server=bom_memory_per_server(bom),
        required_storage_per_server=bom_storage_per_server(bom),
    )
    normalized["validation"] = validation
    normalized["validation_status"] = validation.get("status")

    offer = SupplierOffer(
        contract_id=contract_id,
        supplier_id=raw.get("supplier_id"),
        quote_number=normalized.get("quote_number"),
        unit_price=Decimal(str(normalized["unit_price"])) if normalized.get("unit_price") is not None else None,
        quantity_basis=Decimal(str(normalized["quantity"])) if normalized.get("quantity") is not None else None,
        extended_price=Decimal(str(normalized["extended_price"]))
        if normalized.get("extended_price") is not None
        else None,
        extended_price_status=normalized.get("extended_price_status"),
        availability=normalized.get("availability"),
        lead_time=normalized.get("lead_time"),
        shipping_included=True if raw.get("freight") in (0, 0.0, "0") else None,
        quote_date=_parse_date(normalized.get("quote_date")),
        expiration_date=_parse_date(normalized.get("expiration_date")),
        source_type="FORMAL_QUOTE",
        temporal_class="CURRENT",
        verification_status=normalized.get("verification_status") or "OPERATOR_ENTERED",
        validation_status=validation.get("status"),
        bom_match=normalized.get("bom_match"),
        notes=normalized.get("document_notes"),
        quote_details_json=normalized,
        retrieved_at=now_utc(),
    )
    session.add(offer)
    session.flush()

    if deal is not None:
        checkpoint = dict(deal.funnel_checkpoint_json or {})
        commercial = dict(checkpoint.get("commercial") or {})
        commercial["last_quote_id"] = offer.id
        commercial["last_quote_at"] = now_utc().isoformat()
        checkpoint["commercial"] = commercial
        deal.funnel_checkpoint_json = checkpoint
        _recalc_and_store_economics(session, deal, contract_id=contract_id)

    return {
        "LIVE_API_REQUESTS": 0,
        "offer_id": offer.id,
        "quote": normalized,
        "validation": validation,
    }


def attach_quote_document(
    session: Any,
    *,
    contract_id: int,
    supplier_id: int | None,
    offer_id: int | None,
    filename: str,
    content_hash: str | None = None,
    quote_number: str | None = None,
) -> dict[str, Any]:
    from models import QuoteDocument

    rec = quote_document_record(
        filename=filename,
        content_hash=content_hash,
        supplier_id=supplier_id,
        opportunity_id=contract_id,
        quote_number=quote_number,
    )
    row = QuoteDocument(
        contract_id=contract_id,
        supplier_id=supplier_id,
        offer_id=offer_id,
        quote_number=quote_number,
        filename=filename,
        content_hash=content_hash,
        received_date=today_local(),
        verification_state=rec["verification_state"],
        extraction_status=rec["extraction_status"],
        notes=rec["notes"],
    )
    session.add(row)
    session.flush()
    return {"LIVE_API_REQUESTS": 0, "document_id": row.id, "record": rec}


def save_financing_entry(
    session: Any,
    *,
    contract_id: int,
    raw: dict[str, Any],
) -> dict[str, Any]:
    from models import DealState, FinancingProvider, FinancingPursuit

    evaluated = evaluate_financing_entry(raw)
    provider_name = raw.get("provider")
    provider_id = raw.get("provider_id")
    if provider_id is None and provider_name:
        fp = session.query(FinancingProvider).filter(FinancingProvider.name == provider_name).first()
        if fp is None:
            fp = FinancingProvider(name=str(provider_name), verification_status="OPERATOR_REPORTED")
            session.add(fp)
            session.flush()
        provider_id = fp.id

    pursuit = FinancingPursuit(
        contract_id=contract_id,
        provider_id=provider_id,
        contact_name=raw.get("contact"),
        transaction_type=raw.get("transaction_type") or "federal_product_resale",
        pg_required=None if raw.get("pg_required") in (None, "UNKNOWN") else bool(raw.get("pg_required")),
        personal_credit_required=None
        if raw.get("personal_credit_required") in (None, "UNKNOWN")
        else bool(raw.get("personal_credit_required")),
        borrower_cash_required=None
        if raw.get("cash_contribution") in (None, "", "UNKNOWN", 0, 0.0, "0")
        else True,
        supplier_direct_payment=None
        if raw.get("supplier_paid_direct") in (None, "UNKNOWN")
        else bool(raw.get("supplier_paid_direct")),
        minimum_amount=Decimal(str(raw["minimum"])) if raw.get("minimum") not in (None, "", "UNKNOWN") else None,
        maximum_amount=Decimal(str(raw["maximum"])) if raw.get("maximum") not in (None, "", "UNKNOWN") else None,
        fees=None if raw.get("fees") in (None, "UNKNOWN") else str(raw.get("fees")),
        recourse=None if raw.get("recourse") in (None, "UNKNOWN") else str(raw.get("recourse")),
        approval_status=evaluated["status"],
        verification_status="OPERATOR_ENTERED",
        notes=raw.get("notes"),
        evidence_json={"entry": raw, "evaluated": evaluated, "date": raw.get("date")},
    )
    # cash contribution amount tracking
    cash = raw.get("cash_contribution")
    if cash in (None, "", "UNKNOWN"):
        pursuit.borrower_cash_required = None
    elif cash in (0, 0.0, "0", False, "NO", "NONE", "ZERO"):
        pursuit.borrower_cash_required = False
    else:
        try:
            pursuit.borrower_cash_required = float(cash) > 0
        except (TypeError, ValueError):
            pursuit.borrower_cash_required = None

    session.add(pursuit)
    session.flush()

    deal = session.query(DealState).filter_by(contract_id=contract_id).first()
    if deal is not None:
        _recalc_and_store_economics(session, deal, contract_id=contract_id)

    return {"LIVE_API_REQUESTS": 0, "pursuit_id": pursuit.id, "evaluation": evaluated}


def save_proposed_bid(
    session: Any,
    *,
    contract_id: int,
    amount: float | None,
    entered_by: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    from models import DealState

    deal = session.query(DealState).filter_by(contract_id=contract_id).first()
    if deal is None:
        from knowledge_store import upsert_deal_state

        deal = upsert_deal_state(session, contract_id, {"core_fit": "CORE_PRODUCT"})

    checkpoint = dict(deal.funnel_checkpoint_json or {})
    commercial = dict(checkpoint.get("commercial") or {})
    history = list(commercial.get("proposed_bid_history") or [])
    recorded = record_proposed_bid(
        amount=amount,
        entered_by=entered_by,
        reason=reason,
        prior_history=history,
    )
    commercial["proposed_bid"] = recorded["current"]
    commercial["proposed_bid_history"] = recorded["history"]
    checkpoint["commercial"] = commercial
    deal.funnel_checkpoint_json = checkpoint
    if amount is not None:
        deal.operator_bid_amount = Decimal(str(amount))
    else:
        deal.operator_bid_amount = None

    econ = _recalc_and_store_economics(session, deal, contract_id=contract_id)
    return {
        "LIVE_API_REQUESTS": 0,
        "proposed_bid": recorded,
        "economics": econ,
        "is_government_verified_value": False,
        "fact_class": "COMPANY_PROPOSED_BID_PRICE",
    }


def _recalc_and_store_economics(session: Any, deal: Any, *, contract_id: int) -> dict[str, Any]:
    from models import FinancingPursuit, SupplierOffer
    from quote_validation import validate_supplier_quote

    offers = session.query(SupplierOffer).filter_by(contract_id=contract_id).all()
    best_acq = None
    best_freight = None
    supplier_status = COST_REQUIRED_UNKNOWN
    freight_status = COST_REQUIRED_UNKNOWN

    for o in offers:
        details = o.quote_details_json if isinstance(o.quote_details_json, dict) else {}
        validation = details.get("validation") or validate_supplier_quote(
            {
                "verification_status": o.verification_status,
                "quantity": float(o.quantity_basis) if o.quantity_basis is not None else None,
                "unit_price": float(o.unit_price) if o.unit_price is not None else None,
                "extended_price": float(o.extended_price) if o.extended_price is not None else None,
                "expiration_date": o.expiration_date.isoformat() if o.expiration_date else None,
                "bom_match": o.bom_match,
            },
            required_quantity=14,
        )
        if validation.get("status") != QUOTE_VALID or not validation.get("usable_as_acquisition_cost"):
            # still allow incomplete product price for display, not acquisition
            continue
        ext = float(o.extended_price) if o.extended_price is not None else None
        if ext is None and o.unit_price is not None and o.quantity_basis is not None:
            ext = float(o.unit_price) * float(o.quantity_basis)
        if ext is None:
            continue
        if best_acq is None or ext < best_acq:
            best_acq = ext
            supplier_status = COST_VERIFIED if o.verification_status in {
                "VERIFIED",
                "OPERATOR_CONFIRMED",
                "VERIFIED_FROM_DOCUMENT",
            } else COST_CALCULATED
            fr = details.get("freight")
            if details.get("freight_status") == "UNKNOWN" or fr is None:
                freight_status = COST_REQUIRED_UNKNOWN
                best_freight = None
            else:
                best_freight = float(fr)
                freight_status = COST_VERIFIED

    fin_status = COST_REQUIRED_UNKNOWN
    fin_cost = None
    pursuits = session.query(FinancingPursuit).filter_by(contract_id=contract_id).all()
    if any(p.approval_status == "FINANCING_PASS" and p.verification_status == "VERIFIED" for p in pursuits):
        # fee unknown still blocks
        fin_status = COST_REQUIRED_UNKNOWN
        for p in pursuits:
            if p.approval_status == "FINANCING_PASS":
                if p.fees and str(p.fees).upper() not in {"UNKNOWN", "NONE", "0"}:
                    try:
                        fin_cost = float(p.fees)
                        fin_status = COST_VERIFIED
                    except (TypeError, ValueError):
                        fin_status = COST_REQUIRED_UNKNOWN
                elif p.fees in ("0", 0, "NONE"):
                    fin_cost = 0.0
                    fin_status = COST_VERIFIED

    existing = dict(deal.economics_json or {})
    costs_ex = existing.get("costs") if isinstance(existing.get("costs"), dict) else {}
    install = costs_ex.get("installation") or {}
    install_status = install.get("status") or COST_NOT_APPLICABLE
    sub = costs_ex.get("subcontract") or {}
    sub_status = sub.get("status") or COST_NOT_APPLICABLE

    proposed = float(deal.operator_bid_amount) if deal.operator_bid_amount is not None else None
    econ = recalculate_commercial_economics(
        proposed_bid_amount=proposed,
        supplier_acquisition=best_acq,
        supplier_status=supplier_status if best_acq is not None else COST_REQUIRED_UNKNOWN,
        freight=best_freight,
        freight_status=freight_status,
        financing_cost=fin_cost,
        financing_status=fin_status,
        installation_status=install_status,
        subcontract_status=sub_status,
    )
    # Preserve prior cost objects for categories we didn't touch when useful
    merged_costs = dict(costs_ex)
    merged_costs.update(econ.get("costs") or {})
    econ["costs"] = merged_costs
    deal.economics_json = {**(existing or {}), **econ, "costs": merged_costs}
    return econ


def mark_quote_requested(session: Any, *, contract_id: int, supplier_id: int | None) -> dict[str, Any]:
    from models import DealState
    from operator_crm import ACT_QUOTE_REQUESTED, log_manual_activity

    result = log_manual_activity(
        session,
        contract_id=contract_id,
        activity_type=ACT_QUOTE_REQUESTED,
        supplier_id=supplier_id,
        what_happened="Quote requested",
        outcome="QUOTE_REQUESTED",
        next_action="FOLLOW UP FOR QUOTE",
    )
    deal = session.query(DealState).filter_by(contract_id=contract_id).first()
    if deal is not None:
        checkpoint = dict(deal.funnel_checkpoint_json or {})
        commercial = dict(checkpoint.get("commercial") or {})
        commercial["quote_requested"] = True
        commercial["quote_requested_at"] = now_utc().isoformat()
        commercial["quote_requested_supplier_id"] = supplier_id
        checkpoint["commercial"] = commercial
        deal.funnel_checkpoint_json = checkpoint
    return result
