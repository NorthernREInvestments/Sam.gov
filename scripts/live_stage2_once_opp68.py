"""EXACTLY ONE live Stage 2 evidence extraction for Opp 68 — then cache verify only."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

EXPECTED_S1 = "526f0a0fd7a304b3308cd8c48402c3e35a3638667b8179533906cc108bba6eb9"
EXPECTED_S2 = "9a90fc474c003967f31fd8c7cbd7ecf625da3b7bc261c8817caee9118e8c724a"


class SecondOpenAICallAttempted(RuntimeError):
    pass


IMPORTANT = {
    "RESPONSE_DEADLINE": ("dates", "response_deadline"),
    "QUANTITY": ("scope", "quantity"),
    "PLACE_OF_PERFORMANCE": ("execution", "place_of_performance"),
    "DELIVERY_LOCATION": ("execution", "delivery_location"),
    "INSTALLATION_REQUIRED": ("execution", "installation_required"),
    "BOND_REQUIREMENT": ("compliance", "bond_requirement"),
    "LICENSES": ("compliance", "licenses"),
    "INSURANCE_REQUIREMENT": ("compliance", "insurance_requirement"),
    "WAGE_DETERMINATION": ("compliance", "wage_determination"),
    "SUBMISSION_METHOD": ("submission", "submission_method"),
    "OPTION_PERIODS": ("economic_evidence", "option_periods"),
    "MANUFACTURER_AUTHORIZATION": ("compliance", "manufacturer_authorization"),
    "SET_ASIDE": ("procurement", "set_aside"),
    "STATED_VALUE": ("economic_evidence", "stated_value"),
}


def _fact_status(tree: dict[str, Any], section: str, key: str) -> dict[str, Any]:
    f = ((tree.get(section) or {}).get(key)) or {}
    if not f or f.get("value") is None or str(f.get("status")) in {"UNKNOWN", ""}:
        return {"value": None, "status": "UNKNOWN"}
    return {
        "value": f.get("value"),
        "status": f.get("status"),
        "document_id": f.get("document_id"),
        "evidence_text": f.get("evidence_text"),
    }


def main() -> int:
    import openai_runtime
    from ai_analysis_cache import analysis_fingerprint, content_source_hash, get_cached_analysis
    from ai_funnel import stage0_evaluate
    from ai_model_router import FunnelStage, ModelTier, model_for_tier
    from ai_pricing import breakdown_call_cost_usd, estimate_tokens_from_chars, rates_for_model
    from ai_stage1 import resolve_current_stage1_result
    from ai_stage2 import (
        FACT_CODE_TO_PATH,
        STAGE2_INSTRUCTIONS,
        STAGE2_MAX_OUTPUT_TOKENS,
        STAGE2_SCHEMA_VERSION,
        STAGE2_TASK,
        build_stage2_input,
        can_run_stage2,
        evidence_text_in_source,
        run_stage2_evidence,
        stage2_schema_fingerprint_component,
        validate_and_envelope_fact,
        validate_fatal_facts,
    )
    from data_integrity import STATUS_ASSESSMENT, STATUS_UNKNOWN, STATUS_VERIFIED
    from database import SessionLocal
    from models import Contract

    openai_calls = {"n": 0}
    real_get = openai_runtime.get_openai_client

    def guarded_get(*a, **k):
        openai_calls["n"] += 1
        if openai_calls["n"] > 1:
            raise SecondOpenAICallAttempted(f"SECOND_OPENAI_CALL_BLOCKED n={openai_calls['n']}")
        return real_get(*a, **k)

    openai_runtime.get_openai_client = guarded_get  # type: ignore[assignment]

    session = SessionLocal()
    try:
        row = session.query(Contract).filter_by(id=68).first()
        if not row:
            print(json.dumps({"error": "opportunity_68_not_found"}))
            return 2
    finally:
        session.close()

    s0 = stage0_evaluate(row)
    r1 = resolve_current_stage1_result(row, stage0=s0)
    gate = can_run_stage2(row, stage0=s0, stage1_resolution=r1)
    built = build_stage2_input(row, stage0=s0, stage1=r1.get("result"))
    content = [{"type": "input_text", "text": built["text"]}]
    src = content_source_hash(content, STAGE2_INSTRUCTIONS)
    fp = analysis_fingerprint(
        notice_id=row.notice_id,
        task=STAGE2_TASK,
        model_tier=ModelTier.CHEAP.value,
        source_hash=src,
        schema_version=stage2_schema_fingerprint_component(),
        funnel_stage=FunnelStage.STAGE_2.value,
    )
    cached_pre = get_cached_analysis(fp)
    cache_pre = "HIT" if cached_pre and cached_pre.get("result_text") is not None else "MISS"

    stops = []
    if s0.get("decision") != "ADVANCE":
        stops.append("stage0_not_advance")
    if r1.get("status") != "HIT" or r1.get("fingerprint") != EXPECTED_S1:
        stops.append(
            {
                "stage1_fingerprint_mismatch_or_miss": True,
                "expected": EXPECTED_S1,
                "actual": r1.get("fingerprint"),
                "status": r1.get("status"),
            }
        )
    if not (r1.get("result") or {}).get("advance"):
        stops.append("stage1_not_advance")
    if not gate.get("allowed"):
        stops.append({"gate_blocked": gate.get("reason")})
    if STAGE2_SCHEMA_VERSION != "stage2-evidence-v2":
        stops.append("schema_not_v2")
    if fp != EXPECTED_S2:
        stops.append({"stage2_fingerprint_mismatch": True, "expected": EXPECTED_S2, "actual": fp})
    if cache_pre != "MISS":
        stops.append("expected_cache_miss")
    if not built["oversize"].get("ok"):
        stops.append("oversize")

    pre = {
        "stage0": s0.get("decision"),
        "stage1_current": r1.get("status"),
        "stage1_fingerprint": r1.get("fingerprint"),
        "stage1_advance": (r1.get("result") or {}).get("advance"),
        "stage2_eligibility": gate.get("allowed"),
        "fingerprint": fp,
        "cache": cache_pre,
        "schema": STAGE2_SCHEMA_VERSION,
        "model": model_for_tier(ModelTier.CHEAP),
        "tier": ModelTier.CHEAP.value,
        "web": False,
        "corpus_chars": built["corpus"]["char_count"],
        "input_limit": built["oversize"].get("limit"),
        "output_ceiling": STAGE2_MAX_OUTPUT_TOKENS,
        "estimated_input_tokens": estimate_tokens_from_chars(len(built["text"]) + len(STAGE2_INSTRUCTIONS)),
    }
    print("PRE_CALL")
    print(json.dumps(pre, indent=2))

    if stops:
        print(json.dumps({"STOP": True, "reasons": stops, "TOTAL_LIVE_OPENAI_REQUESTS": 0}, indent=2, default=str))
        return 4

    # ONE production Stage 2 call
    result = run_stage2_evidence(
        row,
        stage0=s0,
        stage1_resolution=r1,
        automatic=False,
    )

    if openai_calls["n"] != 1:
        print(json.dumps({"STOP": "unexpected_openai_count", "n": openai_calls["n"]}, indent=2))
        return 8

    meta = openai_runtime.get_last_response_meta()
    usage = {
        "request_id": meta.get("request_id"),
        "model": meta.get("model") or model_for_tier(ModelTier.CHEAP),
        "input_tokens": meta.get("input_tokens"),
        "cached_input_tokens": meta.get("cached_input_tokens") or 0,
        "output_tokens": meta.get("output_tokens"),
        "reasoning_tokens": meta.get("reasoning_tokens"),
        "total_tokens": meta.get("total_tokens"),
        "cache_hit": meta.get("cache_hit"),
        "openai_called": meta.get("openai_called"),
        "fingerprint": meta.get("fingerprint"),
    }
    costs = breakdown_call_cost_usd(
        model=str(usage["model"]),
        input_tokens=int(usage["input_tokens"] or 0),
        cached_input_tokens=int(usage["cached_input_tokens"] or 0),
        output_tokens=int(usage["output_tokens"] or 0),
        web_search=False,
    )

    # Re-validate raw AI facts against corpus for reporting
    cached = get_cached_analysis(fp)
    raw_text = (cached or {}).get("result_text") or ""
    try:
        raw = openai_runtime.extract_json_object(raw_text) if raw_text else {"facts": [], "fatal_candidates": []}
    except Exception:
        raw = {"facts": [], "fatal_candidates": [], "_parse_error": True}

    corpus = built["corpus"].get("text") or ""
    manifest_ids = {str(d.get("document_id")) for d in built["manifest"] if d.get("document_id")}
    candidates = list(raw.get("facts") or []) if isinstance(raw, dict) else []
    verified_list: list[dict[str, Any]] = []
    assessments: list[dict[str, Any]] = []
    unknown_unusable: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for item in candidates:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "")
        path = FACT_CODE_TO_PATH.get(code)
        field = f"{path[0]}.{path[1]}" if path else code
        env = validate_and_envelope_fact(
            {**item, "code": code}, field=field, corpus=corpus, manifest_ids=manifest_ids
        )
        st = str(env.get("status") or "")
        row_out = {
            "code": code,
            "ai_value": item.get("value"),
            "status": st,
            "document_id": env.get("document_id") or item.get("document_id"),
            "evidence_text": env.get("evidence_text") or item.get("evidence_text"),
            "notes": env.get("notes"),
        }
        if st == STATUS_VERIFIED:
            verified_list.append(row_out)
        elif st == STATUS_ASSESSMENT:
            assessments.append(row_out)
            failures.append(
                {
                    "fact_code": code,
                    "ai_value": item.get("value"),
                    "reason": env.get("notes")
                    or (
                        "evidence_text not found in corpus"
                        if item.get("evidence_text")
                        and not evidence_text_in_source(item.get("evidence_text"), corpus)
                        else "downgraded_to_assessment"
                    ),
                }
            )
        else:
            unknown_unusable.append(row_out)
            failures.append(
                {
                    "fact_code": code,
                    "ai_value": item.get("value"),
                    "reason": env.get("notes") or "unknown_or_rejected",
                }
            )

    # Also include seeded structured VERIFIED facts in verified display (local, not AI)
    extracted = result.get("extracted_facts") or []
    seeded_verified = [
        e
        for e in extracted
        if str(e.get("status")) == STATUS_VERIFIED
        and e.get("code") not in {v["code"] for v in verified_list}
    ]

    fatal_ai = list(raw.get("fatal_candidates") or []) if isinstance(raw, dict) else []
    fatal_validated = result.get("fatal_facts") or []
    fatal_rejected = []
    for item in fatal_ai:
        if not isinstance(item, dict):
            continue
        ok = any(
            f.get("code") == item.get("code") and f.get("evidence_text") == item.get("evidence_text")
            for f in fatal_validated
        )
        if not ok:
            why = []
            if item.get("document_id") and str(item.get("document_id")) not in manifest_ids:
                why.append("document_id not in manifest")
            if not evidence_text_in_source(item.get("evidence_text"), corpus):
                why.append("evidence_text not in corpus")
            fatal_rejected.append({"candidate": item, "reason": "; ".join(why) or "failed validation"})

    tree = result.get("facts") or {}
    important = {k: _fact_status(tree, sec, key) for k, (sec, key) in IMPORTANT.items()}

    econ = result.get("economic_requirements") or {}
    costs_map = econ.get("costs") or {}

    # Integrity review
    unsupported_numbers = []
    unsupported_reqs = []
    unsupported_verified = []
    for f in assessments + unknown_unusable:
        if isinstance(f.get("ai_value"), (int, float)) and f.get("ai_value") is not None:
            unsupported_numbers.append(f)
    for f in assessments:
        unsupported_reqs.append(f)
    # VERIFIED without evidence_text from AI path is only OK for structured seed
    for v in verified_list:
        if not v.get("evidence_text"):
            unsupported_verified.append({**v, "issue": "AI verified without evidence_text"})

    unknown_became_zero = False
    qty = important["QUANTITY"]
    if qty.get("status") == "UNKNOWN" and qty.get("value") in (0, 0.0, False):
        unknown_became_zero = True

    hist_as_revenue = bool((econ.get("revenue_evidence") or {}).get("is_current_revenue"))
    actual_profit = econ.get("actual_profit")
    profit_improper = actual_profit is not None and any(
        str((costs_map.get(c) or {}).get("status")) == "REQUIRED_UNKNOWN" for c in costs_map
    )
    assessment_reject = result.get("advance") is False and not fatal_validated

    # Cache verify
    read_back = get_cached_analysis(fp)
    write_ok = bool(read_back and read_back.get("result_text"))
    fp_match = bool(read_back and (read_back.get("fingerprint") == fp or write_ok))

    # Simulate next identical run would HIT — do not call OpenAI
    def block_all(*a, **k):
        openai_calls["n"] += 1
        raise SecondOpenAICallAttempted("BLOCKED_AFTER_STAGE2")

    openai_runtime.get_openai_client = block_all  # type: ignore[assignment]
    next_lookup = get_cached_analysis(fp)
    next_would_hit = bool(next_lookup and next_lookup.get("result_text") is not None)

    if profit_improper:
        next_state = "STAGE2_NEEDS_FIXES"
        defect = "actual_profit_with_required_unknown_costs"
    elif assessment_reject:
        next_state = "STAGE2_NEEDS_FIXES"
        defect = "rejected_on_assessment"
    elif not write_ok or not next_would_hit:
        next_state = "STAGE2_NEEDS_FIXES"
        defect = "cache_persistence_failed"
    elif openai_calls["n"] != 1:
        next_state = "STAGE2_NEEDS_FIXES"
        defect = "openai_call_count"
    else:
        next_state = "STAGE2_PROVEN_READY_FOR_STAGE3_DESIGN"
        defect = None

    report = {
        "LIVE_STAGE_2_OPPORTUNITY_68": True,
        "PRE_CALL": pre,
        "OPENAI": usage,
        "COST": {
            "input": costs["input_cost"],
            "cached": costs["cached_input_cost"],
            "output": costs["output_cost"],
            "TOTAL": costs["total_cost"],
            "rates": rates_for_model(str(usage["model"])),
            "note": "reasoning tokens NOT double-charged",
        },
        "EXTRACTION_SUMMARY": {
            "candidates": len(candidates),
            "verified_from_ai": len(verified_list),
            "assessments": len(assessments),
            "unknown_unusable": len(unknown_unusable),
            "validation_failures": failures,
            "seeded_structured_verified_count": len(seeded_verified),
        },
        "VERIFIED_FACTS_AI": [
            {
                "FACT_CODE": v["code"],
                "VALUE": v["ai_value"],
                "STATUS": v["status"],
                "DOCUMENT": v.get("document_id"),
                "EVIDENCE_TEXT": v.get("evidence_text"),
            }
            for v in verified_list
        ],
        "VERIFIED_FACTS_STRUCTURED_SEED": [
            {
                "FACT_CODE": v.get("code"),
                "VALUE": v.get("value"),
                "STATUS": v.get("status"),
                "DOCUMENT": v.get("document_id"),
                "EVIDENCE_TEXT": v.get("evidence_text"),
            }
            for v in seeded_verified
        ],
        "IMPORTANT_FACT_STATUS": important,
        "ECONOMIC_REQUIREMENTS": {
            "supplier": (costs_map.get("supplier") or {}).get("status"),
            "freight": (costs_map.get("freight") or {}).get("status"),
            "installation": (costs_map.get("installation") or {}).get("status"),
            "subcontract": (costs_map.get("subcontract") or {}).get("status"),
            "financing": (costs_map.get("financing") or {}).get("status"),
            "other_keys": {k: (costs_map.get(k) or {}).get("status") for k in costs_map if k not in {
                "supplier", "freight", "installation", "subcontract", "financing"
            }},
            "actual_profit": actual_profit,
            "economic_status": econ.get("actual_profit_status"),
            "revenue_is_current": hist_as_revenue,
        },
        "RESEARCH_NEEDS": result.get("research_needs") or [],
        "FATAL_SAFETY": {
            "ai_fatal_candidates": fatal_ai,
            "validated_fatal_facts": fatal_validated,
            "unvalidated_rejected": fatal_rejected,
            "stage2_reject": result.get("advance") is False,
        },
        "DECISION": {
            "advance": result.get("advance"),
            "reason_code": result.get("reason_code"),
        },
        "CACHE": {
            "write": write_ok,
            "commit_readback": bool(read_back),
            "fingerprint_match": (read_back or {}).get("fingerprint") == fp if read_back else False,
            "cached_at": (read_back or {}).get("cached_at"),
            "next_identical_run": "CACHE_HIT" if next_would_hit else "MISS",
            "incremental_cost_next_identical_run": 0.0 if next_would_hit else None,
            "runtime_cache_hit": result.get("cache_hit"),
            "runtime_incremental_cost": result.get("incremental_cost_usd"),
        },
        "INTEGRITY_REVIEW": {
            "unsupported_numbers_invented_candidates": unsupported_numbers,
            "unsupported_requirements_assessment_count": len(unsupported_reqs),
            "unsupported_statement_verified": unsupported_verified,
            "unknown_became_zero_false": unknown_became_zero,
            "historical_value_as_current_revenue": hist_as_revenue,
            "actual_profit_improperly_calculated": profit_improper,
            "assessment_caused_reject": assessment_reject,
            "evidence_validator_ran": True,
            "defect": defect,
        },
        "NEXT_STATE": next_state,
        "TOTAL_LIVE_OPENAI_REQUESTS": openai_calls["n"],
    }
    print("REPORT")
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
