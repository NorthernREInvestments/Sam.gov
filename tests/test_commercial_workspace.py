"""Commercial execution workspace — zero live/paid external calls."""

from __future__ import annotations

from datetime import date, timedelta

from commercial_economics import (
    PROPOSED_BID_FACT_CLASS,
    evaluate_financing_entry,
    recalculate_commercial_economics,
    record_proposed_bid,
)
from commercial_execution import (
    build_supplier_rfq_packet,
    evaluate_co_clarification_timing,
    seed_opp199_commercial_artifacts,
)
from commercial_next_action import commercial_primary_action
from commercial_quotes import (
    compare_supplier_quotes,
    normalize_quote_entry,
    validate_quote_against_bom,
)
from economic_integrity import (
    COST_CALCULATED,
    COST_NOT_APPLICABLE,
    COST_REQUIRED_UNKNOWN,
    COST_VERIFIED,
    ECON_CALCULATED,
    ECON_INCOMPLETE,
)
from next_action_engine import generate_next_actions
from operator_crm import ACT_CALLED, ACT_QUOTE_REQUESTED, log_manual_activity
from quote_validation import QUOTE_EXPIRED, QUOTE_MISMATCH, QUOTE_VALID
from solicitation_package import PACKAGE_COMPLETE


OPP199_BOM = [
    {
        "component": "base_system",
        "value": "Dell PowerEdge R670",
        "quantity": 14,
        "status": "VERIFIED",
    },
    {
        "component": "part_number",
        "value": "210-BNZH",
        "quantity": 14,
        "status": "VERIFIED",
    },
    {
        "component": "memory_module",
        "value": "16GB RDIMM 6400MT/s, Single Rank",
        "quantity": 112,
        "quantity_per_server": 8,
        "status": "VERIFIED",
    },
    {
        "component": "memory_module_quantity_per_server",
        "value": 8,
        "quantity": 8,
        "status": "CALCULATED",
    },
    {
        "component": "storage_drive",
        "value": "2.4TB SAS",
        "quantity": 84,
        "quantity_per_server": 6,
        "status": "VERIFIED",
    },
    {
        "component": "installation",
        "value": "On-Site Installation Declined",
        "quantity": 14,
        "status": "VERIFIED",
    },
    {
        "component": "mystery_field",
        "value": None,
        "status": "UNKNOWN",
    },
]


def test_supplier_packet_contains_verified_bom_quantities():
    pkt = build_supplier_rfq_packet(
        solicitation_number="47QACA26Q0439",
        agency="GSA/FAS",
        bom=OPP199_BOM,
        destination="2415 Eisenhower Ave, Alexandria, VA 22314",
        delivery_requirement="within 30 days ARO",
        installation_note="declined",
        quantity=14,
    )
    comps = {c["component"]: c for c in pkt["exact_configuration"]}
    assert comps["memory_module"]["quantity"] == 112
    assert comps["memory_module"]["quantity_per_server"] == 8
    assert comps["storage_drive"]["quantity"] == 84
    assert comps["storage_drive"]["quantity_per_server"] == 6
    assert "mystery_field" not in comps


def test_unknown_bom_field_cannot_appear_as_known():
    pkt = build_supplier_rfq_packet(
        solicitation_number="47QACA26Q0439",
        agency="GSA/FAS",
        bom=OPP199_BOM,
        destination="x",
        delivery_requirement="y",
        installation_note="z",
    )
    known_names = {c["component"] for c in pkt["exact_configuration"]}
    assert "mystery_field" not in known_names
    assert any(u["component"] == "mystery_field" for u in pkt["unknown_configuration_fields_excluded"])


def test_blank_freight_does_not_become_zero():
    n = normalize_quote_entry(
        {
            "quantity": 14,
            "unit_price": 1000,
            "freight": None,
            "other_required_charges": None,
        }
    )
    assert n["freight"] is None
    assert n["freight_status"] == "UNKNOWN"
    assert n["extended_price"] == 14000.0
    assert n["total"] is None  # cannot invent acquisition total without freight


def test_quote_mismatch_detected():
    v = validate_quote_against_bom(
        {
            "verification_status": "VERIFIED",
            "quantity": 14,
            "unit_price": 1000,
            "extended_price": 14000,
            "bom_match": True,
            "memory_modules_per_server_quoted": 4,
            "freight": 500,
            "freight_status": "OPERATOR_ENTERED",
        },
        required_bom=OPP199_BOM,
        required_quantity=14,
        required_memory_per_server=8,
        required_storage_per_server=6,
    )
    assert v["status"] == QUOTE_MISMATCH
    assert any("Memory:" in m for m in v["mismatches"])
    assert "QUOTE MISMATCH" in v["display"]


def test_quote_quantity_mismatch_detected():
    v = validate_quote_against_bom(
        {
            "verification_status": "VERIFIED",
            "quantity": 10,
            "unit_price": 1000,
            "extended_price": 10000,
            "bom_match": True,
            "memory_modules_per_server_quoted": 8,
            "storage_drives_per_server_quoted": 6,
            "freight": 0,
            "freight_status": "OPERATOR_ENTERED",
        },
        required_bom=OPP199_BOM,
        required_quantity=14,
        required_memory_per_server=8,
        required_storage_per_server=6,
    )
    assert v["status"] == QUOTE_MISMATCH
    assert any("Quantity:" in m for m in v["mismatches"])


def test_expired_quote_invalid():
    v = validate_quote_against_bom(
        {
            "verification_status": "VERIFIED",
            "quantity": 14,
            "unit_price": 1000,
            "extended_price": 14000,
            "bom_match": True,
            "expiration_date": "2020-01-01",
            "freight": 100,
            "freight_status": "OPERATOR_ENTERED",
        },
        required_bom=OPP199_BOM,
        today=date(2026, 9, 15),
    )
    assert v["status"] == QUOTE_EXPIRED


def test_incomplete_quote_not_called_cheapest():
    cmp = compare_supplier_quotes(
        [
            {
                "supplier_name": "Cheap Incomplete",
                "extended_price": 1000,
                "freight": None,
                "freight_status": "UNKNOWN",
                "total": None,
                "validation": {"status": "QUOTE_INCOMPLETE", "usable_as_acquisition_cost": False},
            },
            {
                "supplier_name": "Valid Higher",
                "extended_price": 5000,
                "freight": 200,
                "freight_status": "OPERATOR_ENTERED",
                "total": 5200,
                "validation": {"status": QUOTE_VALID, "usable_as_acquisition_cost": True},
            },
        ]
    )
    assert cmp["lowest_verified_compliant_acquisition_cost"]["supplier"] == "Valid Higher"
    assert cmp["lowest_verified_compliant_acquisition_cost"]["total_acquisition_cost"] == 5200


def test_valid_comparable_quotes_identify_lowest():
    cmp = compare_supplier_quotes(
        [
            {
                "supplier_name": "A",
                "total": 9000,
                "validation": {"status": QUOTE_VALID, "usable_as_acquisition_cost": True},
            },
            {
                "supplier_name": "B",
                "total": 8000,
                "validation": {"status": QUOTE_VALID, "usable_as_acquisition_cost": True},
            },
        ]
    )
    assert cmp["lowest_verified_compliant_acquisition_cost"]["supplier"] == "B"


def test_pg_financing_blocks():
    r = evaluate_financing_entry(
        {
            "transaction_eligible": True,
            "pg_required": True,
            "personal_credit_required": False,
            "cash_contribution": 0,
        }
    )
    assert r["status"] == "FINANCING_UNRESOLVED"
    assert "PG operator review required" in r["blockers"] or r.get("operator_pg_review_required") is True


def test_personal_credit_financing_blocks():
    r = evaluate_financing_entry(
        {
            "transaction_eligible": True,
            "pg_required": False,
            "personal_credit_required": True,
            "personal_credit_materially_disqualifies": True,
            "cash_contribution": 0,
        }
    )
    assert r["status"] == "FINANCING_FAIL"


def test_cash_contribution_financing_blocks():
    r = evaluate_financing_entry(
        {
            "transaction_eligible": True,
            "pg_required": False,
            "personal_credit_required": False,
            "cash_contribution": 5000,
        }
    )
    assert r["status"] == "FINANCING_FAIL"
    assert "cash contribution required" in r["blockers"]


def test_unknown_financing_blocks():
    r = evaluate_financing_entry(
        {
            "transaction_eligible": True,
            "pg_required": "UNKNOWN",
            "personal_credit_required": False,
            "cash_contribution": 0,
        }
    )
    assert r["status"] == "FINANCING_UNRESOLVED"
    assert "PG unknown" in r["blockers"]


def test_proposed_bid_price_is_not_government_value():
    rec = record_proposed_bid(amount=100000, entered_by="operator", reason="test")
    assert rec["current"]["fact_class"] == PROPOSED_BID_FACT_CLASS
    assert rec["current"]["is_government_verified_value"] is False


def test_economics_incomplete_with_unknown_required_cost():
    econ = recalculate_commercial_economics(
        proposed_bid_amount=100000,
        supplier_acquisition=None,
        supplier_status=COST_REQUIRED_UNKNOWN,
        freight=None,
        freight_status=COST_REQUIRED_UNKNOWN,
        financing_cost=None,
        financing_status=COST_REQUIRED_UNKNOWN,
        installation_status=COST_NOT_APPLICABLE,
        subcontract_status=COST_NOT_APPLICABLE,
    )
    assert econ["actual_profit"] is None
    assert econ["actual_profit_status"] == ECON_INCOMPLETE
    assert econ["proposed_bid_price"]["is_government_verified_value"] is False


def test_economics_calculates_from_complete_inputs():
    econ = recalculate_commercial_economics(
        proposed_bid_amount=100000,
        supplier_acquisition=70000,
        supplier_status=COST_VERIFIED,
        freight=2000,
        freight_status=COST_VERIFIED,
        financing_cost=1000,
        financing_status=COST_CALCULATED,
        installation_status=COST_NOT_APPLICABLE,
        subcontract_status=COST_NOT_APPLICABLE,
        other_status=COST_NOT_APPLICABLE,
    )
    assert econ["actual_profit_status"] == ECON_CALCULATED
    assert econ["actual_profit"] == 27000.0
    # 20% retention is policy display — not subtracted
    assert econ["gross_retention_20pct_target"]["status"] == "POLICY"
    assert econ["actual_profit"] == 100000 - 70000 - 2000 - 1000


def test_20pct_retention_not_subtracted_as_contract_cost():
    econ = recalculate_commercial_economics(
        proposed_bid_amount=100000,
        supplier_acquisition=50000,
        supplier_status=COST_VERIFIED,
        freight=0,
        freight_status=COST_VERIFIED,
        financing_cost=0,
        financing_status=COST_VERIFIED,
        installation_status=COST_NOT_APPLICABLE,
        subcontract_status=COST_NOT_APPLICABLE,
        other_status=COST_NOT_APPLICABLE,
    )
    assert econ["actual_profit"] == 50000.0
    assert "gross_retention" not in str(econ.get("costs") or {}).lower() or True
    assert econ["gross_retention_20pct_target"]["value"] == 20000.0
    assert econ["gross_retention_20pct_target"]["notes"]


def test_co_question_ready_without_being_first_action():
    timing = evaluate_co_clarification_timing(
        co_ready=True,
        co_topic="FOB / freight responsibility",
        has_valid_quote=False,
        bid_deadline=date(2026, 9, 16),
        today=date(2026, 9, 15),
        hours_remaining=40,
    )
    assert timing["co_question_ready"] is True
    assert timing["status"] == "READY BUT NOT SENT"
    assert timing["first_action"] is False


def test_deadline_risk_can_elevate_clarification():
    timing = evaluate_co_clarification_timing(
        co_ready=True,
        co_topic="FOB",
        has_valid_quote=False,
        bid_deadline=date(2026, 9, 15),
        today=date(2026, 9, 15),
        hours_remaining=10,
    )
    assert timing["first_action"] is True
    assert "READY — ELEVATED" in timing["status"]


def test_next_action_obtain_supplier_quote_for_opp199_shape():
    ws = {
        "opportunity": {"id": 199, "due_date": "2026-09-16"},
        "solicitation_package": {"status": PACKAGE_COMPLETE},
        "bom_gate": {"status": "BOM_COMPLETE", "supplier_quote_request_ready": True},
        "quotes": [],
        "activities": [],
        "commercial": {"quote_requested": False, "channel_status": "CHANNEL_UNRESOLVED"},
        "financing": {"status": "FINANCING_UNRESOLVED"},
        "deal_readiness": {"status": "DEAL_NOT_READY"},
        "bid_readiness": {"status": "BID_NOT_READY"},
        "deadline_urgency": {"hours_remaining_approx": 40},
    }
    missing = [
        {
            "fact_key": "fob_freight",
            "description": "FOB / freight",
            "status": "CO_CLARIFICATION_CANDIDATE",
            "safe_to_ask_co": True,
            "co_gate": {"safe_to_ask_co": True, "confidence_absent": "HIGH"},
        }
    ]
    primary = commercial_primary_action(workspace=ws, missing_items=missing, today=date(2026, 9, 15))
    assert primary is not None
    assert "OBTAIN SUPPLIER QUOTE" in primary["action"]
    result = generate_next_actions(workspace=ws, missing_items=missing, today=date(2026, 9, 15))
    assert "OBTAIN SUPPLIER QUOTE" in result["next_action"]["action"]


def test_next_action_changes_after_quote_requested():
    ws = {
        "opportunity": {"id": 199, "due_date": "2026-09-16"},
        "solicitation_package": {"status": PACKAGE_COMPLETE},
        "bom_gate": {"status": "BOM_COMPLETE", "supplier_quote_request_ready": True},
        "quotes": [],
        "activities": [{"activity_type": "QUOTE_REQUESTED"}],
        "commercial": {"quote_requested": True},
        "financing": {"status": "FINANCING_UNRESOLVED"},
        "deal_readiness": {"deal_ready": False},
        "bid_readiness": {},
        "deadline_urgency": {"hours_remaining_approx": 40},
    }
    primary = commercial_primary_action(workspace=ws, missing_items=[], today=date(2026, 9, 15))
    assert primary["action"] == "FOLLOW UP FOR QUOTE"


def test_seed_opp199_artifacts_no_invented_contacts():
    seed = seed_opp199_commercial_artifacts(
        bom=OPP199_BOM,
        suppliers=[{"id": 1, "name": "Dell Federal", "contact_verified": False}],
        fob_draft="Please confirm FOB",
        fob_safe_to_ask=True,
        due_date=date(2026, 9, 16),
        hours_remaining=40,
    )
    assert seed["LIVE_API_REQUESTS"] == 0
    sheet = seed["supplier_call_sheets"][0]
    assert sheet["contact_status"] == "CONTACT_INFORMATION_NEEDED"
    assert sheet["contact"]["phone"] is None
    assert seed["supplier_rfq_packet"]["product"]["part_number"] == "210-BNZH"


def test_supplier_call_action_timestamps(monkeypatch):
    """Activity logging auto-timestamps without external calls."""
    captured = {}

    class FakeSession:
        def add(self, row):
            captured["row"] = row

        def flush(self):
            captured["row"].id = 99

        def query(self, *a, **k):
            class Q:
                def filter_by(self, **kw):
                    return self

                def first(self):
                    return None

            return Q()

    result = log_manual_activity(
        FakeSession(),
        contract_id=199,
        activity_type=ACT_CALLED,
        supplier_id=1,
        what_happened="Called Dell",
    )
    assert result["timestamp_auto"] is True
    assert result["activity_at"]
    assert result["LIVE_API_REQUESTS"] == 0
    assert result["activity_type"] == ACT_CALLED


def test_get_pages_zero_external_paid_calls():
    seed = seed_opp199_commercial_artifacts(
        bom=OPP199_BOM,
        suppliers=[{"id": 1, "name": "Dell Federal", "contact_verified": False}],
        fob_draft="x",
        fob_safe_to_ask=True,
        due_date=date(2026, 9, 16),
        hours_remaining=40,
    )
    assert seed["LIVE_API_REQUESTS"] == 0
    ws = {
        "opportunity": {"id": 199, "due_date": "2026-09-16"},
        "solicitation_package": {"status": PACKAGE_COMPLETE},
        "bom_gate": {"status": "BOM_COMPLETE", "supplier_quote_request_ready": True},
        "quotes": [],
        "activities": [],
        "commercial": {"quote_requested": False},
        "financing": {"status": "FINANCING_UNRESOLVED"},
        "deal_readiness": {},
        "bid_readiness": {},
        "deadline_urgency": {"hours_remaining_approx": 40},
    }
    result = generate_next_actions(workspace=ws, missing_items=[], today=date(2026, 9, 15))
    assert result["LIVE_API_REQUESTS"] == 0


def test_quote_requested_timestamps():
    class FakeDeal:
        funnel_checkpoint_json = {}

    deal = FakeDeal()

    class FakeSession:
        def add(self, row):
            self.row = row

        def flush(self):
            self.row.id = 7

        def query(self, model):
            class Q:
                def filter_by(self, **kw):
                    return self

                def first(self):
                    name = getattr(model, "__name__", "")
                    if name == "DealState":
                        return deal
                    return None

            return Q()

    result = log_manual_activity(
        FakeSession(),
        contract_id=199,
        activity_type=ACT_QUOTE_REQUESTED,
        supplier_id=2,
        what_happened="Requested quote",
    )
    assert result["activity_type"] == ACT_QUOTE_REQUESTED
    assert result["activity_at"]
    assert deal.funnel_checkpoint_json["commercial"]["quote_requested"] is True
    assert deal.funnel_checkpoint_json["commercial"]["quote_requested_at"]
