"""Tests for OpenGov public-first candidates + Euna portal enumeration."""

from __future__ import annotations


def test_opengov_public_candidates():
    from opengov_discovery.public_harvest import candidate_public_urls

    urls = candidate_public_urls("https://procurement.opengov.com/portal/sanantonio")
    assert any("/portal/embed/sanantonio/project-list" in u for u in urls)
    assert any(u.rstrip("/").endswith("/portal/sanantonio/projects") for u in urls)


def test_opengov_states_include_working_public():
    from opengov_auth.states import SUCCESS_STATUSES, WORKING_PUBLIC

    assert WORKING_PUBLIC in SUCCESS_STATUSES


def test_euna_portals_only_bonfirehub():
    from euna_discovery.portals import known_euna_portals

    portals = known_euna_portals()
    assert portals, "expected bonfirehub portals from catalog"
    assert all("bonfirehub.com" in str(p.get("portal_url") or "").lower() for p in portals)


def test_euna_config_loads():
    from euna_auth.config import load_euna_auth_config

    cfg = load_euna_auth_config()
    assert cfg.discovery_scope == "BROAD_PRODUCT_RESALE"
    assert "bonfirehub.com" in cfg.verify_url


def test_opengov_public_client_import():
    from opengov_discovery import OpenGovPublicDiscoveryClient

    assert OpenGovPublicDiscoveryClient is not None


def test_bidnet_partitioned_import():
    from bidnet_discovery import run_bidnet_partitioned_harvest

    assert callable(run_bidnet_partitioned_harvest)


def test_euna_central_candidates():
    from euna_discovery.supplier_network import CENTRAL_SEARCH_CANDIDATES

    assert any("vendor.bonfirehub.com" in u for u in CENTRAL_SEARCH_CANDIDATES)


def test_euna_login_url_starts_on_vendor_network():
    from euna_auth.config import DEFAULT_LOGIN_URL, load_euna_auth_config

    assert "vendor.bonfirehub.com" in DEFAULT_LOGIN_URL
    cfg = load_euna_auth_config()
    hy = cfg.credential_hygiene()
    assert "username_present" in hy
    assert "password_length" in hy


def test_euna_classify_bad_credentials():
    from euna_auth.classify import classify_login_failure
    from euna_auth.states import BAD_CREDENTIALS

    out = classify_login_failure(
        html='<input type="password"> Invalid email or password',
        url="https://account.bonfirehub.com/login?flow=abc",
        visible_text="Invalid email or password",
        had_password_field=True,
        had_email_field=True,
        flow_present=True,
    )
    assert out["reason"] == BAD_CREDENTIALS


def test_euna_normalize_secret_strips_quotes():
    from euna_auth.config import _normalize_secret

    assert _normalize_secret('  "abc"  ') == "abc"
    assert _normalize_secret("  'xyz' ") == "xyz"


def test_euna_verify_url_avoids_opportunities_cf():
    from euna_auth.config import DEFAULT_VERIFY_URL

    assert "/agencies" in DEFAULT_VERIFY_URL
    assert "/opportunities" not in DEFAULT_VERIFY_URL


def test_euna_detect_supplier_network_not_enabled():
    from euna_auth.classify import detect_account_activation
    from euna_auth.states import SUPPLIER_NETWORK_NOT_ENABLED

    state = detect_account_activation(
        "",
        visible_text="No Agencies Available. Sign up for Euna Supplier Network and browse over 2,000 agency opportunities.",
        url="https://vendor.bonfirehub.com/agencies",
    )
    assert state == SUPPLIER_NETWORK_NOT_ENABLED


def test_euna_national_discovery_disabled_by_default(monkeypatch):
    monkeypatch.delenv("EUNA_NATIONAL_DISCOVERY_ENABLED", raising=False)
    monkeypatch.delenv("EUNA_ENABLED_STATES", raising=False)
    from euna_auth.config import load_euna_auth_config
    from euna_auth.states import OPTIONAL_TARGETED_SOURCE, PAID_OPTIONAL

    cfg = load_euna_auth_config()
    assert cfg.national_discovery_enabled is False
    assert cfg.enabled_states == ()
    assert cfg.should_run_scheduled_discovery is False
    assert cfg.source_role == OPTIONAL_TARGETED_SOURCE
    assert cfg.coverage_category == PAID_OPTIONAL


def test_euna_enabled_states_triggers_targeted_run(monkeypatch):
    monkeypatch.setenv("EUNA_NATIONAL_DISCOVERY_ENABLED", "false")
    monkeypatch.setenv("EUNA_ENABLED_STATES", "NE, wy, CO")
    from euna_auth.config import load_euna_auth_config

    cfg = load_euna_auth_config()
    assert cfg.enabled_states == ("NE", "WY", "CO")
    assert cfg.should_run_scheduled_discovery is True


def test_euna_scheduled_skips_when_national_disabled(monkeypatch):
    monkeypatch.setenv("EUNA_NATIONAL_DISCOVERY_ENABLED", "false")
    monkeypatch.delenv("EUNA_ENABLED_STATES", raising=False)
    from euna_auth.scheduled import run_scheduled_euna_pipeline
    from euna_auth.states import PAID_OPTIONAL

    report = run_scheduled_euna_pipeline(run_id="test-skip")
    assert report["skipped"] is True
    assert report["status"] == PAID_OPTIONAL
    assert report.get("blocker") is None


def test_free_source_roadmap_euna_paid_optional():
    from free_source_roadmap import PAID_OPTIONAL, source_coverage_categories

    dash = source_coverage_categories()
    assert dash["primary_repair_target"] == "OpenGov"
    assert dash["next_free_source_after_opengov"] == "PublicPurchase"
    euna = next(r for r in dash["sources"] if r["source"] == "Euna/Bonfire")
    assert euna["category"] == PAID_OPTIONAL
    assert euna["counts_toward_national_health"] is False


def test_opengov_candidates_prefer_structured_and_embed():
    from opengov_discovery.public_harvest import candidate_public_urls

    urls = candidate_public_urls("https://procurement.opengov.com/portal/sanantonio")
    assert any("/portal/embed/sanantonio/project-list" in u for u in urls)
    # JSON / structured should appear before bare portal shell
    json_i = next(i for i, u in enumerate(urls) if "api.procurement.opengov.com" in u)
    portal_i = next(i for i, u in enumerate(urls) if u.endswith("/portal/sanantonio"))
    assert json_i < portal_i


def test_planetbids_audit_and_public_purchase_prep():
    from planetbids_audit import planetbids_audit_summary
    from public_purchase_prep import public_purchase_status

    pb = planetbids_audit_summary()
    assert pb["adapter_present"] is True
    assert pb["overall_state"] in {"WORKING", "PARTIAL", "BROKEN", "AUTH_REQUIRED", "NOT_IMPLEMENTED"}
    pp = public_purchase_status()
    assert pp["coverage_category"] == "FREE_PENDING_ACCOUNT"
