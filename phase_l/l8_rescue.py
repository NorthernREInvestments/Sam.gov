"""Phase L.8 — population-wide evidence recovery rescue (all Stage 3, no caps)."""

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
    SOLE_SOURCE_RESTRICTED,
    SOURCE_APPROVAL_REQUIRED,
    STAGE3_NO_ROW_CAP,
    UNKNOWN_ACQUISITION_CHANNEL,
    classify_acquisition_lane,
)
from phase_l.economic_evaluability import (
    ECONOMICALLY_EVALUABLE_QUOTE_DEPENDENT,
    ECONOMICALLY_EVALUABLE_RANGE,
    ECONOMICALLY_EVALUABLE_UNIT_ONLY,
    ECONOMICALLY_EVALUABLE_VERIFIED,
    recompute_economics_from_recovery,
)
from phase_l.evidence_recovery import (
    BUILD,
    BUYER_PATTERN_PATH,
    CONFIGURATION_RECOVERY,
    GOV_VALUE_RECOVERY,
    QUANTITY_RECOVERY,
    SOURCE_LEARNING_PATH,
    SUPPLIER_RECOVERY,
    UOM_RECOVERY,
    bump_source_learning,
    classify_buyer_type,
    classify_product_category,
    load_json,
    run_parallel_recovery,
    save_json,
)
from phase_l.legacy_cleanup import assert_canonical_caps, assert_no_fixed_positive_cap
from phase_l.original_solicitation import resolve_original_solicitation, submission_path_checklist
from phase_l.progressive_funnel import run_progressive_stages_cheap
from phase_l.quote_economics import (
    BUYER_VALUE_PATH,
    GOV_VALUE_COMPARABLE,
    GOV_VALUE_EXACT,
    GOV_VALUE_RANGE,
    GOV_VALUE_STRONG,
    GOV_VALUE_UNKNOWN,
    SUPPLIER_MEMORY_PATH,
    convert_unknown_lane,
    remember_buyer_value,
    _product_memory_key,
)
from phase_l.quote_readiness import (
    AUTHORIZED_CONFIRMED,
    AUTHORIZED_LIKELY,
    READY_FOR_QUOTE_OUTREACH,
    evaluate_quote_readiness,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
OUT.mkdir(parents=True, exist_ok=True)

L7_BASELINE = {
    "stage3": 420,
    "gov_value_missing": 301,
    "no_supplier": 259,
    "quantity_unresolved": 236,
    "unknown_lane": 78,
    "economically_evaluable": 32,
    "quote_dependent_positive": 32,
    "ready_for_quote": 30,
    "authorized_confirmed": 0,
    "supplier_candidates": 863,
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
    # Prefer live opportunities; expired still processed for learning
    return None


def run_phase_l8_population_recovery(
    rows: list[dict[str, Any]] | None = None,
    *,
    authorize_live: bool = True,
    refresh_hunt: bool = False,
    max_hunt_sources: int = 70,
    usaspending_max: int = 60,
) -> dict[str, Any]:
    assert_canonical_caps()
    assert_no_fixed_positive_cap()
    assert STAGE3_NO_ROW_CAP and DEEP_RESEARCH_NO_FIXED_COUNT and MANUAL_QUEUE_NO_FIXED_CAP

    discovery_meta: dict[str, Any] = {}
    if refresh_hunt and authorize_live:
        print("[l8] fresh hunt...", flush=True)
        try:
            from phase_l.hunt import run_phase_l_hunt

            hunt = run_phase_l_hunt(authorize_live=True, max_sources=max_hunt_sources, profile="commercial_feed")
            discovery_meta = hunt.get("discovery_meta") or {}
            rows = list(
                (json.loads((OUT / "accessible_latest.json").read_text(encoding="utf-8"))).get("rows") or []
            )
        except Exception as exc:
            discovery_meta = {"hunt_error": str(exc)[:200]}

    if rows is None:
        data = json.loads((OUT / "accessible_latest.json").read_text(encoding="utf-8"))
        rows = list(data.get("rows") or [])

    buyer_memory = load_json(BUYER_VALUE_PATH) if BUYER_VALUE_PATH.exists() else {}
    try:
        from phase_l.quote_economics import load_json as qe_load

        buyer_memory = qe_load(BUYER_VALUE_PATH)
        supplier_memory = qe_load(SUPPLIER_MEMORY_PATH)
    except Exception:
        supplier_memory = {}
    source_learning = load_json(SOURCE_LEARNING_PATH)
    buyer_patterns = load_json(BUYER_PATTERN_PATH)

    access_yes = [r for r in rows if str(r.get("our_bid_access") or "") == "YES"]
    stage3: list[dict[str, Any]] = []
    stage_counts = Counter()
    for i, row in enumerate(access_yes):
        if i and i % 200 == 0:
            print(f"[l8] stage-scan {i}/{len(access_yes)} s3={len(stage3)}", flush=True)
        pipe = run_progressive_stages_cheap(row)
        if (pipe.get("stage1") or {}).get("pass"):
            stage_counts["stage1"] += 1
        if (pipe.get("stage2") or {}).get("pass"):
            stage_counts["stage2"] += 1
        if pipe.get("survives_to_stage3"):
            stage3.append({"row": row, "pipe": pipe})
    stage_counts["stage3"] = len(stage3)

    # Prioritize live commercial / quote-required for history budget
    def _prio(item: dict[str, Any]) -> tuple[int, float, str]:
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
        dd = _deadline_days(row, pipe)
        # Prefer longer runway; expired last for recovery (but still process)
        urgency = -(dd if dd is not None and dd >= 0 else -1)
        return (order, urgency, str(row.get("title") or "")[:40])

    stage3.sort(key=_prio)
    print(f"[l8] Stage 3={len(stage3)} — evidence recovery on ALL (no cap)...", flush=True)

    try:
        from phase_l.enrichment import lookup_government_history, load_cache, save_cache

        cache = load_cache()
    except Exception:
        lookup_government_history = None  # type: ignore
        cache = {}
        save_cache = lambda c: None  # noqa: E731

    hist_budget = {"usaspending": 0, "usaspending_max": usaspending_max}

    # Before counters (L7 baseline semantics applied to this population)
    before = dict(L7_BASELINE)

    recovered_gov: list[dict[str, Any]] = []
    recovered_sup: list[dict[str, Any]] = []
    recovered_qty: list[dict[str, Any]] = []
    evaluable_rows: list[dict[str, Any]] = []
    new_positives: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    lane_before = Counter()
    lane_after = Counter()
    unknown_before = unknown_after = unknown_converted = 0

    gov_states = Counter()
    qty_quality = Counter()
    eval_states = Counter()
    buyer_type_ok = Counter()
    buyer_type_total = Counter()
    category_ok = Counter()
    category_total = Counter()
    source_gov = Counter()
    source_sup = Counter()
    source_qty = Counter()

    gov_missing = 0
    no_supplier = 0
    qty_unresolved = 0
    qdep_pos = qdep_5 = qdep_10 = qdep_25 = qdep_50 = qdep_100 = 0
    ready_n = 0
    supplier_candidates_n = 0
    with_2plus = with_3plus = 0
    auth_confirmed = auth_likely = 0
    original_verified = original_unresolved = 0
    submission_resolved = 0
    evaluable_n = 0
    newly_evaluable = 0  # vs rough L7 ~32

    for i, item in enumerate(stage3):
        row = item["row"]
        pipe = item["pipe"]
        screen = pipe.get("stage2_screen") or {}
        commercial = screen.get("commercial_identity") or ((pipe.get("stage2") or {}).get("commercial") or {})
        identity = screen.get("identity") or ((pipe.get("stage2") or {}).get("identity") or {})
        s3 = pipe.get("stage3") or {}

        lane_info = classify_acquisition_lane(row, commercial=commercial)
        lane = row.get("acquisition_lane") or s3.get("acquisition_lane") or lane_info.get("acquisition_lane")
        lane_before[str(lane)] += 1

        if lane == UNKNOWN_ACQUISITION_CHANNEL:
            unknown_before += 1
            conv = convert_unknown_lane(row, commercial=commercial, current_lane=lane)
            if not conv["converted"]:
                # L.8: also convert when recovery will find category suppliers
                from phase_l.l7_rescue import re_search_commercial

                blob = " ".join(
                    str(x or "")
                    for x in (row.get("title"), row.get("description"), commercial.get("manufacturer"), commercial.get("model"))
                )
                if re_search_commercial(blob):
                    conv = {
                        "before": lane,
                        "after": QUOTE_REQUIRED_COMMERCIAL,
                        "converted": True,
                        "lane_info": {
                            **lane_info,
                            "acquisition_lane": QUOTE_REQUIRED_COMMERCIAL,
                            "reason": "l8_unknown_title_signal",
                        },
                    }
            if conv["converted"]:
                unknown_converted += 1
                lane = conv["after"]
                lane_info = conv["lane_info"]
            else:
                lane = conv["after"]
        lane_after[str(lane)] += 1
        if lane == UNKNOWN_ACQUISITION_CHANNEL:
            unknown_after += 1

        title = (row.get("title") or "")[:50]
        print(f"[l8] {i+1}/{len(stage3)} [{lane}] {title}", flush=True)

        original = resolve_original_solicitation(row)
        submission = submission_path_checklist(row, original=original)
        if original.get("original_source_verified"):
            original_verified += 1
        else:
            original_unresolved += 1
        if submission.get("submission_path_ready") or (submission.get("checks") or {}).get("submission_method_known"):
            submission_resolved += 1

        buyer_type = classify_buyer_type(row)
        category = classify_product_category(row, commercial)
        buyer_type_total[buyer_type] += 1
        category_total[category] += 1

        # Optional live history (budgeted) — specialty cheap
        hist: dict[str, Any] = {}
        do_hist = lane in {
            QUOTE_REQUIRED_COMMERCIAL,
            COMMERCIAL_OPEN_CHANNEL,
            COMMERCIAL_DISTRIBUTOR_CHANNEL,
            MILSPEC_OPEN_CHANNEL,
            UNKNOWN_ACQUISITION_CHANNEL,
        } or bool(commercial.get("model") or row.get("nsn"))
        if lookup_government_history and authorize_live and do_hist and lane != MILSPEC_SPECIALTY:
            hist = _call_timeout(
                lambda: lookup_government_history(
                    row, identity, budget=hist_budget, cache=cache, authorize_live=authorize_live
                ),
                10.0,
                {},
            )

        # Parallel recovery branches
        recovery = run_parallel_recovery(
            row,
            commercial=commercial,
            history=hist,
            buyer_memory=buyer_memory,
            supplier_memory=supplier_memory,
            stage3=s3,
            missing={
                GOV_VALUE_RECOVERY,
                SUPPLIER_RECOVERY,
                QUANTITY_RECOVERY,
                UOM_RECOVERY,
                CONFIGURATION_RECOVERY,
            },
        )

        gov_rec = recovery.get("gov") or {}
        sup_rec = recovery.get("suppliers") or {}
        qty_rec = recovery.get("quantity") or {}
        uom_rec = recovery.get("uom") or {}
        cfg_rec = recovery.get("configuration") or {}

        # Immediate economic recompute
        econ = recompute_economics_from_recovery(
            row, gov_rec=gov_rec, qty_rec=qty_rec, supplier_rec=sup_rec
        )

        # Source productivity
        if gov_rec.get("recovered"):
            fam = str(gov_rec.get("source_family") or gov_rec.get("source") or "gov")
            source_gov[fam] += 1
            bump_source_learning(source_learning, family=fam, kind="gov", success=True)
            recovered_gov.append(
                {
                    "solicitation": original.get("solicitation_number") or row.get("solicitation_id"),
                    "buyer": row.get("agency"),
                    "title": (row.get("title") or "")[:120],
                    "state": gov_rec.get("state"),
                    "unit_value": gov_rec.get("unit_value"),
                    "total_value": gov_rec.get("total_value"),
                    "source": gov_rec.get("source"),
                    "quality_label": gov_rec.get("quality_label"),
                    "match_rationale": gov_rec.get("match_rationale"),
                    "original_url": original.get("original_posting_url"),
                }
            )
        if sup_rec.get("recovered"):
            fam = str(sup_rec.get("source_family") or "supplier")
            source_sup[fam] += 1
            bump_source_learning(source_learning, family=fam, kind="supplier", success=True)
            recovered_sup.append(
                {
                    "solicitation": original.get("solicitation_number") or row.get("solicitation_id"),
                    "buyer": row.get("agency"),
                    "title": (row.get("title") or "")[:120],
                    "supplier_count": sup_rec.get("count"),
                    "top_suppliers": [
                        {
                            "domain": s.get("supplier_domain"),
                            "auth": s.get("authorization_state"),
                            "score": s.get("SupplierPathScore"),
                        }
                        for s in (sup_rec.get("suppliers") or [])[:5]
                    ],
                    "prior_awardee_lead": sup_rec.get("prior_awardee_lead"),
                    "original_url": original.get("original_posting_url"),
                }
            )
        if qty_rec.get("recovered") and (
            qty_rec.get("quantity") or qty_rec.get("unit_only")
        ):
            fam = str(qty_rec.get("source_family") or "quantity")
            source_qty[fam] += 1
            bump_source_learning(source_learning, family=fam, kind="quantity", success=True)
            recovered_qty.append(
                {
                    "solicitation": original.get("solicitation_number") or row.get("solicitation_id"),
                    "buyer": row.get("agency"),
                    "title": (row.get("title") or "")[:120],
                    "quantity": qty_rec.get("quantity"),
                    "quality": qty_rec.get("quality"),
                    "unit_only": qty_rec.get("unit_only"),
                    "source": qty_rec.get("source"),
                    "uom": uom_rec.get("uom"),
                    "original_url": original.get("original_posting_url"),
                }
            )

        gov = econ.get("government_value") or {}
        gov_states[str(gov.get("state"))] += 1
        if gov.get("state") in {None, GOV_VALUE_UNKNOWN}:
            gov_missing += 1

        suppliers = econ.get("suppliers") or []
        if not suppliers:
            no_supplier += 1
        supplier_candidates_n += len(suppliers)
        if len(suppliers) >= 2:
            with_2plus += 1
        if len(suppliers) >= 3:
            with_3plus += 1
        for s in suppliers:
            if s.get("authorization_state") == AUTHORIZED_CONFIRMED:
                auth_confirmed += 1
            elif s.get("authorization_state") == AUTHORIZED_LIKELY:
                auth_likely += 1

        qqual = str(qty_rec.get("quality") or "UNRESOLVED")
        qty_quality[qqual] += 1
        if qqual == "UNRESOLVED" and not qty_rec.get("unit_only"):
            qty_unresolved += 1

        ev_state = (econ.get("evaluability") or {}).get("state")
        eval_states[str(ev_state)] += 1
        is_eval = bool((econ.get("evaluability") or {}).get("evaluable"))
        if is_eval:
            evaluable_n += 1
            buyer_type_ok[buyer_type] += 1
            category_ok[category] += 1
            evaluable_rows.append(
                {
                    "solicitation": original.get("solicitation_number") or row.get("solicitation_id"),
                    "buyer": row.get("agency"),
                    "title": (row.get("title") or "")[:120],
                    "lane": lane,
                    "evaluability": ev_state,
                    "gov_state": gov.get("state"),
                    "gov_unit": gov.get("unit_value"),
                    "supplier_count": len(suppliers),
                    "quantity_quality": qqual,
                    "max_buy_target": (econ.get("max_buy") or {}).get("supplier_quote_target"),
                    "quote_target_kind": econ.get("quote_target_kind"),
                    "original_url": original.get("original_posting_url"),
                }
            )
            if evaluable_n > before["economically_evaluable"]:
                newly_evaluable += 1  # cumulative above baseline tracked later

        qdep = econ.get("quote_dependent") or {}
        tiers = qdep.get("tiers") or {}
        is_pos = bool(tiers.get("quote_dependent_positive"))
        if is_pos:
            qdep_pos += 1
            if tiers.get("ge_5k"):
                qdep_5 += 1
            if tiers.get("ge_10k"):
                qdep_10 += 1
            if tiers.get("ge_25k"):
                qdep_25 += 1
            if tiers.get("ge_50k"):
                qdep_50 += 1
            # ge_100k rough
            rev = _f((econ.get("expected_revenue") or {}).get("ExpectedRevenueMid"))
            if rev and rev >= 150000 and tiers.get("ge_50k"):
                qdep_100 += 1
            if qdep_pos > before["quote_dependent_positive"]:
                new_positives.append(
                    {
                        "solicitation": original.get("solicitation_number") or row.get("solicitation_id"),
                        "title": (row.get("title") or "")[:120],
                        "buyer": row.get("agency"),
                        "gov_state": gov.get("state"),
                        "max_buy_target": (econ.get("max_buy") or {}).get("supplier_quote_target"),
                        "profit_tiers": tiers,
                        "origin": "L8_NEWLY_RECOVERED_POSITIVE",
                        "original_url": original.get("original_posting_url"),
                    }
                )

        # Quote readiness expansion (no send)
        deadline = _deadline_days(row, pipe)
        # Build minimal ev for readiness
        ev_for_ready = {
            "government_value": gov,
            "max_buy": econ.get("max_buy"),
            "quote_dependent": qdep,
            "suppliers": suppliers,
            "supplier_count": len(suppliers),
            "uom": {"quantity": qty_rec.get("quantity"), "uom": uom_rec.get("uom"), "status": uom_rec.get("status")},
            "freight": econ.get("freight"),
            "expected_revenue": econ.get("expected_revenue"),
            "apparently_within_quote_target": False,
        }
        readiness = evaluate_quote_readiness(
            row,
            ev=ev_for_ready,
            lane=lane,
            commercial=commercial,
            original=original,
            submission=submission,
            deadline_days=deadline if deadline is not None else 21,  # unknown deadline: don't auto-block
            was_l6_positive=False,
        )
        if readiness.get("ready"):
            ready_n += 1

        # Persist buyer value for strong/exact
        unit = _f(gov.get("unit_value"))
        if unit and gov.get("state") in {GOV_VALUE_EXACT, GOV_VALUE_STRONG} and gov.get("final_award_value"):
            remember_buyer_value(
                buyer_memory,
                buyer=row.get("agency"),
                unit=unit,
                product_key=_product_memory_key(row, commercial),
            )
            # Buyer retrieval pattern
            bkey = str(row.get("agency") or "").upper()
            if bkey:
                buyer_patterns.setdefault("buyers", {})
                bp = buyer_patterns["buyers"].setdefault(bkey, {"productive_sources": [], "hits": 0})
                bp["hits"] = int(bp.get("hits") or 0) + 1
                src = gov_rec.get("source")
                if src and src not in bp["productive_sources"]:
                    bp["productive_sources"].append(src)

        results.append(
            {
                "lane": lane,
                "recovery": {
                    "gov_recovered": gov_rec.get("recovered"),
                    "supplier_recovered": sup_rec.get("recovered"),
                    "quantity_recovered": qty_rec.get("recovered"),
                    "uom": uom_rec.get("uom"),
                    "configuration": cfg_rec.get("options_detected"),
                },
                "evaluability": econ.get("evaluability"),
                "government_value": gov,
                "quote_dependent": qdep,
                "ready": readiness.get("ready"),
                "readiness_status": readiness.get("status"),
                "buyer_type": buyer_type,
                "category": category,
                "original": original,
                "send_authorized": False,
            }
        )

    try:
        save_cache(cache)
    except Exception:
        pass
    from phase_l.quote_economics import save_json as qe_save

    qe_save(BUYER_VALUE_PATH, buyer_memory)
    qe_save(SUPPLIER_MEMORY_PATH, supplier_memory)
    save_json(SOURCE_LEARNING_PATH, source_learning)
    save_json(BUYER_PATTERN_PATH, buyer_patterns)

    # Newly evaluable = after - before baseline
    newly_evaluable = max(0, evaluable_n - before["economically_evaluable"])
    # New positives beyond baseline
    new_pos_n = max(0, qdep_pos - before["quote_dependent_positive"])
    # Keep only truly new in artifact (last N or those marked)
    if len(new_positives) > new_pos_n:
        new_positives = new_positives[-new_pos_n:] if new_pos_n else []

    after = {
        "gov_value_missing": gov_missing,
        "no_supplier": no_supplier,
        "quantity_unresolved": qty_unresolved,
        "unknown_lane": unknown_after,
        "economically_evaluable": evaluable_n,
        "quote_dependent_positive": qdep_pos,
        "ready_for_quote": ready_n,
        "authorized_confirmed": auth_confirmed,
        "supplier_candidates": supplier_candidates_n,
    }

    def delta(k: str) -> int:
        return int(after.get(k, 0)) - int(before.get(k, 0))

    # Material improvement for WORKING
    material = (
        len(results) == len(stage3)
        and after["economically_evaluable"] >= before["economically_evaluable"] + 40
        and after["gov_value_missing"] <= before["gov_value_missing"] - 40
        and after["no_supplier"] <= before["no_supplier"] - 40
        and (after["quantity_unresolved"] <= before["quantity_unresolved"] - 30 or after["economically_evaluable"] >= 100)
    )
    partial = (
        len(results) == len(stage3)
        and (
            after["economically_evaluable"] > before["economically_evaluable"]
            or after["gov_value_missing"] < before["gov_value_missing"]
            or after["no_supplier"] < before["no_supplier"]
        )
    )
    if material:
        verdict = "PHASE_L8_POPULATION_EVIDENCE_RECOVERY_WORKING"
    elif partial:
        verdict = "PHASE_L8_PARTIAL_POPULATION_EVIDENCE_RECOVERY"
    else:
        verdict = "PHASE_L8_POPULATION_EVIDENCE_RECOVERY_FAILED"

    bottleneck = (
        "Verified public acquisition prices still scarce; economically evaluable rows still need owner-approved supplier quotes"
        if evaluable_n > 80 and after["gov_value_missing"] < 200
        else "Government-value and supplier evidence remain sparse on specialty/sterile titles"
    )

    comparison_table = [
        {"metric": k, "before": before.get(k), "after": after.get(k), "delta": delta(k)}
        for k in (
            "gov_value_missing",
            "no_supplier",
            "quantity_unresolved",
            "unknown_lane",
            "economically_evaluable",
            "quote_dependent_positive",
            "ready_for_quote",
            "authorized_confirmed",
            "supplier_candidates",
        )
    ]

    payload = {
        "kind": "PhaseL8PopulationEvidenceRecoveryResult",
        "phase": "L.8",
        "build": BUILD,
        "generated_at": _utc(),
        "verdict": verdict,
        "stage3_no_row_cap": STAGE3_NO_ROW_CAP,
        "no_fixed_positive_cap": True,
        "fresh_discovery": {"accessible": len(rows), "discovery_meta": discovery_meta},
        "opportunity_coverage": {
            "stage1": int(stage_counts["stage1"]),
            "stage2": int(stage_counts["stage2"]),
            "stage3": len(stage3),
            "stage3_processed": len(results),
        },
        "acquisition_lanes": {
            "before_unknown_conversion": dict(lane_before),
            "after_unknown_conversion": dict(lane_after),
        },
        "unknown_conversion": {"before": unknown_before or before["unknown_lane"], "after": unknown_after, "converted": unknown_converted},
        "comparison_table": comparison_table,
        "blocker_reduction": {
            "gov_value_missing": {"before": before["gov_value_missing"], "after": gov_missing, "delta": gov_missing - before["gov_value_missing"]},
            "no_supplier": {"before": before["no_supplier"], "after": no_supplier, "delta": no_supplier - before["no_supplier"]},
            "quantity_unresolved": {
                "before": before["quantity_unresolved"],
                "after": qty_unresolved,
                "delta": qty_unresolved - before["quantity_unresolved"],
            },
            "unknown_lane": {"before": before["unknown_lane"], "after": unknown_after, "delta": unknown_after - before["unknown_lane"]},
        },
        "government_side_value": {
            "exact": int(gov_states.get(GOV_VALUE_EXACT, 0)),
            "strong": int(gov_states.get(GOV_VALUE_STRONG, 0)),
            "range": int(gov_states.get(GOV_VALUE_RANGE, 0)),
            "comparable": int(gov_states.get(GOV_VALUE_COMPARABLE, 0)),
            "unknown": int(gov_states.get(GOV_VALUE_UNKNOWN, 0)),
        },
        "supplier_recovery": {
            "supplier_candidates": supplier_candidates_n,
            "authorized_confirmed": auth_confirmed,
            "authorized_likely": auth_likely,
            "opportunities_with_2plus": with_2plus,
            "opportunities_with_3plus": with_3plus,
        },
        "quantity_uom": dict(qty_quality),
        "economic_conversion": {
            "economically_evaluable_before": before["economically_evaluable"],
            "economically_evaluable_after": evaluable_n,
            "newly_evaluable": newly_evaluable,
            "evaluability_states": dict(eval_states),
            "quote_dependent_positives": qdep_pos,
            "new_positives": new_pos_n,
            "READY_FOR_QUOTE_OUTREACH": ready_n,
        },
        "profit_tiers_quote_dependent": {
            "positive": qdep_pos,
            "ge_5k": qdep_5,
            "ge_10k": qdep_10,
            "ge_25k": qdep_25,
            "ge_50k": qdep_50,
            "ge_100k": qdep_100,
        },
        "profit_tiers_verified": {"positive": 0, "ge_5k": 0, "ge_10k": 0, "ge_25k": 0, "ge_50k": 0, "ge_100k": 0},
        "source_productivity": {
            "gov_values_recovered_by_family": dict(source_gov),
            "suppliers_recovered_by_family": dict(source_sup),
            "quantities_recovered_by_family": dict(source_qty),
        },
        "buyer_type_recovery": {
            bt: {"total": buyer_type_total[bt], "evaluable": buyer_type_ok.get(bt, 0)}
            for bt in buyer_type_total
        },
        "category_recovery": {
            cat: {"total": category_total[cat], "evaluable": category_ok.get(cat, 0)}
            for cat in category_total
        },
        "original_solicitation_integrity": {
            "authoritative_verified": original_verified,
            "unresolved": original_unresolved,
            "submission_path_resolved": submission_resolved,
        },
        "remaining_bottleneck": bottleneck,
        "send_authorized": False,
        "legacy_cleanup_notes": {
            "missing_gov_not_dead_end": True,
            "missing_supplier_not_final": True,
            "missing_qty_allows_unit_only": True,
            "no_fixed_caps": True,
        },
    }

    (OUT / "l8_population_recovery.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    (OUT / "l8_recovered_gov_values.json").write_text(
        json.dumps({"count": len(recovered_gov), "rows": recovered_gov}, indent=2, default=str), encoding="utf-8"
    )
    (OUT / "l8_recovered_suppliers.json").write_text(
        json.dumps({"count": len(recovered_sup), "rows": recovered_sup}, indent=2, default=str), encoding="utf-8"
    )
    (OUT / "l8_recovered_quantities.json").write_text(
        json.dumps({"count": len(recovered_qty), "rows": recovered_qty}, indent=2, default=str), encoding="utf-8"
    )
    (OUT / "l8_economically_evaluable.json").write_text(
        json.dumps({"count": len(evaluable_rows), "rows": evaluable_rows}, indent=2, default=str), encoding="utf-8"
    )
    (OUT / "l8_new_positives.json").write_text(
        json.dumps({"count": len(new_positives), "rows": new_positives}, indent=2, default=str), encoding="utf-8"
    )
    summary = {
        "verdict": verdict,
        "opportunity_coverage": payload["opportunity_coverage"],
        "comparison_table": comparison_table,
        "blocker_reduction": payload["blocker_reduction"],
        "government_side_value": payload["government_side_value"],
        "supplier_recovery": payload["supplier_recovery"],
        "quantity_uom": payload["quantity_uom"],
        "economic_conversion": payload["economic_conversion"],
        "profit_tiers_quote_dependent": payload["profit_tiers_quote_dependent"],
        "source_productivity": payload["source_productivity"],
        "original_solicitation_integrity": payload["original_solicitation_integrity"],
        "remaining_bottleneck": bottleneck,
    }
    (OUT / "l8_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return payload
