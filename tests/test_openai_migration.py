"""OpenAI migration tests — mocks only, no live API calls."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import api_budget
import openai_runtime


def test_no_anthropic_import_in_openai_client():
    import openai_client
    import inspect

    src = inspect.getsource(openai_client)
    assert "from anthropic" not in src
    assert "import anthropic" not in src
    assert "Anthropic(" not in src
    assert "messages.create" not in src


def test_web_search_off_by_default(monkeypatch):
    monkeypatch.delenv("OPENAI_WEB_SEARCH_ENABLED", raising=False)
    assert openai_runtime.openai_web_search_enabled_default() is False


def test_web_search_can_be_enabled(monkeypatch):
    monkeypatch.setenv("OPENAI_WEB_SEARCH_ENABLED", "true")
    assert openai_runtime.openai_web_search_enabled_default() is True


def test_create_response_uses_responses_api_and_logs(monkeypatch):
    calls = {}

    class FakeResponse:
        output_text = '{"pursue": true, "score": 8}'
        usage = SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15)

    class FakeClient:
        class responses:
            @staticmethod
            def create(**kwargs):
                calls["kwargs"] = kwargs
                return FakeResponse()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("OPENAI_MODEL_CHEAP", "gpt-5.6-luna")
    monkeypatch.setenv("OPENAI_WEB_SEARCH_ENABLED", "false")
    monkeypatch.setattr(openai_runtime, "get_openai_client", lambda: FakeClient())
    monkeypatch.setattr(openai_runtime, "log_ai_usage", lambda **kw: calls.setdefault("logs", []).append(kw))
    monkeypatch.setattr(openai_runtime, "get_cached_analysis", lambda *_: None)
    monkeypatch.setattr(openai_runtime, "put_cached_analysis", lambda *a, **k: None)
    monkeypatch.setattr(openai_runtime, "record_ai_dollar_spend", lambda *_: {})

    text = openai_runtime.create_response(
        task="screen_contract_text",
        instructions="sys",
        content=[{"type": "text", "text": "hello"}],
        max_output_tokens=1024,
        web_search=False,
        notice_id="n1",
        use_cache=False,
    )
    assert '"pursue"' in text
    assert "tools" not in calls["kwargs"]
    assert calls["kwargs"]["model"] == "gpt-5.6-luna"
    assert calls["logs"][0]["web_search"] is False
    assert calls["logs"][0]["success"] is True


def test_create_response_can_enable_web_search(monkeypatch):
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

    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.setattr(openai_runtime, "get_openai_client", lambda: FakeClient())
    monkeypatch.setattr(openai_runtime, "log_ai_usage", lambda **kw: None)
    monkeypatch.setattr(openai_runtime, "get_cached_analysis", lambda *_: None)
    monkeypatch.setattr(openai_runtime, "put_cached_analysis", lambda *a, **k: None)
    monkeypatch.setattr(openai_runtime, "record_ai_dollar_spend", lambda *_: {})

    openai_runtime.create_response(
        task="research",
        instructions=None,
        content=[{"type": "input_text", "text": "find suppliers"}],
        max_output_tokens=512,
        web_search=True,
        funnel_stage=3,
        use_cache=False,
    )
    assert captured.get("tools") == [{"type": "web_search"}]


def test_budget_gate_blocks_when_exhausted(monkeypatch):
    monkeypatch.setenv("AI_DAILY_SCREEN_BUDGET", "2")
    monkeypatch.delenv("ANTHROPIC_DAILY_SCREEN_BUDGET", raising=False)
    # can_screen reads _usage_counts (not get_usage_snapshot) after recursion-safe refactor
    monkeypatch.setattr(
        api_budget,
        "_usage_counts",
        lambda: {
            "sam_used_today": 0,
            "sam_pdf_downloads_today": 0,
            "screens_used_today": 2,
        },
    )
    assert api_budget.can_screen() is False


def test_budget_default_not_unlimited(monkeypatch):
    monkeypatch.delenv("AI_DAILY_SCREEN_BUDGET", raising=False)
    monkeypatch.delenv("ANTHROPIC_DAILY_SCREEN_BUDGET", raising=False)
    assert api_budget.screen_daily_limit() == 25


def test_intake_on_sync_defaults_off(monkeypatch):
    monkeypatch.delenv("INTAKE_ON_SYNC", raising=False)
    assert api_budget.intake_on_sync_enabled() is False
    assert api_budget.ai_intake_allowed() is False


def test_auto_screen_on_detail_defaults_off(monkeypatch):
    monkeypatch.delenv("AUTO_SCREEN_ON_CONTRACT_DETAIL", raising=False)
    assert api_budget.auto_screen_on_contract_detail() is False


def test_screen_contract_text_structure(monkeypatch):
    from openai_client import screen_contract_text

    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.setattr(
        "ai_stage1.run_stage1_triage",
        lambda opportunity, stage0=None, automatic=False: {
            "category": "SUBCONTRACTABLE_SERVICE",
            "buying": "janitorial",
            "advance": True,
            "reseller_fit": "MEDIUM",
            "score": 7,
            "text_score": 7,
            "pursue": True,
            "screening_stage": "text",
            "pdfs_sent_to_claude": 0,
            "reason_code": "FIT",
        },
    )
    contract = SimpleNamespace(
        title="Test",
        agency="Agency",
        location="City, ST",
        due_date=None,
        naics_code="561720",
        description="Janitorial services",
        set_aside="Total Small Business",
        link="https://example.com",
        sam_raw={},
        notice_id="abc",
        analysis={},
        status="new",
    )
    analysis = screen_contract_text(contract)
    assert analysis["screening_stage"] == "text"
    assert analysis["pdfs_sent_to_claude"] == 0  # legacy key preserved
    assert analysis.get("score") == 7 or analysis.get("text_score") is not None


def test_create_response_does_not_retry_on_failure(monkeypatch):
    attempts = {"n": 0}

    class FakeClient:
        class responses:
            @staticmethod
            def create(**kwargs):
                attempts["n"] += 1
                raise RuntimeError("insufficient_quota")

    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.setattr(openai_runtime, "get_openai_client", lambda: FakeClient())
    monkeypatch.setattr(openai_runtime, "log_ai_usage", lambda **kw: None)
    monkeypatch.setattr(openai_runtime, "get_cached_analysis", lambda *_: None)
    monkeypatch.setattr(openai_runtime, "put_cached_analysis", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="insufficient_quota"):
        openai_runtime.create_response(
            task="fail_once",
            instructions=None,
            content=[{"type": "input_text", "text": "x"}],
            max_output_tokens=16,
            web_search=False,
            funnel_stage=2,
            use_cache=False,
        )
    assert attempts["n"] == 1


def test_is_ai_api_blocked_detects_openai_quota():
    assert api_budget.is_ai_api_blocked(RuntimeError("Error code: 429 - insufficient_quota"))
    assert api_budget.is_ai_api_blocked(RuntimeError("Incorrect API key provided"))


def test_ai_intake_cannot_bypass_when_intake_on_sync_false(monkeypatch):
    monkeypatch.setenv("INTAKE_ON_SYNC", "false")
    monkeypatch.setenv("AI_DAILY_SCREEN_BUDGET", "100")
    monkeypatch.setattr(api_budget, "get_usage_snapshot", lambda: {
        "screens_remaining": 100,
        "screen_daily_limit": 100,
        "screens_unlimited": False,
    })
    assert api_budget.ai_intake_allowed() is False


def test_claude_shim_reexports_openai_client():
    import claude_client
    import openai_client

    assert claude_client.screen_contract_text is openai_client.screen_contract_text
