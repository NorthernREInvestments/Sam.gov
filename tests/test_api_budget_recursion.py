"""Regression: usage snapshot must not recurse via can_screen."""

from __future__ import annotations

import api_budget


def test_get_usage_snapshot_no_recursion(monkeypatch):
    monkeypatch.setattr(api_budget, "_usage_counts", lambda: {
        "sam_used_today": 1,
        "sam_pdf_downloads_today": 0,
        "screens_used_today": 0,
    })
    monkeypatch.setattr(api_budget, "sam_daily_limit", lambda: 100)
    monkeypatch.setattr(api_budget, "sam_pdf_download_limit", lambda: 0)
    monkeypatch.setattr(api_budget, "screen_daily_limit", lambda: 50)
    monkeypatch.setattr(api_budget, "intake_on_sync_enabled", lambda: True)
    monkeypatch.setattr(api_budget, "auto_screen_on_startup", lambda: False)
    monkeypatch.setattr(api_budget, "auto_screen_on_contract_detail", lambda: False)
    monkeypatch.setattr(api_budget, "enrich_on_sync_limit", lambda: 0)
    monkeypatch.setattr(api_budget, "intake_per_sync_limit", lambda: None)
    monkeypatch.setattr(api_budget, "scheduled_naics_per_sync", lambda: 1)
    monkeypatch.setattr(api_budget, "attachment_enrich_per_sync_limit", lambda: None)
    monkeypatch.setattr(api_budget, "attachment_enrich_on_list_limit", lambda: 0)
    monkeypatch.setattr(api_budget, "scheduled_sync_attachments_only", lambda: False)
    monkeypatch.setattr(api_budget, "scheduled_sync_attachments_only_from", lambda: None)
    monkeypatch.setattr(api_budget, "scheduled_sync_attachments_only_until", lambda: None)

    # If recursion returns, this blows the stack
    snap = api_budget.get_usage_snapshot()
    assert snap["sam_remaining"] == 99
    assert "ai_intake_allowed" in snap
    assert api_budget.can_spend_sam(1) is True
