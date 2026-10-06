"""Per-opportunity deep completion pipeline."""

from __future__ import annotations

from typing import Any

from deep_completion_3to5.economics import allocate_line_targets, compute_target_economics, model_financing
from deep_completion_3to5.execution import review_execution
from deep_completion_3to5.models import COLLIER_OID, EXECUTION_COMPLEX, LOWER_PRIORITY, OPP_DEADLINE_S
from deep_completion_3to5.outreach import owner_next_action
from deep_completion_3to5.packets import build_quote_packets
from deep_completion_3to5.public_check import last_public_price_check
from deep_completion_3to5.revenue import validate_revenue
from deep_completion_3to5.suppliers import build_supplier_map


def process_opportunity(
    corpus_item: dict[str, Any],
    lines: list[dict[str, Any]],
    *,
    stats: dict[str, Any] | None = None,
    skip_public_check: bool = False,
) -> dict[str, Any]:
    stats = stats if stats is not None else {}
    oid = corpus_item["opportunity_id"]
    print(f"[dc35] revenue {oid}", flush=True)
    revenue = validate_revenue(oid)
    print(f"[dc35] revenue {oid} {revenue.get('PASS_FAIL')} class={revenue.get('classification')} value={revenue.get('value')}", flush=True)

    print(f"[dc35] execution {oid}", flush=True)
    execution = review_execution(oid, lines, corpus_item)

    # Collier: product-only subset for packets if complex
    work_lines = list(lines)
    if oid == COLLIER_OID:
        product_ids = set(execution.get("product_line_ids") or [])
        if product_ids:
            work_lines = [l for l in lines if l.get("line_id") in product_ids or l.get("identity_usable")]
        execution["priority"] = LOWER_PRIORITY
        execution["complexity"] = EXECUTION_COMPLEX

    print(f"[dc35] public-check {oid}", flush=True)
    if skip_public_check:
        checked = work_lines
    else:
        checked = last_public_price_check(work_lines, stats=stats, deadline_s=min(OPP_DEADLINE_S, 90.0))

    print(f"[dc35] suppliers {oid}", flush=True)
    supplier_map = build_supplier_map(oid, checked)

    print(f"[dc35] packets {oid}", flush=True)
    packets = build_quote_packets(
        oid,
        checked,
        supplier_map=supplier_map,
        revenue=revenue,
        execution=execution,
        buyer=corpus_item.get("buyer"),
        solicitation_id=corpus_item.get("solicitation_id"),
        deadline=corpus_item.get("deadline"),
    )

    target = compute_target_economics(revenue)
    checked = allocate_line_targets(checked, target)
    financing = model_financing(
        oid,
        revenue=revenue,
        target=target,
        packet_coverage=float(packets.get("coverage") or 0),
    )

    public_priced = sum(1 for l in checked if l.get("quote_required_state") == "PRICED_EXECUTABLE" or l.get("acquisition_state") == "PRICED_EXECUTABLE")
    quote_ready = int(packets.get("quote_ready_lines") or 0)
    material_n = len(lines)
    coverage = {
        "material_lines": material_n,
        "usable_identities": sum(1 for l in lines if l.get("identity_usable")),
        "public_priced": public_priced,
        "PUBLIC_PRICE_COVERAGE": round(public_priced / material_n, 4) if material_n else 0,
        "quote_ready_lines": quote_ready,
        "QUOTE_PACKET_COVERAGE": packets.get("coverage"),
        "quote_packet_coverage": packets.get("coverage"),
        "ACTUAL_QUOTE_COVERAGE": 0.0,  # no quotes received yet
        "actual_quoted": 0,
        "TOTAL_EXECUTABLE_COST_COVERAGE": round(
            (public_priced + quote_ready) / material_n, 4
        ) if material_n else 0,
        "remaining_unresolved": max(0, material_n - public_priced - quote_ready),
    }

    result = {
        "opportunity_id": oid,
        "buyer": corpus_item.get("buyer"),
        "solicitation_id": corpus_item.get("solicitation_id"),
        "deadline": corpus_item.get("deadline"),
        "revenue": revenue,
        "execution": execution,
        "supplier_map": {
            "manufacturers": supplier_map.get("manufacturers"),
            "authorized_distributors": supplier_map.get("authorized_distributors"),
            "unique_quote_channels": supplier_map.get("unique_quote_channels"),
            "suppliers": [
                {
                    k: s.get(k)
                    for k in (
                        "supplier_name",
                        "domain",
                        "manufacturer_relationship",
                        "contact_route",
                        "quote_request_capability",
                        "account_login_required",
                        "notes",
                    )
                }
                for s in (supplier_map.get("suppliers") or [])
            ],
        },
        "quote_packets": packets,
        "target_economics": target,
        "financing": financing,
        "coverage": coverage,
        "lines": checked,
        "owner_view": {
            "material_lines": material_n,
            "public_priced": public_priced,
            "quote_required": quote_ready,
            "quote_packets": packets.get("packet_count"),
            "suppliers": len(packets.get("suppliers") or []),
            "revenue_evidence": revenue.get("classification"),
            "target_acquisition_ceiling": target.get("MAX_ACQUISITION_COST_FOR_$5K_PROFIT"),
            "financing_state": financing.get("status"),
            "execution_risk": execution.get("delivery_risk"),
            "deadline": corpus_item.get("deadline"),
            "next_action": None,
            "expected_profit": None,
        },
    }
    result["owner_view"]["next_action"] = owner_next_action(result)

    # go-metro extras
    if oid.endswith("298984"):
        result["go_metro_detail"] = {
            "material_lines": material_n,
            "manufacturers": supplier_map.get("manufacturers"),
            "supplier_candidates": [s.get("domain") for s in supplier_map.get("suppliers") or []],
            "quote_packets": packets.get("packet_count"),
            "coverage": packets.get("coverage"),
            "cummins_direct_possible": any(
                "cummins" in str(s.get("domain") or "").lower() for s in (supplier_map.get("suppliers") or [])
            ),
            "authorized_distributor_alternatives": [
                s.get("domain")
                for s in (supplier_map.get("suppliers") or [])
                if "cummins" not in str(s.get("domain") or "").lower()
            ],
        }
    if oid.endswith("286698"):
        result["dekalb_detail"] = {
            "material_lines": material_n,
            "manufacturers": supplier_map.get("manufacturers"),
            "supplier_candidates": [s.get("domain") for s in supplier_map.get("suppliers") or []],
            "quote_packets": packets.get("packet_count"),
            "coverage": packets.get("coverage"),
            "consolidation_ratio": packets.get("consolidation_ratio"),
        }
    if oid == COLLIER_OID:
        result["collier_detail"] = {
            "product_lines": execution.get("product_lines"),
            "install_service_lines": int(execution.get("install_labor") or 0) + int(execution.get("service_lines") or 0),
            "quote_ready_product_lines": quote_ready,
            "complexity": execution.get("complexity"),
            "priority": execution.get("priority"),
            "next_action": result["owner_view"]["next_action"],
        }

    print(
        f"[dc35] done {oid} packets={packets.get('packet_count')} "
        f"coverage={packets.get('coverage')} next={result['owner_view']['next_action']}",
        flush=True,
    )
    return result
