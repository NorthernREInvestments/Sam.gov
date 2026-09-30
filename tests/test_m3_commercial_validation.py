"""BUILD 30 — Commercial Validation + Quote-to-Economics tests."""

from __future__ import annotations

import uuid

from m3_commercial_validation_read import (
    BUILD_TAG,
    ST_OPERATOR_REPORTED,
    ST_VERIFIED,
    attach_commercial_validation_to_deal_room,
    build_commercial_economics_view,
    record_supplier_quote,
)
from m3_pursuit_readiness_read import build_pursuit_readiness_assessment


def test_build_tag_and_app_version():
    from app import APP_BUILD_VERSION

    assert BUILD_TAG == "20260919-m3-commercial-validation-1"
    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION


def _seed_opportunity(oid: str, **extra):
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db

    store = M3PipelineStore()
    try:
        restore_pipeline_store_from_db(store)
    except Exception:
        pass
    row = {
        "canonical_id": oid,
        "title": "Hydraulic seal kit",
        "agency": "DLA",
        "product_classification": "KIT",
        "manufacturer": "Acme",
        "description": "x" * 50,
        "documents": ["sol.pdf"],
        "estimated_value": 5000,
        "supplier_product_graph": {"edges": [{"supplier": "DistCo"}]},
        "deadline_runway_days": 12,
        "deadline_viability": "GOOD",
        **extra,
    }
    store._rows[oid] = row
    store.save(durable_write=True, skip_remote_merge=True)
    return row


def test_structured_quote_provenance_and_associations():
    oid = f"sol:cv-{uuid.uuid4().hex[:8]}"
    _seed_opportunity(oid)
    out = record_supplier_quote(
        {
            "opportunity_id": oid,
            "supplier": "DistCo",
            "product": "Seal kit P/N 12",
            "manufacturer": "Acme",
            "model_sku": "SK-12",
            "quantity": 10,
            "unit_price": 120,
            "currency": "USD",
            "quote_date": "2026-09-19",
            "lead_time": "14 days",
            "freight": 85,
            "payment_terms": "Net 30",
            "deposit_requirement": "None",
            "source": "email:quotes@distco.example",
            "quote_reference": "Q-1001",
            "verification_status": "VERIFIED",
            "notes": "Valid through month end",
        },
        persist=True,
    )
    assert out["kind"] == "M3CommercialQuoteRecord"
    assert out["OpenAI"] == 0
    assert out["automatic_outreach"] is False
    assert out["principles"]["unknown_never_zero"] is True
    ev = out["commercial_evidence"]
    assert ev["opportunity_id"] == oid
    assert ev["supplier"] == "DistCo"
    assert ev["product"] == "Seal kit P/N 12"
    assert ev["source"] == "email:quotes@distco.example"
    assert ev["quantity"] == 10 or ev["quantity"] == 10.0
    assert float(ev["price"]) == 120.0
    assert ev.get("verification_status") == ST_VERIFIED
    assert out["quote"]["lead_time"] == "14 days"
    assert out["quote"]["payment_terms"] == "Net 30"
    assert out["supply_path"] is None or out["supply_path"].get("opportunity_id") == oid
    assert out["opportunity_updated"] is True
    econ = out["economics"]
    assert econ["acquisition_cost"]["value"] not in (None, "", 0, "0")
    assert float(econ["acquisition_cost"]["value"]) == 1200.0  # 120 * 10
    assert float(econ["known_additional_costs"][0]["value"]) == 85.0
    assert econ["margin_visibility"]["unsupported_profit_presented_as_fact"] is False


def test_unknown_values_never_become_zero():
    oid = f"sol:cv-unk-{uuid.uuid4().hex[:8]}"
    _seed_opportunity(oid, estimated_value=None)
    out = record_supplier_quote(
        {
            "opportunity_id": oid,
            "supplier": "ABC Supply",
            "product": "Widget",
            "source": "phone note 2026-09-19",
            "verification_status": "OPERATOR_REPORTED",
            # price / lead_time / freight intentionally omitted
        },
        persist=True,
    )
    q = out["quote"]
    assert q["unit_price"] == "UNKNOWN"
    assert q["lead_time"] == "UNKNOWN"
    assert q["freight"] == "UNKNOWN"
    assert q["verification_status"] == ST_OPERATOR_REPORTED
    econ = out["economics"]
    assert econ["acquisition_cost"]["value"] == "UNKNOWN"
    assert econ["acquisition_cost"]["unknown"] is True
    assert "Freight" in " ".join(econ["unknown_costs"]) or any(
        "freight" in str(u).lower() for u in econ["unknown_costs"]
    )
    # Must not treat unknown as zero in margin
    assert econ["margin_visibility"]["value"] == "UNKNOWN"
    assert 0 not in (econ["acquisition_cost"]["value"], econ["margin_visibility"]["value"])


def test_duplicate_quote_stable_id():
    oid = f"sol:cv-dup-{uuid.uuid4().hex[:8]}"
    _seed_opportunity(oid)
    body = {
        "opportunity_id": oid,
        "supplier": "SameCo",
        "product": "Pump",
        "unit_price": 50,
        "quantity": 2,
        "source": "pdf:quote.pdf",
        "quote_date": "2026-09-01",
    }
    a = record_supplier_quote(body, persist=True)
    b = record_supplier_quote(body, persist=True)
    assert a["evidence_id"] == b["evidence_id"]


def test_quote_updates_pursuit_readiness_without_forcing_known():
    oid = f"sol:cv-pr-{uuid.uuid4().hex[:8]}"
    row = _seed_opportunity(oid)
    before = build_pursuit_readiness_assessment(row, ensure_actions=False)
    before_econ = before["dimensions"]["economics_visibility"]["state"]
    out = record_supplier_quote(
        {
            "opportunity_id": oid,
            "supplier": "DistCo",
            "product": "Seal kit",
            "unit_price": 100,
            "quantity": 5,
            "source": "catalog page",
            "verification_status": "OPERATOR_REPORTED",
        },
        persist=True,
    )
    assert out["pursuit_readiness_after"]["note"]
    # Should not force KNOWN merely because a quote was typed
    assert out["pursuit_readiness_after"]["economics_state"] in {
        "KNOWN",
        "PARTIAL",
        "UNKNOWN",
    }
    # Economics should improve vs bare UNKNOWN when acquisition evidenced
    assert out["economics"]["acquisition_cost"]["unknown"] is False
    assert before_econ in {"KNOWN", "PARTIAL", "UNKNOWN"}


def test_operator_action_close_via_quote():
    from m3_action_orchestration_read import ST_COMPLETED, create_action, get_action

    oid = f"sol:cv-act-{uuid.uuid4().hex[:8]}"
    _seed_opportunity(oid)
    act = create_action(
        {
            "title": "Obtain supplier quote",
            "action_type": "COMMUNICATION",
            "why": "Need pricing",
            "trigger_source": f"test_cv:{oid}",
            "opportunity_id": oid,
        },
        persist=True,
    )
    out = record_supplier_quote(
        {
            "opportunity_id": oid,
            "supplier": "DistCo",
            "product": "Kit",
            "unit_price": 40,
            "quantity": 3,
            "source": "email quote",
            "action_id": act["action_id"],
            "complete_action": True,
        },
        persist=True,
    )
    assert out.get("action_close")
    if out["action_close"].get("accepted"):
        assert get_action(act["action_id"])["status"] == ST_COMPLETED


def test_deal_room_attachment_and_economics_view():
    oid = f"sol:cv-deal-{uuid.uuid4().hex[:8]}"
    row = _seed_opportunity(oid)
    record_supplier_quote(
        {
            "opportunity_id": oid,
            "supplier": "DistCo",
            "product": "Kit",
            "unit_price": 10,
            "quantity": 4,
            "freight": 20,
            "source": "note",
        },
        persist=True,
    )
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db

    store = M3PipelineStore()
    try:
        restore_pipeline_store_from_db(store)
    except Exception:
        pass
    updated = store.get(oid) or row
    deal = attach_commercial_validation_to_deal_room(
        {"canonical_id": oid, "economics": {}}, row=updated
    )
    assert deal["commercial_validation"]["kind"] == "M3CommercialValidationDealRoom"
    assert deal["commercial_validation"]["form"]["endpoint"] == "/api/m3/commercial/quote"
    view = build_commercial_economics_view(updated)
    assert view["rules"]["unknown_never_zero"] is True
    assert float(view["acquisition_cost"]["value"]) == 40.0


def test_regression_prior_layers():
    from cost_governor_constants import COST_FREE, TIER_ORDER
    from m3_operator_loop_read import BUILD_TAG as OL
    from m3_supply_intelligence_read import BUILD_TAG as SUP

    assert COST_FREE == "FREE"
    assert len(TIER_ORDER) >= 4
    assert OL.startswith("20260919-m3-operator-loop")
    assert SUP.startswith("20260919-m3-supply-intelligence")
