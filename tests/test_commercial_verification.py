"""Focused tests — commercial verification + execution control."""

from __future__ import annotations

from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, MODE_OPERATIONAL, set_operating_mode
from commercial_verification_engine import (
    authorize_verification_spend,
    build_commercial_verification_bundle,
    process_verification_result_and_recalc,
)
from commercial_verification_plan import build_commercial_verification_plan
from commercial_result_pipeline import (
    apply_supplier_quote_to_economics,
    compare_financing_offers,
    compare_supplier_quotes,
    evaluate_commercial_freshness,
    ingest_verification_result,
    invalidate_commercial_evidence,
)
from commercial_readiness_gate import evaluate_commercial_readiness, evaluate_execution_gate
from commercial_verification_constants import (
    AUTH_BINDING_QUOTE,
    AUTH_MARKETING,
    CV_FAILED,
    CV_PASSED,
    EG_NOT_READY,
    EXEC_BLOCKED_MODE,
    FUND_NOT_REQUIRED,
    PRI_DEAL_KILLER,
)
from external_action_control import (
    DryRunExecutionAdapter,
    execute_external_action,
    get_external_action_store,
    reset_external_action_store,
)
from financing_verification import (
    assess_financing_compatibility,
    build_transaction_funding_requirement,
    evaluate_funding_gate,
    financier_profile,
    financing_verification_script,
    load_financier_profiles_from_underwriting_artifact,
)


def test_verification_plan_deal_killers():
    plan = build_commercial_verification_plan(
        opportunity_id="D1",
        acquisition_estimate=112000,
        acquisition_confidence="DEFENSIBLE_ESTIMATE",
        max_acquisition=120000,
        financing_required=True,
        financing_amount=112000,
        product_description="PRODUCT X",
        quantity=60,
        unit_target=1525,
        unit_ceiling=1640,
        delivery_by="2026-11-01",
    )
    assert plan["deal_killer_count"] >= 1
    assert any(i["priority"] == PRI_DEAL_KILLER for i in plan["active_items"])
    assert plan["supplier_target"]["bands"]["TARGET"] == 1525
    assert plan["outreach_performed"] is False


def test_funding_requirement_and_script():
    req = build_transaction_funding_requirement(
        acquisition_cost=112000,
        freight=3000,
        supplier_payment_timing="before_shipment",
        government_payment_terms="Net 30",
        bid_revenue=150000,
        estimated_cash_conversion_days=45,
    )
    assert req["financing_amount_required"] == 115000
    assert req["maximum_financing_cost"] is not None
    assert req["models_supplier_payment_timing"] is True
    script = financing_verification_script(funding_requirement=req, award_value=150000, agency="DoD")
    assert script["sent"] is False
    assert "minimum FICO" in script["script"]


def test_pg_vs_fico_and_unknown():
    p = financier_profile(
        financier="Test Fund",
        personal_guarantee="REQUIRED",
        personal_credit_pull="NONE",
        minimum_fico=None,
        personal_credit_dependency="NONE",
        underwriting_model="UNDERWRITING_INTERNAL",
        broker_vs_direct="DIRECT",
    )
    assert p["personal_guarantee"] == "REQUIRED"
    assert p["minimum_fico"] is None  # UNKNOWN
    assert p["fabricated_policies"] is False
    req = build_transaction_funding_requirement(acquisition_cost=50000, freight=1000, bid_revenue=70000)
    compat = assess_financing_compatibility(profile=p, funding_requirement=req, pg_acceptable=True)
    assert compat["pg_distinct_from_fico"] is True
    assert compat["predicts_approval"] is False

    bad = assess_financing_compatibility(
        profile=financier_profile(financier="Strict", minimum_fico=650, personal_guarantee="UNKNOWN"),
        funding_requirement=req,
        operator_fico=480,
    )
    assert bad["state"] == "INCOMPATIBLE"


def test_funding_gate_separate_from_economics():
    req = build_transaction_funding_requirement(acquisition_cost=100000, freight=2000, bid_revenue=130000)
    gate = evaluate_funding_gate(
        funding_requirement=req,
        compatibility=[{"state": "POTENTIALLY_COMPATIBLE"}],
        economically_attractive=True,
    )
    assert gate["economically_attractive_but_funding_unverified"] is True
    assert gate["separate_from_economics_gate"] is True
    none = evaluate_funding_gate(funding_requirement={"funding_required": False})
    assert none["state"] == FUND_NOT_REQUIRED


def test_operator_auth_and_dev_mode_hard_block():
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    reset_external_action_store()
    store = get_external_action_store()
    action = store.propose(
        opportunity_id="D1",
        action_type="REQUEST_SUPPLIER_QUOTE",
        purpose="quote",
        target="supplier@example.com",
        information_to_send="Need quote",
    )
    # Authorize but still blocked by mode
    store.authorize(action["action_id"], operator_id="operator")
    result = execute_external_action(action["action_id"])
    assert result["execution_state"] == EXEC_BLOCKED_MODE
    assert result["network_transmitted"] is False
    assert result["quote_requests_made"] == 0
    # Idempotent
    result2 = execute_external_action(action["action_id"])
    assert result2["execution_state"] == EXEC_BLOCKED_MODE
    assert any(e["event"] == "BLOCKED" for e in store.audit())


def test_dry_run_shows_would_have_sent():
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    reset_external_action_store()
    store = get_external_action_store()
    a = store.propose(
        opportunity_id="D1",
        action_type="REQUEST_FINANCING_INDICATION",
        purpose="financing",
        information_to_send="Can you fund?",
    )
    store.authorize(a["action_id"])
    r = DryRunExecutionAdapter().execute(store.get(a["action_id"]), store)
    assert r["would_have_sent"]["information_to_send"] == "Can you fund?"
    assert r["emails_sent"] == 0


def test_result_ingestion_authority_and_recalc():
    marketing = ingest_verification_result(
        opportunity_id="D1",
        result_type="supplier_quote",
        payload={"extended_price": 100},
        authority=AUTH_MARKETING,
    )
    assert marketing["usable_as_binding"] is False
    binding = ingest_verification_result(
        opportunity_id="D1",
        result_type="supplier_quote",
        payload={"extended_price": 118500},
        authority=AUTH_BINDING_QUOTE,
        expires_at="2026-12-01T00:00:00+00:00",
    )
    assert binding["usable_as_binding"] is True
    recalc = apply_supplier_quote_to_economics(
        bid_revenue=150000,
        quoted_acquisition=118500,
        freight=3000,
        financing=4000,
        hard_ceiling=112000,
    )
    assert recalc["verification_outcome"] == CV_FAILED
    assert "ceiling" in recalc["reasons"][0].lower() or "exceeds" in recalc["reasons"][0].lower()

    ok = apply_supplier_quote_to_economics(
        bid_revenue=150000,
        quoted_acquisition=110000,
        freight=3000,
        financing=4000,
        hard_ceiling=120000,
    )
    assert ok["verification_outcome"] == CV_PASSED
    assert ok["stale_profit_preserved"] is False


def test_quote_and_financing_comparison():
    qc = compare_supplier_quotes(
        [
            {"vendor": "A", "unit_price": 100, "quantity": 10, "freight": 50, "compliance_ok": False, "delivery_ok": True},
            {"vendor": "B", "unit_price": 110, "quantity": 10, "freight": 50, "compliance_ok": True, "delivery_ok": True},
        ]
    )
    assert qc["preferred"]["vendor"] == "B"
    assert qc["selects_cheapest_noncompliant"] is False

    fc = compare_financing_offers(
        [
            {"financier": "LowRate", "expected_total_financing_cost": 2000, "rate": 0.01},
            {"financier": "BetterProfit", "expected_total_financing_cost": 5000, "rate": 0.05},
        ],
        bid_revenue=100000,
        acquisition=70000,
        freight=2000,
    )
    assert fc["considers_more_than_rate"] is True
    assert fc["preferred"]["financier"] == "LowRate"  # lower fin cost → higher profit


def test_freshness_and_invalidation():
    fresh = evaluate_commercial_freshness(expires_at="2099-01-01T00:00:00+00:00", verified_at="2026-09-01T00:00:00+00:00")
    assert fresh["state"] == "CURRENT"
    expired = evaluate_commercial_freshness(expires_at="2020-01-01T00:00:00+00:00")
    assert expired["blocks_final_readiness"] is True
    inv = invalidate_commercial_evidence(
        {"conclusions": {"supplier_quote_economics": 1, "freight": 1, "unrelated_naics": {"ok": True}}},
        change_type="QUANTITY_CHANGE",
    )
    assert inv["conclusions"]["supplier_quote_economics"]["status"] == "INVALIDATED"
    assert "unrelated_naics" in inv["last_invalidation"]["preserved"]


def test_readiness_and_execution_gate():
    r = evaluate_commercial_readiness(plan_built=True, actions_pending_auth=True)
    assert r["state"] == "OPERATOR_AUTHORIZATION_REQUIRED"
    eg = evaluate_execution_gate(
        funding_required=True,
        funding_feasible=False,
        profit_floor_preserved=True,
        profit_after_financing_ok=False,
    )
    assert eg["state"] == EG_NOT_READY
    assert "funding_not_feasible" in eg["blockers"]
    assert eg["checks_funding_when_required"] is True
    assert eg["submitted"] is False


def test_cost_governor_voi():
    denied = authorize_verification_spend(
        opportunity_id="D1",
        question="fluff background",
        could_change=False,
    )
    assert denied["authorized"] is False


def test_full_bundle_and_profiles():
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    reset_external_action_store()
    bundle = build_commercial_verification_bundle(
        opportunity_id="IT-1",
        product_description="Dell Latitude 5540",
        quantity=40,
        acquisition_estimate=112000,
        acquisition_confidence="DEFENSIBLE_ESTIMATE",
        freight_estimate=2800,
        financing_estimate=4000,
        bid_revenue=150000,
        delivery_by="2026-11-15",
        economically_attractive=True,
        agency="DoD",
        supplier="CDW",
        operator_fico=480,
        operator_cash_available=0,
    )
    assert bundle["verification_plan"]["deal_killer_count"] >= 1
    assert bundle["funding_requirement"]["financing_amount_required"] is not None
    assert bundle["financier_profiles"]
    assert bundle["fabricated_financier_policies"] is False
    assert bundle["outreach"]["quote_requests"] == 0
    assert bundle["funding_gate"]["economically_attractive_but_funding_unverified"] is True
    # Dry-run all proposed
    for a in bundle["external_actions"]:
        get_external_action_store().authorize(a["action_id"])
        r = execute_external_action(a["action_id"])
        assert r["network_transmitted"] is False

    result = ingest_verification_result(
        opportunity_id="IT-1",
        result_type="supplier_quote",
        payload={"extended_price": 118500},
        authority=AUTH_BINDING_QUOTE,
    )
    updated = process_verification_result_and_recalc(
        bundle, result=result, bid_revenue=150000, freight=2800, financing=4000
    )
    assert updated["verification_outcome"] in {CV_FAILED, CV_PASSED}


def test_load_profiles_no_fabricate():
    profiles = load_financier_profiles_from_underwriting_artifact()
    assert len(profiles) >= 1
    for p in profiles:
        assert p["fabricated_policies"] is False
        assert p["unknown_preserved"] is True


def test_api_commercial_routes(monkeypatch):
    monkeypatch.setenv("APP_EMAIL", "")
    monkeypatch.setenv("APP_PASSWORD", "")
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    reset_external_action_store()
    from app import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    r = client.post(
        "/api/opportunities/CV-API-1/build-verification-plan",
        json={
            "acquisition_estimate": 50000,
            "acquisition_confidence": "DEFENSIBLE_ESTIMATE",
            "freight_estimate": 2000,
            "bid_revenue": 70000,
            "economically_attractive": True,
            "product_description": "Widget",
            "quantity": 10,
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["kind"] == "CommercialVerificationBundle"
    assert client.get("/api/opportunities/CV-API-1/commercial-verification").status_code == 200
    assert client.get("/api/opportunities/CV-API-1/verification-items").status_code == 200
    assert client.get("/api/opportunities/CV-API-1/funding-requirement").status_code == 200
    assert client.get("/api/opportunities/CV-API-1/financing-compatibility").status_code == 200
    assert client.get("/api/opportunities/CV-API-1/external-actions").status_code == 200
    assert client.get("/api/opportunities/CV-API-1/commercial-readiness").status_code == 200
    assert client.get("/api/opportunities/CV-API-1/execution-gate").status_code == 200
    aid = data["external_actions"][0]["action_id"]
    assert client.post(f"/api/external-actions/{aid}/authorize", json={"operator_id": "op"}).status_code == 200
    dry = client.post(f"/api/external-actions/{aid}/dry-run")
    assert dry.status_code == 200
    assert dry.json()["execution_state"] == EXEC_BLOCKED_MODE
