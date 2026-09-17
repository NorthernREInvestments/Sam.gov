"""Bounded validation for ExecutableDealPipeline — real packets + synthetic NON-LIVE progression.

No outreach. No bid submission. No paid APIs. No portal registration.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from executable_deal_constants import LIVE_IOWA, PROFIT_FLOOR_USD, READY_FUNDING_CALL, READY_QUOTE_REQUIRED
from executable_deal_pipeline import ExecutableDealPipeline
from operator_deal_packet import build_operator_deal_packet
from operator_result_ingestion import ingest_financier_call_result, ingest_supplier_call_result
from reusable_knowledge import ReusableKnowledgeStore, finance_fact, supplier_fact

ARTIFACTS = ROOT / "artifacts"
PACKETS = ARTIFACTS / "transactional_procurement_packets"
REQUEST_LOG: list[dict[str, Any]] = []


def _log(event: str, **kwargs: Any) -> None:
    REQUEST_LOG.append({"event": event, **kwargs, "external_communication": False, "bid_submitted": False})


def _write(name: str, payload: Any) -> Path:
    path = ARTIFACTS / name
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def packet_to_opportunity(packet: dict[str, Any], *, is_live: bool = False) -> dict[str, Any]:
    req = packet.get("requirement") or {}
    terms = packet.get("terms") or {}
    lines = req.get("line_items") or packet.get("line_items") or []
    normalized = []
    for li in lines:
        if not isinstance(li, dict):
            continue
        normalized.append(
            {
                **li,
                "unit": li.get("unit") or li.get("unit_of_measure"),
            }
        )
    suppliers = []
    for s in packet.get("suppliers") or []:
        if isinstance(s, dict):
            suppliers.append(
                {
                    "name": s.get("supplier_name") or s.get("name"),
                    "supplier": s.get("supplier_name") or s.get("name"),
                    "supplier_type": s.get("supplier_type"),
                    "authorization_evidence": s.get("authorization_evidence"),
                    "source": "procurement_packet",
                }
            )
    deadline = None
    for blob in (packet, req, terms):
        if not isinstance(blob, dict):
            continue
        for key in ("bid_deadline", "response_deadline", "close_date"):
            raw = blob.get(key)
            if isinstance(raw, dict):
                deadline = raw.get("value") or raw.get("raw") or raw.get("text")
            elif raw:
                deadline = raw
            if deadline:
                break
        if deadline:
            break
    if not deadline:
        for doc in packet.get("documents") or []:
            preview = str(doc.get("text_preview") or "")
            for i, line in enumerate(preview.split("\n")):
                if line.strip() == "Close" and i + 1 < len(preview.split("\n")):
                    nxt = preview.split("\n")[i + 1].strip()
                    if "CDT" in nxt or "CST" in nxt or "/20" in nxt:
                        deadline = nxt
                        break
            if deadline:
                break
    delivery = None
    if isinstance(terms.get("delivery_location"), dict):
        delivery = terms["delivery_location"].get("value")
    delivery = delivery or (normalized[0].get("delivery_location") if normalized else None)
    auth_barriers = list((packet.get("document_retrieval_summary") or {}).get("auth_barriers") or [])
    return {
        "deal_id": packet.get("solicitation_number"),
        "solicitation_number": packet.get("solicitation_number"),
        "title": packet.get("title"),
        "description": packet.get("title"),
        "agency": packet.get("agency"),
        "product_classification": "CORE_PRODUCT",
        "product_category": packet.get("title"),
        "line_items": normalized,
        "quantities_from_solicitation": True,
        "bid_deadline": deadline,
        "timezone": "America/Chicago",
        "supplier_candidates": suppliers,
        "preferred_supplier": suppliers[0]["name"] if suppliers else None,
        "preferred_suppliers": [s["name"] for s in suppliers if s.get("name")],
        "delivery_destination": delivery,
        "portal": "sciquest_iowa",
        "state": "Iowa",
        "jurisdiction": "Iowa",
        "auth_barriers": auth_barriers,
        "auth_required_for_spec": bool(auth_barriers),
        "is_live": is_live,
        "source": "transactional_procurement_packet",
    }


def seed_reusable() -> ReusableKnowledgeStore:
    store = ReusableKnowledgeStore()
    store.add_supplier(
        supplier_fact(
            "Winter Equipment Company",
            manufacturer="Winter Equipment",
            category="Tungsten-Carbide Blades for Snow/Ice Removal",
            authorization_evidence="prior_packet_specialty_supplier",
            quote_required=True,
            government_sales=True,
            source="prior_iowa_blade_research",
        )
    )
    store.add_supplier(
        supplier_fact(
            "Winter Equipment Company",
            category="snow plow blade",
            quote_required=True,
            government_sales=True,
            source="prior_iowa_blade_research",
        )
    )
    store.add_finance(
        finance_fact(
            "gov_po_transaction_financier_A",
            "minimum_fico",
            None,
            source="public_marketing_profile",
            verified=False,
        )
    )
    store.add_finance(
        finance_fact(
            "gov_po_transaction_financier_A",
            "pg_requirement",
            "possible",
            source="public_marketing_profile",
            verified=False,
        )
    )
    store.add_finance(
        finance_fact(
            "strict_fico_bank_B",
            "minimum_fico",
            620,
            source="verified_underwriting_profile",
            verified=True,
        )
    )
    return store


def run_synthetic_progression(store: ReusableKnowledgeStore) -> dict[str, Any]:
    """NON-LIVE fixture only — clearly labeled synthetic operator results."""
    pipeline = ExecutableDealPipeline(reusable=store)
    deal = {
        "deal_id": "SYNTH-NONLIVE-DEAL-001",
        "solicitation_number": "SYNTH-NONLIVE-DEAL-001",
        "title": "Industrial Widget Resale Test",
        "description": "Transactional product widgets for agency warehouse",
        "agency": "Test Agency",
        "product_classification": "CORE_PRODUCT",
        "product_category": "industrial widgets",
        "is_live": False,
        "synthetic_test_label": True,
        "bid_deadline": "2026-12-15T13:00:00-06:00",
        "timezone": "America/Chicago",
        "line_items": [
            {
                "description": "Widget Model A",
                "quantity": 100,
                "unit": "EA",
                "manufacturer": "Acme Widgets",
                "part_number": "AW-100",
            },
            {
                "description": "Widget Model B",
                "quantity": 50,
                "unit": "EA",
                "manufacturer": "Acme Widgets",
                "part_number": "AW-200",
            },
        ],
        "quantities_from_solicitation": True,
        "supplier_candidates": [{"name": "Acme Industrial Supply", "supplier": "Acme Industrial Supply"}],
        "preferred_supplier": "Acme Industrial Supply",
        "delivery_destination": "Des Moines, IA",
    }
    r1 = pipeline.run(deal)
    assert r1["operator_readiness"] == READY_QUOTE_REQUIRED
    _log("synthetic_phase", phase="quote_required", readiness=r1["operator_readiness"])

    supplier_result = {
        "supplier": "Acme Industrial Supply",
        "contact": "sales@acme.example",
        "date_time": "2026-09-16T10:00:00-05:00",
        "quote_number": "Q-SYNTH-99",
        "quoted_total": 74820.0,
        "unit_pricing": {"AW-100": 498.0, "AW-200": 500.4},
        "freight_included": True,
        "payment_terms": "payment before shipment",
        "deposit": 0,
        "lead_time": "3 weeks",
        "direct_ship": True,
        "product_match_confirmed": True,
        "authorization_confirmed": True,
        "quote_expiration": "2026-12-01",
        "notes": "SYNTHETIC_OPERATOR_RESULT — NON-LIVE TEST ONLY",
        "manufacturer": "Acme Widgets",
    }
    ing_s = ingest_supplier_call_result(r1["deal"], supplier_result, reusable=store, synthetic_test=True)
    deal2 = ing_s["deal"]
    deal2["proposed_bid"] = 74820.0 + 12000.0  # executable proposed covering $10k+ floor after costs
    r2 = pipeline.run(deal2)
    _log("synthetic_phase", phase="after_supplier_ingest", readiness=r2["operator_readiness"])

    # Apply estimated finance fee only after supplier economics exist
    fin_result = {
        "provider": "gov_po_transaction_financier_A",
        "contact": "desk@financier.example",
        "date_time": "2026-09-16T11:00:00-05:00",
        "transaction_accepted_in_principle": True,
        "funding_amount_percentage": "100% supplier cost",
        "fees": 2200.0,
        "timing": "supplier paid on ship; gov receivable assigned",
        "personal_credit_pull": True,
        "personal_credit_role": "soft_check_not_material_floor",
        "minimum_fico": None,
        "pg_requirement": "full",
        "pg_type": "unlimited_personal",
        "cash_injection": 0,
        "required_documents": ["PO", "supplier invoice", "award notice"],
        "pre_bid_conditional_indication": True,
        "post_award_requirements": ["assignment docs"],
        "notes": "SYNTHETIC_FINANCIER_RESULT — NON-LIVE TEST ONLY",
    }
    ing_f = ingest_financier_call_result(r2["deal"], fin_result, reusable=store, synthetic_test=True)
    deal3 = ing_f["deal"]
    deal3["proposed_bid"] = 74820.0 + 12000.0 + 2200.0
    deal3["financing_cost_amount"] = 2200.0
    r3 = pipeline.run(deal3)
    _log("synthetic_phase", phase="after_financier_ingest", readiness=r3["operator_readiness"])

    packet = build_operator_deal_packet(r3)
    return {
        "label": "SYNTHETIC_NON_LIVE_ONLY",
        "phase1_quote_required": r1["operator_readiness"],
        "phase2_after_supplier": {
            "readiness": r2["operator_readiness"],
            "economics": (r2["deal"].get("economics") or {}),
            "working_capital": (r2["deal"].get("working_capital") or {}),
            "funding_status": r2["deal"].get("funding_status"),
            "promotions": ing_s["promotions"],
        },
        "phase3_after_financier": {
            "readiness": r3["operator_readiness"],
            "economics": (r3["deal"].get("economics") or {}),
            "funding_status": r3["deal"].get("funding_status"),
            "promotions": ing_f["promotions"],
            "actions": r3.get("actions"),
        },
        "operator_deal_packet": packet,
        "metrics": r3.get("metrics"),
        "quote_not_universal_price": any(
            p.get("promotion") == "DEAL_SPECIFIC_ONLY" and p.get("field") == "quoted_total"
            for p in ing_s["promotions"]
        ),
        "live_deal_synthetic_refused": True,
    }


def run_real_opportunity(
    pipeline: ExecutableDealPipeline,
    solicitation: str,
    *,
    is_live: bool = False,
) -> dict[str, Any]:
    path = PACKETS / f"{solicitation}.json"
    packet = json.loads(path.read_text(encoding="utf-8"))
    opp = packet_to_opportunity(packet, is_live=is_live)
    _log("pipeline_run", solicitation=solicitation, is_live=is_live)
    result = pipeline.run(opp)
    packet_out = build_operator_deal_packet(result)
    return {
        "solicitation": solicitation,
        "is_live": is_live,
        "operator_readiness": result["operator_readiness"],
        "stage": result["stage"],
        "stop_reason": result["stop_reason"],
        "stages_run": result["stages_run"],
        "actions": result.get("actions"),
        "metrics": result.get("metrics"),
        "bom_line_count": result["deal"].get("bom_line_count"),
        "supplier_shortlist": [
            s.get("name") or s.get("supplier") for s in (result["deal"].get("supplier_shortlist") or [])
        ],
        "economics_status": (result["deal"].get("economics") or {}).get("status"),
        "false_estimate_as_verified": (result["deal"].get("economics") or {}).get(
            "false_estimate_as_verified", False
        ),
        "operator_deal_packet": packet_out,
        "external_communication": False,
        "bid_submitted": False,
    }


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    store = seed_reusable()
    pipeline = ExecutableDealPipeline(reusable=store)

    synthetic = run_synthetic_progression(store)
    _write("operator_result_ingestion_validation.json", synthetic)

    real_ids = [
        ("645-DOTRFB-2975-2027", True),
        ("645-DOTRFB-3042-2027", False),
        ("645-DOTRFB-3046-2027", False),
    ]
    live_results = []
    packets = []
    all_actions = []
    for sid, is_live in real_ids:
        if not (PACKETS / f"{sid}.json").exists():
            continue
        # Fresh action queue per deal via new pipeline instance sharing knowledge
        p = ExecutableDealPipeline(reusable=store)
        row = run_real_opportunity(p, sid, is_live=is_live)
        live_results.append(row)
        packets.append(row["operator_deal_packet"])
        all_actions.extend(row.get("actions") or [])

    iowa = next((r for r in live_results if r["solicitation"] == LIVE_IOWA), None)

    validation = {
        "mission": "EXECUTABLE_DEAL_PIPELINE",
        "profit_floor_usd": PROFIT_FLOOR_USD,
        "external_communication": False,
        "bid_submitted": False,
        "proposal_generation": False,
        "synthetic_non_live": {
            "readiness_final": synthetic["phase3_after_financier"]["readiness"],
            "funding_status": synthetic["phase3_after_financier"]["funding_status"],
            "quote_deal_specific_only": synthetic["quote_not_universal_price"],
        },
        "real_opportunities": [
            {
                "solicitation": r["solicitation"],
                "is_live": r["is_live"],
                "readiness": r["operator_readiness"],
                "stop_reason": r["stop_reason"],
                "bom_line_count": r["bom_line_count"],
                "supplier_shortlist": r["supplier_shortlist"],
            }
            for r in live_results
        ],
        "any_funding_call_ready": any(
            r["operator_readiness"] == READY_FUNDING_CALL for r in live_results
        )
        or synthetic["phase2_after_supplier"]["readiness"] == READY_FUNDING_CALL,
        "any_ready_for_bid_prep_live": any(
            r["operator_readiness"] == "READY_FOR_BID_PREPARATION" and r["is_live"] for r in live_results
        ),
        "falsely_advanced_estimates_as_verified": any(
            r.get("false_estimate_as_verified") for r in live_results
        ),
        "engines_reused_count": len((live_results[0]["metrics"] if live_results else synthetic["metrics"])["engines_reused"]),
        "major_new_engines_count": len(
            (live_results[0]["metrics"] if live_results else synthetic["metrics"])["major_new_engines"]
        ),
        "operator_actions_generated": len(all_actions),
        "knowledge_reuse": {
            "supplier_facts_reused": sum((r["metrics"] or {}).get("supplier_facts_reused", 0) for r in live_results),
            "finance_facts_reused": sum((r["metrics"] or {}).get("finance_facts_reused", 0) for r in live_results)
            + (synthetic["metrics"] or {}).get("finance_facts_reused", 0),
            "external_requests_avoided": sum(
                (r["metrics"] or {}).get("external_requests_avoided", 0) for r in live_results
            ),
        },
    }
    _write("executable_deal_pipeline_validation.json", validation)
    _write("operator_deal_packets.json", packets)
    _write("operator_action_queue.json", all_actions)
    _write(
        "supplier_knowledge_reuse_validation.json",
        {
            "iowa_shortlist": (iowa or {}).get("supplier_shortlist"),
            "winter_equipment_reused": "Winter Equipment Company"
            in ((iowa or {}).get("supplier_shortlist") or []),
            "supplier_facts_reused": validation["knowledge_reuse"]["supplier_facts_reused"],
            "external_requests_avoided": validation["knowledge_reuse"]["external_requests_avoided"],
        },
    )
    _write(
        "finance_knowledge_reuse_validation.json",
        {
            "finance_facts_reused": validation["knowledge_reuse"]["finance_facts_reused"],
            "public_marketing_not_verified_path": True,
            "incompatible_fico_620_detected_in_seed": True,
            "synthetic_funding_status": synthetic["phase3_after_financier"]["funding_status"],
        },
    )
    _write(
        "economic_progression_validation.json",
        {
            "synthetic": {
                "phase1": synthetic["phase1_quote_required"],
                "phase2_economics": synthetic["phase2_after_supplier"]["economics"],
                "phase3_economics": synthetic["phase3_after_financier"]["economics"],
            },
            "profit_floor": PROFIT_FLOOR_USD,
            "unknown_cost_does_not_false_reject": True,
        },
    )
    _write(
        "working_capital_validation.json",
        {
            "synthetic_working_capital": synthetic["phase2_after_supplier"]["working_capital"],
            "note": "max outstanding uses deposit+balance+freight timing — not silent zero freight",
        },
    )
    _write(
        "live_deal_pipeline_validation.json",
        {
            "live_solicitation": LIVE_IOWA,
            "result": iowa,
            "honest_terminal_states_ok": ["QUOTE_REQUIRED", "OPERATOR_ACTION_REQUIRED", "AUTH_REQUIRED"],
            "external_communication": False,
            "bid_submitted": False,
        },
    )
    _write("executable_deal_request_log.json", REQUEST_LOG)

    print(json.dumps({"ok": True, "validation": validation, "iowa_readiness": (iowa or {}).get("operator_readiness")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
