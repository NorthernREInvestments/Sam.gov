"""Offline re-score Phase H deep-research packets with eligibility gate."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from eligibility_gate import blocks_quote_outreach, evaluate_eligibility_gate
from phase_h.deep_research import (
    STATE_BID_DECISION,
    STATE_ELIGIBILITY_ACTION,
    STATE_QUOTE_OUTREACH,
    STATE_RESEARCHED,
    _operator_packet,
    _phase_h_readiness,
)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "artifacts" / "phase_h" / "deep_research_latest.json"
OUT_DIR = ROOT / "artifacts" / "phase_h"


def _blob(r: dict) -> tuple[str, str]:
    src = r.get("phase_h_source_row") or {}
    opp = r.get("opportunity") or {}
    title = src.get("title") or opp.get("title") or r.get("title") or ""
    parts = [title, str(opp.get("description") or ""), str(opp.get("solicitation_text") or "")]
    sam = r.get("SAM") or {}
    if isinstance(sam, dict):
        parts.append(str(sam.get("noticedesc") or sam.get("description") or ""))
    dr = r.get("document_review") or {}
    for d in (dr.get("documents") or [])[:5]:
        if isinstance(d, dict):
            parts.append(str(d.get("excerpt") or d.get("text") or "")[:4000])
    facts = r.get("extracted_facts") or {}
    if facts:
        parts.append(json.dumps(facts, default=str)[:8000])
    # Preserve noticedesc body if stored under document_review.raw
    if dr.get("body"):
        parts.append(str(dr.get("body"))[:12000])
    return title, "\n".join(p for p in parts if p)


def main() -> None:
    data = json.loads(SRC.read_text(encoding="utf-8"))
    results = data["results"]
    before = Counter(r.get("phase_h_readiness") for r in results)
    rows_out: list[dict] = []

    for r in results:
        src = r.get("phase_h_source_row") or {}
        opp = r.get("opportunity") or {}
        title, blob = _blob(r)
        gate = evaluate_eligibility_gate(
            {
                **src,
                **opp,
                "title": title,
                "description": blob,
                "set_aside": opp.get("typeOfSetAside") or r.get("set_aside"),
            },
            text=blob,
        )
        r["eligibility_gate"] = gate
        prev = r.get("phase_h_readiness")
        new_state = _phase_h_readiness(
            identity=r.get("phase_h_identity_confidence") or "UNKNOWN",
            docs_reviewed=bool((r.get("document_review") or {}).get("reviewed")),
            history_class=r.get("phase_h_history_class") or "NO_HISTORY_FOUND",
            economics_state=r.get("phase_h_economics_state") or "UNKNOWN",
            funding_state=r.get("phase_h_funding_state") or "UNKNOWN",
            deadline_blocked=False,
            hard_blockers=list(r.get("hard_blockers") or []),
            packet=r,
        )
        r["phase_h_readiness_before_eligibility"] = prev
        r["phase_h_readiness"] = new_state
        if new_state == STATE_ELIGIBILITY_ACTION:
            r["phase_h_next_action"] = gate.get("next_action")
            r["primary_next_action"] = gate.get("next_action")
        pkt = r.get("phase_h_operator_packet") or {}
        opp_pkt = pkt.get("opportunity") or {}
        row_for_packet = {
            **src,
            "buyer": opp_pkt.get("agency"),
            "solicitation_number": opp_pkt.get("solicitation"),
            "product_class": "LIKELY_PRODUCT_RESALE",
        }
        r["phase_h_operator_packet"] = _operator_packet(row_for_packet, r)
        rows_out.append(
            {
                "title": title[:100],
                "canonical_id": src.get("canonical_id"),
                "before": prev,
                "after": new_state,
                "eligibility": gate.get("overall_status"),
                "vehicle": gate.get("vehicle_name"),
                "vehicle_code": gate.get("vehicle_code"),
                "boa_required": gate.get("boa_required"),
                "jcp_required": gate.get("jcp_required"),
                "approved_source": gate.get("approved_source_required"),
                "blocking": gate.get("blocking_reasons"),
                "next": (gate.get("next_action") or "")[:160],
                "quote_blocked": blocks_quote_outreach(gate),
            }
        )

    after = Counter(r.get("phase_h_readiness") for r in results)
    elig = Counter((r.get("eligibility_gate") or {}).get("overall_status") for r in results)
    quote_ready = [r for r in results if r.get("phase_h_readiness") == STATE_QUOTE_OUTREACH]
    bid_ready = [r for r in results if r.get("phase_h_readiness") == STATE_BID_DECISION]

    scorecard = dict(data.get("scorecard") or {})
    scorecard.update(
        {
            "READY_FOR_QUOTE_OUTREACH": len(quote_ready),
            "READY_FOR_BID_DECISION": len(bid_ready),
            "ELIGIBILITY_ACTION_REQUIRED": after.get(STATE_ELIGIBILITY_ACTION, 0),
            "researched_not_ready": after.get(STATE_RESEARCHED, 0),
            "readiness_counts": dict(after),
            "eligibility_status_counts": dict(elig),
            "eligibility_rescore": True,
            "previous_quote_ready": before.get(STATE_QUOTE_OUTREACH, 0),
            "previous_readiness_counts": dict(before),
        }
    )

    payload = {
        "kind": "PhaseHDeepResearchRun",
        "scorecard": scorecard,
        "quote_ready": [
            {
                "title": (r.get("phase_h_source_row") or {}).get("title"),
                "canonical_id": (r.get("phase_h_source_row") or {}).get("canonical_id"),
                "nsn": r.get("phase_h_nsn"),
                "max_supplier_cost": (r.get("phase_h_max_supplier_cost") or {}).get(
                    "maximum_allowable_supplier_cost"
                ),
                "history_class": r.get("phase_h_history_class"),
                "next_action": r.get("phase_h_next_action"),
                "packet": r.get("phase_h_operator_packet"),
                "eligibility": r.get("eligibility_gate"),
            }
            for r in quote_ready
        ],
        "bid_ready": [],
        "results": results,
        "cohort_ids": data.get("cohort_ids"),
        "audit_rows": rows_out,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "deep_research_eligibility_rescore.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    (OUT_DIR / "deep_research_latest.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    (OUT_DIR / "quote_ready_latest.json").write_text(
        json.dumps({"count": len(quote_ready), "cases": payload["quote_ready"]}, indent=2, default=str),
        encoding="utf-8",
    )
    (OUT_DIR / "scorecard_latest.json").write_text(
        json.dumps(scorecard, indent=2, default=str), encoding="utf-8"
    )
    (OUT_DIR / "eligibility_audit_rows.json").write_text(
        json.dumps(rows_out, indent=2, default=str), encoding="utf-8"
    )

    print("BEFORE", dict(before))
    print("AFTER", dict(after))
    print("ELIG", dict(elig))
    print("quote_ready", len(quote_ready), "bid_ready", len(bid_ready))
    for x in rows_out:
        interesting = (
            x["before"] != x["after"]
            or x["vehicle"]
            or x["jcp_required"]
            or x["approved_source"]
            or x["eligibility"] not in ("ELIGIBILITY_NOT_APPLICABLE", None)
        )
        if interesting:
            print(json.dumps(x, default=str))


if __name__ == "__main__":
    main()
