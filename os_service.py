"""GovCon OS — dashboard, pipeline, global today queue (zero external calls)."""

from __future__ import annotations
from application_clock import now_utc, today_local

from datetime import date, datetime, timedelta, timezone
from typing import Any

from deal_lifecycle import ACTIVE_LIFECYCLES, map_internal_to_lifecycle, pipeline_bucket
from funding_engine import FUNDING_BLOCKED, build_funding_plan_snapshot
from next_action_engine import build_today_queue, generate_next_actions


def _lite_workspace(session: Any, contract: Any, deal: Any) -> dict[str, Any]:
    """Minimal workspace slice for OS aggregation — avoids full snapshot cost."""
    from deal_context import primary_product_label, required_quantity_from_bom
    from deal_readiness import evaluate_bid_readiness, evaluate_deal_readiness
    from bom_gate import evaluate_bom_completeness
    from models import DealState, FinancingPursuit, SupplierOffer
    from quote_validation import QUOTE_VALID

    checkpoint = (deal.funnel_checkpoint_json if deal else None) or {}
    bom = checkpoint.get("bom") if isinstance(checkpoint.get("bom"), list) else []
    bom_gate = evaluate_bom_completeness(bom)
    pkg = checkpoint.get("package_completeness") if isinstance(checkpoint.get("package_completeness"), dict) else {}
    offers = session.query(SupplierOffer).filter_by(contract_id=contract.id).all()
    quotes = [{"validation_status": o.validation_status, "total": float(o.extended_price) if o.extended_price else None} for o in offers]
    has_valid = any(o.validation_status == QUOTE_VALID for o in offers)
    commercial = checkpoint.get("commercial") or {}
    commercial_status = {
        "has_valid_quote": has_valid,
        "quote_requested": bool(commercial.get("quote_requested")),
        "proposed_bid_set": deal.operator_bid_amount is not None if deal else False,
        "financing_status": "FINANCING_UNRESOLVED",
    }
    pursuits = session.query(FinancingPursuit).filter_by(contract_id=contract.id).all()
    fin_view = {"status": "FINANCING_UNRESOLVED", "pursuits": [{"pg_required": p.pg_required} for p in pursuits]}
    if any(p.approval_status == "FINANCING_PASS" for p in pursuits):
        fin_view["status"] = "FINANCING_PASS"
    economics = (deal.economics_json if deal else None) or {}
    deal_rd = evaluate_deal_readiness(
        bom=bom,
        bom_gate=bom_gate,
        quotes=quotes,
        required_quantity=required_quantity_from_bom(bom),
        financing=fin_view,
        economics=economics,
        availability_verified=False,
    )
    bid_rd = evaluate_bid_readiness(deal_readiness=deal_rd, solicitation_package=pkg)
    funding_plan = build_funding_plan_snapshot(
        contract_id=contract.id,
        pursuits=[{"pg_required": p.pg_required, "personal_credit_required": p.personal_credit_required,
                   "borrower_cash_required": p.borrower_cash_required, "approval_status": p.approval_status,
                   "verification_status": p.verification_status} for p in pursuits],
        quotes=quotes,
        economics=economics,
    )
    lifecycle = map_internal_to_lifecycle(
        decision=deal.decision if deal else None,
        pipeline_stage=deal.pipeline_stage if deal else None,
        deal_readiness=deal_rd,
        bid_readiness=bid_rd,
        commercial_status=commercial_status,
        funding_status=funding_plan.get("pre_bid_status"),
    )
    ws = {
        "opportunity": {
            "id": contract.id,
            "title": contract.title,
            "agency": contract.agency,
            "due_date": contract.due_date.isoformat() if contract.due_date else None,
        },
        "solicitation_package": pkg,
        "bom_gate": bom_gate,
        "commercial_status": commercial_status,
        "deal_readiness": deal_rd,
        "bid_readiness": bid_rd,
        "funding_plan": funding_plan,
        "product": primary_product_label(bom),
    }
    ws["lifecycle"] = lifecycle
    ws["pipeline_bucket"] = pipeline_bucket(lifecycle=lifecycle, workspace=ws)
    na = generate_next_actions(workspace={**ws, "quotes": quotes, "financing": fin_view, "commercial": commercial}, missing_items=[])
    ws["next_action"] = na.get("next_action")
    return ws


def build_os_dashboard(session: Any, *, today: date | None = None) -> dict[str, Any]:
    from models import Contract, DealState

    as_of = today or today_local()
    deals = (
        session.query(DealState, Contract)
        .join(Contract, DealState.contract_id == Contract.id)
        .filter(DealState.core_fit == "CORE_PRODUCT")
        .all()
    )
    counts = {
        "active_deals": 0,
        "needs_supplier_quotes": 0,
        "waiting_on_suppliers": 0,
        "funding_problems": 0,
        "needs_co_clarification": 0,
        "deal_ready": 0,
        "bid_ready": 0,
        "due_soon": 0,
        "submitted": 0,
        "awarded": 0,
    }
    cards: list[dict[str, Any]] = []
    for deal, contract in deals:
        ws = _lite_workspace(session, contract, deal)
        lc = ws.get("lifecycle")
        if lc in ACTIVE_LIFECYCLES:
            counts["active_deals"] += 1
        cs = ws.get("commercial_status") or {}
        if not cs.get("has_valid_quote") and not cs.get("quote_requested"):
            counts["needs_supplier_quotes"] += 1
        if cs.get("quote_requested") and not cs.get("has_valid_quote"):
            counts["waiting_on_suppliers"] += 1
        fp = ws.get("funding_plan") or {}
        if fp.get("pre_bid_status") in {FUNDING_BLOCKED, "FINANCING_FAIL", "FINANCING_UNRESOLVED"}:
            counts["funding_problems"] += 1
        if ws.get("pipeline_bucket") == "Needs CO Clarification":
            counts["needs_co_clarification"] += 1
        if (ws.get("deal_readiness") or {}).get("status") == "DEAL_READY":
            counts["deal_ready"] += 1
        if (ws.get("bid_readiness") or {}).get("status") == "BID_READY":
            counts["bid_ready"] += 1
        if contract.due_date and contract.due_date <= as_of + timedelta(days=7):
            counts["due_soon"] += 1
        if lc == "SUBMITTED":
            counts["submitted"] += 1
        if lc == "AWARDED":
            counts["awarded"] += 1

    cards = [
        {"label": "Active Deals", "count": counts["active_deals"], "filter": "active_deals", "href": "#gos-pipeline"},
        {"label": "Need Supplier Quotes", "count": counts["needs_supplier_quotes"], "filter": "needs_quote", "href": "#gos-pipeline?bucket=Needs Quote"},
        {"label": "Waiting on Suppliers", "count": counts["waiting_on_suppliers"], "filter": "waiting_supplier", "href": "#gos-pipeline?bucket=Waiting on Supplier"},
        {"label": "Funding Problems", "count": counts["funding_problems"], "filter": "funding", "href": "#gos-pipeline?bucket=Funding Blocked"},
        {"label": "Deal Ready", "count": counts["deal_ready"], "filter": "deal_ready", "href": "#gos-pipeline?bucket=Deal Ready"},
        {"label": "Bid Ready", "count": counts["bid_ready"], "filter": "bid_ready", "href": "#gos-pipeline?bucket=Bid Ready"},
        {"label": "Due Soon", "count": counts["due_soon"], "filter": "due_soon", "href": "#gos-pipeline?due=7"},
    ]
    return {
        "as_of": as_of.isoformat(),
        "headline": "What needs my attention today?",
        "counts": counts,
        "cards": cards,
        "LIVE_API_REQUESTS": 0,
        "external_calls": {"SAM": 0, "OpenAI": 0, "web": 0, "USAspending": 0},
    }


def build_active_deals_pipeline(session: Any, *, bucket: str | None = None) -> dict[str, Any]:
    from models import Contract, DealState

    rows = []
    q = (
        session.query(DealState, Contract)
        .join(Contract, DealState.contract_id == Contract.id)
        .filter(DealState.core_fit == "CORE_PRODUCT")
    )
    for deal, contract in q.all():
        ws = _lite_workspace(session, contract, deal)
        b = ws.get("pipeline_bucket") or ""
        if bucket and bucket.lower() not in b.lower():
            continue
        econ = (deal.economics_json if deal else None) or {}
        profit = econ.get("actual_profit")
        rows.append(
            {
                "opportunity_id": contract.id,
                "title": contract.title,
                "agency": contract.agency,
                "product": ws.get("product"),
                "deadline": contract.due_date.isoformat() if contract.due_date else None,
                "potential_profit": profit if profit is not None else None,
                "profit_status": econ.get("actual_profit_status") or "INCOMPLETE",
                "funding_status": (ws.get("funding_plan") or {}).get("pre_bid_status"),
                "deal_readiness": (ws.get("deal_readiness") or {}).get("status"),
                "bid_readiness": (ws.get("bid_readiness") or {}).get("status"),
                "lifecycle": ws.get("lifecycle"),
                "pipeline_bucket": b,
                "next_action": (ws.get("next_action") or {}).get("action"),
            }
        )
    rows.sort(key=lambda r: (r.get("deadline") or "9999", r.get("opportunity_id") or 0))
    return {"rows": rows, "count": len(rows), "bucket_filter": bucket, "LIVE_API_REQUESTS": 0}


def build_global_today_queue(session: Any, *, today: date | None = None) -> dict[str, Any]:
    from models import Contract, DealState

    as_of = today or today_local()
    all_actions: list[dict[str, Any]] = []
    q = (
        session.query(DealState, Contract)
        .join(Contract, DealState.contract_id == Contract.id)
        .filter(DealState.core_fit == "CORE_PRODUCT")
    )
    for deal, contract in q.all():
        ws = _lite_workspace(session, contract, deal)
        na = generate_next_actions(
            workspace={
                **ws,
                "quotes": [],
                "financing": {"status": (ws.get("funding_plan") or {}).get("pre_bid_status")},
                "commercial": ws.get("commercial_status") or {},
            },
            missing_items=[],
            today=as_of,
        )
        primary = na.get("next_action") or {}
        if not primary.get("action"):
            continue
        blocker = (ws.get("deal_readiness") or {}).get("blockers") or []
        all_actions.append(
            {
                **primary,
                "opportunity_id": contract.id,
                "opportunity_title": contract.title,
                "deadline": contract.due_date.isoformat() if contract.due_date else None,
                "blocker": blocker[0] if blocker else None,
                "primary_button": _primary_button_for_action(primary.get("action") or ""),
                "pipeline_bucket": ws.get("pipeline_bucket"),
            }
        )
    all_actions.sort(key=lambda a: (a.get("priority") or 999, a.get("deadline") or ""))
    tq = build_today_queue({"queue": all_actions}, today=as_of)
    return {
        **tq,
        "actions": all_actions,
        "count": len(all_actions),
        "LIVE_API_REQUESTS": 0,
    }


def _primary_button_for_action(action: str) -> str:
    a = action.upper()
    if "SUPPLIER QUOTE" in a or "SUPPLIER" in a:
        return "LOG CALL"
    if "FOLLOW UP" in a and "QUOTE" in a:
        return "ENTER QUOTE"
    if "FINANCING" in a or "FUNDING" in a:
        return "REVIEW FUNDING"
    if "CO CLARIFICATION" in a:
        return "SHOW ME WHAT TO SAY"
    if "BID" in a:
        return "OPEN DEAL"
    return "OPEN DEAL"
