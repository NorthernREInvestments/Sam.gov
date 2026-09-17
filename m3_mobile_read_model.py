"""Thin mobile operator read-models — backend-authoritative, no fabricated economics."""

from __future__ import annotations

from typing import Any

from m3_lifecycle import derive_lifecycle, determine_next_action, readiness_summary
from m3_pipeline_store import M3PipelineStore
from operating_mode import is_controlled_verification, is_development_no_outreach
from procurement_source_registry import ProcurementSourceRegistry


def _unknown(v: Any) -> Any:
    if v is None or v == "" or v == "UNKNOWN":
        return "UNKNOWN"
    return v


def _money(v: Any) -> Any:
    if v is None or v == "" or v == "UNKNOWN":
        return "UNKNOWN"
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return "UNKNOWN"


def opportunity_card_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Compact mobile card payload — never invent revenue/profit."""
    econ = row.get("transaction_economics") or row.get("economics") or {}
    readiness = row.get("readiness_summary") or readiness_summary(row)
    nxt = row.get("pending_next_action") or determine_next_action(row)
    revenue = _money(
        econ.get("revenue")
        or econ.get("government_revenue")
        or row.get("government_revenue")
        or row.get("revenue")
        or readiness.get("supported_revenue")
    )
    profit = _money(
        econ.get("expected_actual_profit")
        or row.get("expected_actual_profit")
        or readiness.get("supported_expected_profit")
    )
    # Do not fabricate: if both unknown, leave unknown
    if revenue == "UNKNOWN" and profit == "UNKNOWN":
        pass
    days = readiness.get("time_left")
    if days == "UNKNOWN":
        days = (row.get("deadline_evaluation") or {}).get("calendar_days_remaining")
        days = days if days is not None else "UNKNOWN"
    return {
        "canonical_id": row.get("canonical_id"),
        "buyer": row.get("agency") or row.get("buyer") or "UNKNOWN",
        "title": row.get("title") or "Untitled",
        "solicitation_number": row.get("solicitation_number") or row.get("external_id"),
        "deadline": row.get("deadline") or "UNKNOWN",
        "days_remaining": days,
        "lifecycle": row.get("lifecycle") or derive_lifecycle(row),
        "supported_revenue": revenue,
        "supported_profit": profit,
        "profit_confidence": _unknown(
            econ.get("confidence") or row.get("profit_confidence") or readiness.get("profit_confidence")
        ),
        "funding_state": _unknown(
            row.get("funding_status")
            or (row.get("funding_requirement") or {}).get("status")
            or "UNKNOWN"
        ),
        "next_action": nxt.get("next_action") if isinstance(nxt, dict) else nxt,
        "next_action_reason": (nxt or {}).get("reason") if isinstance(nxt, dict) else None,
        "why_waiting": readiness.get("why_not_ready") or row.get("stop_reason") or "UNKNOWN",
        "package_access": row.get("package_access") or "UNKNOWN",
        "product_category": row.get("product_category") or row.get("product_classification") or "UNKNOWN",
        "priority": _priority_score(row, nxt if isinstance(nxt, dict) else {}),
    }


def _priority_score(row: dict[str, Any], nxt: dict[str, Any]) -> int:
    """Lower = higher priority. Deadline risk > deal-killer > funding > commercial."""
    score = 50
    days = (row.get("deadline_evaluation") or {}).get("calendar_days_remaining")
    if isinstance(days, (int, float)):
        if days <= 3:
            score -= 40
        elif days <= 9:
            score -= 25
        elif days <= 14:
            score -= 10
    action = str(nxt.get("next_action") or "")
    if "COMPLIANCE" in action or "deal.killer" in str(row.get("stop_reason") or "").lower():
        score -= 20
    if "FUNDING" in action:
        score -= 15
    if "COMMERCIAL" in action or "QUOTE" in str(row.get("stop_reason") or "").upper():
        score -= 12
    if row.get("expected_actual_profit") not in {None, "UNKNOWN"}:
        try:
            if float(row["expected_actual_profit"]) >= 10000:
                score -= 8
        except (TypeError, ValueError):
            pass
    return score


def deal_room_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Full mobile deal-room sections — UNKNOWN preserved, no fabrication."""
    card = opportunity_card_summary(row)
    readiness = row.get("readiness_summary") or readiness_summary(row)
    econ = row.get("transaction_economics") or row.get("economics") or {}
    funding = row.get("funding_requirement") or {}
    compliance = row.get("bid_compliance") or row.get("compliance") or {}
    pricing = row.get("bid_pricing") or {}
    actions = row.get("operator_actions") or []
    bom = row.get("line_items") or row.get("bom") or []
    return {
        "kind": "M3DealRoom",
        "canonical_id": row.get("canonical_id"),
        "overview": {
            "buyer": card["buyer"],
            "solicitation": card["solicitation_number"],
            "title": card["title"],
            "source": row.get("source_id") or "UNKNOWN",
            "deadline": card["deadline"],
            "days_remaining": card["days_remaining"],
            "lifecycle": card["lifecycle"],
            "detail_url": row.get("detail_url"),
        },
        "product_fit": {
            "category": card["product_category"],
            "classification": row.get("product_classification") or "UNKNOWN",
            "confidence": (row.get("product_audit") or {}).get("audit_label")
            or (row.get("category_meta") or {}).get("confidence")
            or "UNKNOWN",
            "evidence": (row.get("product_audit") or {}).get("title") or row.get("title"),
        },
        "requirements": {
            "bom_lines": [
                {
                    "description": li.get("description") if isinstance(li, dict) else str(li),
                    "quantity": (li.get("quantity") if isinstance(li, dict) else None) or "UNKNOWN",
                    "unit": (li.get("unit") if isinstance(li, dict) else None) or "UNKNOWN",
                    "specification": (li.get("part_number") or li.get("manufacturer"))
                    if isinstance(li, dict)
                    else "UNKNOWN",
                }
                for li in (bom[:40] if isinstance(bom, list) else [])
            ],
            "missing_information": readiness.get("what_we_dont_know") or [],
            "package_access": card["package_access"],
        },
        "economics": {
            "revenue": card["supported_revenue"],
            "acquisition_evidence": _money(row.get("acquisition_cost") or econ.get("acquisition")),
            "freight": _money(
                row.get("freight_cost")
                or (
                    (row.get("freight") or {}).get("value")
                    if isinstance(row.get("freight"), dict)
                    else None
                )
            ),
            "financing": _money(row.get("financing_cost") or row.get("financing_cost_amount")),
            "expected_profit": card["supported_profit"],
            "confidence": card["profit_confidence"],
            "note": "UNKNOWN values are not fabricated",
        },
        "funding": {
            "capital_requirement": _money(
                funding.get("capital_amount") or row.get("working_capital_required") or readiness.get("capital_requirement")
            ),
            "funding_status": card["funding_state"],
            "unknowns": ["eligibility", "cost", "pg", "fico"]
            if str(card["funding_state"]).upper()
            in {"UNKNOWN", "FUNDING_VERIFICATION_REQUIRED", "VERIFICATION_REQUIRED"}
            else [],
            "verification_needs": (
                "FUTURE_ACTION_IF_PURSUED"
                if is_development_no_outreach()
                else (
                    "OPERATOR_AUTHORIZED_CONTROLLED"
                    if is_controlled_verification()
                    else "operator_authorized"
                )
            ),
            "unknown_financing_is_not_rejection": True,
        },
        "compliance": {
            "requirements": compliance.get("resolved") or compliance.get("requirements") or [],
            "blockers": compliance.get("unresolved")
            or compliance.get("critical")
            or row.get("compliance_blockers")
            or [],
            "missing_items": compliance.get("mandatory_unresolved") or [],
        },
        "pricing": {
            "bid_status": row.get("bid_price_status")
            or ("PRESENT" if pricing else "UNKNOWN"),
            "evidence_level": "COMMERCIAL_VERIFICATION_REQUIRED"
            if row.get("bid_price_status") == "BID_PRICE_REQUIRES_COMMERCIAL_VERIFICATION"
            or card["supported_profit"] == "UNKNOWN"
            else "SUPPORTED_WHEN_EVIDENCE_EXISTS",
            "verification_needs": list((row.get("commercial_verification_plan") or {}).get("items") or [])[:8]
            or "UNKNOWN",
        },
        "actions": {
            "what_m3_can_do": readiness.get("what_m3_can_do_automatically"),
            "what_operator_must_do": readiness.get("what_requires_operator"),
            "next_action": card["next_action"],
            "why_not_ready": card["why_waiting"],
            "operator_actions": [
                {
                    "action_type": a.get("action_type"),
                    "why": a.get("why_needed"),
                    "priority": a.get("priority"),
                    "deadline": a.get("deadline"),
                    "timing": a.get("action_timing"),
                    "status": a.get("status"),
                }
                for a in actions
                if isinstance(a, dict)
            ],
        },
        "readiness": readiness,
        "DEVELOPMENT_NO_OUTREACH": is_development_no_outreach(),
    }


def action_queue_mobile(store: M3PipelineStore | None = None) -> dict[str, Any]:
    store = store or M3PipelineStore()
    raw = store.operator_queue()
    cards = []
    by_id = {r["canonical_id"]: r for r in store.all()}
    for item in raw:
        cid = item.get("canonical_id") or item.get("deal_id")
        row = by_id.get(cid) or {}
        nxt = item.get("next_action") if isinstance(item.get("next_action"), dict) else determine_next_action(row or item)
        cards.append(
            {
                "canonical_id": cid,
                "opportunity_title": item.get("title") or row.get("title") or "Opportunity",
                "buyer": item.get("buyer") or row.get("agency") or "UNKNOWN",
                "priority_label": _priority_label(item.get("priority") or nxt),
                "priority": item.get("priority") if item.get("priority") is not None else _priority_score(row, nxt if isinstance(nxt, dict) else {}),
                "need": item.get("action_type")
                or (nxt.get("next_action") if isinstance(nxt, dict) else None)
                or "REVIEW",
                "reason": item.get("why_needed")
                or (nxt.get("reason") if isinstance(nxt, dict) else None)
                or row.get("stop_reason")
                or "UNKNOWN",
                "deadline": item.get("deadline") or row.get("deadline") or "UNKNOWN",
                "missing_evidence": (readiness_summary(row).get("what_we_dont_know") if row else [])
                or ["UNKNOWN"],
                "expected_impact": item.get("unlocks_pipeline_stage")
                or (nxt.get("note") if isinstance(nxt, dict) else None)
                or "Unblocks next pipeline stage",
                "lifecycle": row.get("lifecycle") or item.get("lifecycle"),
            }
        )
    cards.sort(key=lambda c: (c.get("priority") if isinstance(c.get("priority"), int) else 50, str(c.get("deadline"))))
    return {
        "kind": "M3MobileActionQueue",
        "question": "WHAT DO I NEED TO DO?",
        "count": len(cards),
        "actions": cards,
        "DEVELOPMENT_NO_OUTREACH": is_development_no_outreach(),
    }


def _priority_label(priority: Any) -> str:
    if isinstance(priority, dict):
        action = str(priority.get("next_action") or "")
        if "FUNDING" in action or "COMPLIANCE" in action:
            return "HIGH PRIORITY"
        return "NORMAL"
    try:
        p = int(priority)
    except (TypeError, ValueError):
        return "NORMAL"
    if p <= 15:
        return "HIGH PRIORITY"
    if p <= 35:
        return "MEDIUM PRIORITY"
    return "NORMAL"


def mobile_dashboard_summary(store: M3PipelineStore | None = None) -> dict[str, Any]:
    store = store or M3PipelineStore()
    rows = store.all()
    cards = [opportunity_card_summary(r) for r in rows]
    cards.sort(key=lambda c: (c.get("priority", 50), str(c.get("deadline") or "9999")))
    actions = action_queue_mobile(store)
    active = [
        c
        for c in cards
        if c.get("lifecycle")
        not in {"REJECTED", "REJECTED_CHEAP_SCREEN", "CANCELLED", "CLOSED", "AWARDED", "ARCHIVED", "LOST"}
    ]
    return {
        "kind": "M3MobileDashboard",
        "active_count": len(active),
        "action_count": actions["count"],
        "active_opportunities": active[:40],
        "top_actions": actions["actions"][:15],
        "DEVELOPMENT_NO_OUTREACH": is_development_no_outreach(),
        "payload_note": "compact read-model; economics UNKNOWN when unsupported",
    }


def mobile_sources_summary() -> dict[str, Any]:
    reg = ProcurementSourceRegistry()
    summary = reg.coverage_summary()
    healthy = len(reg.by_health("HEALTHY_PRODUCTION"))
    return {
        "kind": "M3MobileSources",
        "registered": len(reg.all_sources()),
        "healthy_production": healthy,
        "by_health": (summary.get("KNOWN_SOURCE_COVERAGE") or {}).get("by_health_state")
        or summary.get("by_health_state"),
        "claim_100_percent_coverage": False,
        "note": "Source health ≠ national market coverage",
    }
