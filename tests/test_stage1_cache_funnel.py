"""Stage 1 cache short-circuit + invalidation — zero live OpenAI calls."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import ai_analysis_cache as cache_mod
import ai_stage1
import openai_runtime
from ai_analysis_cache import (
    PROMPT_SCHEMA_VERSION,
    analysis_fingerprint,
    content_source_hash,
)
from ai_funnel import should_advance_to_next_stage
from ai_model_router import ModelTier
from ai_stage1 import (
    STAGE1_INSTRUCTIONS,
    STAGE1_SCHEMA_VERSION,
    build_stage1_input,
    normalize_stage1_result,
    run_stage1_triage,
    stage1_schema_fingerprint_component,
)


class OpenAICallAttemptedDuringCacheTest(RuntimeError):
    pass


CACHED_JSON = (
    '{"category":"LABOR_HEAVY","buying":"Groundskeeping services at NUWC DETPAC Building 977",'
    '"quantity":null,"exact_model_identified":false,"install_required":false,"bond_likely":null,'
    '"license_likely":null,"channel_restriction_likely":true,"reseller_fit":"NONE",'
    '"execution_complexity":"HIGH","fatal_issue":null,"advance":true,'
    '"reason_code":"NEEDS_STAGE2_DOCUMENT_REVIEW"}'
)


def _opp(**kwargs):
    base = dict(
        notice_id="dc6ac7432d224e06a44b6340db61442f",
        title="Groundskeeping Services at Building 977",
        description="Groundskeeping services",
        due_date=None,
        status="new",
        set_aside="Total Small Business",
        naics_code="561730",
        agency="NUWC",
        location="Kapolei, HI",
        analysis={},
        sam_raw={},
        estimated_value="27081.00",
        link="https://sam.gov/opp/abc",
        attachment_text=None,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _fp_for(opportunity, *, schema_version=None, model_tier=ModelTier.CHEAP.value, stage0=None):
    s0 = stage0 or {"decision": "ADVANCE", "classification": "SUBCONTRACTABLE_SERVICE", "flags": []}
    built = build_stage1_input(opportunity, stage0=s0)
    content = [{"type": "input_text", "text": built["text"]}]
    src = content_source_hash(content, STAGE1_INSTRUCTIONS)
    sv = schema_version or stage1_schema_fingerprint_component()
    return analysis_fingerprint(
        notice_id=opportunity.notice_id,
        task="screen_contract_text",
        model_tier=model_tier,
        source_hash=src,
        schema_version=sv,
        funnel_stage=1,
    )


@pytest.fixture
def block_openai(monkeypatch):
    entered = {"n": 0}

    def boom(*a, **k):
        entered["n"] += 1
        raise OpenAICallAttemptedDuringCacheTest("OPENAI_CALL_ATTEMPTED_DURING_CACHE_TEST")

    monkeypatch.setattr(openai_runtime, "get_openai_client", boom)
    return entered


def test_cache_hit_prevents_openai_and_returns_structured_result(monkeypatch, block_openai):
    store = {}

    def get_cached(fp):
        return store.get(fp)

    def put_cached(fp, result_text="", meta=None):
        store[fp] = {"result_text": result_text, "fingerprint": fp}
        return True

    opp = _opp()
    fp = _fp_for(opp)
    store[fp] = {"result_text": CACHED_JSON, "fingerprint": fp}

    monkeypatch.setattr(openai_runtime, "get_cached_analysis", get_cached)
    monkeypatch.setattr(openai_runtime, "put_cached_analysis", put_cached)
    monkeypatch.setattr(ai_stage1, "record_stage1_metric", lambda *_: None)

    result = run_stage1_triage(opp, stage0={"decision": "ADVANCE", "classification": "SUBCONTRACTABLE_SERVICE", "flags": []})
    assert block_openai["n"] == 0
    assert result["cache_hit"] is True
    assert result["incremental_cost_usd"] == 0.0
    assert result["category"] == "LABOR_HEAVY"
    assert result["reseller_fit"] == "NONE"
    assert result["advance"] is True
    assert result["reason_code"] == "NEEDS_STAGE2_DOCUMENT_REVIEW"
    assert result["score"] == 6
    assert result["pursue"] is True
    funnel = should_advance_to_next_stage(opp, 1, evidence=result)
    assert funnel["next_stage"] == 2


def test_same_data_same_fingerprint(block_openai):
    opp = _opp()
    a = _fp_for(opp)
    b = _fp_for(opp)
    assert a == b


def test_source_data_change_invalidates_fingerprint(monkeypatch, block_openai):
    opp_a = _opp(description="Groundskeeping services")
    opp_b = _opp(description="CHANGED SOURCE DATA — different solicitation text")
    fa = _fp_for(opp_a)
    fb = _fp_for(opp_b)
    assert fa != fb

    # Cache only A; B must miss and then OpenAI is blocked
    store = {fa: {"result_text": CACHED_JSON, "fingerprint": fa}}
    monkeypatch.setattr(openai_runtime, "get_cached_analysis", lambda fp: store.get(fp))
    monkeypatch.setattr(ai_stage1, "record_stage1_metric", lambda *_: None)

    with pytest.raises(OpenAICallAttemptedDuringCacheTest):
        run_stage1_triage(
            opp_b,
            stage0={"decision": "ADVANCE", "classification": "SUBCONTRACTABLE_SERVICE", "flags": []},
        )
    assert block_openai["n"] == 1


def test_schema_version_change_invalidates_fingerprint(monkeypatch, block_openai):
    opp = _opp()
    fa = _fp_for(opp, schema_version=f"{PROMPT_SCHEMA_VERSION}:{STAGE1_SCHEMA_VERSION}")
    fb = _fp_for(opp, schema_version=f"{PROMPT_SCHEMA_VERSION}:stage1-compact-v999")
    assert fa != fb

    store = {fa: {"result_text": CACHED_JSON, "fingerprint": fa}}
    monkeypatch.setattr(openai_runtime, "get_cached_analysis", lambda fp: store.get(fp))
    monkeypatch.setattr(ai_stage1, "record_stage1_metric", lambda *_: None)

    # Force create_response to use a different schema version via monkeypatch of run path
    # Direct fingerprint miss simulation: call create_response with different schema
    content = [{"type": "input_text", "text": build_stage1_input(opp, stage0={"decision": "ADVANCE", "flags": []})["text"]}]
    with pytest.raises(OpenAICallAttemptedDuringCacheTest):
        openai_runtime.create_response(
            task="screen_contract_text",
            instructions=STAGE1_INSTRUCTIONS,
            content=content,
            max_output_tokens=400,
            web_search=False,
            notice_id=opp.notice_id,
            funnel_stage=1,
            use_cache=True,
            schema_version=f"{PROMPT_SCHEMA_VERSION}:stage1-compact-v999",
        )
    assert block_openai["n"] == 1


def test_model_tier_change_invalidates_fingerprint():
    opp = _opp()
    fa = _fp_for(opp, model_tier=ModelTier.CHEAP.value)
    fb = _fp_for(opp, model_tier=ModelTier.STANDARD.value)
    assert fa != fb


def test_cache_hit_incremental_cost_zero(monkeypatch, block_openai):
    store = {}
    opp = _opp()
    stage0 = {"decision": "ADVANCE", "classification": "SUBCONTRACTABLE_SERVICE", "flags": []}
    fp = _fp_for(opp, stage0=stage0)
    store[fp] = {"result_text": CACHED_JSON, "fingerprint": fp}
    monkeypatch.setattr(openai_runtime, "get_cached_analysis", lambda f: store.get(f))
    monkeypatch.setattr(ai_stage1, "record_stage1_metric", lambda *_: None)
    result = run_stage1_triage(opp, stage0=stage0)
    assert result["cache_hit"] is True
    assert result["incremental_cost_usd"] == 0.0
    assert block_openai["n"] == 0
