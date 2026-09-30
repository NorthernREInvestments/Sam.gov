"""Phase L.6 — quote-required economics rescue (all Stage 3, no caps)."""

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
from phase_l.original_solicitation import resolve_original_solicitation, submission_path_checklist
from phase_l.progressive_funnel import run_progressive_stages_cheap
from phase_l.quote_economics import (
    BUILD,
    BUYER_VALUE_PATH,
    GOV_VALUE_COMPARABLE,
    GOV_VALUE_EXACT,
    GOV_VALUE_RANGE,
    GOV_VALUE_STRONG,
    GOV_VALUE_UNKNOWN,
    QUOTE_DEPENDENT_POSITIVE,
    SUPPLIER_MEMORY_PATH,
    convert_unknown_lane,
    evaluate_quote_opportunity,
    load_json,
    remember_buyer_value,
    save_json,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
OUT.mkdir(parents=True, exist_ok=True)

L5_BASELINE = {
    "stage3": 420,
    "quote_required": 149,
    "unknown": 198,
    "specialty": 52,
    "max_buy_calculated": 5,
    "quote_dependent_positive": 0,
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


def run_phase_l6_quote_economics(
    rows: list[dict[str, Any]] | None = None,
    *,
    authorize_live: bool = True,
    refresh_hunt: bool = False,
    max_hunt_sources: int = 70,
    usaspending_max: int = 80,
) -> dict[str, Any]:
    assert STAGE3_NO_ROW_CAP and DEEP_RESEARCH_NO_FIXED_COUNT and MANUAL_QUEUE_NO_FIXED_CAP

    discovery_meta: dict[str, Any] = {}
    if refresh_hunt and authorize_live:
        print("[l6] fresh hunt...", flush=True)
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

    buyer_memory = load_json(BUYER_VALUE_PATH)
    supplier_memory = load_json(SUPPLIER_MEMORY_PATH)

    access_yes = [r for r in rows if str(r.get("our_bid_access") or "") == "YES"]
    stage3: list[dict[str, Any]] = []
    stage_counts = Counter()
    for i, row in enumerate(access_yes):
        if i and i % 200 == 0:
            print(f"[l6] stage-scan {i}/{len(access_yes)} s3={len(stage3)}", flush=True)
        pipe = run_progressive_stages_cheap(row)
        if (pipe.get("stage1") or {}).get("pass"):
            stage_counts["stage1"] += 1
        if (pipe.get("stage2") or {}).get("pass"):
            stage_counts["stage2"] += 1
        if not pipe.get("survives_to_stage3"):
            continue
        stage3.append({"row": row, "pipe": pipe})
    stage_counts["stage3"] = len(stage3)

    # Prioritize quote/commercial lanes for history budget (do not burn on specialty first)
    _lane_priority = {
        QUOTE_REQUIRED_COMMERCIAL: 0,
        COMMERCIAL_DISTRIBUTOR_CHANNEL: 1,
        COMMERCIAL_OPEN_CHANNEL: 1,
        MILSPEC_OPEN_CHANNEL: 2,
        UNKNOWN_ACQUISITION_CHANNEL: 3,
        MILSPEC_SPECIALTY: 4,
        SOURCE_APPROVAL_REQUIRED: 5,
        SOLE_SOURCE_RESTRICTED: 5,
    }

    def _prio(item: dict[str, Any]) -> tuple[int, str]:
        row = item["row"]
        pipe = item["pipe"]
        screen = pipe.get("stage2_screen") or {}
        commercial = screen.get("commercial_identity") or ((pipe.get("stage2") or {}).get("commercial") or {})
        s3 = pipe.get("stage3") or {}
        lane_info = classify_acquisition_lane(row, commercial=commercial)
        lane = row.get("acquisition_lane") or s3.get("acquisition_lane") or lane_info.get("acquisition_lane")
        return (_lane_priority.get(str(lane), 9), str(row.get("title") or "")[:40])

    stage3.sort(key=_prio)

    print(f"[l6] Stage 3={len(stage3)} — quote economics on ALL (no cap)...", flush=True)

    # Optional history helper
    try:
        from phase_l.enrichment import lookup_government_history, load_cache, save_cache

        cache = load_cache()
    except Exception:
        lookup_government_history = None  # type: ignore
        cache = {}
        save_cache = lambda c: None  # noqa: E731

    hist_budget = {"usaspending": 0, "usaspending_max": usaspending_max}

    results: list[dict[str, Any]] = []
    quote_priority: list[dict[str, Any]] = []
    manual_review: list[dict[str, Any]] = []
    deep_queue: list[dict[str, Any]] = []

    gov_states = Counter()
    lane_before = Counter()
    lane_after = Counter()
    unknown_before = unknown_after = unknown_converted = 0
    max_buy_n = 0
    break_even_n = p5_n = p10_n = p25_n = margin_n = 0
    supplier_candidates_n = 0
    with_2plus = with_3plus = 0
    quote_targets_n = 0
    qdep_pos = qdep_5 = qdep_10 = qdep_25 = qdep_50 = 0
    apparent_within = public_within = 0
    verified_prices = strong_leads = 0
    original_verified = original_unresolved = 0
    econ_states = Counter()

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

        # Unknown conversion
        if lane == UNKNOWN_ACQUISITION_CHANNEL:
            unknown_before += 1
            conv = convert_unknown_lane(row, commercial=commercial, current_lane=lane)
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
        print(f"[l6] {i+1}/{len(stage3)} [{lane}] {title}", flush=True)

        original = resolve_original_solicitation(row)
        submission = submission_path_checklist(row, original=original)
        if original.get("original_source_verified"):
            original_verified += 1
        else:
            original_unresolved += 1

        # Specialty / approval — cheap track only
        if lane in {MILSPEC_SPECIALTY, SOURCE_APPROVAL_REQUIRED, SOLE_SOURCE_RESTRICTED}:
            hist = {}
            if lookup_government_history and authorize_live:
                hist = _call_timeout(
                    lambda: lookup_government_history(
                        row, identity, budget=hist_budget, cache=cache, authorize_live=authorize_live
                    ),
                    12.0,
                    {},
                )
            ev = evaluate_quote_opportunity(
                row,
                commercial=commercial,
                history=hist,
                stage3=s3,
                lane=lane,
                deadline_days=_deadline_days(row, pipe),
                buyer_memory=buyer_memory,
                supplier_memory=supplier_memory,
                original=original,
            )
            econ_states[ev.get("economic_state") or "SPECIALTY_CHANNEL"] += 1
            gov_states[str((ev.get("government_value") or {}).get("state"))] += 1
            results.append({**ev, "research_intensity": "specialty_cheap", "submission": submission})
            continue

        # History for quote / commercial / unknown researchable
        hist = {}
        if lookup_government_history and authorize_live and lane in {
            QUOTE_REQUIRED_COMMERCIAL,
            COMMERCIAL_OPEN_CHANNEL,
            COMMERCIAL_DISTRIBUTOR_CHANNEL,
            MILSPEC_OPEN_CHANNEL,
            UNKNOWN_ACQUISITION_CHANNEL,
        }:
            hist = _call_timeout(
                lambda: lookup_government_history(
                    row, identity, budget=hist_budget, cache=cache, authorize_live=authorize_live
                ),
                15.0,
                {},
            )

        # Recon lead from stage3 if present — reject tiny APPROXIMATE placeholders
        lead_price = None
        ar = s3.get("acquisition_range") or {}
        acq_conf = str(ar.get("confidence") or "").upper()
        lo_a, hi_a = _f(ar.get("low")), _f(ar.get("high"))
        if acq_conf in {"EXACT", "STRONG"} and lo_a and lo_a >= 100:
            lead_price = lo_a
            strong_leads += 1
        elif lo_a and hi_a and lo_a >= 1000 and lane in {
            QUOTE_REQUIRED_COMMERCIAL,
            COMMERCIAL_OPEN_CHANNEL,
            COMMERCIAL_DISTRIBUTOR_CHANNEL,
        }:
            lead_price = (lo_a + hi_a) / 2.0
            strong_leads += 1

        ev = evaluate_quote_opportunity(
            row,
            commercial=commercial,
            history=hist,
            stage3=s3,
            lane=lane,
            lead_price=lead_price,
            verified_price=None,
            deadline_days=_deadline_days(row, pipe),
            buyer_memory=buyer_memory,
            supplier_memory=supplier_memory,
            original=original,
        )

        mb = ev.get("max_buy") or {}
        th = mb.get("thresholds") or {}
        if mb.get("supplier_quote_target"):
            max_buy_n += 1
            quote_targets_n += 1
        if th.get("BREAK_EVEN_MAX_BUY"):
            break_even_n += 1
        if th.get("MAX_BUY_FOR_5K_PROFIT"):
            p5_n += 1
        if th.get("MAX_BUY_FOR_10K_PROFIT"):
            p10_n += 1
        if th.get("MAX_BUY_FOR_25K_PROFIT"):
            p25_n += 1
        if th.get("MAX_BUY_FOR_15_PERCENT_MARGIN") or th.get("MAX_BUY_FOR_20_PERCENT_MARGIN"):
            margin_n += 1

        suppliers = ev.get("suppliers") or []
        supplier_candidates_n += len(suppliers)
        if len(suppliers) >= 2:
            with_2plus += 1
        if len(suppliers) >= 3:
            with_3plus += 1

        qdep = ev.get("quote_dependent") or {}
        tiers = qdep.get("tiers") or {}
        if tiers.get("quote_dependent_positive"):
            qdep_pos += 1
        if tiers.get("ge_5k"):
            qdep_5 += 1
        if tiers.get("ge_10k"):
            qdep_10 += 1
        if tiers.get("ge_25k"):
            qdep_25 += 1
        if tiers.get("ge_50k"):
            qdep_50 += 1

        if ev.get("apparently_within_quote_target"):
            apparent_within += 1
        if ev.get("public_price_within_target"):
            public_within += 1

        gov = ev.get("government_value") or {}
        gov_states[str(gov.get("state"))] += 1
        econ_states[str(ev.get("economic_state"))] += 1

        unit = _f(gov.get("unit_value"))
        # Only remember when evidence is product-tied (exact/strong), not buyer-memory echo
        if unit and gov.get("source") not in {"BUYER_PRICE_HISTORY_AVAILABLE"} and gov.get("state") in {
            GOV_VALUE_EXACT,
            GOV_VALUE_STRONG,
        }:
            from phase_l.quote_economics import _product_memory_key

            remember_buyer_value(
                buyer_memory,
                buyer=row.get("agency"),
                unit=unit,
                product_key=_product_memory_key(row, commercial),
            )

        # Quote priority queue — all quote-required + converted
        if lane == QUOTE_REQUIRED_COMMERCIAL or (ev.get("max_buy") and suppliers):
            score = (ev.get("quote_economics_score") or {}).get("score") or 0
            quote_priority.append(
                {
                    "opportunity": (row.get("title") or "")[:120],
                    "solicitation": row.get("solicitation_id") or row.get("notice_id"),
                    "buyer": row.get("agency"),
                    "original_url": (original or {}).get("original_posting_url") or row.get("detail_url"),
                    "deadline_days": _deadline_days(row, pipe),
                    "lane": lane,
                    "government_value": gov.get("unit_value") or gov.get("total_value"),
                    "government_evidence_quality": gov.get("state"),
                    "max_buy_5k": th.get("MAX_BUY_FOR_5K_PROFIT"),
                    "max_buy_10k": th.get("MAX_BUY_FOR_10K_PROFIT"),
                    "max_buy_25k": th.get("MAX_BUY_FOR_25K_PROFIT"),
                    "supplier_candidates": len(suppliers),
                    "lead_price": lead_price,
                    "acquisition_headroom": ev.get("acquisition_headroom"),
                    "quote_priority_score": score,
                    "quote_target_label": mb.get("label"),
                }
            )
            if score >= 40 or tiers.get("quote_dependent_positive"):
                deep_queue.append(
                    {
                        "solicitation": row.get("solicitation_id") or row.get("notice_id"),
                        "lane": lane,
                        "score": score,
                        "reason": "quote_economics_viable",
                    }
                )

        # Manual review
        if (
            gov.get("state") in {GOV_VALUE_EXACT, GOV_VALUE_STRONG}
            and (ev.get("remaining_unknowns") or [])
            or (ev.get("acquisition_headroom") or {}).get("dollars", 0) > 5000
            and lead_price
        ):
            manual_review.append(
                {
                    "solicitation": row.get("solicitation_id") or row.get("notice_id"),
                    "title": (row.get("title") or "")[:100],
                    "reasons": ev.get("remaining_unknowns"),
                    "headroom": ev.get("acquisition_headroom"),
                    "gov_state": gov.get("state"),
                }
            )

        results.append({**ev, "research_intensity": "quote_economics", "submission": submission, "lane": lane})

    try:
        save_cache(cache)
    except Exception:
        pass
    save_json(BUYER_VALUE_PATH, buyer_memory)
    save_json(SUPPLIER_MEMORY_PATH, supplier_memory)

    quote_priority.sort(key=lambda x: (-(x.get("quote_priority_score") or 0), x.get("deadline_days") or 0))

    # Verdict
    material = (
        max_buy_n >= 20
        and quote_targets_n >= 20
        and (qdep_pos >= 5 or p10_n >= 15)
        and with_2plus >= 10
        and unknown_converted >= 10
        and len(results) == len(stage3)
    )
    partial = len(results) == len(stage3) and (max_buy_n >= 5 or quote_targets_n >= 5 or unknown_converted >= 5)
    if material:
        verdict = "PHASE_L6_QUOTE_ECONOMICS_WORKING"
    elif partial:
        verdict = "PHASE_L6_PARTIAL_QUOTE_ECONOMICS"
    else:
        verdict = "PHASE_L6_QUOTE_ECONOMICS_FAILED"

    bottleneck = (
        "Verified public acquisition prices still scarce; quote-dependent positives need actual supplier quotes next"
        if verified_prices == 0 and qdep_pos > 0
        else "Government-side value still sparse for many quote-required rows"
        if gov_states.get(GOV_VALUE_UNKNOWN, 0) > len(stage3) * 0.5
        else "Unknown lanes remain high after conversion"
        if unknown_after > 100
        else "Economics architecture working; execution requires authorized supplier outreach"
    )

    payload = {
        "kind": "PhaseL6QuoteEconomicsResult",
        "phase": "L.6",
        "build": BUILD,
        "generated_at": _utc(),
        "verdict": verdict,
        "stage3_no_row_cap": STAGE3_NO_ROW_CAP,
        "deep_no_cap": DEEP_RESEARCH_NO_FIXED_COUNT,
        "fresh_discovery": {"accessible": len(rows), "discovery_meta": discovery_meta},
        "opportunity_coverage": {
            "stage1": int(stage_counts["stage1"]),
            "stage2": int(stage_counts["stage2"]),
            "stage3": len(stage3),
            "stage3_processed": len(results),
            "deep_researched_queue": len(deep_queue),
        },
        "acquisition_lanes": {
            "before_unknown_conversion": dict(lane_before),
            "after_unknown_conversion": dict(lane_after),
        },
        "unknown_conversion": {
            "before": unknown_before,
            "after": unknown_after,
            "converted": unknown_converted,
        },
        "government_side_value": {
            "exact": int(gov_states.get(GOV_VALUE_EXACT, 0)),
            "strong": int(gov_states.get(GOV_VALUE_STRONG, 0)),
            "comparable": int(gov_states.get(GOV_VALUE_COMPARABLE, 0)),
            "range": int(gov_states.get(GOV_VALUE_RANGE, 0)),
            "unknown": int(gov_states.get(GOV_VALUE_UNKNOWN, 0)),
            "distribution": dict(gov_states),
        },
        "max_buy_engine": {
            "break_even_calculated": break_even_n,
            "max_buy_5k": p5_n,
            "max_buy_10k": p10_n,
            "max_buy_25k": p25_n,
            "margin_ceilings": margin_n,
            "max_buy_any": max_buy_n,
        },
        "supplier_mapping": {
            "supplier_candidates": supplier_candidates_n,
            "opportunities_with_2plus": with_2plus,
            "opportunities_with_3plus": with_3plus,
        },
        "quote_economics": {
            "quote_targets_generated": quote_targets_n,
            "quote_dependent_positive": qdep_pos,
            "ge_5k": qdep_5,
            "ge_10k": qdep_10,
            "ge_25k": qdep_25,
            "ge_50k": qdep_50,
            "priority_queue_n": len(quote_priority),
        },
        "public_price_economics": {
            "verified_prices": verified_prices,
            "strong_leads": strong_leads,
            "observed_within_max_buy": apparent_within + public_within,
            "apparently_within_quote_target": apparent_within,
            "public_price_within_target": public_within,
            "verified_positives": 0,
        },
        "economic_states": dict(econ_states),
        "supplier_quote_priority": quote_priority[:100],
        "economics_manual_review_n": len(manual_review),
        "original_solicitation_integrity": {
            "authoritative_verified": original_verified,
            "unresolved": original_unresolved,
        },
        "l5_baseline": L5_BASELINE,
        "remaining_bottleneck": bottleneck,
        "results_sample": results[:30],
    }

    (OUT / "l6_quote_economics.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    summary = {
        "verdict": verdict,
        "opportunity_coverage": payload["opportunity_coverage"],
        "acquisition_lanes": payload["acquisition_lanes"],
        "unknown_conversion": payload["unknown_conversion"],
        "government_side_value": payload["government_side_value"],
        "max_buy_engine": payload["max_buy_engine"],
        "supplier_mapping": payload["supplier_mapping"],
        "quote_economics": payload["quote_economics"],
        "public_price_economics": payload["public_price_economics"],
        "original_solicitation_integrity": payload["original_solicitation_integrity"],
        "remaining_bottleneck": bottleneck,
        "priority_top10": quote_priority[:10],
    }
    (OUT / "l6_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return payload
