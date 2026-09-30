"""Phase L.7 — quote outreach readiness + expansion (all Stage 3, no caps, no send)."""

from __future__ import annotations

import json
import re
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
from phase_l.legacy_cleanup import assert_canonical_caps, assert_no_fixed_positive_cap, legacy_cleanup_report
from phase_l.original_solicitation import resolve_original_solicitation, submission_path_checklist
from phase_l.progressive_funnel import run_progressive_stages_cheap
from phase_l.quote_economics import (
    BUILD as L6_BUILD,
    BUYER_VALUE_PATH,
    GOV_VALUE_COMPARABLE,
    GOV_VALUE_EXACT,
    GOV_VALUE_RANGE,
    GOV_VALUE_STRONG,
    GOV_VALUE_UNKNOWN,
    SUPPLIER_MEMORY_PATH,
    convert_unknown_lane,
    evaluate_quote_opportunity,
    load_json,
    remember_buyer_value,
    save_json,
    _product_memory_key,
)
from phase_l.quote_readiness import (
    BUILD,
    L6_EXISTING_POSITIVE,
    L7_NEWLY_RECOVERED_POSITIVE,
    PRODUCT_MEMORY_PATH,
    READY_FOR_QUOTE_OUTREACH,
    STRONG_LEAD_WITHIN_TARGET,
    SUPPLIER_PERF_MEMORY_PATH,
    AUTHORIZED_CONFIRMED,
    AUTHORIZED_LIKELY,
    QUOTE_BLOCKED_CONFIGURATION,
    QUOTE_BLOCKED_DEADLINE,
    QUOTE_BLOCKED_ELIGIBILITY,
    QUOTE_BLOCKED_GOV_VALUE,
    QUOTE_BLOCKED_NO_SUPPLIER,
    QUOTE_BLOCKED_PRODUCT_IDENTITY,
    QUOTE_BLOCKED_QUANTITY,
    QUOTE_BLOCKED_SOURCE_APPROVAL,
    QUOTE_BLOCKED_UOM,
    evaluate_quote_readiness,
    owner_queue_row,
    remember_product,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
OUT.mkdir(parents=True, exist_ok=True)

L6_BASELINE = {
    "quote_dependent_positive": 32,
    "unknown": 167,
    "stage3": 420,
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


def _load_l6_positive_keys() -> set[str]:
    path = OUT / "l6_quote_economics.json"
    keys: set[str] = set()
    if not path.exists():
        return keys
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return keys
    for r in data.get("results_sample") or []:
        qd = r.get("quote_dependent") or {}
        if (qd.get("tiers") or {}).get("quote_dependent_positive"):
            row = r.get("row") or {}
            # sample may embed differently — also check solicitation on packet
            sid = (
                row.get("solicitation_id")
                or row.get("notice_id")
                or (r.get("quote_packet") or {}).get("solicitation_number")
                or (r.get("original") or {}).get("solicitation_number")
            )
            if sid:
                keys.add(str(sid))
    for p in data.get("supplier_quote_priority") or []:
        # L.6 priority includes non-positives; use score heuristic later
        pass
    # Prefer summary count; also scan l6 results if full file has more
    for r in data.get("results") or []:
        qd = r.get("quote_dependent") or {}
        if (qd.get("tiers") or {}).get("quote_dependent_positive"):
            sid = (r.get("original") or {}).get("solicitation_number") or (r.get("quote_packet") or {}).get(
                "solicitation_number"
            )
            if sid:
                keys.add(str(sid))
    return keys


def run_phase_l7_quote_readiness(
    rows: list[dict[str, Any]] | None = None,
    *,
    authorize_live: bool = True,
    refresh_hunt: bool = False,
    max_hunt_sources: int = 70,
    usaspending_max: int = 80,
    l6_positive_keys: set[str] | None = None,
) -> dict[str, Any]:
    assert_canonical_caps()
    assert_no_fixed_positive_cap()
    assert STAGE3_NO_ROW_CAP and DEEP_RESEARCH_NO_FIXED_COUNT and MANUAL_QUEUE_NO_FIXED_CAP

    discovery_meta: dict[str, Any] = {}
    if refresh_hunt and authorize_live:
        print("[l7] fresh hunt...", flush=True)
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
    product_memory = load_json(PRODUCT_MEMORY_PATH)
    supplier_perf = load_json(SUPPLIER_PERF_MEMORY_PATH)

    known_l6 = l6_positive_keys if l6_positive_keys is not None else _load_l6_positive_keys()

    access_yes = [r for r in rows if str(r.get("our_bid_access") or "") == "YES"]
    stage3: list[dict[str, Any]] = []
    stage_counts = Counter()
    for i, row in enumerate(access_yes):
        if i and i % 200 == 0:
            print(f"[l7] stage-scan {i}/{len(access_yes)} s3={len(stage3)}", flush=True)
        pipe = run_progressive_stages_cheap(row)
        if (pipe.get("stage1") or {}).get("pass"):
            stage_counts["stage1"] += 1
        if (pipe.get("stage2") or {}).get("pass"):
            stage_counts["stage2"] += 1
        if not pipe.get("survives_to_stage3"):
            continue
        stage3.append({"row": row, "pipe": pipe})
    stage_counts["stage3"] = len(stage3)

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
    print(f"[l7] Stage 3={len(stage3)} — quote readiness on ALL (no cap)...", flush=True)

    try:
        from phase_l.enrichment import lookup_government_history, load_cache, save_cache

        cache = load_cache()
    except Exception:
        lookup_government_history = None  # type: ignore
        cache = {}
        save_cache = lambda c: None  # noqa: E731

    hist_budget = {"usaspending": 0, "usaspending_max": usaspending_max}

    results: list[dict[str, Any]] = []
    owner_queue: list[dict[str, Any]] = []
    ready_queue: list[dict[str, Any]] = []
    positive_audit: list[dict[str, Any]] = []
    newly_recovered: list[dict[str, Any]] = []

    lane_before = Counter()
    lane_after = Counter()
    unknown_before = unknown_after = unknown_converted = 0
    gov_states = Counter()
    blocker_counts = Counter()
    qdep_pos = qdep_5 = qdep_10 = qdep_25 = qdep_50 = qdep_100 = 0
    ready_n = 0
    ready_tiers = Counter()
    l6_still = l6_downgraded = l6_blocked = 0
    l7_new = 0
    supplier_candidates_n = 0
    with_2plus = with_3plus = 0
    auth_confirmed = auth_likely = 0
    strong_lead_within = 0
    original_verified = original_unresolved = 0
    submission_resolved = submission_unresolved = 0
    specialty_retained = 0

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

        l7_extra_convert = False
        if lane == UNKNOWN_ACQUISITION_CHANNEL:
            unknown_before += 1
            conv = convert_unknown_lane(row, commercial=commercial, current_lane=lane)
            # L.7: slightly richer unknown signals
            if not conv["converted"]:
                blob = " ".join(
                    str(x or "")
                    for x in (
                        row.get("title"),
                        row.get("description"),
                        commercial.get("manufacturer"),
                        commercial.get("model"),
                        row.get("naics"),
                        row.get("psc"),
                    )
                )
                if re_search_commercial(blob):
                    conv = {
                        "before": lane,
                        "after": QUOTE_REQUIRED_COMMERCIAL,
                        "converted": True,
                        "lane_info": {
                            **lane_info,
                            "acquisition_lane": QUOTE_REQUIRED_COMMERCIAL,
                            "reason": "l7_unknown_title_signal",
                        },
                    }
                    l7_extra_convert = True
            if conv["converted"]:
                unknown_converted += 1
                lane = conv["after"]
                lane_info = conv["lane_info"]
                if conv.get("lane_info", {}).get("reason") == "l7_unknown_title_signal":
                    l7_extra_convert = True
            else:
                lane = conv["after"]
        lane_after[str(lane)] += 1
        if lane == UNKNOWN_ACQUISITION_CHANNEL:
            unknown_after += 1

        title = (row.get("title") or "")[:50]
        print(f"[l7] {i+1}/{len(stage3)} [{lane}] {title}", flush=True)

        original = resolve_original_solicitation(row)
        submission = submission_path_checklist(row, original=original)
        if original.get("original_source_verified"):
            original_verified += 1
        else:
            original_unresolved += 1
        if submission.get("submission_path_ready") or submission.get("submission_path_resolved") or submission.get("method") or (
            submission.get("checks") or {}
        ).get("submission_method_known"):
            submission_resolved += 1
        else:
            submission_unresolved += 1

        sid = str(
            original.get("solicitation_number")
            or row.get("solicitation_id")
            or row.get("notice_id")
            or ""
        )
        # L6-existing positives = those reachable without L.7-only unknown conversion
        was_l6 = (sid in known_l6) or (not l7_extra_convert)

        # Specialty cheap unless commercially sourceable
        hist: dict[str, Any] = {}
        do_hist = lane in {
            QUOTE_REQUIRED_COMMERCIAL,
            COMMERCIAL_OPEN_CHANNEL,
            COMMERCIAL_DISTRIBUTOR_CHANNEL,
            MILSPEC_OPEN_CHANNEL,
            UNKNOWN_ACQUISITION_CHANNEL,
        } or (lane == MILSPEC_SPECIALTY and (commercial.get("manufacturer") or commercial.get("model")))
        if lane == MILSPEC_SPECIALTY and not do_hist:
            specialty_retained += 1

        if lookup_government_history and authorize_live and do_hist:
            hist = _call_timeout(
                lambda: lookup_government_history(
                    row, identity, budget=hist_budget, cache=cache, authorize_live=authorize_live
                ),
                12.0 if lane != MILSPEC_SPECIALTY else 8.0,
                {},
            )

        lead_price = None
        ar = s3.get("acquisition_range") or {}
        acq_conf = str(ar.get("confidence") or "").upper()
        lo_a, hi_a = _f(ar.get("low")), _f(ar.get("high"))
        if acq_conf in {"EXACT", "STRONG"} and lo_a and lo_a >= 100:
            lead_price = lo_a
        elif lo_a and hi_a and lo_a >= 1000 and lane in {
            QUOTE_REQUIRED_COMMERCIAL,
            COMMERCIAL_OPEN_CHANNEL,
            COMMERCIAL_DISTRIBUTOR_CHANNEL,
        }:
            lead_price = (lo_a + hi_a) / 2.0

        deadline = _deadline_days(row, pipe)
        ev = evaluate_quote_opportunity(
            row,
            commercial=commercial,
            history=hist,
            stage3=s3,
            lane=lane,
            lead_price=lead_price,
            verified_price=None,
            deadline_days=deadline,
            buyer_memory=buyer_memory,
            supplier_memory=supplier_memory,
            original=original,
        )

        # Product memory reuse boost
        pkey = _product_memory_key(row, commercial)
        if pkey and (product_memory.get("products") or {}).get(pkey):
            mem = product_memory["products"][pkey]
            if not (ev.get("government_value") or {}).get("unit_value") and mem.get("government_value"):
                # Do not overwrite — memory is advisory via buyer path already
                pass

        recurring = bool(
            (product_memory.get("products") or {}).get(pkey or "")
            or row.get("recurring_buy")
            or (ev.get("quote_economics_score") or {}).get("factors")
            and "recurring" in str((ev.get("quote_economics_score") or {}).get("factors"))
        )

        readiness = evaluate_quote_readiness(
            row,
            ev=ev,
            lane=lane,
            commercial=commercial,
            original=original,
            submission=submission,
            deadline_days=deadline,
            was_l6_positive=was_l6,
            recurring=recurring,
        )

        qdep = ev.get("quote_dependent") or {}
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
            if tiers.get("ge_100k") or readiness.get("profit_tier") == "ge_100k":
                qdep_100 += 1

            audit_entry = {
                "opportunity_id": sid or row.get("id"),
                "solicitation_number": sid,
                "buyer": row.get("agency"),
                "product": (row.get("title") or "")[:160],
                "estimated_government_value": (ev.get("government_value") or {}).get("unit_value")
                or (ev.get("government_value") or {}).get("total_value"),
                "government_evidence_quality": (ev.get("government_value") or {}).get("state"),
                "max_buy_thresholds": (ev.get("max_buy") or {}).get("thresholds"),
                "profit_tier": readiness.get("profit_tier"),
                "supplier_candidates": [
                    {
                        "name": s.get("name") or s.get("supplier_domain"),
                        "auth": s.get("authorization_state"),
                        "rank": s.get("rank"),
                    }
                    for s in (readiness.get("suppliers_ranked") or [])[:5]
                ],
                "remaining_blockers": readiness.get("blockers"),
                "ready": readiness.get("ready"),
                "status": readiness.get("status"),
                "original_solicitation_location": original.get("original_posting_url"),
                "positive_origin": readiness.get("positive_origin"),
                "lane": lane,
            }
            positive_audit.append(audit_entry)

            if was_l6 or readiness.get("positive_origin") == L6_EXISTING_POSITIVE:
                if readiness.get("ready"):
                    l6_still += 1
                elif readiness.get("blockers"):
                    l6_blocked += 1
                else:
                    l6_downgraded += 1
            else:
                l7_new += 1
                newly_recovered.append(audit_entry)

        if readiness.get("ready"):
            ready_n += 1
            ready_tiers[readiness.get("profit_tier") or "positive"] += 1
            ready_queue.append(
                owner_queue_row(
                    row, ev=ev, readiness=readiness, lane=lane, original=original, deadline_days=deadline
                )
            )

        for b in readiness.get("blockers") or []:
            blocker_counts[b] += 1

        if readiness.get("lead_status") == STRONG_LEAD_WITHIN_TARGET:
            strong_lead_within += 1

        suppliers = readiness.get("suppliers_ranked") or []
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

        gov = ev.get("government_value") or {}
        gov_states[str(gov.get("state"))] += 1

        unit = _f(gov.get("unit_value"))
        if unit and gov.get("source") != "BUYER_PRICE_HISTORY_AVAILABLE" and gov.get("state") in {
            GOV_VALUE_EXACT,
            GOV_VALUE_STRONG,
        }:
            remember_buyer_value(
                buyer_memory,
                buyer=row.get("agency"),
                unit=unit,
                product_key=_product_memory_key(row, commercial),
            )

        if pkey and (is_pos or readiness.get("ready")):
            remember_product(
                product_memory,
                key=pkey,
                payload={
                    "manufacturer": commercial.get("manufacturer"),
                    "model": commercial.get("model"),
                    "lane": lane,
                    "government_value": unit or gov.get("total_value"),
                    "gov_state": gov.get("state"),
                    "max_buy_target": (ev.get("max_buy") or {}).get("supplier_quote_target"),
                    "supplier_count": len(suppliers),
                },
            )

        oq = owner_queue_row(
            row, ev=ev, readiness=readiness, lane=lane, original=original, deadline_days=deadline
        )
        owner_queue.append(oq)

        results.append(
            {
                **ev,
                "readiness": readiness,
                "lane": lane,
                "research_intensity": "quote_readiness",
                "submission": submission,
                "was_l6_positive": was_l6,
            }
        )

    try:
        save_cache(cache)
    except Exception:
        pass
    save_json(BUYER_VALUE_PATH, buyer_memory)
    save_json(SUPPLIER_MEMORY_PATH, supplier_memory)
    save_json(PRODUCT_MEMORY_PATH, product_memory)
    save_json(SUPPLIER_PERF_MEMORY_PATH, supplier_perf)

    ready_queue.sort(key=lambda x: (-(x.get("priority") or 0), x.get("days_remaining") or 0))
    owner_queue.sort(key=lambda x: (-(x.get("priority") or 0), x.get("days_remaining") or 0))
    positive_audit.sort(
        key=lambda x: (
            0 if x.get("profit_tier") == "ge_100k" else
            1 if x.get("profit_tier") == "ge_50k" else
            2 if x.get("profit_tier") == "ge_25k" else
            3 if x.get("profit_tier") == "ge_10k" else
            4 if x.get("profit_tier") == "ge_5k" else 5,
            -(x.get("estimated_government_value") or 0),
        )
    )

    # Dynamic expansion check — no fixed 32
    assert_no_fixed_positive_cap(qdep_pos)

    material = (
        len(results) == len(stage3)
        and ready_n >= 5
        and qdep_pos >= 10
        and unknown_converted >= 5
        and len(positive_audit) == qdep_pos
    )
    partial = len(results) == len(stage3) and (ready_n >= 1 or qdep_pos >= 5)
    if material:
        verdict = "PHASE_L7_QUOTE_OUTREACH_READINESS_WORKING"
    elif partial:
        verdict = "PHASE_L7_PARTIAL_QUOTE_OUTREACH_READINESS"
    else:
        verdict = "PHASE_L7_QUOTE_OUTREACH_READINESS_FAILED"

    bottleneck = (
        "Owner approval + authorized supplier outreach required; verified public prices still 0"
        if ready_n > 0
        else "Few rows clear READY_FOR_QUOTE_OUTREACH — gov-value/deadline/config blockers dominate"
    )

    cleanup = legacy_cleanup_report()

    payload = {
        "kind": "PhaseL7QuoteOutreachReadinessResult",
        "phase": "L.7",
        "build": BUILD,
        "l6_build": L6_BUILD,
        "generated_at": _utc(),
        "verdict": verdict,
        "legacy_cleanup": cleanup,
        "stage3_no_row_cap": STAGE3_NO_ROW_CAP,
        "deep_no_cap": DEEP_RESEARCH_NO_FIXED_COUNT,
        "no_fixed_32_cap": True,
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
        "unknown_conversion": {
            "before": unknown_before,
            "after": unknown_after,
            "converted": unknown_converted,
        },
        "l6_positive_revalidation": {
            "known_l6_keys": len(known_l6),
            "original_baseline": L6_BASELINE["quote_dependent_positive"],
            "still_positive_ready": l6_still,
            "downgraded": l6_downgraded,
            "blocked": l6_blocked,
        },
        "expansion": {
            "newly_recovered_positives": l7_new,
            "total_quote_dependent_positives": qdep_pos,
            "ge_5k": qdep_5,
            "ge_10k": qdep_10,
            "ge_25k": qdep_25,
            "ge_50k": qdep_50,
            "ge_100k": qdep_100,
        },
        "quote_readiness": {
            "READY_FOR_QUOTE_OUTREACH": ready_n,
            "owner_approval_required": ready_n,  # all ready enter owner gate
            "blocked_product_config": blocker_counts.get(QUOTE_BLOCKED_PRODUCT_IDENTITY, 0)
            + blocker_counts.get(QUOTE_BLOCKED_CONFIGURATION, 0),
            "blocked_quantity_uom": blocker_counts.get(QUOTE_BLOCKED_QUANTITY, 0)
            + blocker_counts.get(QUOTE_BLOCKED_UOM, 0),
            "blocked_deadline": blocker_counts.get(QUOTE_BLOCKED_DEADLINE, 0),
            "blocked_eligibility": blocker_counts.get(QUOTE_BLOCKED_ELIGIBILITY, 0)
            + blocker_counts.get(QUOTE_BLOCKED_SOURCE_APPROVAL, 0),
            "blocked_supplier": blocker_counts.get(QUOTE_BLOCKED_NO_SUPPLIER, 0),
            "blocked_gov_value": blocker_counts.get(QUOTE_BLOCKED_GOV_VALUE, 0),
            "blocker_distribution": dict(blocker_counts),
        },
        "ready_profit_tiers": dict(ready_tiers),
        "supplier_coverage": {
            "supplier_candidates": supplier_candidates_n,
            "opportunities_with_2plus": with_2plus,
            "opportunities_with_3plus": with_3plus,
            "authorized_confirmed": auth_confirmed,
            "authorized_likely": auth_likely,
        },
        "government_side_value": {
            "exact": int(gov_states.get(GOV_VALUE_EXACT, 0)),
            "strong": int(gov_states.get(GOV_VALUE_STRONG, 0)),
            "comparable": int(gov_states.get(GOV_VALUE_COMPARABLE, 0)),
            "range": int(gov_states.get(GOV_VALUE_RANGE, 0)),
            "unknown": int(gov_states.get(GOV_VALUE_UNKNOWN, 0)),
        },
        "strong_lead_within_target": strong_lead_within,
        "specialty_retained": specialty_retained,
        "original_solicitation_integrity": {
            "authoritative_verified": original_verified,
            "unresolved": original_unresolved,
            "submission_path_resolved": submission_resolved,
            "submission_unresolved": submission_unresolved,
        },
        "owner_queue_ready": ready_queue,
        "owner_queue_all_sample": owner_queue[:50],
        "positive_audit": positive_audit,
        "newly_recovered": newly_recovered,
        "remaining_bottleneck": bottleneck,
        "send_authorized": False,
        "outreach_authorized": False,
    }

    (OUT / "l7_quote_readiness.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    (OUT / "l7_positive_audit.json").write_text(
        json.dumps({"positives": positive_audit, "count": len(positive_audit)}, indent=2, default=str),
        encoding="utf-8",
    )
    (OUT / "l7_ready_queue.json").write_text(
        json.dumps({"ready": ready_queue, "count": len(ready_queue)}, indent=2, default=str),
        encoding="utf-8",
    )
    summary = {
        "verdict": verdict,
        "opportunity_coverage": payload["opportunity_coverage"],
        "acquisition_lanes": payload["acquisition_lanes"],
        "unknown_conversion": payload["unknown_conversion"],
        "l6_positive_revalidation": payload["l6_positive_revalidation"],
        "expansion": payload["expansion"],
        "quote_readiness": payload["quote_readiness"],
        "ready_profit_tiers": payload["ready_profit_tiers"],
        "supplier_coverage": payload["supplier_coverage"],
        "original_solicitation_integrity": payload["original_solicitation_integrity"],
        "remaining_bottleneck": bottleneck,
        "ready_top10": ready_queue[:10],
    }
    (OUT / "l7_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return payload


def re_search_commercial(blob: str) -> bool:
    import re

    return bool(
        re.search(
            r"\b(ford|chevrolet|dell|cisco|bobcat|kubota|vehicle|truck|suv|laptop|server|switch|"
            r"forklift|trailer|furniture|hvac|equipment|fleet)\b",
            blob,
            re.I,
        )
    )
