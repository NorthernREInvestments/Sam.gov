"""Prove production Stage 1 path hits existing Opportunity 68 cache with OpenAI blocked.

ZERO live OpenAI requests. Also runs local cache-invalidation checks (no OpenAI generation).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

EXPECTED_FP = "b3c7aeca16fd5ac28b3d434e2f7dc1465c8ffa297548da5ceee80cc60b23816d"


class OpenAICallAttemptedDuringCacheTest(RuntimeError):
    pass


def main() -> int:
    import openai_runtime
    from ai_analysis_cache import (
        PROMPT_SCHEMA_VERSION,
        analysis_fingerprint,
        content_source_hash,
        get_cached_analysis,
    )
    from ai_funnel import should_advance_to_next_stage, stage0_evaluate
    from ai_model_router import ModelTier
    from ai_stage1 import (
        STAGE1_INSTRUCTIONS,
        STAGE1_SCHEMA_VERSION,
        build_stage1_input,
        run_stage1_triage,
        stage1_schema_fingerprint_component,
    )
    from database import SessionLocal
    from models import Contract
    from openai_client import screen_contract_text

    openai_entered = {"n": 0}

    def block_openai(*args, **kwargs):
        openai_entered["n"] += 1
        raise OpenAICallAttemptedDuringCacheTest("OPENAI_CALL_ATTEMPTED_DURING_CACHE_TEST")

    # Block every OpenAI entry point used by production runtime
    openai_runtime.get_openai_client = block_openai  # type: ignore[assignment]

    session = SessionLocal()
    try:
        row = session.query(Contract).filter_by(id=68).first()
        if not row:
            print(json.dumps({"error": "opportunity_68_not_found"}))
            return 2

        s0 = stage0_evaluate(row)
        built = build_stage1_input(row, stage0=s0)
        content = [{"type": "input_text", "text": built["text"]}]
        src_hash = content_source_hash(content, STAGE1_INSTRUCTIONS)
        schema_v = stage1_schema_fingerprint_component()
        fp = analysis_fingerprint(
            notice_id=row.notice_id,
            task="screen_contract_text",
            model_tier=ModelTier.CHEAP.value,
            source_hash=src_hash,
            schema_version=schema_v,
            funnel_stage=1,
        )

        pre = get_cached_analysis(fp)
        cache_lookup = "HIT" if pre and pre.get("result_text") else "MISS"

        # Production path: screen_contract_text → stage0 + run_stage1_triage → create_response (cache first)
        openai_before = openai_entered["n"]
        result = screen_contract_text(row)
        openai_after_funnel = openai_entered["n"]

        funnel = should_advance_to_next_stage(row, 1, evidence=result)

        # --- Cache invalidation (local/mocks only; never generate replacements) ---
        invalidation: dict[str, Any] = {}

        # Test A: same data/schema/model → same fingerprint → HIT → 0 OpenAI
        fp_a = analysis_fingerprint(
            notice_id=row.notice_id,
            task="screen_contract_text",
            model_tier=ModelTier.CHEAP.value,
            source_hash=src_hash,
            schema_version=schema_v,
            funnel_stage=1,
        )
        hit_a = get_cached_analysis(fp_a)
        invalidation["same_data_schema_model"] = {
            "fingerprint_same": fp_a == fp,
            "cache_lookup": "HIT" if hit_a and hit_a.get("result_text") else "MISS",
            "openai_calls": 0,
        }

        # Test B: changed source data → different FP → MISS → block OpenAI
        alt = SimpleNamespace(
            notice_id=row.notice_id,
            title=row.title,
            description="CACHE_INVALIDATION_TEST_CHANGED_SOURCE_DATA",
            due_date=getattr(row, "due_date", None),
            status=getattr(row, "status", None),
            set_aside=getattr(row, "set_aside", None),
            naics_code=getattr(row, "naics_code", None),
            agency=getattr(row, "agency", None),
            location=getattr(row, "location", None),
            analysis=getattr(row, "analysis", None) or {},
            sam_raw=getattr(row, "sam_raw", None) or {},
            estimated_value=getattr(row, "estimated_value", None),
            link=getattr(row, "link", None),
            attachment_text=None,
        )
        built_b = build_stage1_input(alt, stage0=s0)
        content_b = [{"type": "input_text", "text": built_b["text"]}]
        src_b = content_source_hash(content_b, STAGE1_INSTRUCTIONS)
        fp_b = analysis_fingerprint(
            notice_id=row.notice_id,
            task="screen_contract_text",
            model_tier=ModelTier.CHEAP.value,
            source_hash=src_b,
            schema_version=schema_v,
            funnel_stage=1,
        )
        miss_b = get_cached_analysis(fp_b)
        openai_before_b = openai_entered["n"]
        blocked_b = False
        try:
            run_stage1_triage(alt, stage0=s0, automatic=False)
        except OpenAICallAttemptedDuringCacheTest:
            blocked_b = True
        invalidation["changed_source_data"] = {
            "fingerprint_different": fp_b != fp,
            "cache_lookup": "HIT" if miss_b and miss_b.get("result_text") else "MISS",
            "openai_blocked_after_miss": blocked_b,
            "openai_calls": openai_entered["n"] - openai_before_b,
        }

        # Test C: changed schema version → different FP → MISS → block OpenAI
        fp_c = analysis_fingerprint(
            notice_id=row.notice_id,
            task="screen_contract_text",
            model_tier=ModelTier.CHEAP.value,
            source_hash=src_hash,
            schema_version=f"{PROMPT_SCHEMA_VERSION}:stage1-compact-v999",
            funnel_stage=1,
        )
        miss_c = get_cached_analysis(fp_c)
        openai_before_c = openai_entered["n"]
        blocked_c = False
        try:
            openai_runtime.create_response(
                task="screen_contract_text",
                instructions=STAGE1_INSTRUCTIONS,
                content=content,
                max_output_tokens=400,
                web_search=False,
                notice_id=row.notice_id,
                funnel_stage=1,
                use_cache=True,
                schema_version=f"{PROMPT_SCHEMA_VERSION}:stage1-compact-v999",
            )
        except OpenAICallAttemptedDuringCacheTest:
            blocked_c = True
        invalidation["changed_schema"] = {
            "fingerprint_different": fp_c != fp,
            "cache_lookup": "HIT" if miss_c and miss_c.get("result_text") else "MISS",
            "openai_blocked_after_miss": blocked_c,
            "openai_calls": openai_entered["n"] - openai_before_c,
        }

        # Test D: changed model/tier → different FP → MISS → block OpenAI
        fp_d = analysis_fingerprint(
            notice_id=row.notice_id,
            task="screen_contract_text",
            model_tier=ModelTier.STANDARD.value,
            source_hash=src_hash,
            schema_version=schema_v,
            funnel_stage=1,
        )
        miss_d = get_cached_analysis(fp_d)
        openai_before_d = openai_entered["n"]
        blocked_d = False
        try:
            openai_runtime.create_response(
                task="screen_contract_text",
                instructions=STAGE1_INSTRUCTIONS,
                content=content,
                max_output_tokens=400,
                web_search=False,
                notice_id=row.notice_id,
                funnel_stage=1,
                model_tier=ModelTier.STANDARD,
                use_cache=True,
                schema_version=schema_v,
            )
        except OpenAICallAttemptedDuringCacheTest:
            blocked_d = True
        invalidation["changed_model_tier"] = {
            "fingerprint_different": fp_d != fp,
            "cache_lookup": "HIT" if miss_d and miss_d.get("result_text") else "MISS",
            "openai_blocked_after_miss": blocked_d,
            "openai_calls": openai_entered["n"] - openai_before_d,
        }

        report = {
            "stage0_decision": s0.get("decision"),
            "generated_fingerprint": fp,
            "expected_fingerprint": EXPECTED_FP,
            "fingerprint_match": fp == EXPECTED_FP,
            "cache_lookup": cache_lookup,
            "openai_request_function_entered": openai_after_funnel > openai_before,
            "openai_enter_count_total": openai_entered["n"],
            "openai_enter_count_during_normal_funnel": openai_after_funnel - openai_before,
            "cached_result_returned": bool(result),
            "cache_hit_flag": result.get("cache_hit"),
            "category": result.get("category"),
            "reseller_fit": result.get("reseller_fit"),
            "advance": result.get("advance"),
            "reason_code": result.get("reason_code"),
            "legacy_score": result.get("score"),
            "legacy_pursue": result.get("pursue"),
            "recommended_next_stage": funnel.get("next_stage"),
            "incremental_api_cost_usd": float(result.get("incremental_cost_usd") or 0.0),
            "new_openai_input_tokens": 0,
            "new_openai_output_tokens": 0,
            "new_openai_requests": openai_after_funnel - openai_before,
            "schema_version": STAGE1_SCHEMA_VERSION,
            "invalidation": invalidation,
        }
        print("NORMAL_FUNNEL_CACHE_TEST")
        print(json.dumps(report, indent=2, default=str))

        ok = (
            s0.get("decision") == "ADVANCE"
            and fp == EXPECTED_FP
            and cache_lookup == "HIT"
            and (openai_after_funnel - openai_before) == 0
            and result.get("category") == "LABOR_HEAVY"
            and result.get("reseller_fit") == "NONE"
            and result.get("advance") is True
            and result.get("reason_code") == "NEEDS_STAGE2_DOCUMENT_REVIEW"
            and result.get("score") == 6
            and result.get("pursue") is True
            and funnel.get("next_stage") == 2
            and float(result.get("incremental_cost_usd") or 0.0) == 0.0
            and invalidation["same_data_schema_model"]["fingerprint_same"]
            and invalidation["same_data_schema_model"]["cache_lookup"] == "HIT"
            and invalidation["changed_source_data"]["fingerprint_different"]
            and invalidation["changed_source_data"]["cache_lookup"] == "MISS"
            and invalidation["changed_source_data"]["openai_blocked_after_miss"]
            and invalidation["changed_schema"]["fingerprint_different"]
            and invalidation["changed_schema"]["cache_lookup"] == "MISS"
            and invalidation["changed_schema"]["openai_blocked_after_miss"]
            and invalidation["changed_model_tier"]["fingerprint_different"]
            and invalidation["changed_model_tier"]["cache_lookup"] == "MISS"
            and invalidation["changed_model_tier"]["openai_blocked_after_miss"]
        )
        print("RESULT", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
