"""Owner outreach queue — do not auto-send."""

from __future__ import annotations

from typing import Any

from deep_completion_3to5.models import DEEP_OIDS, GO_METRO_OID, DEKALB_OID, READY_FOR_OWNER_QUOTE_OUTREACH


def build_outreach_queue(
    opportunity_results: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for oid in DEEP_OIDS:
        result = opportunity_results.get(oid) or {}
        packets = ((result.get("quote_packets") or {}).get("quote_packets") or [])
        target = result.get("target_economics") or {}
        for p in packets:
            if not p.get("ready_for_owner_quote_outreach"):
                continue
            # Priority
            if oid == GO_METRO_OID:
                priority = "P0"
                impact = "Unlocks economics on top deep-completion candidate (Cummins basket)"
            elif oid == DEKALB_OID:
                priority = "P0" if p.get("line_count", 0) >= 40 else "P1"
                impact = "High coverage EMS basket — consolidates many lines"
            elif oid.endswith("299806") or oid.endswith("284328"):
                priority = "P1"
                impact = "Smaller basket — near-complete quote coverage"
            else:
                priority = "P2"
                impact = "Secondary / complexity-limited candidate"

            ceiling = target.get("MAX_ACQUISITION_COST_FOR_$5K_PROFIT")
            rows.append(
                {
                    "Priority": priority,
                    "Opportunity": oid,
                    "Supplier": (p.get("supplier") or {}).get("supplier_name") or (p.get("supplier") or {}).get("domain"),
                    "Manufacturer": ", ".join(p.get("manufacturers") or []) or p.get("cluster_key"),
                    "Lines": p.get("line_count"),
                    "Quote packet": p.get("packet_id"),
                    "Contact route": (p.get("supplier") or {}).get("contact_route"),
                    "Deadline": p.get("deadline") or "UNKNOWN",
                    "Urgency": p.get("urgency") or "NORMAL",
                    "Expected impact": impact,
                    "Target $5K acquisition ceiling": ceiling,
                    "Status": READY_FOR_OWNER_QUOTE_OUTREACH,
                    "Action": "SEND QUOTE REQUEST",
                    "do_not_send_automatically": True,
                }
            )

    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    rows.sort(key=lambda r: (order.get(r["Priority"], 9), -(r.get("Lines") or 0), str(r.get("Opportunity"))))
    return rows


def owner_next_action(result: dict[str, Any]) -> str:
    if (result.get("execution") or {}).get("PASS_FAIL") == "FAIL":
        return "REVIEW BLOCKER"
    if (result.get("revenue") or {}).get("status") == "REVENUE_NOT_READY":
        return "REVIEW BLOCKER"
    packets = (result.get("quote_packets") or {}).get("quote_packets") or []
    ready = [p for p in packets if p.get("ready_for_owner_quote_outreach")]
    public_n = int((result.get("coverage") or {}).get("public_priced") or 0)
    if ready:
        return "SEND QUOTE REQUEST" if ready[0].get("contact_route") or True else "CALL SUPPLIER"
    if public_n and (result.get("coverage") or {}).get("quote_packet_coverage", 0) >= 0.9:
        return "READY FOR ECONOMICS"
    return "WAITING FOR QUOTE"
