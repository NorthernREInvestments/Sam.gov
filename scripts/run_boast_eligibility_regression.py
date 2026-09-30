"""Live re-research BOAST Phase H regression case only."""

from __future__ import annotations

import json
from pathlib import Path

from deep_deal_research import ResearchBudget
from phase_h.cohort import select_cohort
from phase_h.deep_research import research_one_phase_h

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_h" / "boast_live_regression.json"


def main() -> None:
    cohort = select_cohort(size=25)
    boast = [
        r
        for r in cohort
        if "BOAST" in str(r.get("title") or "").upper() or "6110-01-082-8958" in str(r.get("title") or "")
    ]
    if not boast:
        raise SystemExit("BOAST case not in cohort")
    budget = ResearchBudget(
        public_http_hard_max=20, public_http_target=15, usaspending_hard_max=3, sam_hard_max=0
    )
    counter = {"usaspending": 0, "usaspending_max": 3}
    pkt = research_one_phase_h(boast[0], budget=budget, budget_counter=counter, authorize_live=True)
    eg = pkt.get("eligibility_gate") or {}
    payload = {
        "phase_h_readiness": pkt.get("phase_h_readiness"),
        "eligibility_gate": eg,
        "next_action": pkt.get("phase_h_next_action"),
        "operator_packet": pkt.get("phase_h_operator_packet"),
        "nsn": pkt.get("phase_h_nsn"),
        "max_cost": pkt.get("phase_h_max_supplier_cost"),
        "identity": pkt.get("phase_h_identity_confidence"),
        "history_class": pkt.get("phase_h_history_class"),
    }
    OUT.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print("readiness", payload["phase_h_readiness"])
    print("elig", eg.get("overall_status"), eg.get("vehicle_name"), eg.get("boa_status"))
    print("next", (payload["next_action"] or "")[:200])


if __name__ == "__main__":
    main()
