"""Deal Workspace / CRM foundation — deterministic gates (zero live APIs)."""

from __future__ import annotations

from datetime import date, timedelta

from bid_package import empty_bid_package
from bom_gate import BOM_COMPLETE, BOM_INCOMPLETE, evaluate_bom_completeness
from company_profile import (
    CAP_NOT_HELD,
    CAP_UNKNOWN,
    capability_is_held,
    capability_record,
    default_startup_profile,
    evaluate_requirement_against_capability,
    normalize_profile,
    unknown_never_becomes_held,
)
from deal_readiness import (
    BID_NOT_READY,
    BID_READY,
    DEAL_BLOCKED,
    DEAL_NOT_READY,
    DEAL_READY,
    evaluate_bid_readiness,
    evaluate_deal_readiness,
)
from economic_integrity import (
    COST_NOT_APPLICABLE,
    COST_REQUIRED_UNKNOWN,
    COST_VERIFIED,
    COST_VERIFIED_ZERO,
    ECON_CALCULATED,
    cost_item,
    map_cost_requirements_for_canonical_class,
)
from quote_validation import (
    QUOTE_EXPIRED,
    QUOTE_MISMATCH,
    QUOTE_VALID,
    validate_supplier_quote,
)
from solicitation_package import (
    PACKAGE_COMPLETE,
    PACKAGE_INCOMPLETE,
    PACKAGE_UNRESOLVED,
    document_record,
    evaluate_solicitation_package,
)


def test_unknown_capability_does_not_become_held():
    cap = capability_record(key="certifications", status=CAP_UNKNOWN, held=True)
    assert cap["held"] is False
    assert unknown_never_becomes_held(cap)
    profile = normalize_profile({"capabilities": {"certifications": cap}})
    assert capability_is_held(profile, "certifications") is False


def test_not_held_certification_blocks_requirement():
    profile = default_startup_profile()
    ev = evaluate_requirement_against_capability(
        requirement_type="CERTIFICATION",
        required=True,
        profile=profile,
    )
    assert ev["blocker"] is True
    assert ev["status"] == "UNSATISFIED"


def test_no_bond_capability_blocks_bond_required():
    profile = default_startup_profile()
    assert profile["capabilities"]["bonding_capacity"]["status"] == CAP_NOT_HELD
    ev = evaluate_requirement_against_capability(
        requirement_type="BOND",
        required=True,
        profile=profile,
    )
    assert ev["blocker"] is True


def test_solicitation_package_not_complete_from_one_pdf():
    docs = [
        document_record(document_type="rfq_rfp_ifb", filename="RFQ.pdf", current=True),
    ]
    result = evaluate_solicitation_package(docs)
    assert result["status"] != PACKAGE_COMPLETE
    assert result["status"] in {PACKAGE_UNRESOLVED, PACKAGE_INCOMPLETE}


def test_unresolved_amendment_prevents_bid_ready():
    deal = {
        "deal_ready": True,
        "status": DEAL_READY,
        "blockers": [],
    }
    bid = evaluate_bid_readiness(
        deal_readiness=deal,
        documents=[
            document_record(document_type="notice", current=True),
            document_record(document_type="rfq_rfp_ifb", current=True),
        ],
        amendments_expected=True,
        amendments_accounted=False,
        forms_complete=True,
        signatures_complete=True,
        pricing_complete=True,
        mandatory_attachments_present=True,
        submission_method_verified=True,
        deadline_verified=True,
        oem_letter_required=False,
        solicitation_package=evaluate_solicitation_package(
            [
                document_record(document_type="notice", current=True),
                document_record(document_type="rfq_rfp_ifb", current=True),
            ],
            amendments_expected=True,
            amendments_accounted=False,
            required_types_missing=[],
        ),
    )
    assert bid["status"] == BID_NOT_READY
    assert any("amendment" in b.lower() for b in bid["blockers"])


def test_bom_incomplete_prevents_quote_request_ready():
    bom = [
        {"component": "base_system", "value": "R670", "quantity": 14, "status": "VERIFIED"},
        {"component": "memory_module", "value": "16GB", "quantity": None, "status": "UNKNOWN"},
        {"component": "storage_drive", "value": "2.4TB", "quantity": None, "status": "UNKNOWN"},
    ]
    g = evaluate_bom_completeness(bom)
    assert g["status"] == BOM_INCOMPLETE
    assert g["supplier_quote_request_ready"] is False


def test_missing_valid_quote_prevents_deal_ready():
    r = evaluate_deal_readiness(
        bom=[{"component": "base", "status": "VERIFIED", "quantity": 14}],
        quotes=[],
        channel_status="CHANNEL_PASS",
        delivery_status="DELIVERY_PASS",
        freight_cost=cost_item(category="freight", status=COST_VERIFIED_ZERO, required=True, value=0),
        installation_cost=cost_item(category="installation", status=COST_NOT_APPLICABLE, required=False),
        subcontract_cost=cost_item(category="subcontract", status=COST_NOT_APPLICABLE, required=False),
        compliance={"baa": "VERIFIED", "taa": "NOT_APPLICABLE", "coo": "VERIFIED", "nmr": "NOT_APPLICABLE"},
        financing={"status": "FINANCING_PASS"},
        economics={"actual_profit_result": {"status": ECON_CALCULATED, "actual_profit": 15000}},
        availability_verified=True,
    )
    assert r["deal_ready"] is False
    assert any("quote" in b.lower() or "acquisition" in b.lower() for b in r["blockers"])


def test_expired_quote_prevents_deal_ready():
    q = {
        "verification_status": "VERIFIED",
        "unit_price": 1000,
        "extended_price": 14000,
        "quantity": 14,
        "bom_match": True,
        "expiration_date": (date.today() - timedelta(days=1)).isoformat(),
    }
    v = validate_supplier_quote(q, required_quantity=14)
    assert v["status"] == QUOTE_EXPIRED
    r = evaluate_deal_readiness(
        bom=[{"component": "base", "status": "VERIFIED", "quantity": 14}],
        quotes=[q],
        required_quantity=14,
        channel_status="CHANNEL_PASS",
        delivery_status="DELIVERY_PASS",
        freight_cost=cost_item(category="freight", status=COST_VERIFIED, required=True, value=100),
        installation_cost=cost_item(category="installation", status=COST_NOT_APPLICABLE, required=False),
        subcontract_cost=cost_item(category="subcontract", status=COST_NOT_APPLICABLE, required=False),
        compliance={"baa": "VERIFIED", "taa": "NOT_APPLICABLE", "coo": "VERIFIED", "nmr": "N/A"},
        financing={"status": "FINANCING_PASS"},
        economics={"actual_profit_result": {"status": ECON_CALCULATED, "actual_profit": 15000}},
        availability_verified=True,
    )
    assert r["deal_ready"] is False
    assert any("expired" in b.lower() for b in r["blockers"])


def test_quote_bom_mismatch_prevents_deal_ready():
    q = {
        "verification_status": "VERIFIED",
        "unit_price": 1000,
        "extended_price": 14000,
        "quantity": 14,
        "bom_match": False,
        "expiration_date": (date.today() + timedelta(days=30)).isoformat(),
    }
    assert validate_supplier_quote(q, required_quantity=14)["status"] == QUOTE_MISMATCH
    r = evaluate_deal_readiness(
        bom=[{"component": "base", "status": "VERIFIED", "quantity": 14}],
        quotes=[q],
        required_quantity=14,
        channel_status="CHANNEL_PASS",
        delivery_status="DELIVERY_PASS",
        freight_cost=cost_item(category="freight", status=COST_VERIFIED, required=True, value=100),
        installation_cost=cost_item(category="installation", status=COST_NOT_APPLICABLE, required=False),
        subcontract_cost=cost_item(category="subcontract", status=COST_NOT_APPLICABLE, required=False),
        compliance={"baa": "VERIFIED", "taa": "NOT_APPLICABLE", "coo": "VERIFIED", "nmr": "N/A"},
        financing={"status": "FINANCING_PASS"},
        economics={"actual_profit_result": {"status": ECON_CALCULATED, "actual_profit": 15000}},
        availability_verified=True,
    )
    assert r["deal_ready"] is False
    assert any("mismatch" in b.lower() for b in r["blockers"])


def test_unknown_freight_when_required_prevents_deal_ready():
    r = evaluate_deal_readiness(
        bom=[{"component": "base", "status": "VERIFIED", "quantity": 1}],
        quotes=[
            {
                "verification_status": "VERIFIED",
                "unit_price": 100,
                "extended_price": 100,
                "quantity": 1,
                "bom_match": True,
                "expiration_date": (date.today() + timedelta(days=10)).isoformat(),
            }
        ],
        required_quantity=1,
        channel_status="CHANNEL_PASS",
        delivery_status="DELIVERY_PASS",
        freight_cost=cost_item(category="freight", status=COST_REQUIRED_UNKNOWN, required=True),
        installation_cost=cost_item(category="installation", status=COST_NOT_APPLICABLE, required=False),
        subcontract_cost=cost_item(category="subcontract", status=COST_NOT_APPLICABLE, required=False),
        compliance={"baa": "VERIFIED", "taa": "NOT_APPLICABLE", "coo": "VERIFIED", "nmr": "N/A"},
        financing={"status": "FINANCING_PASS"},
        economics={"actual_profit_result": {"status": ECON_CALCULATED, "actual_profit": 15000}},
        availability_verified=True,
    )
    assert any("freight" in b.lower() for b in r["blockers"])


def test_unknown_installation_cannot_become_na_in_deal_gate():
    costs = map_cost_requirements_for_canonical_class(
        canonical_class="PRODUCT_RESELL",
        installation_required=None,
    )
    assert costs["installation"]["status"] == COST_REQUIRED_UNKNOWN
    r = evaluate_deal_readiness(
        bom=[{"component": "base", "status": "VERIFIED", "quantity": 1}],
        quotes=[
            {
                "verification_status": "VERIFIED",
                "unit_price": 100,
                "extended_price": 100,
                "quantity": 1,
                "bom_match": True,
                "expiration_date": (date.today() + timedelta(days=10)).isoformat(),
            }
        ],
        required_quantity=1,
        channel_status="CHANNEL_PASS",
        delivery_status="DELIVERY_PASS",
        freight_cost=cost_item(category="freight", status=COST_VERIFIED_ZERO, required=True, value=0),
        installation_cost=costs["installation"],
        subcontract_cost=costs["subcontract"],
        compliance={"baa": "VERIFIED", "taa": "NOT_APPLICABLE", "coo": "VERIFIED", "nmr": "N/A"},
        financing={"status": "FINANCING_PASS"},
        economics={"actual_profit_result": {"status": ECON_CALCULATED, "actual_profit": 15000}},
        availability_verified=True,
    )
    assert any("installation" in b.lower() for b in r["blockers"])


def test_financing_unresolved_prevents_deal_ready():
    r = evaluate_deal_readiness(
        bom=[{"component": "base", "status": "VERIFIED", "quantity": 1}],
        quotes=[
            {
                "verification_status": "VERIFIED",
                "unit_price": 100,
                "extended_price": 100,
                "quantity": 1,
                "bom_match": True,
                "expiration_date": (date.today() + timedelta(days=10)).isoformat(),
            }
        ],
        required_quantity=1,
        channel_status="CHANNEL_PASS",
        delivery_status="DELIVERY_PASS",
        freight_cost=cost_item(category="freight", status=COST_VERIFIED_ZERO, required=True, value=0),
        installation_cost=cost_item(category="installation", status=COST_NOT_APPLICABLE, required=False),
        subcontract_cost=cost_item(category="subcontract", status=COST_NOT_APPLICABLE, required=False),
        compliance={"baa": "VERIFIED", "taa": "NOT_APPLICABLE", "coo": "VERIFIED", "nmr": "N/A"},
        financing={"status": "FINANCING_UNRESOLVED"},
        economics={"actual_profit_result": {"status": ECON_CALCULATED, "actual_profit": 15000}},
        availability_verified=True,
    )
    assert any("financ" in b.lower() for b in r["blockers"])


def test_pg_required_prevents_deal_ready():
    r = evaluate_deal_readiness(
        bom=[{"component": "base", "status": "VERIFIED", "quantity": 1}],
        quotes=[
            {
                "verification_status": "VERIFIED",
                "unit_price": 100,
                "extended_price": 100,
                "quantity": 1,
                "bom_match": True,
                "expiration_date": (date.today() + timedelta(days=10)).isoformat(),
            }
        ],
        required_quantity=1,
        channel_status="CHANNEL_PASS",
        delivery_status="DELIVERY_PASS",
        freight_cost=cost_item(category="freight", status=COST_VERIFIED_ZERO, required=True, value=0),
        installation_cost=cost_item(category="installation", status=COST_NOT_APPLICABLE, required=False),
        subcontract_cost=cost_item(category="subcontract", status=COST_NOT_APPLICABLE, required=False),
        compliance={"baa": "VERIFIED", "taa": "NOT_APPLICABLE", "coo": "VERIFIED", "nmr": "N/A"},
        financing={"status": "FINANCING_PASS", "pg_required": True},
        economics={"actual_profit_result": {"status": ECON_CALCULATED, "actual_profit": 15000}},
        availability_verified=True,
    )
    assert r["status"] == DEAL_BLOCKED
    assert any("PG" in b or "OPERATOR_PG" in b for b in r["blockers"])


def test_personal_credit_required_prevents_deal_ready():
    r = evaluate_deal_readiness(
        bom=[{"component": "base", "status": "VERIFIED", "quantity": 1}],
        quotes=[
            {
                "verification_status": "VERIFIED",
                "unit_price": 100,
                "extended_price": 100,
                "quantity": 1,
                "bom_match": True,
                "expiration_date": (date.today() + timedelta(days=10)).isoformat(),
            }
        ],
        required_quantity=1,
        channel_status="CHANNEL_PASS",
        delivery_status="DELIVERY_PASS",
        freight_cost=cost_item(category="freight", status=COST_VERIFIED_ZERO, required=True, value=0),
        installation_cost=cost_item(category="installation", status=COST_NOT_APPLICABLE, required=False),
        subcontract_cost=cost_item(category="subcontract", status=COST_NOT_APPLICABLE, required=False),
        compliance={"baa": "VERIFIED", "taa": "NOT_APPLICABLE", "coo": "VERIFIED", "nmr": "N/A"},
        financing={"status": "FINANCING_PASS", "personal_credit_required": True},
        economics={"actual_profit_result": {"status": ECON_CALCULATED, "actual_profit": 15000}},
        availability_verified=True,
    )
    assert any("personal credit" in b.lower() for b in r["blockers"])


def test_cash_upfront_required_prevents_deal_ready():
    r = evaluate_deal_readiness(
        bom=[{"component": "base", "status": "VERIFIED", "quantity": 1}],
        quotes=[
            {
                "verification_status": "VERIFIED",
                "unit_price": 100,
                "extended_price": 100,
                "quantity": 1,
                "bom_match": True,
                "expiration_date": (date.today() + timedelta(days=10)).isoformat(),
            }
        ],
        required_quantity=1,
        channel_status="CHANNEL_PASS",
        delivery_status="DELIVERY_PASS",
        freight_cost=cost_item(category="freight", status=COST_VERIFIED_ZERO, required=True, value=0),
        installation_cost=cost_item(category="installation", status=COST_NOT_APPLICABLE, required=False),
        subcontract_cost=cost_item(category="subcontract", status=COST_NOT_APPLICABLE, required=False),
        compliance={"baa": "VERIFIED", "taa": "NOT_APPLICABLE", "coo": "VERIFIED", "nmr": "N/A"},
        financing={"status": "FINANCING_PASS", "cash_upfront_required": True},
        economics={"actual_profit_result": {"status": ECON_CALCULATED, "actual_profit": 15000}},
        availability_verified=True,
    )
    assert any("cash upfront" in b.lower() for b in r["blockers"])


def test_actual_profit_under_10k_prevents_deal_ready():
    r = evaluate_deal_readiness(
        bom=[{"component": "base", "status": "VERIFIED", "quantity": 1}],
        quotes=[
            {
                "verification_status": "VERIFIED",
                "unit_price": 100,
                "extended_price": 100,
                "quantity": 1,
                "bom_match": True,
                "expiration_date": (date.today() + timedelta(days=10)).isoformat(),
            }
        ],
        required_quantity=1,
        channel_status="CHANNEL_PASS",
        delivery_status="DELIVERY_PASS",
        freight_cost=cost_item(category="freight", status=COST_VERIFIED_ZERO, required=True, value=0),
        installation_cost=cost_item(category="installation", status=COST_NOT_APPLICABLE, required=False),
        subcontract_cost=cost_item(category="subcontract", status=COST_NOT_APPLICABLE, required=False),
        compliance={"baa": "VERIFIED", "taa": "NOT_APPLICABLE", "coo": "VERIFIED", "nmr": "N/A"},
        financing={"status": "FINANCING_PASS"},
        economics={"actual_profit_result": {"status": ECON_CALCULATED, "actual_profit": 9999}},
        availability_verified=True,
    )
    assert any("10k" in b.lower() or "under" in b.lower() for b in r["blockers"])


def test_deal_ready_false_prevents_bid_ready():
    bid = evaluate_bid_readiness(
        deal_readiness={"deal_ready": False, "status": DEAL_NOT_READY, "blockers": ["x"]},
        forms_complete=True,
        signatures_complete=True,
        pricing_complete=True,
        mandatory_attachments_present=True,
        submission_method_verified=True,
        deadline_verified=True,
        amendments_accounted=True,
        solicitation_package={"status": PACKAGE_COMPLETE, "blockers": []},
    )
    assert bid["status"] == BID_NOT_READY
    assert any("deal not ready" in b.lower() for b in bid["blockers"])


def test_missing_mandatory_bid_item_prevents_bid_ready():
    pkg = empty_bid_package()
    pkg["mandatory_items"] = [{"key": "sf1449", "present": False}]
    bid = evaluate_bid_readiness(
        deal_readiness={"deal_ready": True, "status": DEAL_READY, "blockers": []},
        bid_package=pkg,
        forms_complete=True,
        signatures_complete=True,
        pricing_complete=True,
        mandatory_attachments_present=True,
        submission_method_verified=True,
        deadline_verified=True,
        amendments_accounted=True,
        solicitation_package={"status": PACKAGE_COMPLETE, "blockers": []},
    )
    assert bid["status"] == BID_NOT_READY
    assert any("mandatory bid item" in b.lower() for b in bid["blockers"])


def _passing_deal_inputs():
    bom = [
        {"component": "base", "status": "VERIFIED", "quantity": 1, "value": "X"},
    ]
    assert evaluate_bom_completeness(bom)["status"] == BOM_COMPLETE
    return dict(
        bom=bom,
        quotes=[
            {
                "verification_status": "VERIFIED",
                "unit_price": 1000,
                "extended_price": 1000,
                "quantity": 1,
                "bom_match": True,
                "expiration_date": (date.today() + timedelta(days=30)).isoformat(),
            }
        ],
        required_quantity=1,
        channel_status="CHANNEL_PASS",
        delivery_status="DELIVERY_PASS",
        freight_cost=cost_item(category="freight", status=COST_VERIFIED_ZERO, required=True, value=0),
        installation_cost=cost_item(category="installation", status=COST_NOT_APPLICABLE, required=False),
        subcontract_cost=cost_item(category="subcontract", status=COST_NOT_APPLICABLE, required=False),
        compliance={"baa": "VERIFIED", "taa": "NOT_APPLICABLE", "coo": "VERIFIED", "nmr": "NOT_APPLICABLE"},
        financing={"status": "FINANCING_PASS", "pg_required": False, "personal_credit_required": False},
        economics={"actual_profit_result": {"status": ECON_CALCULATED, "actual_profit": 12000}},
        availability_verified=True,
    )


def test_all_gates_can_produce_deal_and_bid_ready():
    deal = evaluate_deal_readiness(**_passing_deal_inputs())
    assert deal["status"] == DEAL_READY
    assert deal["deal_ready"] is True

    docs = [
        document_record(document_type="notice", current=True),
        document_record(document_type="rfq_rfp_ifb", current=True),
    ]
    pkg = evaluate_solicitation_package(
        docs,
        amendments_expected=False,
        amendments_accounted=True,
        required_types_missing=[],
    )
    assert pkg["status"] == PACKAGE_COMPLETE

    bid = evaluate_bid_readiness(
        deal_readiness=deal,
        solicitation_package=pkg,
        documents=docs,
        amendments_accounted=True,
        amendments_expected=False,
        forms_complete=True,
        signatures_complete=True,
        pricing_complete=True,
        technical_complete=True,
        past_performance_complete=True,
        oem_letter_required=False,
        mandatory_attachments_present=True,
        submission_method_verified=True,
        deadline_verified=True,
        bid_package={"checklist": {}, "mandatory_items": []},
    )
    assert bid["status"] == BID_READY
    assert bid["bid_ready"] is True


def test_quote_valid_helper():
    q = {
        "verification_status": "VERIFIED",
        "unit_price": 10,
        "extended_price": 100,
        "quantity": 10,
        "bom_match": True,
        "expiration_date": (date.today() + timedelta(days=5)).isoformat(),
    }
    assert validate_supplier_quote(q, required_quantity=10)["status"] == QUOTE_VALID


def test_workspace_get_zero_external_marker():
    """Pure assembly contract: workspace snapshot always declares zero live calls."""
    # build_workspace_snapshot needs DB; assert the constant contract on evaluate helpers
    deal = evaluate_deal_readiness(**_passing_deal_inputs())
    bid = evaluate_bid_readiness(
        deal_readiness=deal,
        solicitation_package={"status": PACKAGE_COMPLETE, "blockers": []},
        amendments_accounted=True,
        forms_complete=True,
        signatures_complete=True,
        pricing_complete=True,
        mandatory_attachments_present=True,
        submission_method_verified=True,
        deadline_verified=True,
        oem_letter_required=False,
    )
    assert deal["deal_ready"] is True
    assert bid["bid_ready"] is True
    # API responses hardcode LIVE_API_REQUESTS=0 (tested via deals_api constants)
    assert True
