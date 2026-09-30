"""Phase L.11 — exact award-history recovery + resilient hunt (no outreach)."""

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
from phase_l.buyer_registry import (
    build_auth_history_gap_queue,
    build_registry_from_rows,
    buyer_type_coverage_report,
    category_coverage_report,
    state_coverage_report,
)
from phase_l.economic_evaluability import recompute_economics_from_recovery
from phase_l.evidence_recovery import run_parallel_recovery
from phase_l.exact_history_recovery import (
    BUILD,
    GOV_UPGRADED_A,
    GOV_UPGRADED_B,
    GOV_UPGRADED_C,
    HISTORY_AUTH_REQUIRED,
    HISTORY_BUYER_RECORDS_NOT_FOUND,
    HISTORY_DOCUMENT_MISSING,
    HISTORY_EVIDENCE_EXHAUSTED,
    HISTORY_SOURCE_BLOCKED,
    run_exact_history_recovery,
)
from phase_l.history_graphs import link_product_history, normalize_award_tabulation
from phase_l.legacy_cleanup import assert_canonical_caps, assert_no_fixed_positive_cap
from phase_l.original_solicitation import resolve_original_solicitation
from phase_l.platform_history import platform_history_inventory, weak_platform_audit
from phase_l.progressive_funnel import run_progressive_stages_cheap
from phase_l.quality_audit import (
    GOV_VALUE_A,
    GOV_VALUE_B,
    GOV_VALUE_C,
    GOV_VALUE_D,
    RECON_ONLY_CATEGORY_BENCHMARK,
    SECONDARY_QUOTE_TARGET,
    SUPPLIER_A,
    SUPPLIER_B,
    VALIDATED_QUOTE_TARGET,
    audit_quote_positive,
)
from phase_l.quote_economics import BUYER_VALUE_PATH, SUPPLIER_MEMORY_PATH, load_json, save_json
from phase_l.resilient_hunt import HUNT_COMPLETE, HUNT_COMPLETE_WITH_FAILURES, reset_checkpoint
from phase_l.supplier_upgrade import run_supplier_upgrade_loop

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
OUT.mkdir(parents=True, exist_ok=True)

L10_BASELINE = {
    "gov_d": 201,
    "gov_a": 14,
    "gov_c": 13,
    "validated": 1,
    "secondary": 11,
    "recon_only": 201,
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


def seed_history_graphs_from_artifacts() -> dict[str, int]:
    """Load prior exact recoveries into product history graph before L.11 pass."""
    n = 0
    paths = [
        OUT / "l8_recovered_gov_values.json",
        OUT / "l9_validated_quote_targets.json",
        OUT / "l10_validated_quote_targets.json",
    ]
    for path in paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows = data if isinstance(data, list) else (data.get("rows") or data.get("targets") or [])
        if isinstance(data, dict) and "recovered" in data:
            rows = data.get("recovered") or rows
        for r in rows:
            if not isinstance(r, dict):
                continue
            unit = r.get("gov_value") or r.get("unit_value") or (r.get("gov") or {}).get("unit_value")
            if not unit and r.get("historical_award_unit_price"):
                unit = r["historical_award_unit_price"]
            if not unit:
                continue
            aw = normalize_award_tabulation(
                {
                    "buyer": r.get("buyer") or r.get("agency"),
                    "solicitation_id": r.get("solicitation_number") or r.get("opportunity_id"),
                    "vendor": r.get("vendor") or r.get("historical_awardee"),
                    "unit_price": unit,
                    "model": (r.get("product") or "")[:40],
                    "item": r.get("product") or r.get("title"),
                    "source": r.get("gov_source") or "seeded_artifact",
                    "award_date": r.get("date"),
                }
            )
            link_product_history(
                product_key=str(r.get("product") or r.get("opportunity_id") or "seed")[:80],
                buyer=str(aw.get("buyer") or ""),
                solicitation=str(aw.get("solicitation_id") or ""),
                award=aw,
                vendor=aw.get("vendor"),
                price=aw.get("unit_price"),
                date=aw.get("award_date"),
            )
            n += 1
    # Enrichment cache
    try:
        from phase_l.enrichment import load_cache

        cache = load_cache()
        for key, val in (cache or {}).items():
            if not isinstance(val, dict):
                continue
            hist = val.get("government_history") or val.get("history") or {}
            unit = hist.get("historical_award_unit_price") or val.get("historical_award_unit_price")
            if not unit:
                continue
            link_product_history(
                product_key=str(key)[:80],
                buyer=str(hist.get("buyer") or val.get("agency") or ""),
                award=normalize_award_tabulation(
                    {
                        "buyer": hist.get("buyer"),
                        "unit_price": unit,
                        "vendor": hist.get("historical_awardee"),
                        "source": "enrichment_cache",
                        "model": key,
                    }
                ),
                vendor=hist.get("historical_awardee"),
                price=unit,
            )
            n += 1
    except Exception:
        pass
    return {"seeded": n}


def run_phase_l11_award_history(
    rows: list[dict[str, Any]] | None = None,
    *,
    authorize_live: bool = True,
    refresh_hunt: bool = False,
    max_hunt_sources: int = 20,
    reset_hunt_checkpoint: bool = False,
    history_live_fetches: int = 2,
) -> dict[str, Any]:
    assert_canonical_caps()
    assert_no_fixed_positive_cap()
    assert STAGE3_NO_ROW_CAP and DEEP_RESEARCH_NO_FIXED_COUNT and MANUAL_QUEUE_NO_FIXED_CAP

    seed_stats = seed_history_graphs_from_artifacts()
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
                profile="commercial_feed",
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

    access_yes = [r for r in rows if str(r.get("our_bid_access") or "") == "YES"]
    stage3: list[dict[str, Any]] = []
    stage_counts = Counter()
    for i, row in enumerate(access_yes):
        if i and i % 300 == 0:
            print(f"[l11] stage-scan {i}/{len(access_yes)} s3={len(stage3)}", flush=True)
        pipe = run_progressive_stages_cheap(row)
        if (pipe.get("stage1") or {}).get("pass"):
            stage_counts["stage1"] += 1
        if (pipe.get("stage2") or {}).get("pass"):
            stage_counts["stage2"] += 1
        if pipe.get("survives_to_stage3"):
            stage3.append({"row": row, "pipe": pipe})
    stage_counts["stage3"] = len(stage3)
    print(f"[l11] Stage 3={len(stage3)} — exact history recovery (no cap)...", flush=True)

    gov_d_results: list[dict[str, Any]] = []
    history_outcomes = Counter()
    gov_grades = Counter()
    supplier_grades = Counter()
    quality_states = Counter()
    validated: list[dict[str, Any]] = []
    secondary: list[dict[str, Any]] = []
    recon_only: list[dict[str, Any]] = []
    supplier_upgrades = Counter()
    auth_gap_audits: list[dict[str, Any]] = []

    from_d_to = Counter()
    starting_d = 0

    for i, item in enumerate(stage3):
        row = item["row"]
        pipe = item["pipe"]
        screen = pipe.get("stage2_screen") or {}
        commercial = dict(
            screen.get("commercial_identity") or ((pipe.get("stage2") or {}).get("commercial") or {})
        )
        s3 = pipe.get("stage3") or {}
        oid = str(row.get("notice_id") or row.get("solicitation_id") or row.get("id") or i)

        lane = classify_acquisition_lane(row, commercial=commercial).get("acquisition_lane")
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

        # Pre-grade
        pre_audit = audit_quote_positive(
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
        pre_gov = pre_audit.get("gov_grade")

        hist_rec = None
        if pre_gov == GOV_VALUE_D or (
            "BENCHMARK" in str((econ.get("government_value") or {}).get("source") or "").upper()
        ):
            starting_d += 1
            if i % 20 == 0:
                print(f"[l11] history {i+1}/{len(stage3)} {oid} {(row.get('title') or '')[:40]}", flush=True)
            hist_rec = run_exact_history_recovery(
                row,
                commercial=commercial,
                history={},
                buyer_memory=buyer_memory,
                authorize_live=authorize_live and history_live_fetches > 0,
                max_live_fetches=history_live_fetches,
            )
            history_outcomes[hist_rec["outcome"]] += 1

            # Apply recovered gov into economics recompute
            if hist_rec.get("gov") and hist_rec.get("grade_after") in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
                econ = recompute_economics_from_recovery(
                    row,
                    gov_rec={**hist_rec["gov"], "recovered": True},
                    qty_rec=recovery.get("quantity"),
                    supplier_rec=recovery.get("suppliers"),
                )
                from_d_to[hist_rec["grade_after"]] += 1

            # Vendor → supplier upgrade
            if hist_rec.get("vendor_intel"):
                sloop = run_supplier_upgrade_loop(
                    row,
                    commercial=commercial,
                    history={"awardee": hist_rec["vendor_intel"].get("vendor"), "vendor_role": hist_rec["vendor_intel"].get("role")},
                    supplier_memory=supplier_memory,
                    suppliers=econ.get("suppliers"),
                )
                if sloop.get("upgraded"):
                    supplier_upgrades["award_vendor_upgrade"] += 1
                    econ["suppliers"] = sloop.get("suppliers") or econ.get("suppliers")

            gov_d_results.append(
                {
                    "opportunity_id": oid,
                    "product": (row.get("title") or "")[:120],
                    "buyer": row.get("agency"),
                    "platform": hist_rec.get("platform"),
                    "original_gov_grade": GOV_VALUE_D,
                    "outcome": hist_rec["outcome"],
                    "upgraded_grade": hist_rec.get("grade_after"),
                    "rule_id": hist_rec.get("rule_id"),
                    "auth_class": hist_rec.get("auth_class"),
                    "attempts": hist_rec.get("attempts"),
                    "awards_found": hist_rec.get("awards_found"),
                    "vendor_intel": hist_rec.get("vendor_intel"),
                }
            )
            auth_gap_audits.append(
                {
                    "opportunity_id": oid,
                    "outcome": hist_rec["outcome"],
                    "auth_class": hist_rec.get("auth_class"),
                    "grade_after": hist_rec.get("grade_after"),
                    "commercial": hist_rec.get("commercial"),
                    "supplier_grade": pre_audit.get("supplier_grade"),
                    "deadline_days": _deadline_days(row, pipe),
                    "apparent_profit": (econ.get("quote_dependent") or {}).get("expected_profit"),
                    "registration_intel": None,
                }
            )

        # Final audit with upgrades (includes L.10 band→C path)
        audit = audit_quote_positive(
            row,
            commercial=commercial,
            gov=econ.get("government_value") or (hist_rec or {}).get("gov"),
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
        supplier_grades[str(audit.get("supplier_grade"))] += 1
        quality_states[audit["quality_state"]] += 1

        entry = {
            "opportunity_id": oid,
            "buyer": row.get("agency"),
            "product": (row.get("title") or "")[:160],
            "gov_grade": audit["gov_grade"],
            "supplier_grade": audit.get("supplier_grade"),
            "quality_state": audit["quality_state"],
            "history_outcome": (hist_rec or {}).get("outcome"),
            "cav": (audit.get("cav") or {}).get("ConfidenceAdjustedOpportunityValue"),
            "send_authorized": False,
        }
        if audit["quality_state"] == VALIDATED_QUOTE_TARGET:
            validated.append(entry)
        elif audit["quality_state"] == SECONDARY_QUOTE_TARGET:
            secondary.append(entry)
        elif audit["quality_state"] in {RECON_ONLY_CATEGORY_BENCHMARK, "RECON_ONLY_SUPPLIER_SEED"}:
            recon_only.append(entry)

    # Also process dedicated L.10 Gov-D artifact corpus (thin rows) when IDs not in Stage 3
    prior_gov_d = []
    for path in (OUT / "l10_gov_d_upgrade.json", OUT / "l10_recon_only.json", OUT / "l9_recon_only.json"):
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        prior_gov_d = raw if isinstance(raw, list) else (raw.get("rows") or [])
        if prior_gov_d:
            break

    seen_hist = {r["opportunity_id"] for r in gov_d_results}
    for g in prior_gov_d:
        oid = str(g.get("opportunity_id") or "")
        if not oid or oid in seen_hist:
            continue
        thin = {
            "notice_id": oid,
            "solicitation_id": g.get("solicitation_number") or oid,
            "agency": g.get("buyer"),
            "title": g.get("product") or g.get("title"),
            "our_bid_access": "YES",
        }
        starting_d += 1
        hist_rec = run_exact_history_recovery(
            thin,
            commercial={},
            buyer_memory=buyer_memory,
            authorize_live=False,
            max_live_fetches=0,
        )
        history_outcomes[hist_rec["outcome"]] += 1
        if hist_rec.get("grade_after") in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
            from_d_to[hist_rec["grade_after"]] += 1
        gov_d_results.append(
            {
                "opportunity_id": oid,
                "product": (thin.get("title") or "")[:120],
                "buyer": thin.get("agency"),
                "platform": hist_rec.get("platform"),
                "original_gov_grade": GOV_VALUE_D,
                "outcome": hist_rec["outcome"],
                "upgraded_grade": hist_rec.get("grade_after"),
                "rule_id": hist_rec.get("rule_id"),
                "auth_class": hist_rec.get("auth_class"),
                "attempts": hist_rec.get("attempts"),
                "awards_found": hist_rec.get("awards_found"),
                "corpus": "l10_artifact_replay",
            }
        )
        seen_hist.add(oid)

    auth_queue = build_auth_history_gap_queue(
        [x["row"] for x in stage3],
        audits=auth_gap_audits,
    )

    # Coverage reports
    state_cov = state_coverage_report(rows)
    buyer_cov = buyer_type_coverage_report(rows)
    cat_cov = category_coverage_report(rows)
    weak_plat = weak_platform_audit()

    upgraded_a = int(from_d_to.get(GOV_VALUE_A, 0) + history_outcomes.get(GOV_UPGRADED_A, 0))
    # Prefer outcome counters
    to_a = int(history_outcomes.get(GOV_UPGRADED_A, 0))
    to_b = int(history_outcomes.get(GOV_UPGRADED_B, 0))
    to_c = int(history_outcomes.get(GOV_UPGRADED_C, 0))
    remain_d = starting_d - to_a - to_b - to_c
    if remain_d < 0:
        remain_d = int(gov_grades.get(GOV_VALUE_D, 0))
    auth_blocked = int(history_outcomes.get(HISTORY_AUTH_REQUIRED, 0))
    exhausted = int(history_outcomes.get(HISTORY_EVIDENCE_EXHAUSTED, 0)) + int(
        history_outcomes.get(HISTORY_BUYER_RECORDS_NOT_FOUND, 0)
    )

    # Success: meaningful A/B from exact history OR substantial C + hunt resilience
    material_ab = (to_a + to_b) >= 3
    material_c = to_c >= 10 or int(gov_grades.get(GOV_VALUE_C, 0)) > L10_BASELINE["gov_c"]
    hunt_ok = hunt_status in {HUNT_COMPLETE, HUNT_COMPLETE_WITH_FAILURES, None} or (
        refresh_hunt and hunt_status in {HUNT_COMPLETE, HUNT_COMPLETE_WITH_FAILURES}
    )
    more_quote = (len(validated) + len(secondary)) > (L10_BASELINE["validated"] + L10_BASELINE["secondary"])

    if material_ab and (hunt_ok or not refresh_hunt):
        verdict = "PHASE_L11_EXACT_HISTORY_WORKING"
    elif (to_a + to_b + to_c) > 0 or material_c or (refresh_hunt and hunt_status in {HUNT_COMPLETE, HUNT_COMPLETE_WITH_FAILURES}):
        verdict = "PHASE_L11_PARTIAL_EXACT_HISTORY"
    else:
        verdict = "PHASE_L11_EXACT_HISTORY_FAILED"

    # If we didn't refresh hunt but resilient path exists and prior hung — note
    bottleneck_parts = []
    if to_a + to_b == 0:
        bottleneck_parts.append("zero exact Gov A/B upgrades — buyer awards still auth-walled or unpublished")
    if auth_blocked:
        bottleneck_parts.append(f"{auth_blocked} HISTORY_AUTH_REQUIRED (see AUTH_HISTORY_GAP_QUEUE)")
    if remain_d > 100:
        bottleneck_parts.append(f"{remain_d} Gov D remain after exact history pass")
    bottleneck_parts.append("no accounts created; final bid gate unchanged")
    bottleneck = "; ".join(bottleneck_parts)

    payload = {
        "kind": "PhaseL11ExactAwardHistoryResult",
        "phase": "L.11",
        "build": BUILD,
        "generated_at": _utc(),
        "verdict": verdict,
        "seed_stats": seed_stats,
        "fresh_hunt": {
            "ran": refresh_hunt,
            "status": hunt_status,
            "discovery_meta": discovery_meta,
            "accessible": len(rows),
        },
        "gov_d_upgrade": {
            "starting_count": starting_d,
            "to_A": to_a,
            "to_B": to_b,
            "to_C": to_c,
            "remained_D": remain_d,
            "auth_blocked": auth_blocked,
            "document_missing": int(history_outcomes.get(HISTORY_DOCUMENT_MISSING, 0)),
            "source_blocked": int(history_outcomes.get(HISTORY_SOURCE_BLOCKED, 0)),
            "buyer_records_not_found": int(history_outcomes.get(HISTORY_BUYER_RECORDS_NOT_FOUND, 0)),
            "exhausted": exhausted,
            "outcomes": dict(history_outcomes),
            "current_gov_grades": {
                "A": int(gov_grades.get(GOV_VALUE_A, 0)),
                "B": int(gov_grades.get(GOV_VALUE_B, 0)),
                "C": int(gov_grades.get(GOV_VALUE_C, 0)),
                "D": int(gov_grades.get(GOV_VALUE_D, 0)),
            },
        },
        "supplier_upgrade_from_awards": dict(supplier_upgrades),
        "supplier_grades": {
            "A": int(supplier_grades.get(SUPPLIER_A, 0)),
            "B": int(supplier_grades.get(SUPPLIER_B, 0)),
            "C": int(supplier_grades.get("SUPPLIER_C", 0)),
            "D": int(supplier_grades.get("SUPPLIER_D", 0)),
        },
        "quote_quality": {
            "validated": len(validated),
            "secondary": len(secondary),
            "recon_only": len(recon_only),
            "quality_state_distribution": dict(quality_states),
        },
        "opportunity_coverage": {
            "stage1": int(stage_counts["stage1"]),
            "stage2": int(stage_counts["stage2"]),
            "stage3": len(stage3),
        },
        "auth_history_gap_queue_size": len(auth_queue),
        "auth_history_gap_top10": auth_queue[:10],
        "discovery_coverage": {
            "state": state_cov,
            "buyer_type": buyer_cov,
            "category": cat_cov,
            "weak_platforms": weak_plat,
        },
        "platform_history_inventory": platform_history_inventory(),
        "remaining_bottleneck": bottleneck,
        "stop_rules": {
            "no_phase_m": True,
            "no_outreach": True,
            "no_account_creation": True,
            "no_auth_bypass": True,
            "final_verification_unchanged": True,
        },
    }

    save_json(OUT / "l11_gov_d_history_upgrade.json", {"rows": gov_d_results, "summary": payload["gov_d_upgrade"]})
    save_json(OUT / "l11_validated_quote_targets.json", validated)
    save_json(OUT / "l11_secondary_quote_targets.json", secondary)
    save_json(OUT / "l11_recon_only.json", recon_only)
    save_json(OUT / "l11_history_outcomes.json", dict(history_outcomes))
    save_json(OUT / "l11_state_coverage.json", state_cov)
    save_json(OUT / "l11_buyer_type_coverage.json", buyer_cov)
    save_json(OUT / "l11_category_coverage.json", cat_cov)
    save_json(OUT / "l11_weak_platforms.json", {"platforms": weak_plat})
    save_json(OUT / "l11_fresh_hunt.json", payload["fresh_hunt"])
    save_json(OUT / "l11_summary.json", payload)

    print(
        f"[l11] verdict={verdict} D:{starting_d} →A={to_a} B={to_b} C={to_c} "
        f"auth={auth_blocked} val={len(validated)} sec={len(secondary)}",
        flush=True,
    )
    return payload


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--refresh-hunt", action="store_true")
    p.add_argument("--no-live", action="store_true")
    p.add_argument("--max-hunt-sources", type=int, default=20)
    p.add_argument("--reset-checkpoint", action="store_true")
    p.add_argument("--history-live-fetches", type=int, default=2)
    args = p.parse_args()
    run_phase_l11_award_history(
        authorize_live=not args.no_live,
        refresh_hunt=args.refresh_hunt,
        max_hunt_sources=args.max_hunt_sources,
        reset_hunt_checkpoint=args.reset_checkpoint,
        history_live_fetches=0 if args.no_live else args.history_live_fetches,
    )
