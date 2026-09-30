"""Phase L.14 — non-BidNet commercial source expansion + exact history recovery."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.acquisition_lanes import (
    DEEP_RESEARCH_NO_FIXED_COUNT,
    MANUAL_QUEUE_NO_FIXED_CAP,
    STAGE3_NO_ROW_CAP,
    classify_acquisition_lane,
)
from phase_l.bidnet_parked import BIDNET_AUTH_HISTORY_PARKED, park_bidnet_auth_history
from phase_l.buyer_registry import (
    build_registry_from_rows,
    buyer_type_coverage_report,
    category_coverage_report,
    state_coverage_report,
    upsert_buyer,
)
from phase_l.economic_evaluability import recompute_economics_from_recovery
from phase_l.evidence_recovery import run_parallel_recovery
from phase_l.legacy_cleanup import assert_canonical_caps, assert_no_fixed_positive_cap, legacy_cleanup_report
from phase_l.nonbidnet_expansion import (
    BUILD,
    SOURCE_PRIORITY_ORDER,
    parked_access_dependency,
    source_economic_yield,
)
from phase_l.original_solicitation import resolve_original_solicitation
from phase_l.platform_history import detect_platform
from phase_l.platform_history_adapters import run_platform_history, source_capability_matrix
from phase_l.progressive_funnel import run_progressive_stages_cheap
from phase_l.quality_audit import (
    GOV_VALUE_A,
    GOV_VALUE_B,
    GOV_VALUE_C,
    GOV_VALUE_D,
    RECON_ONLY_CATEGORY_BENCHMARK,
    SECONDARY_QUOTE_TARGET,
    VALIDATED_QUOTE_TARGET,
    audit_quote_positive,
)
from phase_l.quote_economics import BUYER_VALUE_PATH, SUPPLIER_MEMORY_PATH, load_json, save_json
from phase_l.resilient_hunt import HUNT_COMPLETE, HUNT_COMPLETE_WITH_FAILURES, reset_checkpoint
from phase_l.supplier_upgrade import run_supplier_upgrade_loop

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
OUT.mkdir(parents=True, exist_ok=True)

L13_BASELINE = {
    "validated": 3,
    "secondary": 3,
    "recon_only": 3,
    "accessible": 673,
    "stage3": 176,
}


def _utc() -> str:
    return now_utc().isoformat()


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _deadline_days(row: dict[str, Any], pipe: dict[str, Any]) -> float | None:
    d = (pipe.get("stage0") or {}).get("deadline") or {}
    for k in ("days_remaining", "days_to_deadline", "deadline_days"):
        v = _f(d.get(k) or row.get(k))
        if v is not None:
            return v
    return None


def _is_bidnet_row(row: dict[str, Any]) -> bool:
    blob = " ".join(
        str(row.get(k) or "")
        for k in ("source_portal", "source", "source_url", "original_solicitation_url", "portal")
    ).lower()
    return "bidnet" in blob or detect_platform(row) == "BidNet"


def run_phase_l14_nonbidnet(
    rows: list[dict[str, Any]] | None = None,
    *,
    authorize_live: bool = True,
    refresh_hunt: bool = False,
    max_hunt_sources: int = 48,
    reset_hunt_checkpoint: bool = False,
    history_live_fetches: int = 1,
) -> dict[str, Any]:
    assert_canonical_caps()
    assert_no_fixed_positive_cap()
    assert STAGE3_NO_ROW_CAP and DEEP_RESEARCH_NO_FIXED_COUNT and MANUAL_QUEUE_NO_FIXED_CAP

    parked = park_bidnet_auth_history()
    discovery_meta: dict[str, Any] = {}
    hunt_status = None

    if refresh_hunt and authorize_live:
        if reset_hunt_checkpoint:
            reset_checkpoint()
        try:
            from phase_l.hunt import run_phase_l_hunt

            hunt = run_phase_l_hunt(
                authorize_live=True,
                max_sources=max_hunt_sources,
                profile="non_bidnet",
            )
            discovery_meta = hunt.get("discovery_meta") or {}
            hunt_status = (discovery_meta.get("live_runner") or {}).get("run_status")
            rows = list(
                (json.loads((OUT / "accessible_latest.json").read_text(encoding="utf-8"))).get("rows") or []
            )
        except Exception as exc:
            discovery_meta = {"hunt_error": str(exc)[:300]}
            hunt_status = "FAILED"

    if rows is None:
        data = json.loads((OUT / "accessible_latest.json").read_text(encoding="utf-8"))
        rows = list(data.get("rows") or [])

    buyer_memory = load_json(BUYER_VALUE_PATH)
    supplier_memory = load_json(SUPPLIER_MEMORY_PATH)
    build_registry_from_rows(rows)

    # Prefer non-BidNet rows for Stage processing; still scan all access=YES
    access_yes = [r for r in rows if str(r.get("our_bid_access") or "") == "YES"]
    non_bidnet = [r for r in access_yes if not _is_bidnet_row(r)]
    print(
        f"[l14] accessible={len(rows)} access_yes={len(access_yes)} non_bidnet={len(non_bidnet)} "
        f"(BidNet auth history PARKED)",
        flush=True,
    )

    stage3: list[dict[str, Any]] = []
    stage_counts = Counter()
    commercial_s3 = 0
    for i, row in enumerate(access_yes):
        if i and i % 300 == 0:
            print(f"[l14] stage-scan {i}/{len(access_yes)} s3={len(stage3)}", flush=True)
        pipe = run_progressive_stages_cheap(row)
        if (pipe.get("stage1") or {}).get("pass"):
            stage_counts["stage1"] += 1
        if (pipe.get("stage2") or {}).get("pass"):
            stage_counts["stage2"] += 1
        if pipe.get("survives_to_stage3"):
            stage3.append({"row": row, "pipe": pipe, "bidnet": _is_bidnet_row(row)})
            lane = classify_acquisition_lane(
                row,
                commercial=(pipe.get("stage2_screen") or {}).get("commercial_identity")
                or ((pipe.get("stage2") or {}).get("commercial") or {}),
            )
            if "COMMERCIAL" in str(lane.get("acquisition_lane") or "") or "OPEN" in str(
                lane.get("acquisition_lane") or ""
            ):
                commercial_s3 += 1
    stage_counts["stage3"] = len(stage3)
    print(f"[l14] Stage 3={len(stage3)} commercialish={commercial_s3} — exact history (no BidNet auth)...", flush=True)

    gov_grades = Counter()
    quality_states = Counter()
    platform_stats: dict[str, dict[str, Any]] = {}
    history_rows: list[dict[str, Any]] = []
    gov_upgrades: list[dict[str, Any]] = []
    validated: list[dict[str, Any]] = []
    secondary: list[dict[str, Any]] = []
    recon_only: list[dict[str, Any]] = []
    parked_deps: list[dict[str, Any]] = [parked]
    to_a = to_b = to_c = 0
    non_bidnet_s3 = 0
    non_bidnet_history_hits = 0

    for i, item in enumerate(stage3):
        row = item["row"]
        pipe = item["pipe"]
        is_bn = item["bidnet"]
        if not is_bn:
            non_bidnet_s3 += 1
        screen = pipe.get("stage2_screen") or {}
        commercial = dict(
            screen.get("commercial_identity") or ((pipe.get("stage2") or {}).get("commercial") or {})
        )
        s3 = pipe.get("stage3") or {}
        oid = str(row.get("notice_id") or row.get("solicitation_id") or row.get("id") or i)
        platform = detect_platform(row)
        if is_bn:
            platform = "BidNet"

        ps = platform_stats.setdefault(
            platform,
            {
                "raw": 0,
                "commercial": 0,
                "stage3": 0,
                "exact_history": 0,
                "gov_upgrades": 0,
                "quote_targets": 0,
                "gov_abc": 0,
            },
        )
        ps["stage3"] += 1

        lane = classify_acquisition_lane(row, commercial=commercial).get("acquisition_lane")
        if "COMMERCIAL" in str(lane or "") or "OPEN" in str(lane or ""):
            ps["commercial"] += 1

        # Buyer registry enrichment
        try:
            upsert_buyer(row)
        except Exception:
            pass

        original = resolve_original_solicitation(row)
        recovery = run_parallel_recovery(
            row,
            commercial=commercial,
            history={},
            buyer_memory=buyer_memory,
            supplier_memory=supplier_memory,
            stage3=s3,
        )
        econ = recompute_economics_from_recovery(
            row,
            gov_rec=recovery.get("gov"),
            qty_rec=recovery.get("quantity"),
            supplier_rec=recovery.get("suppliers"),
        )

        pre = audit_quote_positive(
            row,
            commercial=commercial,
            gov=econ.get("government_value"),
            suppliers=econ.get("suppliers"),
            qty_info=econ.get("quantity") or recovery.get("quantity"),
            max_buy=econ.get("max_buy"),
            qdep=econ.get("quote_dependent"),
            freight=econ.get("freight"),
            original=original,
            lane=lane,
            deadline_days=_deadline_days(row, pipe),
            buyer_memory=buyer_memory,
            supplier_memory=supplier_memory,
            stage3=s3,
            attempt_upgrades=False,
        )

        hist = None
        # Skip BidNet auth history — parked
        if is_bn:
            hist = {
                "outcome": BIDNET_AUTH_HISTORY_PARKED,
                "grade_after": pre.get("gov_grade") or GOV_VALUE_D,
                "parked": True,
                "platform": "BidNet",
            }
        elif pre.get("gov_grade") == GOV_VALUE_D or (
            "BENCHMARK" in str((econ.get("government_value") or {}).get("source") or "").upper()
        ):
            if i % 25 == 0:
                print(f"[l14] history {i+1}/{len(stage3)} {platform} {oid}", flush=True)
            hist = run_platform_history(
                row,
                commercial=commercial,
                authorize_live=authorize_live and history_live_fetches > 0 and not is_bn,
                max_fetches=history_live_fetches,
            )
            if hist.get("grade_after") in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
                non_bidnet_history_hits += 1
                ps["exact_history"] += 1
                econ = recompute_economics_from_recovery(
                    row,
                    gov_rec={**(hist.get("gov") or {}), "recovered": True},
                    qty_rec=recovery.get("quantity"),
                    supplier_rec=recovery.get("suppliers"),
                )
                g = hist["grade_after"]
                if g == GOV_VALUE_A:
                    to_a += 1
                elif g == GOV_VALUE_B:
                    to_b += 1
                else:
                    to_c += 1
                ps["gov_upgrades"] += 1
                ps["gov_abc"] += 1
                gov_upgrades.append(
                    {
                        "opportunity_id": oid,
                        "platform": platform,
                        "buyer": row.get("agency"),
                        "before": GOV_VALUE_D,
                        "after": g,
                        "outcome": hist.get("outcome"),
                    }
                )
                if hist.get("vendor_intel"):
                    sloop = run_supplier_upgrade_loop(
                        row,
                        commercial=commercial,
                        history={
                            "awardee": hist["vendor_intel"].get("vendor"),
                            "vendor_role": hist["vendor_intel"].get("role"),
                        },
                        supplier_memory=supplier_memory,
                        suppliers=econ.get("suppliers"),
                    )
                    if sloop.get("upgraded"):
                        econ["suppliers"] = sloop.get("suppliers") or econ.get("suppliers")
            if hist.get("parked_dependency"):
                parked_deps.append(hist["parked_dependency"])

            history_rows.append(
                {
                    "opportunity_id": oid,
                    "platform": platform,
                    "buyer": row.get("agency"),
                    "bidnet_parked": False,
                    "outcome": (hist or {}).get("outcome"),
                    "grade_after": (hist or {}).get("grade_after"),
                    "awards_found": (hist or {}).get("awards_found"),
                }
            )

        audit = audit_quote_positive(
            row,
            commercial=commercial,
            gov=econ.get("government_value") or (hist or {}).get("gov"),
            suppliers=econ.get("suppliers"),
            qty_info=econ.get("quantity") or recovery.get("quantity"),
            max_buy=econ.get("max_buy"),
            qdep=econ.get("quote_dependent"),
            freight=econ.get("freight"),
            original=original,
            lane=lane,
            deadline_days=_deadline_days(row, pipe),
            buyer_memory=buyer_memory,
            supplier_memory=supplier_memory,
            stage3=s3,
            attempt_upgrades=True,
        )
        gov_grades[audit["gov_grade"]] += 1
        quality_states[audit["quality_state"]] += 1
        entry = {
            "opportunity_id": oid,
            "buyer": row.get("agency"),
            "platform": platform,
            "product": (row.get("title") or "")[:160],
            "gov_grade": audit["gov_grade"],
            "supplier_grade": audit.get("supplier_grade"),
            "quality_state": audit["quality_state"],
            "bidnet_parked": is_bn,
            "send_authorized": False,
        }
        if audit["quality_state"] == VALIDATED_QUOTE_TARGET:
            validated.append(entry)
            ps["quote_targets"] += 1
        elif audit["quality_state"] == SECONDARY_QUOTE_TARGET:
            secondary.append(entry)
            ps["quote_targets"] += 1
        elif audit["quality_state"] in {RECON_ONLY_CATEGORY_BENCHMARK, "RECON_ONLY_SUPPLIER_SEED"}:
            recon_only.append(entry)

    # Platform raw counts from accessible
    for r in rows:
        p = detect_platform(r)
        if _is_bidnet_row(r):
            p = "BidNet"
        platform_stats.setdefault(
            p,
            {"raw": 0, "commercial": 0, "stage3": 0, "exact_history": 0, "gov_upgrades": 0, "quote_targets": 0, "gov_abc": 0},
        )
        platform_stats[p]["raw"] += 1

    yields = [
        source_economic_yield(
            source_id=p,
            raw=v.get("raw", 0),
            commercial=v.get("commercial", 0),
            stage3=v.get("stage3", 0),
            gov_abc=v.get("gov_abc", 0),
            quote_targets=v.get("quote_targets", 0),
        )
        for p, v in platform_stats.items()
    ]
    yields.sort(key=lambda x: (-(x["gov_abc"] + x["quote_targets"]), -x["commercial"], -x["stage3"]))

    caps_matrix = source_capability_matrix(
        {
            p: {
                "raw": v.get("raw"),
                "stage3": v.get("stage3"),
                "gov_abc": v.get("gov_abc"),
                "quote_targets": v.get("quote_targets"),
                "commercial_yield": "measured",
            }
            for p, v in platform_stats.items()
        }
    )

    state_cov = state_coverage_report(rows)
    buyer_cov = buyer_type_coverage_report(rows)
    cat_cov = category_coverage_report(rows)

    non_bidnet_live = sum(1 for r in rows if not _is_bidnet_row(r))
    improved = (
        non_bidnet_s3 > 0
        and (
            (to_a + to_b + to_c) > 0
            or non_bidnet_history_hits > 0
            or len(validated) + len(secondary) > (L13_BASELINE["validated"] + L13_BASELINE["secondary"])
            or non_bidnet_live > 50
            or any(
                platform_stats.get(p, {}).get("stage3", 0) > 0
                for p in ("OpenGov", "PlanetBids", "Bonfire", "IonWave", "Public Purchase", "DLA/DIBBS", "cooperatives")
            )
        )
    )
    partial_ok = parked.get("kind") == BIDNET_AUTH_HISTORY_PARKED and (
        non_bidnet_s3 > 0 or len(caps_matrix) >= 8
    )

    hunt_ok = hunt_status in {HUNT_COMPLETE, HUNT_COMPLETE_WITH_FAILURES, None} or (
        refresh_hunt and hunt_status in {HUNT_COMPLETE, HUNT_COMPLETE_WITH_FAILURES}
    )

    if improved and (hunt_ok or not refresh_hunt):
        verdict = "PHASE_L14_NONBIDNET_EXPANSION_WORKING"
    elif partial_ok:
        verdict = "PHASE_L14_PARTIAL_NONBIDNET_EXPANSION"
    else:
        verdict = "PHASE_L14_NONBIDNET_EXPANSION_FAILED"

    # If adapters/matrix exist but no useful non-BidNet Stage3 commercial signal → PARTIAL
    if verdict.endswith("WORKING") and (to_a + to_b + to_c) == 0 and non_bidnet_history_hits == 0:
        if non_bidnet_s3 == 0 and non_bidnet_live < 20:
            verdict = "PHASE_L14_PARTIAL_NONBIDNET_EXPANSION"

    next_move = "more discovery coverage"
    if (to_a + to_b + to_c) == 0 and non_bidnet_s3 > 10:
        next_move = "history recovery"
    elif len(validated) + len(secondary) > 5:
        next_move = "supplier validation"
    elif any(d.get("kind") == "PARKED_ACCESS_DEPENDENCY" for d in parked_deps[1:]):
        next_move = "platform registration"

    gaps = {
        "unsupported_or_weak_platforms": [
            c["platform"]
            for c in caps_matrix
            if c.get("discovery") in {"stub", "degraded_anti_bot", "parked"}
            or c.get("commercial_yield") in {"parked_this_phase", "medium"}
        ],
        "weak_states": (state_cov.get("weak_states") or state_cov.get("underrepresented") or [])[:20]
        if isinstance(state_cov, dict)
        else [],
        "weak_buyer_types": [],
        "award_history_gaps": non_bidnet_s3 - non_bidnet_history_hits,
        "auth_gaps": [d for d in parked_deps if d.get("kind") in {BIDNET_AUTH_HISTORY_PARKED, "PARKED_ACCESS_DEPENDENCY"}],
        "bidnet_parked": True,
    }

    payload = {
        "kind": "PhaseL14NonBidNetExpansionResult",
        "phase": "L.14",
        "build": BUILD,
        "generated_at": _utc(),
        "verdict": verdict,
        "fresh_hunt": {
            "ran": refresh_hunt,
            "status": hunt_status,
            "discovery_meta": discovery_meta,
            "raw_unique": (discovery_meta.get("live_runner") or {}).get("unique_records"),
            "accessible": len(rows),
            "stage1": int(stage_counts["stage1"]),
            "stage2": int(stage_counts["stage2"]),
            "stage3": len(stage3),
            "commercial_stage3": commercial_s3,
            "non_bidnet_accessible": non_bidnet_live,
            "non_bidnet_stage3": non_bidnet_s3,
        },
        "platform_contribution": platform_stats,
        "gov_evidence": {
            "A": int(gov_grades.get(GOV_VALUE_A, 0)),
            "B": int(gov_grades.get(GOV_VALUE_B, 0)),
            "C": int(gov_grades.get(GOV_VALUE_C, 0)),
            "D": int(gov_grades.get(GOV_VALUE_D, 0)),
            "upgrades": {"to_A": to_a, "to_B": to_b, "to_C": to_c},
        },
        "quote_quality": {
            "validated": len(validated),
            "secondary": len(secondary),
            "recon_only": len(recon_only),
            "blocked": int(quality_states.get("HARD_BLOCKED", 0)),
            "before": L13_BASELINE,
        },
        "source_economic_yield": {
            "ranked": yields[:15],
            "highest": yields[0] if yields else None,
            "lowest": yields[-1] if yields else None,
        },
        "coverage": {
            "states": state_cov,
            "buyer_types": buyer_cov,
            "categories": cat_cov,
            "priority_order": list(SOURCE_PRIORITY_ORDER),
        },
        "parked_dependencies": parked_deps[:20],
        "discovery_gaps": gaps,
        "legacy_cleanup": legacy_cleanup_report(),
        "stop_rules": {
            "no_phase_m": True,
            "no_bidnet_auth_history": True,
            "no_account_creation": True,
            "no_outreach": True,
            "evidence_standards_unchanged": True,
        },
        "next_move": next_move,
        "remaining_bottleneck": (
            f"BidNet auth history parked ({parked.get('blocked_history')} rows); "
            f"non-BidNet S3={non_bidnet_s3} history_hits={non_bidnet_history_hits} "
            f"govA={to_a} B={to_b} C={to_c}; next={next_move}"
        ),
    }

    save_json(OUT / "l14_fresh_hunt.json", payload["fresh_hunt"])
    save_json(OUT / "l14_platform_inventory.json", {"platforms": platform_stats, "priority": list(SOURCE_PRIORITY_ORDER)})
    save_json(OUT / "l14_source_capabilities.json", {"matrix": caps_matrix})
    save_json(OUT / "l14_exact_history.json", {"rows": history_rows, "hits": non_bidnet_history_hits})
    save_json(OUT / "l14_gov_upgrades.json", {"rows": gov_upgrades, "summary": payload["gov_evidence"]["upgrades"]})
    save_json(OUT / "l14_validated_quote_targets.json", validated)
    save_json(OUT / "l14_secondary_quote_targets.json", secondary)
    save_json(OUT / "l14_discovery_gaps.json", gaps)
    save_json(OUT / "l14_bidnet_parked.json", parked)
    save_json(OUT / "l14_summary.json", payload)

    print(
        f"[l14] verdict={verdict} nonBN_s3={non_bidnet_s3} hist={non_bidnet_history_hits} "
        f"A={to_a} B={to_b} C={to_c} val={len(validated)} sec={len(secondary)}",
        flush=True,
    )
    return payload


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--refresh-hunt", action="store_true")
    p.add_argument("--no-live", action="store_true")
    p.add_argument("--max-hunt-sources", type=int, default=48)
    p.add_argument("--reset-checkpoint", action="store_true")
    p.add_argument("--history-live-fetches", type=int, default=1)
    args = p.parse_args()
    run_phase_l14_nonbidnet(
        authorize_live=not args.no_live,
        refresh_hunt=args.refresh_hunt,
        max_hunt_sources=args.max_hunt_sources,
        reset_hunt_checkpoint=args.reset_checkpoint,
        history_live_fetches=0 if args.no_live else args.history_live_fetches,
    )
