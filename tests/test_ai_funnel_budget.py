"""AI funnel / model routing / budget / cache tests — mocks only, no live API."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import ai_analysis_cache as cache_mod
import ai_cost_budget as cost_mod
import ai_funnel
import ai_model_router as router
import api_budget
import openai_runtime


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL_CHEAP", "gpt-5.6-luna")
    monkeypatch.setenv("OPENAI_MODEL_STANDARD", "gpt-5.6-terra")
    monkeypatch.setenv("OPENAI_MODEL_PREMIUM", "gpt-5.6-sol")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_WEB_SEARCH_ENABLED", "false")
    monkeypatch.setenv("AI_MONTHLY_DOLLAR_BUDGET", "40")
    monkeypatch.setenv("AI_DAILY_DOLLAR_SOFT_LIMIT", "1")
    monkeypatch.setenv("AI_BUDGET_STAGE1_PERCENT", "20")
    monkeypatch.setenv("AI_BUDGET_STAGE2_PERCENT", "20")
    monkeypatch.setenv("AI_BUDGET_STAGE3_PERCENT", "25")
    monkeypatch.setenv("AI_BUDGET_STAGE4_PERCENT", "20")
    monkeypatch.setenv("AI_BUDGET_STAGE5_PERCENT", "15")
    monkeypatch.setenv("AI_STAGE1_MAX_INPUT_CHARS", "12000")
    monkeypatch.setenv("AUTO_SCREEN_ON_CONTRACT_DETAIL", "false")
    monkeypatch.setenv("INTAKE_ON_SYNC", "false")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")


def test_stage0_never_calls_openai(monkeypatch):
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("OpenAI must not be called at Stage 0")

    monkeypatch.setattr(openai_runtime, "get_openai_client", boom)
    result = ai_funnel.stage0_evaluate(SimpleNamespace(notice_id="x", due_date=None, status="new", analysis={}))
    assert result["ai_cost_usd"] == 0.0
    assert result["funnel_stage"] == 0
    assert called["n"] == 0
    with pytest.raises(ValueError, match="Stage 0"):
        openai_runtime.create_response(
            task="noop",
            instructions=None,
            content=[{"type": "input_text", "text": "x"}],
            max_output_tokens=10,
            funnel_stage=0,
        )


def test_stage1_routes_to_luna(monkeypatch):
    captured = {}

    class FakeResponse:
        output_text = '{"advance": true}'
        usage = SimpleNamespace(input_tokens=5, output_tokens=3, total_tokens=8)

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
    openai_runtime.create_response(
        task="screen_contract_text",
        instructions="static schema",
        content=[{"type": "input_text", "text": "short"}],
        max_output_tokens=400,
        funnel_stage=1,
        use_cache=False,
    )
    assert captured["model"] == "gpt-5.6-luna"


def test_stage1_cannot_use_web_search(monkeypatch):
    captured = {}

    class FakeResponse:
        output_text = "{}"
        usage = None

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
    openai_runtime.create_response(
        task="screen_contract_text",
        instructions=None,
        content=[{"type": "input_text", "text": "x"}],
        max_output_tokens=100,
        funnel_stage=1,
        web_search=True,  # attempt — must be blocked
        use_cache=False,
    )
    assert "tools" not in captured


def test_stage2_routes_to_luna_no_web_search(monkeypatch):
    captured = {}

    class FakeResponse:
        output_text = "{}"
        usage = None

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
    openai_runtime.create_response(
        task="screen_contract",
        instructions=None,
        content=[{"type": "input_text", "text": "x"}],
        max_output_tokens=800,
        funnel_stage=2,
        web_search=True,
        use_cache=False,
    )
    assert captured["model"] == "gpt-5.6-luna"
    assert "tools" not in captured


def test_stage3_luna_can_explicit_web_search(monkeypatch):
    captured = {}

    class FakeResponse:
        output_text = "{}"
        usage = None

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
    logs = []
    monkeypatch.setattr(openai_runtime, "log_ai_usage", lambda **kw: logs.append(kw))

    openai_runtime.create_response(
        task="research_suppliers",
        instructions=None,
        content=[{"type": "input_text", "text": "find distributors"}],
        max_output_tokens=500,
        funnel_stage=3,
        web_search=True,
        use_cache=False,
    )
    assert captured["model"] == "gpt-5.6-luna"
    assert captured.get("tools") == [{"type": "web_search"}]
    assert logs and logs[0]["web_search"] is True


def test_stage4_routes_to_terra(monkeypatch):
    captured = {}

    class FakeResponse:
        output_text = "{}"
        usage = None

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
    openai_runtime.create_response(
        task="generate_contract_advice",
        instructions=None,
        content=[{"type": "input_text", "text": "x"}],
        max_output_tokens=1000,
        funnel_stage=4,
        use_cache=False,
    )
    assert captured["model"] == "gpt-5.6-terra"


def test_stage5_routes_to_sol_not_automatic(monkeypatch):
    assert router.stage_allows_automatic(5) is False
    captured = {}

    class FakeResponse:
        output_text = "{}"
        usage = None

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

    with pytest.raises(ValueError, match="explicit user action"):
        openai_runtime.create_response(
            task="premium_final",
            instructions=None,
            content=[{"type": "input_text", "text": "x"}],
            max_output_tokens=2000,
            funnel_stage=5,
            automatic=True,
            use_cache=False,
        )

    openai_runtime.create_response(
        task="premium_final",
        instructions=None,
        content=[{"type": "input_text", "text": "x"}],
        max_output_tokens=2000,
        funnel_stage=5,
        automatic=False,
        use_cache=False,
    )
    assert captured["model"] == "gpt-5.6-sol"


def test_page_load_auto_screen_off_by_default():
    assert api_budget.auto_screen_on_contract_detail() is False


def test_csv_import_does_not_enable_intake():
    assert api_budget.intake_on_sync_enabled() is False
    assert api_budget.ai_intake_allowed() is False


def test_duplicate_analysis_uses_cache(monkeypatch):
    store: dict[str, dict] = {}
    calls = {"n": 0}

    def get_cached(fp):
        return store.get(fp)

    def put_cached(fp, result_text="", meta=None):
        store[fp] = {"result_text": result_text, "meta": meta or {}}

    class FakeResponse:
        output_text = '{"cached": false}'
        usage = SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15)

    class FakeClient:
        class responses:
            @staticmethod
            def create(**kwargs):
                calls["n"] += 1
                return FakeResponse()

    monkeypatch.setattr(openai_runtime, "get_openai_client", lambda: FakeClient())
    monkeypatch.setattr(openai_runtime, "get_cached_analysis", get_cached)
    monkeypatch.setattr(openai_runtime, "put_cached_analysis", put_cached)
    monkeypatch.setattr(openai_runtime, "record_ai_dollar_spend", lambda *_: {})
    monkeypatch.setattr(openai_runtime, "log_ai_usage", lambda **kw: None)

    kwargs = dict(
        task="screen_contract_text",
        instructions="static",
        content=[{"type": "input_text", "text": "same source"}],
        max_output_tokens=100,
        funnel_stage=1,
        notice_id="N1",
        use_cache=True,
    )
    a = openai_runtime.create_response(**kwargs)
    b = openai_runtime.create_response(**kwargs)
    assert a == b
    assert calls["n"] == 1


def test_changed_source_invalidates_cache(monkeypatch):
    store: dict[str, dict] = {}
    calls = {"n": 0}

    def get_cached(fp):
        return store.get(fp)

    def put_cached(fp, result_text="", meta=None):
        store[fp] = {"result_text": result_text, "meta": meta or {}}

    class FakeResponse:
        output_text = '{"v": 1}'
        usage = None

    class FakeClient:
        class responses:
            @staticmethod
            def create(**kwargs):
                calls["n"] += 1
                FakeResponse.output_text = f'{{"v": {calls["n"]}}}'
                return FakeResponse()

    monkeypatch.setattr(openai_runtime, "get_openai_client", lambda: FakeClient())
    monkeypatch.setattr(openai_runtime, "get_cached_analysis", get_cached)
    monkeypatch.setattr(openai_runtime, "put_cached_analysis", put_cached)
    monkeypatch.setattr(openai_runtime, "record_ai_dollar_spend", lambda *_: {})
    monkeypatch.setattr(openai_runtime, "log_ai_usage", lambda **kw: None)

    openai_runtime.create_response(
        task="screen_contract_text",
        instructions="static",
        content=[{"type": "input_text", "text": "source A"}],
        max_output_tokens=50,
        funnel_stage=1,
        notice_id="N1",
    )
    openai_runtime.create_response(
        task="screen_contract_text",
        instructions="static",
        content=[{"type": "input_text", "text": "source B"}],
        max_output_tokens=50,
        funnel_stage=1,
        notice_id="N1",
    )
    assert calls["n"] == 2


def test_monthly_budget_blocks_automatic(monkeypatch):
    monkeypatch.setattr(
        cost_mod,
        "get_cost_snapshot",
        lambda: {
            "monthly_remaining_usd": 0.0,
            "daily_soft_remaining_usd": 1.0,
            "stage_remaining_usd": {"1": 8.0, "2": 8.0, "3": 10.0, "4": 8.0, "5": 6.0},
        },
    )
    ok, reason = cost_mod.can_afford_automatic_call(stage=1, estimated_cost_usd=0.01)
    assert ok is False
    assert reason == "monthly_budget_exhausted"

    monkeypatch.setattr(openai_runtime, "get_cost_snapshot", cost_mod.get_cost_snapshot)
    monkeypatch.setattr(openai_runtime, "require_automatic_budget", cost_mod.require_automatic_budget)
    monkeypatch.setattr(openai_runtime, "get_cached_analysis", lambda *_: None)

    with pytest.raises(cost_mod.AIDollarBudgetExceeded):
        openai_runtime.create_response(
            task="screen_contract_text",
            instructions=None,
            content=[{"type": "input_text", "text": "x"}],
            max_output_tokens=50,
            funnel_stage=1,
            automatic=True,
            use_cache=False,
        )


def test_stage_budget_protects_later_reserves(monkeypatch):
    monkeypatch.setattr(
        cost_mod,
        "get_cost_snapshot",
        lambda: {
            "monthly_remaining_usd": 30.0,
            "daily_soft_remaining_usd": 1.0,
            "stage_remaining_usd": {"1": 0.0, "2": 8.0, "3": 10.0, "4": 8.0, "5": 6.0},
        },
    )
    ok, reason = cost_mod.can_afford_automatic_call(stage=1, estimated_cost_usd=0.05)
    assert ok is False
    assert reason == "stage_1_reserve_exhausted"
    ok2, _ = cost_mod.can_afford_automatic_call(stage=4, estimated_cost_usd=0.05)
    assert ok2 is True


def test_oversized_stage1_input_rejected(monkeypatch):
    monkeypatch.setattr(openai_runtime, "get_cached_analysis", lambda *_: None)
    huge = "x" * 20000
    with pytest.raises(cost_mod.AIInputTooLarge):
        openai_runtime.create_response(
            task="screen_contract_text",
            instructions=None,
            content=[{"type": "input_text", "text": huge}],
            max_output_tokens=400,
            funnel_stage=1,
            use_cache=False,
        )


def test_stage1_output_limit_clamped():
    assert router.clamp_max_output_tokens(1, 10000) == 400
    assert router.clamp_max_output_tokens(1, 100) == 100


def test_cost_logging_records_stage_model_task(monkeypatch):
    logs = []

    class FakeResponse:
        output_text = "{}"
        usage = SimpleNamespace(input_tokens=11, output_tokens=7, total_tokens=18)

    class FakeClient:
        class responses:
            @staticmethod
            def create(**kwargs):
                return FakeResponse()

    monkeypatch.setattr(openai_runtime, "get_openai_client", lambda: FakeClient())
    monkeypatch.setattr(openai_runtime, "get_cached_analysis", lambda *_: None)
    monkeypatch.setattr(openai_runtime, "put_cached_analysis", lambda *a, **k: None)
    monkeypatch.setattr(openai_runtime, "record_ai_dollar_spend", lambda e: logs.append(e) or {})
    # Also capture logger path via wrapping log_ai_usage internals — call real log then inspect spend
    openai_runtime.create_response(
        task="screen_contract_text",
        instructions="schema",
        content=[{"type": "input_text", "text": "ok"}],
        max_output_tokens=50,
        funnel_stage=1,
        notice_id="ABC",
        use_cache=False,
    )
    assert logs
    assert logs[0]["funnel_stage"] == 1
    assert logs[0]["model"] == "gpt-5.6-luna"
    assert logs[0]["task"] == "screen_contract_text"
    assert logs[0]["estimated_cost_usd"] is not None


def test_web_search_logging(monkeypatch):
    logs = []

    class FakeResponse:
        output_text = "{}"
        usage = None

    class FakeClient:
        class responses:
            @staticmethod
            def create(**kwargs):
                return FakeResponse()

    monkeypatch.setattr(openai_runtime, "get_openai_client", lambda: FakeClient())
    monkeypatch.setattr(openai_runtime, "get_cached_analysis", lambda *_: None)
    monkeypatch.setattr(openai_runtime, "put_cached_analysis", lambda *a, **k: None)
    monkeypatch.setattr(openai_runtime, "record_ai_dollar_spend", lambda *_: {})
    monkeypatch.setattr(openai_runtime, "log_ai_usage", lambda **kw: logs.append(kw))
    openai_runtime.create_response(
        task="research",
        instructions=None,
        content=[{"type": "input_text", "text": "x"}],
        max_output_tokens=100,
        funnel_stage=3,
        web_search=True,
        use_cache=False,
    )
    assert logs[0]["web_search"] is True


def test_should_advance_stage5_requires_explicit():
    result = ai_funnel.should_advance_to_next_stage(
        SimpleNamespace(notice_id="n"),
        4,
        evidence={"advance": True, "estimated_actual_profit": 50000},
    )
    assert result["advance"] is False
    assert "stage5_requires_explicit_action" in result["reasons"]


def test_model_router_tiers():
    assert router.model_for_tier(router.ModelTier.CHEAP) == "gpt-5.6-luna"
    assert router.model_for_tier(router.ModelTier.STANDARD) == "gpt-5.6-terra"
    assert router.model_for_tier(router.ModelTier.PREMIUM) == "gpt-5.6-sol"
    assert router.resolve_model(stage=1) == "gpt-5.6-luna"
    assert router.resolve_model(stage=4) == "gpt-5.6-terra"
    assert router.resolve_model(stage=5) == "gpt-5.6-sol"
