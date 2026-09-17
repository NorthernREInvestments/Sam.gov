"""Focused tests — M3-only production runtime retirement + health."""

from __future__ import annotations

from pathlib import Path

from legacy_runtime import is_m3_only_production, legacy_retirement_snapshot
from m3_procurement_profile import LEGACY_SERVICE_NAICS, assert_m3_isolated_from_legacy, load_m3_procurement_profile
from operating_mode import (
    MODE_DEVELOPMENT_NO_OUTREACH,
    is_controlled_verification,
    is_development_no_outreach,
    set_operating_mode,
)
from product_discovery import PRODUCT_DISCOVERY_NAICS


def test_m3_only_production_default():
    assert is_m3_only_production() is True
    snap = legacy_retirement_snapshot()
    assert snap["active_application"] == "M3"
    assert "LEGACY_ONLY_SAFE_TO_RETIRE" in snap["by_class"]
    assert "M3_REQUIRED_KEEP" in snap["by_class"]


def test_entry_point_is_m3_only():
    html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")
    assert 'class="m3-only-app"' in html or "m3-only-app" in html
    assert 'id="view-m3-home"' in html
    assert "legacy-retired-nav" in html
    assert "GovCon product-resale" in html or "M3" in html
    # Primary home is not hidden at rest in markup (M3 entry)
    assert '<div id="view-m3-home">' in html
    assert 'id="view-dashboard" hidden' in html
    js = (Path(__file__).resolve().parents[1] / "static" / "m3-mobile.js").read_text(encoding="utf-8")
    assert 'showM3View("home")' in js
    assert "m3-only-app" in js
    gos = (Path(__file__).resolve().parents[1] / "static" / "gos-app.js").read_text(encoding="utf-8")
    assert "m3-only-app" in gos


def test_nav_includes_core_m3_tabs_not_legacy_gos():
    html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")
    nav = html.split('id="m3-bottom-nav"')[1].split("</nav>")[0]
    assert 'data-m3-view="home"' in nav
    assert 'data-m3-view="opportunities"' in nav
    assert 'data-m3-view="actions"' in nav
    assert 'data-m3-view="sources"' in nav
    assert 'data-m3-view="settings"' in nav
    assert "Active Deals" not in nav
    assert "Contracts" not in nav


def test_naics_metadata_preserved_legacy_targets_gone():
    profile = load_m3_procurement_profile()
    assert profile["discovery"]["naics_is_primary_filter"] is False
    for code in PRODUCT_DISCOVERY_NAICS:
        assert code not in LEGACY_SERVICE_NAICS
    assert assert_m3_isolated_from_legacy()["ok"] is True


def test_development_no_outreach_default_and_controlled_explicit():
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    assert is_development_no_outreach() is True
    assert is_controlled_verification() is False


def test_health_and_m3_health_endpoints(monkeypatch):
    monkeypatch.setenv("APP_EMAIL", "")
    monkeypatch.setenv("APP_PASSWORD", "")
    from app import APP_BUILD_VERSION, app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    h = client.get("/api/health")
    assert h.status_code == 200
    body = h.json()
    assert body["build_version"] == APP_BUILD_VERSION
    assert body["application"] == "M3"
    assert "20260917-m3-package1b" in body["build_version"]

    m = client.get("/api/m3/health")
    assert m.status_code == 200
    mb = m.json()
    assert mb["m3_application_loaded"] is True
    assert mb["procurement_profile_available"] is True
    assert mb["pipeline_store_available"] is True
    assert mb["DEVELOPMENT_NO_OUTREACH"] is True
    assert mb["controlled_verification_active"] is False
    assert mb["external_action_safety"]["network_transmitted"] is False


def test_cache_bust_matches_build():
    html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")
    assert "20260917-m3-package1b" in html
    assert 'meta name="m3-build" content="20260917-m3-package1b"' in html
    assert "/m3-mobile.js?v=20260917-m3-package1b" in html


def test_no_external_action_regression():
    from operating_mode import mode_snapshot

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    snap = mode_snapshot()
    assert snap["emails_sent"] == snap["bids_submitted"] == 0
    assert snap["network_transmitted"] is False
