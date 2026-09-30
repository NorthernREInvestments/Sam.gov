"""Phase L.10 — exact evidence workflow + discovery quality (all Stage 3, no caps, no outreach)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.acquisition_lanes import (
    COMMERCIAL_DISTRIBUTOR_CHANNEL,
    COMMERCIAL_OPEN_CHANNEL,
    DEEP_RESEARCH_NO_FIXED_COUNT,
    MANUAL_QUEUE_NO_FIXED_CAP,
    MILSPEC_OPEN_CHANNEL,
    MILSPEC_SPECIALTY,
    QUOTE_REQUIRED_COMMERCIAL,
    STAGE3_NO_ROW_CAP,
    UNKNOWN_ACQUISITION_CHANNEL,
    classify_acquisition_lane,
)
from phase_l.canonical_workflow import (
    BUILD,
    EVIDENCE_EXHAUSTED,
    OWNER_REVIEW,
    QUOTE_TARGET_CANDIDATE,
    QUOTE_TARGET_VALIDATED,
    CanonicalOpportunityWorkflow,
    advance_source_verified,
    advance_through_research,
    classify_identity_state,
)
from phase_l.discovery_audit import audit_discovery_coverage
from phase_l.economic_evaluability import recompute_economics_from_recovery
from phase_l.evidence_recovery import run_parallel_recovery
from phase_l.history_graphs import link_product_history
from phase_l.legacy_cleanup import assert_canonical_caps, assert_no_fixed_positive_cap, legacy_cleanup_report
from phase_l.original_solicitation import resolve_original_solicitation, submission_path_checklist
from phase_l.platform_history import fetch_platform_history, platform_history_inventory
from phase_l.progressive_funnel import run_progressive_stages_cheap
from phase_l.quality_audit import (
    CONFIG_A_EXACT,
    CONFIG_B_STRONG,
    CONFIG_C_PARTIAL,
    CONFIG_UNKNOWN,
    GOV_VALUE_A,
    GOV_VALUE_B,
    GOV_VALUE_C,
    GOV_VALUE_D,
    GOV_VALUE_U,
    HARD_BLOCKED,
    QUANTITY_A_EXACT,
    QUANTITY_B_RANGE,
    QUANTITY_C_UNIT_ONLY,
    QUANTITY_UNKNOWN,
    RECON_ONLY_CATEGORY_BENCHMARK,
    RECON_ONLY_SUPPLIER_SEED,
    SECONDARY_QUOTE_TARGET,
    SUPPLIER_A,
    SUPPLIER_B,
    SUPPLIER_C,
    SUPPLIER_D,
    SUPPLIER_UNKNOWN,
    VALIDATED_QUOTE_TARGET,
    audit_quote_positive,
)
from phase_l.quote_economics import (
    BUYER_VALUE_PATH,
    SUPPLIER_MEMORY_PATH,
    convert_unknown_lane,
    load_json,
    save_json,
)
from phase_l.quote_readiness import AUTHORIZED_CONFIRMED, AUTHORIZED_LIKELY

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
OUT.mkdir(parents=True, exist_ok=True)

L9_BASELINE = {
    "stage3": 420,
    "quote_dependent_positive": 228,
    "gov_d": 214,
    "gov_a": 14,
    "gov_c": 0,
    "supplier_c": 150,
    "supplier_d": 55,
    "validated": 1,
    "secondary": 1,
    "recon_only": 214,
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


def _call_timeout(fn, seconds: float, default):
    import concurrent.futures

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn)
    try:
        return fut.result(timeout=seconds)
    except Exception:
        return default
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def _deadline_days(row: dict[str, Any], pipe: dict[str, Any]) -> float | None:
    d = (pipe.get("stage0") or {}).get("deadline") or {}
    for k in ("days_remaining", "days_to_deadline", "deadline_days"):
        v = _f(d.get(k) or row.get(k))
        if v is not None:
            return v
    return None


def _tier_bump(tiers: Counter, revised: dict[str, Any]) -> None:
    if revised.get("ge_100k"):
        tiers["ge_100k"] += 1
    elif revised.get("ge_50k"):
        tiers["ge_50k"] += 1
    elif revised.get("ge_25k"):
        tiers["ge_25k"] += 1
    elif revised.get("ge_10k"):
        tiers["ge_10k"] += 1
    elif revised.get("ge_5k"):
        tiers["ge_5k"] += 1
    elif revised.get("positive"):
        tiers["positive"] += 1


def run_phase_l10_exact_workflow(
    rows: list[dict[str, Any]] | None = None,
    *,
    authorize_live: bool = True,
    refresh_hunt: bool = False,
    max_hunt_sources: int = 40,
    usaspending_max: int = 20,
) -> dict[str, Any]:
    assert_canonical_caps()
    assert_no_fixed_positive_cap()
    assert STAGE3_NO_ROW_CAP and DEEP_RESEARCH_NO_FIXED_COUNT and MANUAL_QUEUE_NO_FIXED_CAP

    discovery_meta: dict[str, Any] = {}
    if refresh_hunt and authorize_live:
        try:
            from phase_l.hunt import run_phase_l_hunt

            hunt = run_phase_l_hunt(authorize_live=True, max_sources=max_hunt_sources, profile="commercial_feed")
            discovery_meta = hunt.get("discovery_meta") or hunt
            rows = list(
                (json.loads((OUT / "accessible_latest.json").read_text(encoding="utf-8"))).get("rows") or []
            )
        except Exception as exc:
            discovery_meta = {"hunt_error": str(exc)[:300]}

    if rows is None:
        data = json.loads((OUT / "accessible_latest.json").read_text(encoding="utf-8"))
        rows = list(data.get("rows") or [])

    buyer_memory = load_json(BUYER_VALUE_PATH)
    supplier_memory = load_json(SUPPLIER_MEMORY_PATH)

    # Discovery coverage audit (parallel concern)
    discovery_audit = audit_discovery_coverage(rows, hunt_meta=discovery_meta)
    hist_inventory = platform_history_inventory()

    access_yes = [r for r in rows if str(r.get("our_bid_access") or "") == "YES"]
    stage3: list[dict[str, Any]] = []
    stage_counts = Counter()
    lane_counts = Counter()
    for i, row in enumerate(access_yes):
        if i and i % 250 == 0:
            print(f"[l10] stage-scan {i}/{len(access_yes)} s3={len(stage3)}", flush=True)
        pipe = run_progressive_stages_cheap(row)
        if (pipe.get("stage1") or {}).get("pass"):
            stage_counts["stage1"] += 1
        if (pipe.get("stage2") or {}).get("pass"):
            stage_counts["stage2"] += 1
        if pipe.get("survives_to_stage3"):
            stage3.append({"row": row, "pipe": pipe})
    stage_counts["stage3"] = len(stage3)

    def _prio(item: dict[str, Any]) -> tuple[int, str]:
        row, pipe = item["row"], item["pipe"]
        screen = pipe.get("stage2_screen") or {}
        com = screen.get("commercial_identity") or ((pipe.get("stage2") or {}).get("commercial") or {})
        lane = classify_acquisition_lane(row, commercial=com).get("acquisition_lane")
        order = {
            QUOTE_REQUIRED_COMMERCIAL: 0,
            COMMERCIAL_DISTRIBUTOR_CHANNEL: 1,
            COMMERCIAL_OPEN_CHANNEL: 1,
            MILSPEC_OPEN_CHANNEL: 2,
            UNKNOWN_ACQUISITION_CHANNEL: 3,
            MILSPEC_SPECIALTY: 4,
        }.get(str(lane), 9)
        return (order, str(row.get("title") or "")[:40])

    stage3.sort(key=_prio)
    print(f"[l10] Stage 3={len(stage3)} — exact workflow (no cap)...", flush=True)

    try:
        from phase_l.enrichment import lookup_government_history, load_cache, save_cache

        cache = load_cache()
    except Exception:
        lookup_government_history = None  # type: ignore
        cache = {}
        save_cache = lambda c: None  # noqa: E731

    hist_budget = {"usaspending": 0, "usaspending_max": usaspending_max}

    validated: list[dict[str, Any]] = []
    secondary: list[dict[str, Any]] = []
    recon_only: list[dict[str, Any]] = []
    exhausted_rows: list[dict[str, Any]] = []
    gov_d_upgrade_rows: list[dict[str, Any]] = []
    supplier_upgrade_rows: list[dict[str, Any]] = []
    workflow_snaps: list[dict[str, Any]] = []

    gov_grades = Counter()
    supplier_grades = Counter()
    qty_grades = Counter()
    config_grades = Counter()
    quality_states = Counter()
    identity_states = Counter()
    gov_d_results = Counter()
    sup_upgrade_results = Counter()

    qdep_n = evaluable_n = 0
    auth_confirmed = auth_likely = 0
    unknown_after = original_verified = submission_resolved = 0
    auth_source_resolved = 0
    commercial_s3 = 0
    val_tiers: Counter = Counter()
    sec_tiers: Counter = Counter()
    hard_blocked_n = 0

    for i, item in enumerate(stage3):
        row = item["row"]
        pipe = item["pipe"]
        screen = pipe.get("stage2_screen") or {}
        commercial = screen.get("commercial_identity") or ((pipe.get("stage2") or {}).get("commercial") or {})
        identity = screen.get("identity") or ((pipe.get("stage2") or {}).get("identity") or {})
        s3 = pipe.get("stage3") or {}

        oid = str(row.get("notice_id") or row.get("solicitation_id") or row.get("id") or i)
        wf = CanonicalOpportunityWorkflow(opportunity_id=oid)

        lane_info = classify_acquisition_lane(row, commercial=commercial)
        lane = row.get("acquisition_lane") or s3.get("acquisition_lane") or lane_info.get("acquisition_lane")
        if lane == UNKNOWN_ACQUISITION_CHANNEL:
            conv = convert_unknown_lane(row, commercial=commercial, current_lane=lane)
            if conv["converted"]:
                lane = conv["after"]
            else:
                from phase_l.l7_rescue import re_search_commercial

                blob = " ".join(
                    str(x or "") for x in (row.get("title"), commercial.get("manufacturer"), commercial.get("model"))
                )
                if re_search_commercial(blob):
                    lane = QUOTE_REQUIRED_COMMERCIAL
        if lane == UNKNOWN_ACQUISITION_CHANNEL:
            unknown_after += 1
        lane_counts[str(lane)] += 1
        if lane in {
            QUOTE_REQUIRED_COMMERCIAL,
            COMMERCIAL_DISTRIBUTOR_CHANNEL,
            COMMERCIAL_OPEN_CHANNEL,
        }:
            commercial_s3 += 1

        title = (row.get("title") or "")[:50]
        if i % 25 == 0 or i < 3:
            print(f"[l10] {i+1}/{len(stage3)} [{lane}] {title}", flush=True)

        original = resolve_original_solicitation(row)
        submission = submission_path_checklist(row, original=original)
        advance_source_verified(wf, original)
        if original.get("original_source_verified"):
            original_verified += 1
            auth_source_resolved += 1
        if submission.get("submission_path_ready") or (submission.get("checks") or {}).get("submission_method_known"):
            submission_resolved += 1

        id_state = classify_identity_state(commercial, row)
        identity_states[id_state] += 1

        hist: dict[str, Any] = {}
        if lookup_government_history and authorize_live and lane != MILSPEC_SPECIALTY:
            hist = _call_timeout(
                lambda: lookup_government_history(
                    row, identity, budget=hist_budget, cache=cache, authorize_live=authorize_live
                ),
                6.0,
                {},
            )

        plat_hist = fetch_platform_history(row, authorize_live=False)
        if plat_hist.get("awards_normalized"):
            for aw in plat_hist["awards_normalized"]:
                link_product_history(
                    product_key=str(commercial.get("model") or commercial.get("mpn") or row.get("title") or "")[:80],
                    manufacturer=commercial.get("manufacturer"),
                    mpn_model=str(commercial.get("model") or commercial.get("mpn") or ""),
                    buyer=str(row.get("agency") or ""),
                    solicitation=str(row.get("solicitation_id") or ""),
                    award=aw,
                    vendor=aw.get("vendor"),
                    quantity=aw.get("quantity"),
                    price=aw.get("unit_price") or aw.get("total"),
                    date=aw.get("award_date"),
                )

        recovery = run_parallel_recovery(
            row,
            commercial=commercial,
            history=hist,
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

        advance_through_research(
            wf,
            identity_state=id_state,
            gov_researched=True,
            qty_researched=True,
            channel=str(lane),
            suppliers_n=len(econ.get("suppliers") or []),
            economics_ok=True,
            graded=True,
        )

        is_eval = bool((econ.get("evaluability") or {}).get("evaluable"))
        if is_eval:
            evaluable_n += 1
        qdep = econ.get("quote_dependent") or {}
        is_pos = bool((qdep.get("tiers") or {}).get("quote_dependent_positive"))
        if is_pos:
            qdep_n += 1

        # Process ALL Stage 3 (no skip) — economics may be weak; still track workflow
        deadline = _deadline_days(row, pipe)
        audit = audit_quote_positive(
            row,
            commercial=commercial,
            history=hist,
            gov=econ.get("government_value"),
            suppliers=econ.get("suppliers"),
            qty_info=econ.get("quantity") or recovery.get("quantity"),
            max_buy=econ.get("max_buy"),
            qdep=qdep,
            freight=econ.get("freight"),
            original=original,
            lane=lane,
            deadline_days=deadline,
            buyer_memory=buyer_memory,
            supplier_memory=supplier_memory,
            stage3=s3,
            attempt_upgrades=True,
        )

        gov_grades[audit["gov_grade"]] += 1
        supplier_grades[str(audit.get("supplier_grade"))] += 1
        qty_grades[audit["quantity_grade"]] += 1
        config_grades[audit["config_grade"]] += 1
        quality_states[audit["quality_state"]] += 1

        if audit.get("authorization_best") == AUTHORIZED_CONFIRMED:
            auth_confirmed += 1
        elif audit.get("authorization_best") == AUTHORIZED_LIKELY:
            auth_likely += 1

        # Gov D upgrade corpus — any row whose recovery gov was category-benchmark class
        src = str((econ.get("government_value") or {}).get("source") or audit.get("gov_source") or "")
        was_benchmark = "BENCHMARK" in src.upper() or "GOVERNMENT_CATEGORY" in src.upper()
        up_gov = (audit.get("upgrades") or {}).get("gov") or {}
        if was_benchmark or up_gov.get("before") == GOV_VALUE_D or audit["gov_grade"] == GOV_VALUE_D:
            from phase_l.buyer_history_workflow import run_gov_value_upgrade_loop

            gov_loop_detail = run_gov_value_upgrade_loop(
                row,
                commercial=commercial,
                history=hist,
                buyer_memory=buyer_memory,
                stage3=s3,
            )
            after_g = audit["gov_grade"]
            gov_d_upgrade_rows.append(
                {
                    "opportunity_id": oid,
                    "product": (row.get("title") or "")[:120],
                    "original_gov_grade": GOV_VALUE_D,
                    "buyer_history_attempts": (gov_loop_detail.get("buyer_history") or {}).get("attempts"),
                    "exact_product_history_attempts": list(gov_loop_detail.get("GOV_VALUE_UPGRADE_ATTEMPTS") or []),
                    "GOV_VALUE_UPGRADE_ATTEMPTS": gov_loop_detail.get("GOV_VALUE_UPGRADE_ATTEMPTS"),
                    "upgraded_grade": after_g,
                    "evidence_exhausted": after_g == GOV_VALUE_D,
                    "exhausted_reason": (
                        gov_loop_detail.get("exhausted_reason") if after_g == GOV_VALUE_D else None
                    ),
                    "benchmark_source": src,
                }
            )
            if after_g == GOV_VALUE_A:
                gov_d_results["to_A"] += 1
            elif after_g == GOV_VALUE_B:
                gov_d_results["to_B"] += 1
            elif after_g == GOV_VALUE_C:
                gov_d_results["to_C"] += 1
            else:
                gov_d_results["remained_D"] += 1
                gov_d_results["exhausted"] += 1

        # Supplier C/D upgrade among positives / evaluable
        if is_pos or is_eval:
            from phase_l.supplier_upgrade import run_supplier_upgrade_loop
            from phase_l.quality_audit import best_supplier_grade as _bsg

            before_s = _bsg(econ.get("suppliers") or [], commercial=commercial).get("grade")
            sloop = run_supplier_upgrade_loop(
                row,
                commercial=commercial,
                history=hist,
                supplier_memory=supplier_memory,
                suppliers=econ.get("suppliers"),
            )
            after_s = audit.get("supplier_grade")
            if before_s in {SUPPLIER_C, SUPPLIER_D} or after_s in {SUPPLIER_A, SUPPLIER_B, SUPPLIER_C, SUPPLIER_D}:
                supplier_upgrade_rows.append(
                    {
                        "opportunity_id": oid,
                        "original_grade": before_s,
                        "exact_manufacturer_confirmed": sloop.get("exact_manufacturer_confirmed"),
                        "exact_family_confirmed": sloop.get("exact_family_confirmed"),
                        "authorization_checked": sloop.get("authorization_checked"),
                        "upgrade_result": after_s,
                        "exhausted_reason": sloop.get("exhausted_reason"),
                    }
                )
                if before_s in {SUPPLIER_C, SUPPLIER_D}:
                    if after_s == SUPPLIER_A and before_s != SUPPLIER_A:
                        sup_upgrade_results["to_A"] += 1
                    elif after_s == SUPPLIER_B and before_s != SUPPLIER_B:
                        if before_s in {SUPPLIER_C, SUPPLIER_D}:
                            sup_upgrade_results["to_B"] += 1
                    elif after_s == SUPPLIER_C:
                        sup_upgrade_results["remained_C"] += 1
                    elif after_s == SUPPLIER_D:
                        sup_upgrade_results["remained_D"] += 1
                elif before_s in {SUPPLIER_A, SUPPLIER_B} and after_s in {SUPPLIER_A, SUPPLIER_B}:
                    sup_upgrade_results["already_ab"] += 1

        entry = {
            "opportunity_id": oid,
            "solicitation_number": original.get("solicitation_number") or row.get("solicitation_id"),
            "buyer": row.get("agency"),
            "original_solicitation_url": original.get("original_posting_url"),
            "product": (row.get("title") or "")[:160],
            "acquisition_lane": lane,
            "workflow_state": wf.state,
            "identity_state": id_state,
            "gov_value": audit.get("gov_value"),
            "gov_grade": audit.get("gov_grade"),
            "gov_freshness": audit.get("gov_freshness"),
            "gov_source": audit.get("gov_source"),
            "quantity": audit.get("quantity"),
            "quantity_grade": audit.get("quantity_grade"),
            "supplier_count": audit.get("supplier_count"),
            "best_supplier_grade": audit.get("supplier_grade"),
            "authorization": audit.get("authorization_best"),
            "config_grade": audit.get("config_grade"),
            "freight_grade": audit.get("freight_grade"),
            "max_buy": audit.get("max_buy"),
            "profit_tier_revised": audit.get("profit_tier_revised"),
            "quality_state": audit.get("quality_state"),
            "blockers": audit.get("blockers"),
            "quote_priority_score": (audit.get("quote_priority_v2") or {}).get("score"),
            "cav": (audit.get("cav") or {}).get("ConfidenceAdjustedOpportunityValue"),
            "economic_confidence": (audit.get("confidence_matrix") or {}).get("economic_confidence"),
            "deadline_days": deadline,
            "confidence_matrix": audit.get("confidence_matrix"),
            "upgrades": audit.get("upgrades"),
            "platform_history": {
                "platform": plat_hist.get("platform"),
                "adapter_status": plat_hist.get("adapter_status"),
            },
            "workflow_audit_len": len(wf.audit_trail),
            "send_authorized": False,
        }

        state = audit["quality_state"]
        revised = audit.get("profit_tier_revised") or {}

        if state == VALIDATED_QUOTE_TARGET:
            try:
                wf.transition(QUOTE_TARGET_CANDIDATE, rule_id="L10_QUOTE_CANDIDATE", force=True)
                wf.transition(QUOTE_TARGET_VALIDATED, rule_id="L10_VALIDATED", force=True)
                wf.transition(OWNER_REVIEW, rule_id="L10_OWNER_REVIEW", force=True)
            except Exception:
                pass
            entry["workflow_state"] = wf.state
            validated.append(entry)
            _tier_bump(val_tiers, revised)
        elif state == SECONDARY_QUOTE_TARGET:
            try:
                wf.transition(QUOTE_TARGET_CANDIDATE, rule_id="L10_QUOTE_CANDIDATE_SECONDARY", force=True)
                wf.transition(OWNER_REVIEW, rule_id="L10_OWNER_REVIEW", force=True)
            except Exception:
                pass
            entry["workflow_state"] = wf.state
            secondary.append(entry)
            _tier_bump(sec_tiers, revised)
        elif state in {RECON_ONLY_CATEGORY_BENCHMARK, RECON_ONLY_SUPPLIER_SEED}:
            try:
                wf.transition(
                    EVIDENCE_EXHAUSTED,
                    rule_id="L10_RECON_EXHAUSTED",
                    force=True,
                    missing_evidence=[state],
                )
            except Exception:
                pass
            entry["workflow_state"] = wf.state
            recon_only.append(entry)
            exhausted_rows.append(
                {
                    **entry,
                    "EVIDENCE_EXHAUSTED": True,
                    "exhausted_reason": "category_benchmark"
                    if state == RECON_ONLY_CATEGORY_BENCHMARK
                    else "no_exact_supplier_found",
                }
            )
        elif state == HARD_BLOCKED:
            hard_blocked_n += 1

        if i < 30:
            workflow_snaps.append(wf.snapshot())

    try:
        save_cache(cache)
    except Exception:
        pass

    validated.sort(key=lambda x: (-(x.get("cav") or 0), -(x.get("quote_priority_score") or 0)))
    secondary.sort(key=lambda x: (-(x.get("cav") or 0), -(x.get("quote_priority_score") or 0)))
    assert_no_fixed_positive_cap(len(validated))

    # Fresh discovery metrics
    fresh_discovery = {
        "raw_unique": len(rows),
        "accessible": len(access_yes),
        "tangible": discovery_audit.get("totals", {}).get("tangible_products"),
        "commercial": discovery_audit.get("totals", {}).get("commercial_products"),
        "authoritative_source_resolved": auth_source_resolved,
        "stage1": int(stage_counts["stage1"]),
        "stage2": int(stage_counts["stage2"]),
        "stage3": len(stage3),
        "commercial_stage3": commercial_s3,
        "lanes": dict(lane_counts),
        "discovery_meta": discovery_meta,
        "refresh_hunt": refresh_hunt,
    }

    gov_d_start = max(len(gov_d_upgrade_rows), int(gov_grades.get(GOV_VALUE_D, 0)))
    # Meaningful improvement gates
    gov_upgraded = int(gov_d_results.get("to_A", 0) + gov_d_results.get("to_B", 0) + gov_d_results.get("to_C", 0))
    sup_upgraded = int(sup_upgrade_results.get("to_A", 0) + sup_upgrade_results.get("to_B", 0))
    recon_down = len(recon_only) < L9_BASELINE["recon_only"]
    more_validated = len(validated) > L9_BASELINE["validated"]
    more_secondary = len(secondary) > L9_BASELINE["secondary"]
    gov_c_present = int(gov_grades.get(GOV_VALUE_C, 0)) > L9_BASELINE["gov_c"]

    material = (
        (gov_upgraded >= 20 or gov_c_present)
        and (len(validated) + len(secondary)) > (L9_BASELINE["validated"] + L9_BASELINE["secondary"])
        and (recon_down or more_secondary)
        and not (len(validated) > 50 and int(gov_grades.get(GOV_VALUE_D, 0)) == 0)  # no inflation wipe
    )
    partial = (
        len(stage3) > 0
        and (gov_c_present or gov_upgraded > 0 or more_secondary or more_validated)
        and int(gov_grades.get(GOV_VALUE_D, 0)) >= 0
    )
    if material and (more_validated or more_secondary) and gov_c_present:
        verdict = "PHASE_L10_EXACT_WORKFLOW_WORKING"
    elif partial:
        verdict = "PHASE_L10_PARTIAL_EXACT_WORKFLOW"
    else:
        verdict = "PHASE_L10_EXACT_WORKFLOW_FAILED"

    bottleneck_parts = []
    if int(gov_grades.get(GOV_VALUE_D, 0)) > 50:
        bottleneck_parts.append(
            f"{gov_grades.get(GOV_VALUE_D, 0)} rows remain Gov D (generic category / no model-specific band)"
        )
    if len(validated) <= L9_BASELINE["validated"] and int(gov_grades.get(GOV_VALUE_A, 0)) <= L9_BASELINE["gov_a"]:
        bottleneck_parts.append("exact buyer awards still scarce — validated gate needs Gov A/B")
    if discovery_audit.get("weak_sources"):
        bottleneck_parts.append(f"weak discovery platforms: {discovery_audit.get('weak_sources')[:5]}")
    bottleneck_parts.append("no supplier outreach; final bid gate unchanged")
    bottleneck = "; ".join(bottleneck_parts)

    # Counterfactual: L.9 validated must remain validated
    counterfactual = []
    try:
        raw_l9 = json.loads((OUT / "l9_validated_quote_targets.json").read_text(encoding="utf-8"))
        l9_list = raw_l9 if isinstance(raw_l9, list) else (raw_l9.get("rows") or raw_l9.get("targets") or [])
    except Exception:
        l9_list = []
    for old in l9_list:
        oid = old.get("opportunity_id")
        match = next((v for v in validated + secondary + recon_only if v.get("opportunity_id") == oid), None)
        counterfactual.append(
            {
                "old_positive": oid,
                "old_state": old.get("quality_state"),
                "new_state": (match or {}).get("quality_state"),
                "reason": "replayed_through_l10",
            }
        )

    cleanup = legacy_cleanup_report()
    cleanup["phase"] = "L.10"
    cleanup["canonical_workflow"] = "phase_l.canonical_workflow.CanonicalOpportunityWorkflow"
    cleanup["live_path"] = "phase_l.l10_rescue.run_phase_l10_exact_workflow"
    cleanup["obsolete_added"] = [
        "LEGACY_LOOSE_CATEGORY_BENCHMARK_AS_VALIDATED",
        "LEGACY_GENERIC_SUPPLIER_AUTO_PROMOTE",
        "LEGACY_DUPLICATE_HISTORY_BRANCH",
    ]

    payload = {
        "kind": "PhaseL10ExactEvidenceWorkflowResult",
        "phase": "L.10",
        "build": BUILD,
        "generated_at": _utc(),
        "verdict": verdict,
        "stage3_no_row_cap": True,
        "no_fixed_queue_cap": True,
        "canonical_orchestrator": "CanonicalOpportunityWorkflow",
        "fresh_discovery": fresh_discovery,
        "discovery_quality": {
            "productive_sources": discovery_audit.get("productive_sources"),
            "weak_sources": discovery_audit.get("weak_sources"),
            "gap_categories": sorted({g.get("category") for g in discovery_audit.get("gaps") or []}),
            "gap_count": len(discovery_audit.get("gaps") or []),
        },
        "gov_d_upgrade": {
            "starting_count": len(gov_d_upgrade_rows),
            "to_A": int(gov_d_results.get("to_A", 0)),
            "to_B": int(gov_d_results.get("to_B", 0)),
            "to_C": int(gov_d_results.get("to_C", 0)),
            "remained_D": int(gov_d_results.get("remained_D", 0)),
            "exhausted": int(gov_d_results.get("exhausted", 0)),
            "current_gov_grades": {
                "A": int(gov_grades.get(GOV_VALUE_A, 0)),
                "B": int(gov_grades.get(GOV_VALUE_B, 0)),
                "C": int(gov_grades.get(GOV_VALUE_C, 0)),
                "D": int(gov_grades.get(GOV_VALUE_D, 0)),
                "U": int(gov_grades.get(GOV_VALUE_U, 0)),
            },
        },
        "supplier_upgrade": {
            "starting_cd_tracked": len(
                [r for r in supplier_upgrade_rows if r.get("original_grade") in {SUPPLIER_C, SUPPLIER_D}]
            ),
            "to_A": int(sup_upgrade_results.get("to_A", 0)),
            "to_B": int(sup_upgrade_results.get("to_B", 0)),
            "remained_C": int(sup_upgrade_results.get("remained_C", 0)),
            "remained_D": int(sup_upgrade_results.get("remained_D", 0)),
            "already_ab": int(sup_upgrade_results.get("already_ab", 0)),
            "current": {
                "A": int(supplier_grades.get(SUPPLIER_A, 0)),
                "B": int(supplier_grades.get(SUPPLIER_B, 0)),
                "C": int(supplier_grades.get(SUPPLIER_C, 0)),
                "D": int(supplier_grades.get(SUPPLIER_D, 0)),
            },
            "vs_l9_baseline": {
                "A": int(supplier_grades.get(SUPPLIER_A, 0)) - 3,
                "B": int(supplier_grades.get(SUPPLIER_B, 0)) - 20,
                "C": int(supplier_grades.get(SUPPLIER_C, 0)) - 150,
                "D": int(supplier_grades.get(SUPPLIER_D, 0)) - 55,
                "note": "L.10 grades all Stage 3; L.9 graded positives only — D/C counts not directly comparable",
            },
        },
        "quantity_config": {
            "exact": int(qty_grades.get(QUANTITY_A_EXACT, 0)),
            "range": int(qty_grades.get(QUANTITY_B_RANGE, 0)),
            "unit_only": int(qty_grades.get(QUANTITY_C_UNIT_ONLY, 0)),
            "unresolved": int(qty_grades.get(QUANTITY_UNKNOWN, 0)),
            "config_A": int(config_grades.get(CONFIG_A_EXACT, 0)),
            "config_B": int(config_grades.get(CONFIG_B_STRONG, 0)),
            "config_C": int(config_grades.get(CONFIG_C_PARTIAL, 0)),
            "config_unknown": int(config_grades.get(CONFIG_UNKNOWN, 0)),
        },
        "quote_quality": {
            "validated": len(validated),
            "secondary": len(secondary),
            "recon_only": len(recon_only),
            "hard_blocked": hard_blocked_n,
            "evidence_exhausted": len(exhausted_rows),
            "quality_state_distribution": dict(quality_states),
        },
        "validated_profit_tiers": dict(val_tiers),
        "secondary_profit_tiers": dict(sec_tiers),
        "identity_states": dict(identity_states),
        "opportunity_coverage": {
            "stage1": int(stage_counts["stage1"]),
            "stage2": int(stage_counts["stage2"]),
            "stage3": len(stage3),
            "economically_evaluable": evaluable_n,
            "quote_dependent_positive": qdep_n,
            "unknown_lane": unknown_after,
            "authoritative_verified": original_verified,
            "submission_resolved": submission_resolved,
        },
        "counterfactual_l9": counterfactual,
        "platform_history_inventory": hist_inventory,
        "remaining_bottleneck": bottleneck,
        "legacy_cleanup": cleanup,
        "stop_rules": {
            "no_phase_m": True,
            "no_outreach": True,
            "no_quotes_requested": True,
            "no_bids": True,
            "no_purchase": True,
            "no_financing": True,
            "final_verification_unchanged": True,
        },
        "workflow_snapshots_sample": workflow_snaps[:5],
    }

    save_json(OUT / "l10_gov_d_upgrade.json", {"rows": gov_d_upgrade_rows, "summary": payload["gov_d_upgrade"]})
    save_json(
        OUT / "l10_supplier_upgrade.json",
        {"rows": supplier_upgrade_rows, "summary": payload["supplier_upgrade"]},
    )
    save_json(OUT / "l10_validated_quote_targets.json", validated)
    save_json(OUT / "l10_secondary_quote_targets.json", secondary)
    save_json(OUT / "l10_recon_only.json", recon_only)
    save_json(OUT / "l10_evidence_exhausted.json", exhausted_rows)
    save_json(OUT / "l10_fresh_discovery.json", {**fresh_discovery, "audit": discovery_audit})
    save_json(OUT / "l10_summary.json", payload)
    save_json(OUT / "l10_discovery_gaps.json", {"gaps": discovery_audit.get("gaps") or []})

    print(
        f"[l10] verdict={verdict} val={len(validated)} sec={len(secondary)} "
        f"recon={len(recon_only)} govC={gov_grades.get(GOV_VALUE_C, 0)} govD={gov_grades.get(GOV_VALUE_D, 0)}",
        flush=True,
    )
    return payload


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--refresh-hunt", action="store_true")
    p.add_argument("--no-live", action="store_true")
    p.add_argument("--max-hunt-sources", type=int, default=40)
    args = p.parse_args()
    run_phase_l10_exact_workflow(
        authorize_live=not args.no_live,
        refresh_hunt=args.refresh_hunt,
        max_hunt_sources=args.max_hunt_sources,
    )
