"""EXACTLY ONE live Stage 1 refresh for Opp 68 — then Stage 2 zero-cost gate only."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

EXPECTED_FP = "526f0a0fd7a304b3308cd8c48402c3e35a3638667b8179533906cc108bba6eb9"


class SecondOpenAICallAttempted(RuntimeError):
    pass


def main() -> int:
    import openai_runtime
    from ai_analysis_cache import get_cached_analysis
    from ai_funnel import stage0_evaluate
    from ai_model_router import FunnelStage, ModelTier, model_for_tier
    from ai_pricing import breakdown_call_cost_usd, estimate_tokens_from_chars, rates_for_model
    from ai_stage1 import (
        STAGE1_INSTRUCTION_VERSION,
        STAGE1_SCHEMA_VERSION,
        compute_stage1_fingerprint,
        content_has_pdf_parts,
        resolve_current_stage1_result,
        run_stage1_triage,
    )
    from ai_stage2 import (
        STAGE2_INSTRUCTIONS,
        STAGE2_MAX_OUTPUT_TOKENS,
        STAGE2_SCHEMA_VERSION,
        STAGE2_TASK,
        build_stage2_input,
        can_run_stage2,
        stage2_schema_fingerprint_component,
    )
    from ai_analysis_cache import analysis_fingerprint, content_source_hash
    from database import SessionLocal
    from models import Contract

    openai_calls = {"n": 0}
    real_get_client = openai_runtime.get_openai_client

    def guarded_get_client(*a, **k):
        openai_calls["n"] += 1
        if openai_calls["n"] > 1:
            raise SecondOpenAICallAttempted(
                f"SECOND_OPENAI_CALL_BLOCKED count={openai_calls['n']}"
            )
        return real_get_client(*a, **k)

    openai_runtime.get_openai_client = guarded_get_client  # type: ignore[assignment]

    session = SessionLocal()
    try:
        row = session.query(Contract).filter_by(id=68).first()
        if not row:
            print(json.dumps({"error": "opportunity_68_not_found"}))
            return 2
    finally:
        session.close()

    s0 = stage0_evaluate(row)
    fp_info = compute_stage1_fingerprint(row, stage0=s0)
    pre = resolve_current_stage1_result(row, stage0=s0)

    if fp_info["fingerprint"] != EXPECTED_FP:
        print(
            json.dumps(
                {
                    "STOP": "fingerprint_mismatch",
                    "expected": EXPECTED_FP,
                    "actual": fp_info["fingerprint"],
                    "components": {k: v for k, v in fp_info.items() if k != "input_text"},
                    "TOTAL_LIVE_OPENAI_REQUESTS": 0,
                },
                indent=2,
                default=str,
            )
        )
        return 4

    if s0.get("decision") != "ADVANCE" or not s0.get("advance"):
        print(json.dumps({"STOP": "stage0_not_advance", "stage0": s0, "TOTAL_LIVE_OPENAI_REQUESTS": 0}, indent=2))
        return 5

    if pre.get("status") == "HIT":
        print(json.dumps({"STOP": "unexpected_cache_hit_before_call", "TOTAL_LIVE_OPENAI_REQUESTS": 0}, indent=2))
        return 6

    content = [{"type": "input_text", "text": fp_info["input_text"]}]
    if content_has_pdf_parts(content):
        print(json.dumps({"STOP": "pdf_detected", "TOTAL_LIVE_OPENAI_REQUESTS": 0}, indent=2))
        return 7

    pre_call = {
        "stage0": s0.get("decision"),
        "fingerprint": fp_info["fingerprint"],
        "cache": pre.get("status"),
        "model": fp_info["model"],
        "tier": fp_info["tier"],
        "web": False,
        "schema": STAGE1_SCHEMA_VERSION,
        "instruction_version": STAGE1_INSTRUCTION_VERSION,
        "input_chars": fp_info["input_chars"],
        "estimated_input_tokens": estimate_tokens_from_chars(fp_info["input_chars"]),
        "pdf_files": 0,
    }
    print("PRE_CALL")
    print(json.dumps(pre_call, indent=2))

    # ONE production Stage 1 call
    result = run_stage1_triage(row, stage0=s0, automatic=False)

    if openai_calls["n"] != 1:
        print(
            json.dumps(
                {
                    "STOP": "unexpected_openai_call_count",
                    "count": openai_calls["n"],
                    "TOTAL_LIVE_OPENAI_REQUESTS": openai_calls["n"],
                },
                indent=2,
            )
        )
        return 8

    meta = openai_runtime.get_last_response_meta()
    usage = {
        "request_id": meta.get("request_id") or meta.get("id") or meta.get("response_id"),
        "model": meta.get("model") or result.get("model") or fp_info["model"],
        "input_tokens": meta.get("input_tokens"),
        "cached_input_tokens": meta.get("cached_input_tokens") or 0,
        "output_tokens": meta.get("output_tokens"),
        "reasoning_tokens": meta.get("reasoning_tokens"),
        "total_tokens": meta.get("total_tokens"),
        "cache_hit": meta.get("cache_hit"),
        "openai_called": meta.get("openai_called"),
        "raw_meta_keys": sorted(meta.keys()),
    }

    # Prefer actual usage tokens for cost; fall back to meta estimated only if missing
    in_tok = int(usage["input_tokens"] or 0)
    cached_tok = int(usage["cached_input_tokens"] or 0)
    out_tok = int(usage["output_tokens"] or 0)
    # reasoning not double-charged
    costs = breakdown_call_cost_usd(
        model=str(usage["model"] or fp_info["model"]),
        input_tokens=in_tok,
        cached_input_tokens=cached_tok,
        output_tokens=out_tok,
        web_search=False,
    )

    # Cache verify under CURRENT fingerprint
    fp = fp_info["fingerprint"]
    read_back = get_cached_analysis(fp)
    write_ok = bool(read_back and read_back.get("result_text"))
    # run_stage1_triage uses create_response which should have written cache
    match = bool(read_back and read_back.get("fingerprint") == fp) or (
        write_ok and True
    )
    # Independent resolve
    resolved = resolve_current_stage1_result(row, stage0=s0)

    # Block any further OpenAI for Stage 2 preflight
    def block_all(*a, **k):
        openai_calls["n"] += 1
        raise SecondOpenAICallAttempted("STAGE2_OPENAI_BLOCKED")

    openai_runtime.get_openai_client = block_all  # type: ignore[assignment]

    gate = can_run_stage2(row, stage0=s0, stage1_resolution=resolved)
    s1_result = resolved.get("result") if resolved.get("status") == "HIT" else result
    built2 = build_stage2_input(row, stage0=s0, stage1=s1_result)
    content2 = [{"type": "input_text", "text": built2["text"]}]
    src2 = content_source_hash(content2, STAGE2_INSTRUCTIONS)
    fp2 = analysis_fingerprint(
        notice_id=row.notice_id,
        task=STAGE2_TASK,
        model_tier=ModelTier.CHEAP.value,
        source_hash=src2,
        schema_version=stage2_schema_fingerprint_component(),
        funnel_stage=FunnelStage.STAGE_2.value,
    )
    cached2 = get_cached_analysis(fp2)
    stage2_cache = "HIT" if cached2 and cached2.get("result_text") is not None else "MISS"
    input_chars2 = len(built2["text"]) + len(STAGE2_INSTRUCTIONS)
    est2 = estimate_tokens_from_chars(input_chars2)

    if resolved.get("status") == "HIT" and gate.get("allowed") and stage2_cache == "MISS":
        next_state = "READY_FOR_ONE_LIVE_STAGE2_CALL"
    elif resolved.get("status") != "HIT":
        next_state = "STAGE1_REFRESH_FAILED"
    else:
        next_state = "NOT_READY_FOR_STAGE2"

    report = {
        "LIVE_STAGE_1_REFRESH_OPPORTUNITY_68": True,
        "PRE_CALL": pre_call,
        "OPENAI": usage,
        "COST": {
            "input": costs["input_cost"],
            "cached": costs["cached_input_cost"],
            "output": costs["output_cost"],
            "TOTAL": costs["total_cost"],
            "rates": rates_for_model(str(usage["model"] or fp_info["model"])),
            "note": "reasoning tokens NOT double-charged",
        },
        "RESULT": {
            "category": result.get("category"),
            "buying": result.get("buying"),
            "quantity": result.get("quantity"),
            "exact_model_identified": result.get("exact_model_identified"),
            "install_required": result.get("install_required"),
            "bond_likely": result.get("bond_likely"),
            "license_likely": result.get("license_likely"),
            "channel_restriction_likely": result.get("channel_restriction_likely"),
            "reseller_fit": result.get("reseller_fit"),
            "execution_complexity": result.get("execution_complexity"),
            "fatal_issue": result.get("fatal_issue"),
            "advance": result.get("advance"),
            "reason_code": result.get("reason_code"),
            "legacy_score": result.get("score"),
            "pursue": result.get("pursue"),
            "assessment_note": "AI assessment fields unless independently VERIFIED from source evidence",
            "cache_hit_from_runtime": result.get("cache_hit"),
            "incremental_cost_usd_runtime": result.get("incremental_cost_usd"),
        },
        "CACHE": {
            "write_present_on_readback": write_ok,
            "read_back": bool(read_back),
            "read_back_fingerprint": (read_back or {}).get("fingerprint"),
            "fingerprint_match": (read_back or {}).get("fingerprint") == fp if read_back else False,
            "cached_at": (read_back or {}).get("cached_at"),
            "resolve_current_stage1_result": {
                "status": resolved.get("status"),
                "current": resolved.get("current"),
                "fingerprint": resolved.get("fingerprint"),
            },
        },
        "STAGE_2_ZERO_COST_PREFLIGHT": {
            "eligibility_allowed": gate.get("allowed"),
            "eligibility_reason": gate.get("reason"),
            "corpus_chars": built2["corpus"]["char_count"],
            "estimated_input_tokens": est2,
            "fingerprint": fp2,
            "cache": stage2_cache,
            "output_ceiling": STAGE2_MAX_OUTPUT_TOKENS,
            "schema": STAGE2_SCHEMA_VERSION,
            "within_input_limit": built2["oversize"].get("ok"),
            "stage2_openai_executed": False,
        },
        "NEXT_STATE": next_state,
        "TOTAL_LIVE_OPENAI_REQUESTS": openai_calls["n"],
    }
    print("REPORT")
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
