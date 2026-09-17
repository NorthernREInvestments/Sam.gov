"""Focused tests — M3 procurement profile vs legacy acquisition separation."""

from __future__ import annotations

import json
from pathlib import Path

from first_pursuit_selection import score_first_pursuit_candidate
from m3_procurement_profile import (
    LEGACY_SERVICE_NAICS,
    assert_m3_isolated_from_legacy,
    load_m3_procurement_profile,
    m3_uses_legacy_naics,
    ranking_uses_legacy_acquisition_signals,
)
from product_discovery import PRODUCT_DISCOVERY_NAICS, build_product_sam_query
from pursuit_ranking import rank_components


def test_m3_procurement_profile_loads_and_isolates():
    profile = load_m3_procurement_profile()
    assert profile["kind"] == "M3ProcurementProfile"
    assert profile["isolated_from_legacy_acquisition"] is True
    assert profile["discovery"]["naics_is_primary_filter"] is False
    assert "equipment" in profile["target_product_types"]
    assert "IFB" in profile["procurement_types"]
    assert m3_uses_legacy_naics(profile) is False
    iso = assert_m3_isolated_from_legacy()
    assert iso["ok"] is True
    assert iso["overlap_legacy_service_naics"] == []
    assert iso["product_discovery_imports_legacy_naics"] is False


def test_product_discovery_never_includes_legacy_service_naics():
    for code in PRODUCT_DISCOVERY_NAICS:
        assert code not in LEGACY_SERVICE_NAICS
    q = build_product_sam_query(naics_codes=["561720", "423430", "238220"])
    assert "561720" not in q["naics_codes"]
    assert "238220" not in q["naics_codes"]
    assert "423430" in q["naics_codes"]
    assert "never" in q["note"].lower()


def test_ranking_excludes_legacy_acquisition_signals():
    opp = {
        "transactional_fit": True,
        "economic_potential": {"status": "UNKNOWN"},
        "cost_intelligence": {},
        "package_readiness": {},
        "deadline_viability": "OPEN",
        "suppliers": [],
    }
    ranked = rank_components(opp)
    assert ranked is not None
    assert ranking_uses_legacy_acquisition_signals(opp) is False
    dirty = {**opp, "seller_signals": 1, "business_age": 12}
    assert ranking_uses_legacy_acquisition_signals(dirty) is True


def test_first_pursuit_uses_product_repeatability_not_service_naics():
    row = {
        "canonical_id": "sep-1",
        "title": "Laptops",
        "agency": "School District",
        "package_access": "PUBLIC",
        "product_category": "IT_HARDWARE",
        "lifecycle": "FUNDING_VERIFICATION_REQUIRED",
        "deadline_evaluation": {"calendar_days_remaining": 14},
        "line_items": [{"description": "Laptop", "quantity": 10}],
        "transaction_economics": {"revenue": 100000},
        "bid_compliance": {"unresolved": []},
    }
    scored = score_first_pursuit_candidate(row)
    factor_names = {f["factor"] for f in scored["factors"]}
    assert "tangible_product_fit" in factor_names
    assert "service_business_acquisition_scores" not in factor_names
    assert "seller_signals" not in factor_names


def test_legacy_naics_catalog_preserved_but_unused_by_m3():
    from naics_labels import ALL_NAICS_CODES

    assert "561720" in ALL_NAICS_CODES
    profile = load_m3_procurement_profile()
    meta = set(profile["discovery"]["product_sector_naics_metadata"])
    assert "561720" not in meta


def test_frontend_m3_settings_separated():
    root = Path(__file__).resolve().parents[1] / "static"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "m3-mobile.js").read_text(encoding="utf-8")
    assert 'id="view-m3-settings"' in html
    assert "loadM3Settings" in js
    assert "/api/m3/procurement-profile" in js
    assert "loadSettingsPage()" not in js
    assert "m3-only-app" in js or "m3-only-app" in html


def test_api_procurement_profile(monkeypatch):
    monkeypatch.setenv("APP_EMAIL", "")
    monkeypatch.setenv("APP_PASSWORD", "")
    from app import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    r = client.get("/api/m3/procurement-profile")
    assert r.status_code == 200
    body = r.json()
    assert body["profile"]["isolated_from_legacy_acquisition"] is True
    assert body["isolation"]["ok"] is True
    sep = client.get("/api/m3/configuration-separation")
    assert sep.status_code == 200
    assert "legacy_service_naics" in sep.json()
    dash = client.get("/api/m3/mobile/dashboard")
    assert dash.status_code == 200
    assert dash.json()["procurement_profile"]["naics_is_primary_filter"] is False


def test_no_external_actions_from_separation():
    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, mode_snapshot, set_operating_mode

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    snap = mode_snapshot()
    assert snap["emails_sent"] == 0
    assert snap["bids_submitted"] == 0
    assert snap["network_transmitted"] is False


def test_audit_artifact_exists():
    path = Path(__file__).resolve().parents[1] / "artifacts" / "m3_configuration_separation_audit.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["kind"] == "M3ConfigurationSeparationAudit"
    assert data["migration_requirements"]["database_schema_change_required"] is False
