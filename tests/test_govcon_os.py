"""GovCon OS — lifecycle, funding engine, generic commercial workspace."""

from __future__ import annotations

from datetime import date

from commercial_execution import seed_commercial_artifacts
from deal_lifecycle import LIFECYCLE_SOURCING, map_internal_to_lifecycle, pipeline_bucket
from funding_engine import (
    FUNDING_BLOCKED,
    FUNDING_INSUFFICIENT_EVIDENCE,
    PHASE_PRE_BID,
    build_funding_plan_snapshot,
    evaluate_pre_bid_funding_viability,
    funding_economics_scenarios,
)
from funding_research import build_funding_research_plan, questions_for_gaps
from os_service import build_global_today_queue, build_os_dashboard


SYNTHETIC_BOM = [
    {"component": "base_system", "value": "Cisco Catalyst 9300 Switch", "quantity": 20, "status": "VERIFIED"},
    {"component": "part_number", "value": "C9300-48P", "quantity": 20, "status": "VERIFIED"},
    {
        "component": "storage_drive",
        "value": "480GB SSD",
        "quantity": 20,
        "quantity_per_server": 1,
        "status": "VERIFIED",
    },
]


def test_generic_commercial_workspace_non_dell_non_199():
    seed = seed_commercial_artifacts(
        bom=SYNTHETIC_BOM,
        suppliers=[{"id": 99, "name": "Authorized Reseller Inc.", "contact_verified": False}],
        fob_draft=None,
        fob_safe_to_ask=False,
        due_date=date(2026, 12, 1),
        solicitation_number="SYNTH-2026-001",
        agency="DOI",
        destination="Denver CO",
        delivery_requirement="30 days ARO",
        quantity=20,
        product_summary="Cisco Catalyst 9300 switches",
    )
    pkt = seed["supplier_rfq_packet"]
    assert pkt["product"]["name"] == "Cisco Catalyst 9300 Switch"
    assert pkt["product"]["part_number"] == "C9300-48P"
    assert "Dell" not in str(pkt["exact_configuration"])
    assert pkt["product"]["quantity"] == 20
    assert seed["LIVE_API_REQUESTS"] == 0


def test_production_deal_workspace_snapshot_has_no_product_hardcoding():
    import inspect
    import deal_workspace

    src = inspect.getsource(deal_workspace.build_workspace_snapshot)
    for token in ("PowerEdge", "210-BNZH", "Alexandria", "47QACA26Q0439", "Dell Federal"):
        assert token not in src


def test_lifecycle_maps_sourcing_for_needs_quotes():
    lc = map_internal_to_lifecycle(
        pipeline_stage="needs_research",
        commercial_status={"label": "NEEDS_SUPPLIER_QUOTES", "has_valid_quote": False},
    )
    assert lc in {LIFECYCLE_SOURCING, "RESEARCHING", "QUALIFYING"}


def test_funding_pg_requires_operator_review_not_auto_block():
    r = evaluate_pre_bid_funding_viability(
        pursuits=[{"pg_required": True, "personal_credit_required": False, "borrower_cash_required": False}],
    )
    assert r["phase"] == PHASE_PRE_BID
    assert r["status"] != FUNDING_BLOCKED
    assert any("PG" in u for u in (r.get("unknowns") or []))


def test_funding_unknown_is_insufficient_not_auto_fail():
    r = evaluate_pre_bid_funding_viability(pursuits=[{"pg_required": None, "personal_credit_required": None}])
    assert r["status"] in {FUNDING_INSUFFICIENT_EVIDENCE, "NEEDS_CONTACT", "NOT_RESEARCHED"}


def test_funding_economics_scenarios_not_silent_30_day():
    s = funding_economics_scenarios(base_amount=100000, fee_pct=0.02)
    assert s["status"] == "SCENARIO"
    assert "30_day_financing_cost" in s["scenarios"]


def test_funding_research_questions_from_gaps_only():
    qs = questions_for_gaps(["PG unknown", "cash contribution unknown"])
    keys = {q["key"] for q in qs}
    assert "personal_guarantee" in keys
    assert "owner_cash" in keys


def test_os_dashboard_zero_external():
    from database import SessionLocal

    session = SessionLocal()
    try:
        out = build_os_dashboard(session)
        assert out["LIVE_API_REQUESTS"] == 0
        assert out["external_calls"]["SAM"] == 0
    finally:
        session.close()


def test_global_today_queue_zero_external():
    class FakeSession:
        def query(self, *a, **k):
            class Q:
                def join(self, *a, **k):
                    return self

                def filter(self, *a, **k):
                    return self

                def all(self):
                    return []

            return Q()

    out = build_global_today_queue(FakeSession())
    assert out["LIVE_API_REQUESTS"] == 0
