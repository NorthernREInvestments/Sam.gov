"""Compact OperatorDealPacket — what Brian needs to know and do next."""

from __future__ import annotations

from typing import Any

from application_clock import now_utc


def build_operator_deal_packet(
    pipeline_result: dict[str, Any],
    *,
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    deal = pipeline_result.get("deal") or {}
    econ = deal.get("economics") or {}
    wc = deal.get("working_capital") or {}
    compliance = deal.get("compliance") or {}
    shortlist = deal.get("supplier_shortlist") or []
    bom = deal.get("bom") or []

    next_actions = []
    for a in actions or pipeline_result.get("actions") or []:
        next_actions.append(
            {
                "type": a.get("action_type"),
                "who": a.get("who_where"),
                "why": a.get("why_needed"),
                "priority": a.get("priority"),
                "unlocks": a.get("unlocks_pipeline_stage"),
            }
        )

    return {
        "kind": "OperatorDealPacket",
        "generated_at": now_utc().isoformat(),
        "DEAL": {
            "buyer": deal.get("agency"),
            "solicitation": deal.get("solicitation_number"),
            "source": deal.get("source") or deal.get("portal"),
            "deadline": deal.get("bid_deadline") or deal.get("deadline"),
            "time_remaining": (deal.get("deadline_evaluation") or {}).get("status"),
            "deal_type": deal.get("deal_type"),
            "title": deal.get("title"),
        },
        "PRODUCT": {
            "line_items": [
                {
                    "description": li.get("description"),
                    "quantity": li.get("quantity"),
                    "unit": li.get("unit"),
                    "manufacturer": li.get("manufacturer"),
                    "part_number": li.get("part_number"),
                }
                for li in bom[:30]
            ],
            "important_specifications": deal.get("important_specifications"),
            "delivery": deal.get("delivery_destination") or deal.get("ship_to"),
        },
        "SUPPLIER": {
            "best_candidates": [
                {
                    "name": s.get("name") or s.get("supplier"),
                    "authorization": s.get("authorization_evidence"),
                    "staleness": s.get("staleness"),
                }
                for s in shortlist[:5]
            ],
            "price": (deal.get("supplier_cost") or {}).get("value"),
            "price_maturity": (deal.get("supplier_cost") or {}).get("maturity"),
            "freight": (deal.get("freight") or {}).get("value"),
            "freight_maturity": (deal.get("freight") or {}).get("maturity"),
            "terms": (deal.get("supplier_quote") or {}).get("payment_terms"),
            "lead_time": (deal.get("supplier_quote") or {}).get("lead_time"),
            "evidence_state": deal.get("price_sufficiency"),
        },
        "ECONOMICS": {
            "supplier_cost": econ.get("supplier_cost"),
            "freight": econ.get("freight"),
            "other_costs": econ.get("other_costs"),
            "finance_estimate": econ.get("financing_cost"),
            "break_even": econ.get("break_even"),
            "min_bid_for_10k_profit": econ.get("minimum_bid_for_10k_profit"),
            "proposed_bid": econ.get("proposed_bid"),
            "expected_profit": econ.get("expected_actual_profit"),
            "meets_profit_floor": econ.get("meets_profit_floor"),
            "status": econ.get("status"),
            "evidence_maturity": (deal.get("supplier_cost") or {}).get("maturity"),
        },
        "FUNDING": {
            "working_capital_required": wc.get("working_capital_required"),
            "timing": (wc.get("components") or {}),
            "known_compatible_providers": [
                m.get("provider_id")
                for m in (deal.get("finance_matches") or [])
                if m.get("status") in {"LIKELY_COMPATIBLE", "POSSIBLE_WITH_VERIFICATION"}
            ],
            "known_conflicts": [
                m.get("provider_id")
                for m in (deal.get("finance_matches") or [])
                if m.get("status") == "INCOMPATIBLE"
            ],
            "funding_status": deal.get("funding_status"),
            "what_requires_verification": "deal-specific confirmation; public marketing ≠ verified path",
        },
        "COMPLIANCE": {
            "resolved": compliance.get("resolved") or [],
            "unresolved": compliance.get("unresolved") or [],
            "critical_blockers": compliance.get("critical") or [],
        },
        "NEXT_ACTIONS": next_actions,
        "DECISION_STATUS": {
            "readiness": pipeline_result.get("operator_readiness"),
            "pipeline_stage": pipeline_result.get("stage"),
            "why": pipeline_result.get("stop_reason"),
            "what_unlocks_next": next_actions[0]["unlocks"] if next_actions else None,
        },
        "worth_brian_time": {
            "transactional_product": deal.get("deal_type") not in {None, "SERVICE"},
            "deadline_open": (deal.get("deadline_evaluation") or {}).get("status") != "EXPIRED",
            "economics_status": econ.get("status"),
            "summary": (
                "Worth continued operator attention"
                if pipeline_result.get("operator_readiness") not in {"REJECTED"}
                else "Rejected / not worth pursuit under current evidence"
            ),
        },
    }
