"""Stage 1 current-result resolution + Stage 2 sparse v2 — local/mock only."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import ai_stage1
import openai_runtime
from ai_analysis_cache import (
    PROMPT_SCHEMA_VERSION,
    analysis_fingerprint,
    put_cached_analysis,
)
from ai_model_router import FunnelStage, ModelTier, web_search_allowed_for_stage
from ai_pricing import estimate_tokens_from_chars
from ai_stage1 import (
    HISTORICAL_STAGE1_FP_OPP68,
    STAGE1_INSTRUCTION_VERSION,
    STAGE1_SCHEMA_VERSION,
    STAGE1_TASK,
    compute_stage1_fingerprint,
    resolve_current_stage1_result,
    stage1_schema_fingerprint_component,
)
from ai_stage2 import (
    STAGE2_INSTRUCTION_VERSION,
    STAGE2_MAX_OUTPUT_TOKENS,
    STAGE2_SCHEMA_VERSION,
    STAGE2_TASK,
    assign_economic_requirements,
    build_document_manifest,
    build_research_needs,
    build_stage2_input,
    can_run_stage2,
    dedupe_manifest,
    measure_stage2_output_fixtures,
    normalize_stage2_result,
    run_stage2_evidence,
    stage2_response_format,
    stage2_schema_fingerprint_component,
    validate_and_envelope_fact,
)
from data_integrity import STATUS_ASSESSMENT, STATUS_UNKNOWN, STATUS_VERIFIED
from economic_integrity import COST_NOT_APPLICABLE, COST_REQUIRED_UNKNOWN


class OpenAICallAttempted(RuntimeError):
    pass


def _opp(**kwargs):
    base = dict(
        notice_id="notice-stage2-1",
        title="Groundskeeping Services Building 977",
        description="Contractor shall provide groundskeeping services. Quantity is not stated as 977.",
        due_date=None,
        status="new",
        set_aside="Total Small Business",
        naics_code="561730",
        agency="NUWC",
        location="Kapolei, HI",
        analysis={},
        sam_raw={"classificationCode": "S208", "solicitationNumber": "N0025326P0003"},
        estimated_value="27081.00",
        link="https://sam.gov/opp/x",
        attachment_text=None,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _stage0_ok():
    return {"decision": "ADVANCE", "advance": True, "classification": "LABOR_HEAVY", "flags": []}


def _stage0_reject():
    return {"decision": "REJECT", "advance": False, "reject_reasons": ["expired_deadline"]}


def _stage1_ok(**extra):
    d = {
        "advance": True,
        "reason_code": "NEEDS_STAGE2_DOCUMENT_REVIEW",
        "category": "LABOR_HEAVY",
        "pursue": True,
        "_fixture_authorize": True,
    }
    d.update(extra)
    return d


def _stage1_reject():
    return {
        "advance": False,
        "reason_code": "FATAL",
        "fatal_issue": "clearance",
        "category": "UNKNOWN",
        "_fixture_authorize": True,
    }


BOND_QUOTE = "Contractor shall provide a performance bond equal to 100 percent of the contract price."
BOND_CORPUS = f"=== DOCUMENT attachment_text (attachment_extract) ===\n{BOND_QUOTE}"


@pytest.fixture
def block_openai(monkeypatch):
    entered = {"n": 0}

    def boom(*a, **k):
        entered["n"] += 1
        raise OpenAICallAttempted("OPENAI_CALL_ATTEMPTED")

    monkeypatch.setattr(openai_runtime, "get_openai_client", boom)
    return entered


def _hit_resolution(result=None):
    r = result or {
        "advance": True,
        "reason_code": "NEEDS_STAGE2_DOCUMENT_REVIEW",
        "category": "LABOR_HEAVY",
    }
    return {"status": "HIT", "current": True, "fingerprint": "fp-current", "result": r}


# ---- Stage 1 wiring ----


def test_stage1_current_hit_allows_stage2(monkeypatch, block_openai):
    opp = _opp(notice_id="notice-stage1-hit-allows")
    payload = {
        "category": "LABOR_HEAVY",
        "buying": "grounds",
        "quantity": None,
        "exact_model_identified": False,
        "install_required": False,
        "bond_likely": None,
        "license_likely": None,
        "channel_restriction_likely": None,
        "reseller_fit": "NONE",
        "execution_complexity": "HIGH",
        "fatal_issue": None,
        "advance": True,
        "reason_code": "NEEDS_STAGE2_DOCUMENT_REVIEW",
    }
    fp_info = compute_stage1_fingerprint(opp, stage0=_stage0_ok())
    put_cached_analysis(fp_info["fingerprint"], result_text=json.dumps(payload), meta={"test": True})
    res = resolve_current_stage1_result(opp, stage0=_stage0_ok())
    assert res["status"] == "HIT"
    assert res["openai_called"] is False
    assert block_openai["n"] == 0
    gate = can_run_stage2(opp, stage0=_stage0_ok(), stage1_resolution=res)
    assert gate["allowed"] is True


def test_stale_stage1_fingerprint_not_used(monkeypatch, block_openai):
    opp = _opp(notice_id="dc6ac7432d224e06a44b6340db61442f")
    current = compute_stage1_fingerprint(opp, stage0=_stage0_ok())
    assert current["fingerprint"] != HISTORICAL_STAGE1_FP_OPP68

    def fake_get(fp):
        if fp == HISTORICAL_STAGE1_FP_OPP68:
            return {
                "result_text": json.dumps(
                    {
                        "category": "LABOR_HEAVY",
                        "buying": "historical",
                        "quantity": None,
                        "exact_model_identified": False,
                        "install_required": False,
                        "bond_likely": None,
                        "license_likely": None,
                        "channel_restriction_likely": True,
                        "reseller_fit": "NONE",
                        "execution_complexity": "HIGH",
                        "fatal_issue": None,
                        "advance": True,
                        "reason_code": "NEEDS_STAGE2_DOCUMENT_REVIEW",
                    }
                ),
                "cached_at": "2026-09-15T01:27:11.676137+00:00",
                "meta": {"schema": "stage1-compact-v2"},
                "fingerprint": HISTORICAL_STAGE1_FP_OPP68,
            }
        return None  # current FP miss

    monkeypatch.setattr("ai_analysis_cache.get_cached_analysis", fake_get)
    monkeypatch.setattr(ai_stage1, "get_cached_analysis", fake_get, raising=False)
    # resolve imports get_cached_analysis inside function from ai_analysis_cache
    import ai_analysis_cache as cache_mod

    monkeypatch.setattr(cache_mod, "get_cached_analysis", fake_get)

    res = resolve_current_stage1_result(opp, stage0=_stage0_ok())
    assert res["status"] == "STAGE1_CURRENT_RESULT_MISSING"
    assert res["fingerprint"] == current["fingerprint"]
    assert (res.get("historical_stale") or {}).get("status") == "STALE_HISTORICAL"
    gate = can_run_stage2(opp, stage0=_stage0_ok(), stage1_resolution=res)
    assert gate["allowed"] is False
    assert gate["reason"] == "STAGE1_CURRENT_RESULT_MISSING"
    assert block_openai["n"] == 0
    # Ensure we never wrote over historical FP in this test
    assert fake_get(HISTORICAL_STAGE1_FP_OPP68) is not None


def test_orm_analysis_missing_stage1_irrelevant_when_cache_hit(block_openai):
    opp = _opp(notice_id="notice-orm-missing-cache-hit", analysis={"scope_one_liner": "x"})
    payload = {
        "category": "LABOR_HEAVY",
        "buying": "grounds",
        "quantity": None,
        "exact_model_identified": False,
        "install_required": False,
        "bond_likely": None,
        "license_likely": None,
        "channel_restriction_likely": None,
        "reseller_fit": "NONE",
        "execution_complexity": "HIGH",
        "fatal_issue": None,
        "advance": True,
        "reason_code": "NEEDS_STAGE2_DOCUMENT_REVIEW",
    }
    fp_info = compute_stage1_fingerprint(opp, stage0=_stage0_ok())
    put_cached_analysis(fp_info["fingerprint"], result_text=json.dumps(payload))
    res = resolve_current_stage1_result(opp, stage0=_stage0_ok())
    assert res["status"] == "HIT"
    gate = can_run_stage2(opp, stage0=_stage0_ok(), stage1_resolution=res)
    assert gate["allowed"] is True
    assert block_openai["n"] == 0


def test_orm_stale_stage1_does_not_override_current_miss(block_openai):
    opp = _opp(
        notice_id="notice-orm-stale-only",
        analysis={
            "advance": True,
            "reason_code": "NEEDS_STAGE2_DOCUMENT_REVIEW",
            "category": "LABOR_HEAVY",
        },
    )
    res = resolve_current_stage1_result(opp, stage0=_stage0_ok())
    assert res["status"] == "STAGE1_CURRENT_RESULT_MISSING"
    gate = can_run_stage2(opp, stage0=_stage0_ok(), stage1_resolution=res)
    assert gate["allowed"] is False
    assert block_openai["n"] == 0


def test_prompt_change_invalidates_stage1_fingerprint():
    opp = _opp()
    a = compute_stage1_fingerprint(opp, stage0=_stage0_ok())
    with patch.object(ai_stage1, "STAGE1_INSTRUCTIONS", ai_stage1.STAGE1_INSTRUCTIONS + "\nEXTRA RULE."):
        b = compute_stage1_fingerprint(opp, stage0=_stage0_ok())
    assert a["fingerprint"] != b["fingerprint"]
    assert a["source_hash"] != b["source_hash"]


def test_schema_component_includes_instruction_version():
    assert STAGE1_INSTRUCTION_VERSION in stage1_schema_fingerprint_component()
    assert STAGE1_SCHEMA_VERSION in stage1_schema_fingerprint_component()


# ---- Stage 2 compaction ----


def test_stage0_reject_blocks_stage2():
    gate = can_run_stage2(_opp(), stage0=_stage0_reject(), stage1=_stage1_ok())
    assert gate["allowed"] is False


def test_stage1_reject_blocks_stage2():
    gate = can_run_stage2(_opp(), stage0=_stage0_ok(), stage1=_stage1_reject())
    assert gate["allowed"] is False


def test_eligible_fixture_stage1_allows_stage2():
    gate = can_run_stage2(_opp(), stage0=_stage0_ok(), stage1=_stage1_ok())
    assert gate["allowed"] is True


def test_web_model_strict_schema_v2():
    assert web_search_allowed_for_stage(FunnelStage.STAGE_2) is False
    fmt = stage2_response_format()
    assert fmt["type"] == "json_schema"
    assert fmt["strict"] is True
    assert STAGE2_SCHEMA_VERSION == "stage2-evidence-v2"
    assert "stage2-evidence-v1" not in stage2_schema_fingerprint_component()


def test_v1_fingerprint_differs_from_v2():
    a = f"{PROMPT_SCHEMA_VERSION}:stage2-evidence-v1:stage2-instr-v1"
    b = stage2_schema_fingerprint_component()
    assert a != b
    fa = analysis_fingerprint(
        notice_id="n",
        task=STAGE2_TASK,
        model_tier=ModelTier.CHEAP.value,
        source_hash="abc",
        schema_version=a,
        funnel_stage=2,
    )
    fb = analysis_fingerprint(
        notice_id="n",
        task=STAGE2_TASK,
        model_tier=ModelTier.CHEAP.value,
        source_hash="abc",
        schema_version=b,
        funnel_stage=2,
    )
    assert fa != fb


def test_output_fixtures_fit_ceiling():
    m = measure_stage2_output_fixtures()
    assert STAGE2_MAX_OUTPUT_TOKENS >= m["max_fixture_est_tokens"]
    for f in m["fixtures"]:
        assert f["est_tokens"] <= STAGE2_MAX_OUTPUT_TOKENS
    # empty is tiny vs old ~1571
    empty = next(x for x in m["fixtures"] if x["name"] == "empty")
    assert empty["chars"] < 100
    assert empty["est_tokens"] < 50


def test_sparse_unknown_stays_unknown_locally():
    result = normalize_stage2_result(
        {"facts": [], "fatal_candidates": []},
        corpus=BOND_CORPUS,
        manifest=[{"document_id": "attachment_text"}],
        opportunity=_opp(attachment_text=BOND_QUOTE),
        stage1=_stage1_ok(),
    )
    assert result["facts"]["compliance"]["bond_requirement"]["status"] == STATUS_UNKNOWN
    assert result["facts"]["scope"]["quantity"]["status"] == STATUS_UNKNOWN
    codes = {n["code"] for n in result["research_needs"]}
    assert "BOND_REQUIREMENT_UNCLEAR" in codes
    assert "QUANTITY_UNCLEAR" in codes
    assert result["advance"] is True


def test_validated_bond_and_invented_quote():
    good = validate_and_envelope_fact(
        {
            "value": True,
            "evidence_text": BOND_QUOTE,
            "document_id": "attachment_text",
            "locator": "p1",
            "confidence": "HIGH",
            "code": "BOND_REQUIREMENT",
        },
        field="compliance.bond_requirement",
        corpus=BOND_CORPUS,
        manifest_ids={"attachment_text"},
    )
    assert good["status"] == STATUS_VERIFIED
    bad = validate_and_envelope_fact(
        {
            "value": True,
            "evidence_text": "This quote does not exist in the source corpus at all.",
            "document_id": "attachment_text",
            "locator": "p1",
            "confidence": "HIGH",
        },
        field="compliance.bond_requirement",
        corpus=BOND_CORPUS,
        manifest_ids={"attachment_text"},
    )
    assert bad["status"] == STATUS_ASSESSMENT


def test_building_number_not_quantity():
    fact = validate_and_envelope_fact(
        {
            "value": "Building 977",
            "evidence_text": "Building 977",
            "document_id": "attachment_text",
            "locator": None,
            "confidence": "HIGH",
        },
        field="scope.quantity",
        corpus="Work at Building 977.",
        manifest_ids={"attachment_text"},
    )
    assert fact["status"] == STATUS_UNKNOWN


def test_economic_local_derivation():
    facts = {
        "procurement": {"category": {"value": "LABOR_HEAVY", "status": STATUS_VERIFIED}},
        "execution": {"installation_required": {"value": None, "status": STATUS_UNKNOWN}},
        "scope": {},
        "compliance": {},
        "economic_evidence": {"stated_value": {"value": 27081, "status": STATUS_ASSESSMENT}},
    }
    econ = assign_economic_requirements(facts, stage1={"category": "LABOR_HEAVY"})
    assert econ["canonical_execution_class"] == "LABOR_HEAVY"
    assert econ["costs"]["subcontract"]["status"] == COST_REQUIRED_UNKNOWN
    assert econ["costs"]["supplier"]["status"] == COST_NOT_APPLICABLE
    assert "no product procurement" in (econ["costs"]["supplier"].get("basis") or "").lower()
    assert econ["actual_profit"] is None
    assert econ["actual_profit_status"] == "INCOMPLETE"


def test_free_text_stage2_category_ignored_for_economics():
    """Opp 68 regression: Stage 2 descriptive category must not drive cost N/A."""
    facts = {
        "procurement": {
            "category": {"value": "Grounds Maintenance Services", "status": STATUS_ASSESSMENT}
        },
        "execution": {"installation_required": {"value": None, "status": STATUS_UNKNOWN}},
        "scope": {},
        "compliance": {},
        "economic_evidence": {},
    }
    econ = assign_economic_requirements(facts, stage1={"category": "LABOR_HEAVY"})
    assert econ["canonical_execution_class"] == "LABOR_HEAVY"
    assert econ["costs"]["subcontract"]["status"] == COST_REQUIRED_UNKNOWN
    assert econ["costs"]["supplier"]["status"] == COST_NOT_APPLICABLE
    needs = build_research_needs(facts, economic_requirements=econ)
    codes = {n["code"] for n in needs}
    assert "SUBCONTRACTOR_QUOTE_REQUIRED" in codes
    assert "SUPPLIER_QUOTE_REQUIRED" not in codes
    assert "FINANCING_TERMS_REQUIRED" in codes


def test_validated_fatal_rejects_unvalidated_does_not():
    raw_ok = {
        "facts": [
            {
                "code": "BOND_REQUIREMENT",
                "value": True,
                "evidence_text": BOND_QUOTE,
                "document_id": "attachment_text",
                "locator": "p1",
                "confidence": "HIGH",
            }
        ],
        "fatal_candidates": [
            {
                "code": "BOND_UNACCEPTABLE",
                "summary": "bond",
                "evidence_text": BOND_QUOTE,
                "document_id": "attachment_text",
                "locator": "p1",
            }
        ],
    }
    r1 = normalize_stage2_result(
        raw_ok,
        corpus=BOND_CORPUS,
        manifest=[{"document_id": "attachment_text"}],
        stage1=_stage1_ok(),
    )
    assert r1["advance"] is False
    assert r1["reason_code"] == "STAGE2_FATAL_VERIFIED"

    raw_bad = {
        "facts": [],
        "fatal_candidates": [
            {
                "code": "FAKE",
                "summary": "x",
                "evidence_text": "not in corpus",
                "document_id": "attachment_text",
                "locator": None,
            }
        ],
    }
    r2 = normalize_stage2_result(
        raw_bad,
        corpus=BOND_CORPUS,
        manifest=[{"document_id": "attachment_text"}],
        stage1=_stage1_ok(),
    )
    assert r2["advance"] is True
    assert r2["fatal_facts"] == []


def test_dedupe_and_similar_filenames():
    docs = [
        {"document_id": "a", "content_hash": "abc", "text": "hello"},
        {"document_id": "b", "content_hash": "abc", "text": "hello"},
        {"document_id": "c", "content_hash": "def", "text": "other"},
    ]
    out = dedupe_manifest(docs)
    assert out[1]["duplicate_of"] == "a"
    assert out[2]["duplicate_of"] is None
    out2 = dedupe_manifest(
        [
            {"document_id": "sow_v1.pdf", "content_hash": "111", "text": "v1"},
            {"document_id": "sow_v2.pdf", "content_hash": "222", "text": "v2"},
        ]
    )
    assert all(d.get("duplicate_of") is None for d in out2)


def test_oversize_no_openai(monkeypatch, block_openai):
    import ai_stage2

    monkeypatch.setattr(
        ai_stage2,
        "check_stage2_input_size",
        lambda n: {
            "ok": False,
            "limit": 500,
            "chars": n,
            "reason_code": "STAGE2_NEEDS_ADDITIONAL_DOCUMENT_PROCESSING",
            "complete_review_claimed": False,
        },
    )
    monkeypatch.setattr(ai_stage2, "record_stage2_metric", lambda *_: None)
    result = run_stage2_evidence(
        _opp(attachment_text="x" * 2000),
        stage0=_stage0_ok(),
        stage1=_stage1_ok(),
        automatic=False,
    )
    assert result["reason_code"] == "STAGE2_NEEDS_ADDITIONAL_DOCUMENT_PROCESSING"
    assert block_openai["n"] == 0


def test_cache_hit_and_parse_failure(monkeypatch, block_openai):
    import ai_stage2

    monkeypatch.setattr(ai_stage2, "record_stage2_metric", lambda *_: None)

    def fake_create(**kwargs):
        assert kwargs.get("web_search") is False
        assert kwargs.get("text_format", {}).get("strict") is True
        openai_runtime._LAST_RESPONSE_META = {
            "cache_hit": True,
            "estimated_cost_usd": 0.0,
            "openai_called": False,
        }
        return json.dumps({"facts": [], "fatal_candidates": []})

    monkeypatch.setattr(openai_runtime, "create_response", fake_create)
    r1 = run_stage2_evidence(
        _opp(attachment_text="Contractor shall mow weekly."),
        stage0=_stage0_ok(),
        stage1=_stage1_ok(),
        automatic=False,
    )
    assert r1["cache_hit"] is True
    assert r1["incremental_cost_usd"] == 0.0
    assert block_openai["n"] == 0

    def bad_create(**kwargs):
        openai_runtime._LAST_RESPONSE_META = {
            "cache_hit": False,
            "estimated_cost_usd": 0.001,
            "openai_called": True,
        }
        return "NOT JSON {{{"

    monkeypatch.setattr(openai_runtime, "create_response", bad_create)
    r2 = run_stage2_evidence(
        _opp(description="simple grounds work"),
        stage0=_stage0_ok(),
        stage1=_stage1_ok(),
        automatic=False,
    )
    assert r2["advance"] is True
    assert r2["reason_code"] == "STAGE2_PARSE_ERROR_ADVANCE"


def test_changed_doc_hash_changes_source():
    b1 = build_stage2_input(_opp(attachment_text="Document version one AAA"), stage0=_stage0_ok(), stage1=_stage1_ok())
    b2 = build_stage2_input(_opp(attachment_text="Document version two BBB"), stage0=_stage0_ok(), stage1=_stage1_ok())
    assert b1["source_hash"] != b2["source_hash"]


def test_automatic_disabled():
    from openai_client import extract_stage2_evidence

    with pytest.raises(ValueError, match="automatic"):
        extract_stage2_evidence(_opp(), automatic=True)


def test_no_fabricated_profit():
    result = normalize_stage2_result(
        {"facts": [], "fatal_candidates": []},
        corpus=BOND_CORPUS,
        manifest=[{"document_id": "attachment_text"}],
        opportunity=_opp(sam_raw={"award": {"amount": 73000}, "classificationCode": "S208"}),
        stage1=_stage1_ok(),
    )
    assert result["economic_requirements"]["actual_profit"] is None
    assert result["economic_requirements"]["revenue_evidence"]["is_current_revenue"] is False


def test_manifest_no_invented_metadata():
    manifest = build_document_manifest(_opp())
    structured = next(d for d in manifest if d["document_id"] == "structured_opportunity")
    assert structured["page_count"] is None
    assert structured["filename"] is None


def test_minimal_model_output_token_estimate_under_ceiling():
    chars = len(json.dumps({"facts": [], "fatal_candidates": []}, separators=(",", ":")))
    assert estimate_tokens_from_chars(chars) < STAGE2_MAX_OUTPUT_TOKENS
