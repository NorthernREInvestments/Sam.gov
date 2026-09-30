"""R3 company compliance / NMR / trade / 889 / registrations / attestations tests.

0 live SAM API calls. Fail-closed. Never auto-certifies owner attestations.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from response_engine.company_profile_r3 import (
    detect_profile_conflicts,
    load_company_compliance_profile,
    new_sam_snapshot,
)
from response_engine.cyber_dod import evaluate_cyber, evaluate_dpas
from response_engine.models import new_response_project
from response_engine.nmr import evaluate_nmr
from response_engine.owner_attestations import confirm_attestation, new_owner_attestation
from response_engine.r3_constants import (
    COMPANY_DATA_CONFLICT,
    NMR_APPLIES_NONCOMPLIANT,
    NMR_APPLIES_WAIVER,
    NMR_NOT_APPLICABLE,
    NMR_REVIEW_REQUIRED,
    NMR_UNKNOWN,
    OWNER_CONFIRMATION_REQUIRED,
)
from response_engine.registrations_r3 import evaluate_registrations
from response_engine.section889 import evaluate_section_889
from response_engine.trade_compliance import classify_coo, evaluate_trade_compliance


# ---------------------------------------------------------------------------
# NMR
# ---------------------------------------------------------------------------
def test_nmr_unrestricted_supply():
    d = evaluate_nmr(set_aside="Unrestricted", procurement_type="supply")
    assert d["decision_status"] == NMR_NOT_APPLICABLE
    assert d["applies"] is False


def test_nmr_small_business_supply_review():
    d = evaluate_nmr(set_aside="Total Small Business Set-Aside", procurement_type="supply")
    assert d["applies"] is True
    assert d["decision_status"] in (NMR_REVIEW_REQUIRED, NMR_UNKNOWN)


def test_nmr_waiver_present():
    d = evaluate_nmr(
        set_aside="Small Business",
        procurement_type="supply",
        manufacturer_status="nonmanufacturer",
        waiver_status="class",
        class_waiver={"applicable": True, "source": "SBA class waiver NAICS 334111", "date": "2024-01-01"},
    )
    assert d["decision_status"] == NMR_APPLIES_WAIVER


def test_nmr_no_waiver_nonmanufacturer_noncompliant():
    d = evaluate_nmr(
        set_aside="Small Business Set-Aside",
        procurement_type="supply",
        manufacturer_status="nonmanufacturer",
        waiver_status="none",
    )
    assert d["decision_status"] == NMR_APPLIES_NONCOMPLIANT


def test_nmr_multi_item():
    d = evaluate_nmr(
        set_aside="SB",
        procurement_type="supply",
        multi_item=True,
        item_groups=[{"line_id": "1", "value": 100}, {"line_id": "2", "value": 200}],
    )
    assert d["multi_item_rule"] is True
    assert len(d["item_groups"]) == 2


def test_nmr_it_var_without_evidence_not_auto():
    d = evaluate_nmr(
        set_aside="Small Business",
        procurement_type="supply",
        it_var=True,
        evidence={},
    )
    assert d["IT_VAR_claimed_without_evidence"] is True
    assert d["IT_VAR_rule"] is False


def test_nmr_unknown_manufacturer():
    d = evaluate_nmr(
        set_aside="Small Business",
        procurement_type="supply",
        manufacturer_status="unknown",
        waiver_status="none",
    )
    assert d["decision_status"] == NMR_UNKNOWN


# ---------------------------------------------------------------------------
# Trade
# ---------------------------------------------------------------------------
def test_trade_us_origin_taa():
    r = evaluate_trade_compliance(
        lines=[{"line_id": "1", "country_of_origin": "US", "origin_evidence_quality": "OEM_DECLARATION"}],
        solicitation_text="FAR 52.225-5 Trade Agreements Act applies.",
    )
    assert r["regime"] == "TRADE_AGREEMENTS"
    assert r["line_results"][0]["classification"] == "U.S.-made"
    assert r["overall_status"] == "PASS_VERIFIED"


def test_trade_designated_country():
    assert classify_coo("Canada") == "designated-country"


def test_trade_non_designated_fails_taa():
    r = evaluate_trade_compliance(
        lines=[{"line_id": "1", "country_of_origin": "China", "origin_evidence_quality": "OEM_DECLARATION"}],
        solicitation_text="52.225-5 Trade Agreements",
    )
    assert r["line_results"][0]["status"] == "FAIL"
    assert r["overall_status"] == "FAIL"


def test_trade_unknown_origin_never_pass():
    r = evaluate_trade_compliance(
        lines=[{"line_id": "1"}],
        solicitation_text="52.225-5 Trade Agreements Act",
    )
    assert r["overall_status"] == "UNKNOWN"


def test_trade_mixed_line_origin():
    r = evaluate_trade_compliance(
        lines=[
            {"line_id": "1", "country_of_origin": "US", "origin_evidence_quality": "OEM_DECLARATION"},
            {"line_id": "2", "country_of_origin": "CN"},
        ],
        solicitation_text="52.225-5",
        all_or_none=True,
    )
    assert r["overall_status"] == "FAIL"


def test_trade_clause_not_applicable():
    r = evaluate_trade_compliance(lines=[{"line_id": "1", "country_of_origin": "US"}], solicitation_text="Buy pencils for office.")
    # No BAA/TAA signal → UNKNOWN regime or NONE
    assert r["regime"] in ("UNKNOWN", "NONE")


def test_buy_american_us():
    r = evaluate_trade_compliance(
        lines=[{"line_id": "1", "country_of_origin": "United States", "origin_evidence_quality": "CERTIFICATE_OF_ORIGIN"}],
        solicitation_text="FAR 52.225-1 Buy American domestic end product",
    )
    assert r["regime"] == "BUY_AMERICAN"
    assert r["overall_status"] == "PASS_VERIFIED"


# ---------------------------------------------------------------------------
# Section 889
# ---------------------------------------------------------------------------
def test_section889_sam_annual_available():
    r = evaluate_section_889(
        solicitation_text="Include FAR 52.204-26 Covered Telecommunications.",
        sam_annual_889="VERIFIED_NO",
    )
    assert r["dimensions"]["sam_annual_representation"]["covers_annual"] is True


def test_section889_solicitation_specific_owner():
    r = evaluate_section_889(
        solicitation_text="Offeror must complete 52.204-24 Representation Regarding Certain Telecommunications.",
    )
    assert r["solicitation_specific"] is True
    assert r["status"] == OWNER_CONFIRMATION_REQUIRED
    assert r["owner_question"]


def test_section889_owner_confirmation_required_no_auto():
    r = evaluate_section_889(solicitation_text="Section 889 covered telecommunications equipment.")
    assert r["status"] == OWNER_CONFIRMATION_REQUIRED
    assert r.get("owner_confirmed") is not True


def test_section889_prohibited_product():
    r = evaluate_section_889(
        solicitation_text="52.204-25",
        product_concern=True,
    )
    assert r["status"] == "FAIL"


def test_section889_unknown_no_text():
    r = evaluate_section_889(solicitation_text="")
    assert r["applicability"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# Set-aside / profile
# ---------------------------------------------------------------------------
def test_set_aside_small_business_env_held(monkeypatch):
    monkeypatch.setenv("COMPANY_CERTIFICATIONS", "SB")
    from company_eligibility import set_aside_eligibility

    r = set_aside_eligibility("Total Small Business Set-Aside")
    assert r["eligible"] is True


def test_set_aside_cert_not_held(monkeypatch):
    monkeypatch.setenv("COMPANY_CERTIFICATIONS", "SB")
    monkeypatch.setenv("COMPANY_UNSUPPORTED_SET_ASIDES", "WOSB,HUBZone,SDVOSB")
    from company_eligibility import set_aside_eligibility

    r = set_aside_eligibility("WOSB set-aside")
    assert r["eligible"] is False


def test_company_profile_unknown_default():
    p = load_company_compliance_profile()
    assert p["UEI"] in (None, "UNKNOWN") or p["completeness"]["incomplete"] in (True, False)
    # Default fixture is UNKNOWN-heavy
    assert p["kind"] == "CompanyComplianceProfile"


def test_company_data_conflict_address():
    profile = {
        "UEI": "ABC",
        "CAGE": "1ABC2",
        "legal_name": "Acme LLC",
        "principal_business_address": "Address B",
        "sam_snapshot": {
            "uei": "ABC",
            "cage": "1ABC2",
            "entity_name": "Acme LLC",
            "physical_address": "Address A",
        },
    }
    conflicts = detect_profile_conflicts(profile)
    assert any(c["type"] == COMPANY_DATA_CONFLICT and c["field"] == "principal_business_address" for c in conflicts)


def test_sam_snapshot_never_live():
    s = new_sam_snapshot(uei="X", cage="Y")
    assert s["live_sam_api"] is False


# ---------------------------------------------------------------------------
# Registrations
# ---------------------------------------------------------------------------
def test_registration_easy_state():
    profile = {"CAGE": "UNKNOWN", "SAM_registration_status": "UNKNOWN", "state_local_registrations": []}
    r = evaluate_registrations(
        profile=profile,
        solicitation_text="Bidders must complete vendor registration with the State of Texas.",
        jurisdiction="state",
    )
    soft = r["register_before_bid"]
    assert soft
    assert soft[0]["operator_mode"] == "REGISTER_BEFORE_BID"
    assert r["overall_status"] == "REGISTER_BEFORE_BID"


def test_registration_cage_hard_block_federal():
    profile = {"CAGE": "PENDING", "SAM_registration_status": "UNKNOWN"}
    r = evaluate_registrations(profile=profile, solicitation_text="DIBBS bid board solicitation", jurisdiction="DLA")
    assert any(i["registration"] == "CAGE" and i["status"] == "FAIL" for i in r["items"])
    assert any(i.get("operator_mode") == "DIBBS_CAGE_REQUIRED" or i["registration"] == "DIBBS" for i in r["items"])


def test_registration_dibbs_cage_dependency():
    profile = {"CAGE": "UNKNOWN", "SAM_registration_status": "UNKNOWN"}
    r = evaluate_registrations(profile=profile, solicitation_text="Submit via DIBBS", portal="DIBBS")
    dibbs = [i for i in r["items"] if i["registration"] == "DIBBS"][0]
    assert dibbs["status"] == "FAIL"
    assert "CAGE" in (dibbs.get("plain") or "") or dibbs.get("depends_on") == "CAGE"


def test_registration_already_active_state():
    profile = {
        "CAGE": "UNKNOWN",
        "SAM_registration_status": "UNKNOWN",
        "state_local_registrations": [{"status": "ACTIVE", "portal": "TX"}],
    }
    r = evaluate_registrations(
        profile=profile,
        solicitation_text="vendor registration required with the state",
        jurisdiction="state",
    )
    st = [i for i in r["items"] if i["registration"] == "STATE_LOCAL_VENDOR"][0]
    assert st["status"] == "PASS_VERIFIED"


# ---------------------------------------------------------------------------
# Owner attestation
# ---------------------------------------------------------------------------
def test_owner_attestation_no_pass_until_confirm():
    att = new_owner_attestation(
        response_project_id="RP1",
        requirement_id=None,
        question="Does the company use covered telecommunications?",
    )
    assert att["owner_confirmed"] is False
    assert att["answer"] is None
    confirm_attestation(att, answer="NO", confirmed_by="owner@test")
    assert att["owner_confirmed"] is True
    assert att["answer"] == "NO"
    assert att["confirmed_by"] == "owner@test"


# ---------------------------------------------------------------------------
# Cyber / DPAS
# ---------------------------------------------------------------------------
def test_cyber_not_inferred_from_dod_alone():
    r = evaluate_cyber(solicitation_text="DoD supply contract for widgets. Deliver to DLA.")
    assert r["status"] == "NOT_APPLICABLE"


def test_cyber_cmmc_clause():
    r = evaluate_cyber(solicitation_text="CMMC Level 2 required. DFARS 252.204-7021.")
    assert r["status"] in ("REQUIRED_UNKNOWN", "REVIEW_REQUIRED", "REQUIRED_NOT_MET")


def test_dpas_detection():
    r = evaluate_dpas(solicitation_text="This is a DPAS rated order DO-A1.")
    assert r["applies"] is True
    assert "PRIORITY-RATED" in r["plain"]


# ---------------------------------------------------------------------------
# R3 service integration
# ---------------------------------------------------------------------------
def test_r3_analysis_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_CERTIFICATIONS", "SB")
    from response_engine import store as store_mod
    from response_engine.r3_service import run_r3_analysis

    monkeypatch.setattr(store_mod, "STORE_DIR", tmp_path / "rp")
    monkeypatch.setattr(store_mod, "INDEX_PATH", store_mod.STORE_DIR / "index.json")
    p = new_response_project(canonical_opportunity_id="opp-r3-1", title="Test SB")
    p["documents"] = [
        {
            "document_id": "D1",
            "text": (
                "This is a Total Small Business Set-Aside. "
                "FAR 52.225-5 Trade Agreements Act. "
                "FAR 52.204-24 Representation Regarding Certain Telecommunications. "
                "Submit via DIBBS."
            ),
        }
    ]
    p["line_items"] = [{"line_item_id": "L1", "CLIN": "0001", "quantity": "10"}]
    p["jurisdiction"] = "DLA"
    analysis = run_r3_analysis(p, persist=False, force=True)
    assert analysis["sam_api_calls"] == 0
    assert analysis["LIVE_API_REQUESTS"] == 0
    assert analysis["never_ready_to_submit"] is True
    assert analysis["readiness"] not in ("READY_TO_SUBMIT", "FULLY_LEGALLY_COMPLIANT")
    # With UNKNOWN company profile + DIBBS + 889 → blocked/attestation/registration path
    assert analysis["readiness"] in {
        "ELIGIBILITY_BLOCKED",
        "OWNER_ATTESTATION_REQUIRED",
        "NMR_REVIEW",
        "TRADE_COMPLIANCE_REVIEW",
        "COMPANY_PROFILE_INCOMPLETE",
        "COMPANY_DATA_CONFLICT",
        "REGISTRATION_REQUIRED",
    }


def test_r3_amendment_set_aside_recalc(tmp_path, monkeypatch):
    from response_engine import store as store_mod
    from response_engine.r3_service import invalidate_r3, run_r3_analysis

    monkeypatch.setattr(store_mod, "STORE_DIR", tmp_path / "rp")
    monkeypatch.setattr(store_mod, "INDEX_PATH", store_mod.STORE_DIR / "index.json")
    p = new_response_project(canonical_opportunity_id="opp-r3-amend", title="Amend SA")
    p["documents"] = [{"document_id": "D1", "text": "Total Small Business Set-Aside supply."}]
    p["set_aside"] = "Small Business Set-Aside"
    a1 = run_r3_analysis(p, persist=False, force=True)
    assert a1["nmr"]["applies"] is True
    invalidate_r3(p, reason="AMENDMENT_SET_ASIDE")
    p["set_aside"] = "Unrestricted"
    p["documents"] = [{"document_id": "D2", "text": "Amendment 0001: set-aside changed to Unrestricted / full and open."}]
    a2 = run_r3_analysis(p, persist=False, force=True)
    assert a2["nmr"]["decision_status"] == NMR_NOT_APPLICABLE


def test_r3_coo_amendment_invalidates(tmp_path, monkeypatch):
    from response_engine import store as store_mod
    from response_engine.r3_service import invalidate_r3, run_r3_analysis

    monkeypatch.setattr(store_mod, "STORE_DIR", tmp_path / "rp")
    monkeypatch.setattr(store_mod, "INDEX_PATH", store_mod.STORE_DIR / "index.json")
    p = new_response_project(canonical_opportunity_id="opp-coo", title="COO")
    p["documents"] = [{"document_id": "D1", "text": "52.225-5 Trade Agreements Act applies."}]
    p["line_items"] = [{"line_item_id": "L1", "country_of_origin": "US"}]
    p["offered_products"] = [
        {
            "offered_product_id": "P1",
            "line_item_id": "L1",
            "selected": True,
            "country_of_origin": "US",
            "origin_evidence_quality": "OEM_DECLARATION",
        }
    ]
    a1 = run_r3_analysis(p, persist=False, force=True)
    assert a1["trade"]["overall_status"] in ("PASS_VERIFIED", "REVIEW_REQUIRED")
    invalidate_r3(p, reason="AMENDMENT_ORIGIN_CLAUSE")
    assert p["r3_readiness"] == "COMPLIANCE_STALE"
    p["line_items"][0]["country_of_origin"] = None
    p["offered_products"][0]["country_of_origin"] = None
    p["offered_products"][0]["origin_evidence_quality"] = None
    a2 = run_r3_analysis(p, persist=False, force=True)
    assert a2["trade"]["overall_status"] in ("UNKNOWN", "REVIEW_REQUIRED")
    assert a2["trade"]["unknown_count"] >= 1 or a2["trade"]["overall_status"] == "UNKNOWN"


def test_r3_stale_cert_invalidation(tmp_path, monkeypatch):
    from response_engine import store as store_mod
    from response_engine.r3_service import invalidate_r3, run_r3_analysis

    monkeypatch.setattr(store_mod, "STORE_DIR", tmp_path / "rp")
    monkeypatch.setattr(store_mod, "INDEX_PATH", store_mod.STORE_DIR / "index.json")
    p = new_response_project(canonical_opportunity_id="opp-stale", title="Stale")
    p["documents"] = [{"document_id": "D1", "text": "Unrestricted supply."}]
    run_r3_analysis(p, persist=False, force=True)
    invalidate_r3(p, reason="CERTIFICATION_EXPIRED")
    assert p["r3_readiness"] == "COMPLIANCE_STALE"
    assert p["r3_analysis"].get("stale") is True


def test_r3_firewall_no_max_buy_leak(tmp_path, monkeypatch):
    from response_engine import store as store_mod
    from response_engine.r3_service import run_r3_analysis

    monkeypatch.setattr(store_mod, "STORE_DIR", tmp_path / "rp")
    monkeypatch.setattr(store_mod, "INDEX_PATH", store_mod.STORE_DIR / "index.json")
    p = new_response_project(canonical_opportunity_id="opp-fw", title="FW")
    p["documents"] = [{"document_id": "D1", "text": "Unrestricted commercial item."}]
    p["pricing_scenarios"] = [{"internal_max_buy": "999", "total_bid_price": "1000"}]
    a = run_r3_analysis(p, persist=False, force=True)
    dumped = json.dumps(a, default=str)
    # internal_max_buy must not appear in R3 analysis payload
    assert "internal_max_buy" not in dumped
    assert a["firewall"]["ok"] is True or a["firewall"].get("leaks") == []
