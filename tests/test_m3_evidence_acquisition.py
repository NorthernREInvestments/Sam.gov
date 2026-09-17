"""Focused tests — M3 evidence acquisition ladder + commercial/wholesale intelligence."""

from __future__ import annotations

from unittest.mock import patch

import pytest


def test_classify_evidence_failure_structured():
    from m3_evidence_acquisition import classify_evidence_failure

    row = {
        "canonical_id": "sol:x",
        "title": "Wildflower Seed",
        "detail_url": "https://example.gov/bid/1",
        "documents": [],
        "line_items": [],
        "evidence_recovery_attempts": [],
    }
    fail = classify_evidence_failure(row)
    assert fail["primary_reason"] == "PACKAGE_URL_FOUND_NOT_FETCHED"
    assert "WEB_SEARCH_NOT_TRIED" in fail["reasons"]


def test_classify_deal_type_construction_and_product():
    from m3_evidence_acquisition import classify_deal_type

    c = classify_deal_type(
        {"title": "FIRE STATION 74 DESIGN-BID-BUILD", "description": "construction of fire station"}
    )
    assert c["deal_type"] == "CONSTRUCTION"

    p = classify_deal_type(
        {"title": "Wildflower and Native Grass Seed", "description": "purchase of seed"},
        evidence_text="Quantity 500 bags of native grass seed for roadside",
    )
    assert p["deal_type"] in {"PRODUCT_RESALE", "UNKNOWN", "MIXED"}

    n = classify_deal_type({"title": "Contract Duration, Extension, & Renewal Policy"})
    assert n.get("not_transactional_solicitation") is True


def test_wholesale_not_auto_reject():
    from m3_commercial_intelligence import assess_commercial_economics, assess_competition_history

    econ = assess_commercial_economics(
        public_unit_price=85.0,
        government_historical_unit=100.0,
        target_acquisition_unit=63.0,
    )
    assert econ["state"] == "WHOLESALE_PRICE_VERIFICATION_REQUIRED"
    assert econ["public_price_signal"] == "PUBLIC_PRICE_UNWORKABLE"
    assert "UNKNOWN_wholesale_access_is_not_unavailable" in econ["notes"]

    hist = assess_competition_history(
        [{"winner": "Acme OEM", "vendor_type": "OEM"}, {"winner": "Acme OEM", "vendor_type": "OEM"}]
    )
    assert hist["auto_reject"] is False
    assert hist["signal"] in {"OEM_DOMINATED_HISTORY", "SPECIALIZED_DISTRIBUTION_LIKELY"}


def test_acquire_evidence_tier0_and_mock_http(monkeypatch):
    from m3_evidence_acquisition import acquire_evidence, TIER_1_DIRECT

    row = {
        "canonical_id": "sol:seed",
        "title": "Wildflower and Native Grass Seed",
        "solicitation_number": "645DOTRFB3046",
        "agency": "Iowa DOT",
        "source_id": "state_ia",
        "detail_url": "https://example.gov/seed",
        "description": "Seed purchase",
        "documents": [],
    }

    def fake_get(url, source_id="x"):
        html = (
            "<html>645DOTRFB3046 Wildflower Seed qty: 120 bag Specification "
            '<a href="https://example.gov/seed-spec.pdf">spec</a></html>'
        )
        return {
            "ok": True,
            "status_code": 200,
            "text": html,
            "url": url,
            "final_url": url,
            "auth_required": False,
            "bot_protected": False,
        }

    monkeypatch.setattr("m3_evidence_acquisition._http_get", fake_get)
    monkeypatch.setattr(
        "m3_evidence_acquisition._tier4_openai_web",
        lambda *a, **k: {"tier": "TIER_4_OPENAI_WEB", "items": [], "attempted": False, "reason": "skip_test"},
    )
    out = acquire_evidence(row, allow_paid=False, max_tier=TIER_1_DIRECT)
    assert out["result"]["improved"] is True
    assert "TIER_1_DIRECT" in out["result"]["tiers_attempted"]
    assert out["row"].get("evidence_text_excerpt") or out["row"].get("documents")


def test_credentials_roundtrip(monkeypatch, tmp_path):
    from m3_source_credentials import list_credentials, upsert_credential

    store = {"portals": {}}

    monkeypatch.setattr("m3_source_credentials._load", lambda: {"portals": dict(store["portals"])})
    monkeypatch.setattr(
        "m3_source_credentials._save",
        lambda state: store.update({"portals": dict(state.get("portals") or {})}),
    )
    out = upsert_credential("state_ia", {"username": "ops@example.com", "password": "secret", "login_url": "https://x"})
    assert out["ok"] is True
    listed = list_credentials(include_secrets=False)
    assert listed["portals"]["state_ia"]["password_set"] is True
    assert listed["portals"]["state_ia"].get("password") == "********"


def test_source_access_queue_ranks():
    from m3_source_access import source_access_queue

    rows = [
        {
            "source_id": "state_mt",
            "source_access_state": "REGISTRATION_REQUIRED",
            "title": "Seed",
            "deal_type": "PRODUCT_RESALE",
            "detail_url": "https://mt.example/1",
        },
        {
            "source_id": "state_mt",
            "source_access_state": "AUTH_REQUIRED",
            "title": "Blades",
            "product_category": "PARTS",
        },
        {
            "source_id": "other",
            "source_access_state": "AUTH_REQUIRED",
            "title": "Consulting",
            "deal_type": "SERVICE",
        },
    ]
    q = source_access_queue(rows)
    assert q["count"] == 2
    assert q["portals"][0]["portal"] == "state_mt"
    assert q["portals"][0]["blocked_opportunities"] == 2


def test_api_evidence_status(monkeypatch):
    monkeypatch.setattr("app.auth_enabled", lambda: False)
    monkeypatch.setattr("auth.auth_enabled", lambda: False)
    from fastapi.testclient import TestClient
    from app import app

    client = TestClient(app)
    with patch("m3_pipeline_store.M3PipelineStore.all", return_value=[]):
        with patch("m3_discovery_service.restore_pipeline_store_from_db", return_value=False):
            r = client.get("/api/m3/evidence/status")
    assert r.status_code == 200
    assert r.json()["kind"] == "M3EvidenceAccessSummary"


def test_ui_has_evidence_access():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "static" / "m3-mobile.js").read_text(encoding="utf-8")
    assert 'id="m3-evidence-status"' in html
    assert "/api/m3/evidence/status" in js
    assert "/api/m3/evidence/acquire" in js
    assert "/api/m3/credentials" in js
    assert "EVIDENCE ACCESS" in html or "EVIDENCE ACCESS" in js
