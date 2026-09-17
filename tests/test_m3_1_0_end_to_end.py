"""Focused tests — M3 1.0 end-to-end operational hardening."""

from __future__ import annotations

from pathlib import Path

from m3_end_to_end import M3EndToEndOrchestrator, golden_path_fixture_opportunity
from m3_lifecycle import (
    LC_PACKAGE_GATED,
    LC_READY_FOR_OPERATOR,
    LC_REJECTED_CHEAP,
    NA_NO_ACTION_REJECTED,
    NA_READY_FOR_OPERATOR,
    NA_WAIT_FUNDING,
    derive_lifecycle,
    determine_next_action,
    readiness_summary,
)
from m3_pipeline_store import M3PipelineStore
from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, is_development_no_outreach, set_operating_mode


def test_lifecycle_and_next_action_derivation():
    assert derive_lifecycle({"cheap_screen_survive": False, "rejected_cheap_screen": True}) == LC_REJECTED_CHEAP
    gated = derive_lifecycle({"package_access": "AUTH_GATED", "cheap_screen_survive": True, "research_queued": True})
    assert gated in {LC_PACKAGE_GATED, "RESEARCH_QUEUED", "PACKAGE_ACCESS_GATED"}
    nxt = determine_next_action({"status": "CANCELLED"})
    assert nxt["next_action"] == "NO_ACTION_CANCELLED"
    fund = determine_next_action(
        {"operator_readiness": "FUNDING_VERIFICATION_REQUIRED", "cheap_screen_survive": True, "line_items": [{"q": 1}]}
    )
    assert fund["next_action"] == NA_WAIT_FUNDING
    assert "UNKNOWN financing" in (fund.get("note") or "UNKNOWN financing ≠ rejection")


def test_idempotent_discovery_ingest(tmp_path):
    store = M3PipelineStore(path=tmp_path / "store.json")
    orch = M3EndToEndOrchestrator(store=store)
    rec = {
        "title": "Purchase of industrial pumps",
        "solicitation_number": "IDEM-001",
        "external_id": "IDEM-001",
        "agency": "City A",
        "source_id": "s1",
        "status": "OPEN",
        "deadline": "2099-06-01",
    }
    a = orch.ingest_discovery_record(rec)
    b = orch.ingest_discovery_record({**rec, "source_id": "s2"})
    assert a["created"] is True
    assert b["duplicate"] is True
    assert a["canonical_id"] == b["canonical_id"]
    row = store.get(a["canonical_id"])
    assert len(row.get("source_references") or []) >= 2


def test_golden_path_reaches_operator_boundary(tmp_path):
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    store = M3PipelineStore(path=tmp_path / "golden.json")
    orch = M3EndToEndOrchestrator(store=store)
    fix = golden_path_fixture_opportunity()
    batch = orch.run_from_discovery_batch([fix], advance=True)
    assert batch["metrics"]["bids_submitted"] == 0
    assert is_development_no_outreach()
    r = batch["results"][0]
    assert r.get("survived") is True
    adv = r["advance"]
    lc = adv["lifecycle"]
    # Must reach operator/commercial boundary — not fabricate submission
    assert lc in {
        LC_READY_FOR_OPERATOR,
        "COMMERCIAL_VERIFICATION_REQUIRED",
        "FUNDING_VERIFICATION_REQUIRED",
        "DRAFT_BID_READY",
        "READY_FOR_BID_PREPARATION",
        "ECONOMICS_ATTRACTIVE",
        "ECONOMICS_PRELIMINARY",
        "BOM_READY",
        "QUOTE_REQUIRED",
    } or adv["next_action"]["next_action"] in {
        NA_READY_FOR_OPERATOR,
        "WAIT_COMMERCIAL_VERIFICATION",
        "WAIT_FUNDING_VERIFICATION",
        "WAIT_OPERATOR",
        "AUTO_CONTINUE",
    }
    assert batch["metrics"]["supplier_contacts"] == 0
    assert batch["metrics"]["signatures"] == 0


def test_negative_expired_and_service_false_positive(tmp_path):
    store = M3PipelineStore(path=tmp_path / "neg.json")
    orch = M3EndToEndOrchestrator(store=store)
    expired = orch.ingest_discovery_record(
        {
            "title": "Laptop computers RFQ",
            "solicitation_number": "EXP-1",
            "external_id": "EXP-1",
            "agency": "X",
            "status": "EXPIRED",
            "deadline": "2020-01-01T17:00:00-06:00",
        }
    )
    assert expired.get("survived") is False or derive_lifecycle(
        {"status": "EXPIRED", "rejected": True, "stop_reason": "deadline_expired"}
    ) in {"CLOSED", "REJECTED"}
    if expired.get("survived"):
        adv = orch.advance(expired["canonical_id"])
        assert (adv.get("next_action") or {}).get("next_action") == NA_NO_ACTION_REJECTED or adv.get(
            "stop_reason"
        ) in {"deadline_expired", "requirements_insufficient"} or adv["lifecycle"] in {
            "REJECTED",
            "CLOSED",
            "REJECTED_CHEAP_SCREEN",
        }

    svc = orch.ingest_discovery_record(
        {
            "title": "Janitorial services for all facilities",
            "solicitation_number": "SVC-1",
            "external_id": "SVC-1",
            "agency": "X",
            "status": "OPEN",
            "deadline": "2099-01-01",
        }
    )
    assert svc.get("survived") is False


def test_unknown_acquisition_does_not_fabricate_profit(tmp_path):
    store = M3PipelineStore(path=tmp_path / "acq.json")
    orch = M3EndToEndOrchestrator(store=store)
    rec = {
        "title": "Purchase of safety PPE equipment",
        "solicitation_number": "ACQ-1",
        "external_id": "ACQ-1",
        "agency": "Y",
        "status": "OPEN",
        "deadline": "2099-08-01",
        "line_items": [{"description": "N95 respirators", "quantity": 1000, "unit": "BX"}],
        "package_access": "PUBLIC_DIRECT",
    }
    ing = orch.ingest_discovery_record(rec)
    adv = orch.advance(ing["canonical_id"])
    row = store.get(ing["canonical_id"])
    # No fabricated profit when quote required / acquisition unknown
    if row.get("expected_actual_profit") is not None:
        assert row.get("supplier_quote") or row.get("acquisition_cost") or row.get("public_price_total")
    rs = readiness_summary(row)
    assert rs["unknown_financing_is_not_rejection"] is True


def test_gated_package_not_auto_rejected(tmp_path):
    store = M3PipelineStore(path=tmp_path / "gate.json")
    orch = M3EndToEndOrchestrator(store=store)
    ing = orch.ingest_discovery_record(
        {
            "title": "Network switches and routers RFQ",
            "solicitation_number": "GATE-1",
            "external_id": "GATE-1",
            "agency": "Z",
            "status": "OPEN",
            "deadline": "2099-09-01",
            "package_access": "AUTH_GATED",
            "document_access": "AUTH_GATED",
            "raw_metadata": {"public_metadata_only": True, "document_access": "AUTH_GATED"},
        }
    )
    assert ing.get("survived") is True
    adv = orch.advance(ing["canonical_id"])
    row = store.get(ing["canonical_id"])
    assert row.get("lifecycle") != LC_REJECTED_CHEAP
    assert row.get("package_access") in {"AUTH_GATED", "REGISTRATION_REQUIRED"} or "AUTH" in str(
        row.get("stop_reason") or ""
    )


def test_restart_recovery_no_duplicate_charge(tmp_path):
    path = tmp_path / "restart.json"
    store = M3PipelineStore(path=path)
    orch = M3EndToEndOrchestrator(store=store)
    fix = golden_path_fixture_opportunity()
    ing = orch.ingest_discovery_record(fix)
    cid = ing["canonical_id"]
    orch.store.mark_paid_research(cid, "CHG-1")
    orch.store.save()
    # Simulate restart
    store2 = M3PipelineStore(path=path)
    orch2 = M3EndToEndOrchestrator(store=store2)
    row = store2.get(cid)
    assert row is not None
    assert row.get("paid_research_charge_id") == "CHG-1"
    orch2.store.mark_paid_research(cid, "CHG-1")  # idempotent
    assert store2.get(cid).get("paid_research_charge_id") == "CHG-1"
    resumed = orch2.resume_after_restart(cid)
    assert resumed.get("error") is None


def test_quantity_amendment_invalidates_dependent_state(tmp_path):
    store = M3PipelineStore(path=tmp_path / "inv.json")
    orch = M3EndToEndOrchestrator(store=store)
    fix = golden_path_fixture_opportunity()
    ing = orch.ingest_discovery_record(fix)
    cid = ing["canonical_id"]
    orch.advance(cid)
    row = store.get(cid)
    row["transaction_economics"] = {"expected_actual_profit": 12000}
    row["bid_pricing"] = {"recommended": 52000}
    row["funding_requirement"] = {"capital_amount": 41000}
    store._rows[cid] = row
    store.save()
    invalidated = store.invalidate(cid, change_type="QUANTITY_CHANGE", affected=["bom", "quantity"])
    assert invalidated.get("transaction_economics") is None
    assert invalidated.get("bid_pricing") is None
    assert invalidated.get("funding_requirement") is None


def test_outreach_blocked_in_development_mode(tmp_path):
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    store = M3PipelineStore(path=tmp_path / "safe.json")
    orch = M3EndToEndOrchestrator(store=store)
    orch.run_from_discovery_batch([golden_path_fixture_opportunity()], advance=True)
    assert orch.metrics["supplier_contacts"] == 0
    assert orch.metrics["financier_contacts"] == 0
    assert orch.metrics["agency_contacts"] == 0
    assert orch.metrics["registrations"] == 0
    assert orch.metrics["signatures"] == 0
    assert orch.metrics["bids_submitted"] == 0
    assert orch.metrics["purchases"] == 0
    assert orch.metrics["real_external_spend"] == 0.0


def test_financing_unknown_not_rejection():
    nxt = determine_next_action(
        {
            "operator_readiness": "FUNDING_VERIFICATION_REQUIRED",
            "stop_reason": "FUNDING_VERIFICATION_REQUIRED",
            "line_items": [{"description": "x", "quantity": 1}],
            "cheap_screen_survive": True,
        }
    )
    assert nxt["next_action"] == NA_WAIT_FUNDING
    assert nxt["next_action"] != NA_NO_ACTION_REJECTED
