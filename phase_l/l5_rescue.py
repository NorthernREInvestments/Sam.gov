"""Phase L.5 — commercial retention repair rescue (audit + Stage 3 economics)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.acquisition_lanes import (
    DEEP_RESEARCH_NO_FIXED_COUNT,
    MANUAL_QUEUE_NO_FIXED_CAP,
    STAGE3_NO_ROW_CAP,
)
from phase_l.retention_audit import BUILD, audit_accessible_rows
from phase_l.stage2_admission import BUILD as L5_BUILD

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
OUT.mkdir(parents=True, exist_ok=True)

L4_BASELINE = {
    "stage1": 1617,
    "stage2": 102,
    "stage3": 102,
    "commercially_sourceable": 16,
    "commercial_share_pct": 15.7,
}


def _utc() -> str:
    return now_utc().isoformat()


def run_phase_l5_commercial_retention_repair(
    rows: list[dict[str, Any]] | None = None,
    *,
    authorize_live: bool = True,
    refresh_hunt: bool = False,
    max_hunt_sources: int = 90,
    run_stage3_economics: bool = True,
    max_shell_fetches: int = 2,
    max_detail_fetches: int = 2,
    usaspending_max: int = 30,
) -> dict[str, Any]:
    """Audit L.4 attrition, apply L.5 admission, optionally fresh hunt + all Stage 3."""
    assert STAGE3_NO_ROW_CAP and DEEP_RESEARCH_NO_FIXED_COUNT and MANUAL_QUEUE_NO_FIXED_CAP
    assert BUILD == L5_BUILD

    discovery_meta: dict[str, Any] = {}
    if refresh_hunt and authorize_live:
        print("[l5] fresh commercial_feed hunt...", flush=True)
        try:
            from phase_l.hunt import run_phase_l_hunt

            hunt = run_phase_l_hunt(
                authorize_live=True,
                max_sources=max_hunt_sources,
                profile="commercial_feed",
            )
            discovery_meta = hunt.get("discovery_meta") or {}
            rows = list(
                (json.loads((OUT / "accessible_latest.json").read_text(encoding="utf-8"))).get("rows") or []
            )
            print(f"[l5] fresh accessible={len(rows)}", flush=True)
        except Exception as exc:
            discovery_meta = {"hunt_error": str(exc)[:200]}
            print(f"[l5] hunt error: {exc}", flush=True)

    if rows is None:
        data = json.loads((OUT / "accessible_latest.json").read_text(encoding="utf-8"))
        rows = list(data.get("rows") or [])

    print(f"[l5] attrition audit + counterfactual on {len(rows)} accessible...", flush=True)
    audit = audit_accessible_rows(rows)

    cf = audit.get("counterfactual_replay") or {}
    recovered_n = (audit.get("recovered_commercial_candidates") or {}).get("count") or 0
    lost_n = (audit.get("commercial_false_negatives") or {}).get("count") or 0

    l3_result: dict[str, Any] = {}
    if run_stage3_economics:
        print("[l5] Stage 3 economics on ALL L.5-admitted candidates (no cap)...", flush=True)
        from phase_l.l3_rescue import run_phase_l3_commercial_rebalance

        l3_result = run_phase_l3_commercial_rebalance(
            rows=rows,
            authorize_live=authorize_live,
            refresh_hunt=False,
            max_shell_fetches=max_shell_fetches,
            max_detail_fetches=max_detail_fetches,
            usaspending_max=usaspending_max,
        )

    cov = l3_result.get("opportunity_coverage") or {}
    lanes = (l3_result.get("acquisition_lanes") or {}).get("stage3") or {}
    commercial_s3 = int(
        (l3_result.get("acquisition_lanes") or {}).get("commercially_sourceable_stage3")
        or sum(
            int(lanes.get(k) or 0)
            for k in (
                "COMMERCIAL_OPEN_CHANNEL",
                "COMMERCIAL_DISTRIBUTOR_CHANNEL",
                "QUOTE_REQUIRED_COMMERCIAL",
                "MILSPEC_OPEN_CHANNEL",
            )
        )
    )
    # Prefer live Stage 3 commercial from l3 lanes; else counterfactual
    if not commercial_s3:
        commercial_s3 = int(cf.get("new_commercial") or 0)

    share_pct = float((l3_result.get("acquisition_lanes") or {}).get("stage3_commercial_share_pct") or 0)
    if share_pct == 0 and cov.get("stage3"):
        from phase_l.commercial_discovery import commercial_share

        share = commercial_share([{"acquisition_lane": k} for k, v in lanes.items() for _ in range(int(v or 0))])
        share_pct = float(share.get("commercial_share_pct") or 0)

    stage3_n = int(cov.get("stage3") or cf.get("new_stage3") or 0)
    stage2_n = int(cov.get("stage2") or cf.get("new_stage2") or 0)
    stage1_n = int(cov.get("stage1") or cf.get("new_stage1") or 0)

    # Verdict
    delta_s2 = int(cf.get("delta_stage2") or (stage2_n - L4_BASELINE["stage2"]))
    delta_comm = int(cf.get("delta_commercial") or (commercial_s3 - L4_BASELINE["commercially_sourceable"]))
    material_recovery = recovered_n >= 50 or delta_s2 >= 50
    commercial_up = commercial_s3 > L4_BASELINE["commercially_sourceable"] or delta_comm >= 5
    share_up = share_pct >= L4_BASELINE["commercial_share_pct"] + 5

    if material_recovery and commercial_up and stage3_n == int(cov.get("stage3_processed") or stage3_n):
        if (delta_s2 >= 100 or recovered_n >= 200) and (commercial_s3 >= 30 or share_up):
            verdict = "PHASE_L5_COMMERCIAL_RETENTION_REPAIR_WORKING"
        else:
            verdict = "PHASE_L5_PARTIAL_COMMERCIAL_RETENTION_REPAIR"
    elif material_recovery or commercial_up:
        verdict = "PHASE_L5_PARTIAL_COMMERCIAL_RETENTION_REPAIR"
    else:
        verdict = "PHASE_L5_COMMERCIAL_RETENTION_REPAIR_FAILED"

    bottleneck = (
        "Stage 3 economics still scarce (verified prices / joins) despite improved commercial admission"
        if int((l3_result.get("acquisition_evidence") or {}).get("verified_public_price") or 0) == 0
        and stage2_n > L4_BASELINE["stage2"]
        else "Commercial retention repaired but Stage 2 admission still limited"
        if stage2_n <= L4_BASELINE["stage2"] + 10
        else "Specialty share still high among NSN-heavy survivors"
    )

    payload = {
        "kind": "PhaseL5CommercialRetentionRepairResult",
        "phase": "L.5",
        "build": BUILD,
        "generated_at": _utc(),
        "verdict": verdict,
        "stage2_no_cap": True,
        "stage3_no_cap": STAGE3_NO_ROW_CAP,
        "deep_no_cap": DEEP_RESEARCH_NO_FIXED_COUNT,
        "l4_attrition_audit": audit.get("l4_attrition_audit"),
        "commercial_false_negatives": audit.get("commercial_false_negatives"),
        "counterfactual_replay": cf,
        "recovered_commercial_candidates": audit.get("recovered_commercial_candidates"),
        "false_negative_sample_n": len(audit.get("false_negative_sample") or []),
        "fresh_discovery": {
            "refresh_hunt": refresh_hunt,
            "accessible": len(rows),
            "discovery_meta": discovery_meta,
        },
        "opportunity_coverage": {
            "stage1": stage1_n,
            "stage2": stage2_n,
            "stage3": stage3_n,
            "stage3_processed": int(cov.get("stage3_processed") or 0),
            "deep_researched_queue": int(cov.get("deep_researched_queue") or 0),
            "counterfactual_stage2": cf.get("new_stage2"),
            "counterfactual_stage3": cf.get("new_stage3"),
        },
        "acquisition_lanes": l3_result.get("acquisition_lanes") or {
            "stage3": {},
            "commercially_sourceable_stage3": commercial_s3,
            "stage3_commercial_share_pct": share_pct,
        },
        "commercial_retention": audit.get("commercial_retention"),
        "historical_evidence": l3_result.get("historical_evidence"),
        "acquisition_evidence": l3_result.get("acquisition_evidence"),
        "reverse_economics": l3_result.get("reverse_economics"),
        "profit": l3_result.get("profit"),
        "original_solicitation_integrity": l3_result.get("original_solicitation_integrity"),
        "remaining_bottleneck": bottleneck,
        "l4_baseline": L4_BASELINE,
    }

    (OUT / "l5_commercial_retention.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    summary = {
        "verdict": verdict,
        "l4_attrition_audit": payload["l4_attrition_audit"],
        "commercial_false_negatives": payload["commercial_false_negatives"],
        "counterfactual_replay": cf,
        "recovered_n": recovered_n,
        "opportunity_coverage": payload["opportunity_coverage"],
        "acquisition_lanes": payload["acquisition_lanes"],
        "acquisition_evidence": payload["acquisition_evidence"],
        "profit": payload["profit"],
        "original_solicitation_integrity": payload["original_solicitation_integrity"],
        "remaining_bottleneck": bottleneck,
    }
    (OUT / "l5_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return payload
