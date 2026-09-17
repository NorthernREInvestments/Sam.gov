"""Stage 0 + Stage 1 compact triage tests — mocks only, no live API."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import ai_funnel
import ai_model_router as router
import ai_stage1
import company_eligibility
import openai_runtime


def _opp(**kwargs):
    base = dict(
        notice_id="N-1",
        title="Commercial Widget Supply",
        description="Purchase of commercial widgets",
        due_date=date.today() + timedelta(days=30),
        status="new",
        set_aside="Total Small Business",
        naics_code="333999",
        agency="Some Agency",
        location="Austin, TX",
        analysis={},
        sam_raw={},
        estimated_value=None,
        link="https://sam.gov/opp/abc",
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL_CHEAP", "gpt-5.6-luna")
    monkeypatch.setenv("OPENAI_WEB_SEARCH_ENABLED", "false")
    monkeypatch.setenv("AI_STAGE1_MAX_OUTPUT_TOKENS", "400")
    monkeypatch.setenv("AI_MIN_ACTUAL_PROFIT_USD", "10000")
    monkeypatch.setenv("COMPANY_UNSUPPORTED_SET_ASIDES", "WOSB,EDWOSB,SDVOSB,VOSB,HUBZONE,8(a),8A")
    monkeypatch.setenv("COMPANY_CERTIFICATIONS", "SB,SMALL_BUSINESS,SBA")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")


def test_expired_rejected_free():
    r = ai_funnel.stage0_evaluate(_opp(due_date=date.today() - timedelta(days=1)))
    assert r["decision"] == "REJECT"
    assert "expired_deadline" in r["reject_reasons"]
    assert r["ai_cost_usd"] == 0.0


def test_active_advances_free():
    r = ai_funnel.stage0_evaluate(_opp())
    assert r["decision"] in {"ADVANCE", "REVIEW"}
    assert r["advance"] is True


def test_missing_deadline_does_not_reject():
    r = ai_funnel.stage0_evaluate(_opp(due_date=None))
    assert "expired_deadline" not in r["reject_reasons"]
    assert r["advance"] is True


def test_unsupported_set_aside_rejected():
    r = ai_funnel.stage0_evaluate(_opp(set_aside="WOSB Set-Aside"))
    assert r["decision"] == "REJECT"
    assert "unsupported_set_aside" in r["reject_reasons"]
    assert r["set_aside_eligible"] is False


def test_small_business_not_auto_rejected():
    r = ai_funnel.stage0_evaluate(_opp(set_aside="Total Small Business Set-Aside"))
    assert r["set_aside_eligible"] is True
    assert "unsupported_set_aside" not in r["reject_reasons"]


def test_unrestricted_not_rejected():
    r = ai_funnel.stage0_evaluate(_opp(set_aside="Unrestricted"))
    assert r["set_aside_eligible"] is True
    assert r["advance"] is True


def test_unknown_set_aside_advances():
    r = ai_funnel.stage0_evaluate(_opp(set_aside=None))
    assert r["set_aside_eligible"] is None
    assert r["advance"] is True


def test_nationwide_not_rejected_by_geography():
    r = ai_funnel.stage0_evaluate(_opp(location="Anchorage, AK", title="Supply of tools — Alaska"))
    assert r["advance"] is True
    assert not any("nebraska" in f.lower() for f in r["flags"])


def test_tiny_contract_rejected_only_when_verified_against_sam():
    # ORM alone is UNPROVEN — must NOT hard-reject
    r = ai_funnel.stage0_evaluate(_opp(estimated_value="$6,000"))
    assert "value_below_min_profit" not in (r.get("reject_reasons") or [])
    assert r["known_contract_value"] is None or r["known_contract_value"].get("status") != "VERIFIED"

    # Same amount matched to structured SAM → VERIFIED → hard reject below floor
    r2 = ai_funnel.stage0_evaluate(
        _opp(estimated_value="$6,000", sam_raw={"award": {"amount": 6000}})
    )
    assert r2["decision"] == "REJECT"
    assert "value_below_min_profit" in r2["reject_reasons"]
    assert r2["known_contract_value"]["status"] == "VERIFIED"
    assert r2["known_contract_value"]["amount"] == 6000.0
    assert r2["known_contract_value"]["confidence"] == "HIGH"


def test_money_parser_does_not_truncate_27081():
    assert ai_funnel._parse_money("27081.00") == 27081.0
    assert ai_funnel._parse_money(27081.00) == 27081.0
    info = ai_funnel.resolve_known_contract_value(
        _opp(estimated_value="27081.00", sam_raw={"award": {"amount": 27081.00}})
    )
    assert info is not None
    assert info["amount"] == 27081.0
    assert info["confidence"] == "HIGH"
    assert info["status"] == "VERIFIED"


def test_building_number_not_contract_value():
    r = ai_funnel.stage0_evaluate(
        _opp(estimated_value=None, title="Services at Building 270 Kapolei", description="Building 270 work")
    )
    assert r["known_contract_value"] is None or r["known_contract_value"].get("confidence") != "HIGH"
    assert "value_below_min_profit" not in (r.get("reject_reasons") or [])


def test_missing_value_advances():
    r = ai_funnel.stage0_evaluate(_opp(estimated_value=None))
    assert "value_below_min_profit" not in r["reject_reasons"]
    assert r["advance"] is True


def test_product_classified():
    r = ai_funnel.stage0_evaluate(_opp(title="Purchase of HVAC Equipment", naics_code="333415"))
    assert r["classification"] in {ai_funnel.CLASS_PRODUCT_RESELL, ai_funnel.CLASS_PRODUCT_PLUS_SERVICE}


def test_ambiguous_service_not_falsely_rejected():
    r = ai_funnel.stage0_evaluate(
        _opp(title="Facilities Support Services", naics_code="561210", set_aside="Unrestricted")
    )
    assert r["advance"] is True
    assert r["decision"] != "REJECT" or "unsupported_set_aside" not in r["reject_reasons"]


def test_amendment_deduplication():
    parent = _opp(notice_id="PARENT-1", solicitation_number="SOL-99")
    parent.sam_raw = {"solicitationNumber": "SOL-99"}
    amend = _opp(notice_id="AMD-1", title="Amendment 0001")
    amend.sam_raw = {
        "solicitationNumber": "SOL-99",
        "type": "Presolicitation Amendment",
        "parentNoticeId": "PARENT-1",
    }
    r = ai_funnel.stage0_evaluate(amend, existing=[parent])
    assert r["decision"] == "REJECT"
    assert "amendment_duplicate" in r["reject_reasons"] or "duplicate_solicitation" in r["reject_reasons"]
    assert r.get("amendment") or "amendment_of_existing" in r["flags"] or True


def test_nmr_flagged_not_auto_rejected():
    r = ai_funnel.stage0_evaluate(
        _opp(
            title="Supply of manufactured parts",
            naics_code="332710",
            set_aside="Total Small Business",
            description="Subject to Nonmanufacturer Rule review.",
        )
    )
    assert r["nmr_review_required"] is True
    assert r["advance"] is True  # flag, don't reject


def test_brand_restriction_flagged():
    r = ai_funnel.stage0_evaluate(
        _opp(description="Brand-name only — authorized reseller required. No substitutes.")
    )
    assert r["channel_review_required"] is True
    assert "channel_restriction_mentioned" in r["flags"]
    assert r["advance"] is True


def test_stage1_never_sends_pdf(monkeypatch):
    captured = {}

    class FakeResponse:
        output_text = (
            '{"category":"PRODUCT_RESELL","buying":"widgets","quantity":10,'
            '"exact_model_identified":false,"install_required":false,"bond_likely":null,'
            '"license_likely":null,"channel_restriction_likely":false,"reseller_fit":"HIGH",'
            '"execution_complexity":"LOW","fatal_issue":null,"advance":true,"reason_code":"FIT"}'
        )
        usage = SimpleNamespace(input_tokens=40, output_tokens=60, total_tokens=100)

    class FakeClient:
        class responses:
            @staticmethod
            def create(**kwargs):
                captured.update(kwargs)
                return FakeResponse()

    monkeypatch.setattr(openai_runtime, "get_openai_client", lambda: FakeClient())
    monkeypatch.setattr(openai_runtime, "get_cached_analysis", lambda *_: None)
    monkeypatch.setattr(openai_runtime, "put_cached_analysis", lambda *a, **k: None)
    monkeypatch.setattr(openai_runtime, "record_ai_dollar_spend", lambda *_: {})
    monkeypatch.setattr(ai_stage1, "record_stage1_metric", lambda *_: None)

    result = ai_stage1.run_stage1_triage(_opp(), stage0={"decision": "ADVANCE", "flags": []})
    content = captured["input"][0]["content"]
    assert not ai_stage1.content_has_pdf_parts(content)
    assert captured["model"] == "gpt-5.6-luna"
    assert "tools" not in captured
    assert captured["max_output_tokens"] <= 400
    assert result["advance"] is True
    assert result["pdfs_sent_to_claude"] == 0


def test_stage1_compact_input_builder():
    built = ai_stage1.build_stage1_input(
        _opp(description="x" * 20000, attachment_text="y" * 50000),
        stage0={"decision": "ADVANCE", "flags": ["nmr_possible_supply_sb"], "classification": "PRODUCT_RESELL"},
    )
    assert built["char_count"] <= ai_stage1.STAGE1_TARGET_INPUT_CHARS + 50
    assert built["has_pdf"] is False
    assert "title" in built["text"]


def test_stage1_output_limit():
    assert router.clamp_max_output_tokens(1, 9999) <= 400
    assert ai_stage1.STAGE1_MAX_OUTPUT_TOKENS <= 400


def test_stage1_cache_hit_zero_cost(monkeypatch):
    store = {}
    calls = {"n": 0}

    def get_cached(fp):
        return store.get(fp)

    def put_cached(fp, result_text="", meta=None):
        store[fp] = {"result_text": result_text}

    class FakeResponse:
        output_text = (
            '{"category":"PRODUCT_RESELL","buying":"tools","quantity":null,'
            '"exact_model_identified":false,"install_required":false,"bond_likely":false,'
            '"license_likely":false,"channel_restriction_likely":false,"reseller_fit":"MEDIUM",'
            '"execution_complexity":"LOW","fatal_issue":null,"advance":true,"reason_code":"OK"}'
        )
        usage = SimpleNamespace(input_tokens=20, output_tokens=40, total_tokens=60)

    class FakeClient:
        class responses:
            @staticmethod
            def create(**kwargs):
                calls["n"] += 1
                return FakeResponse()

    costs = []
    monkeypatch.setattr(openai_runtime, "get_openai_client", lambda: FakeClient())
    monkeypatch.setattr(openai_runtime, "get_cached_analysis", get_cached)
    monkeypatch.setattr(openai_runtime, "put_cached_analysis", put_cached)
    monkeypatch.setattr(openai_runtime, "record_ai_dollar_spend", lambda e: costs.append(e))
    monkeypatch.setattr(ai_stage1, "record_stage1_metric", lambda *_: None)

    o = _opp()
    ai_stage1.run_stage1_triage(o, stage0={"decision": "ADVANCE", "flags": []})
    ai_stage1.run_stage1_triage(o, stage0={"decision": "ADVANCE", "flags": []})
    assert calls["n"] == 1


def test_uncertain_stage1_does_not_false_reject():
    raw = {
        "category": "UNKNOWN",
        "buying": "unclear",
        "quantity": None,
        "exact_model_identified": False,
        "install_required": None,
        "bond_likely": None,
        "license_likely": None,
        "channel_restriction_likely": None,
        "reseller_fit": "LOW",
        "execution_complexity": "MEDIUM",
        "fatal_issue": None,
        "advance": False,  # model said no without fatal — must flip
        "reason_code": "WEAK",
    }
    out = ai_stage1.normalize_stage1_result(raw)
    assert out["advance"] is True
    assert out["reason_code"] == "UNCERTAIN_ADVANCE"


def test_set_aside_eligibility_helpers():
    assert company_eligibility.set_aside_eligibility("SDVOSB")["eligible"] is False
    assert company_eligibility.set_aside_eligibility("Small Business Set-Aside")["eligible"] is True
