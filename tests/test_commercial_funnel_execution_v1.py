"""Regression: commercial funnel economics honesty + selection gates."""

from __future__ import annotations

from decimal import Decimal

from buyer_intelligence.commercial_funnel_execution_v1 import assess_economics, AUTO_SEND


def test_auto_send_hard_false():
    assert AUTO_SEND is False


def test_assess_economics_no_fabricated_finance():
    row = {
        "historical_unit_price": "10.00",
        "public_unit_price": "6.00",
        "primary_quantity": "5",
        "actual_quote_unit_price": None,
    }
    out = assess_economics(row)
    assert out["financeable"] is False
    assert out["econ_state"] == "PROFITABLE_BUT_QUOTE_MISSING"
    assert Decimal(out["estimated_gross_spread"]) == Decimal("20.00")


def test_assess_economics_negative_margin():
    row = {
        "historical_unit_price": "5.00",
        "public_unit_price": "8.00",
        "primary_quantity": "1",
    }
    out = assess_economics(row)
    assert out["econ_state"] == "NEGATIVE_OR_INSUFFICIENT_MARGIN"
    assert out["financeable"] is False


def test_retention_policy_no_auto_discard():
    from buyer_intelligence.operational_retention_v1 import retention_policy

    p = retention_policy()
    assert p["auto_discard_enabled"] is False
    assert "Never bulk-delete L23" in p["hard_rules"][0]


def test_local_env_without_creds_is_distinct_blocker(monkeypatch):
    """CFE must not confuse local Cursor runs with missing Railway production creds."""
    import buyer_intelligence.commercial_funnel_execution_v1 as m

    for k in (
        "BIDNET_USERNAME",
        "BIDNET_PASSWORD",
        "BIDNET_USER",
        "BIDNET_PASS",
        "BIDNET_EMAIL",
        "BIDNETDIRECT_USERNAME",
        "BIDNETDIRECT_PASSWORD",
        "RAILWAY_ENVIRONMENT",
        "RAILWAY_SERVICE_NAME",
        "RAILWAY_VOLUME_MOUNT_PATH",
    ):
        monkeypatch.delenv(k, raising=False)

    monkeypatch.setattr(
        "bidnet_auth.config._portal_store_credentials",
        lambda: ("", "", None),
    )
    status, client = m._open_bidnet_auth_client()
    assert client is None
    assert status["external_blocker"] == "LOCAL_ENV_WITHOUT_PRODUCTION_CREDENTIALS"
    assert status["credential_source"] == "none"


def test_expected_railway_bidnet_env_names():
    """Production Railway uses canonical names already supported by load_bidnet_auth_config."""
    from bidnet_auth.runtime_wiring import env_presence_audit

    keys = env_presence_audit()
    for required in ("BIDNET_USERNAME", "BIDNET_PASSWORD", "BIDNET_AUTH_ENABLED"):
        assert required in keys
