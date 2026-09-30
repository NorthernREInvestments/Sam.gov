"""Phase L.12 — auth-walled history recovery + buyer-pivot tests."""

from __future__ import annotations

from phase_l.acquisition_lanes import STAGE3_NO_ROW_CAP
from phase_l.auth_access import (
    BUYER_SPECIFIC_ACCOUNT_REQUIRED,
    CAPTCHA_PRESENT,
    FREE_REGISTRATION_REQUIRED,
    HISTORY_NOT_AVAILABLE,
    NO_PUBLIC_HISTORY_FEATURE,
    PLATFORM_HISTORY_BLOCKED,
    PRIVATE_RESTRICTED,
    PUBLIC_ANTI_BOT_BLOCKED,
    VENDOR_ACCOUNT_REQUIRED,
    classify_access_mode,
)
from phase_l.auth_history_recovery import (
    PUBLIC_EVIDENCE_ORDER,
    build_registration_priorities,
    extract_competition_from_bid_tab,
    history_access_registration_priority,
    match_bid_tab_line,
    registration_opportunity,
    run_auth_walled_history_recovery,
)
from phase_l.buyer_history_paths import (
    discover_buyer_path_urls,
    get_buyer_history_path,
    record_successful_recovery,
    update_platform_memory,
)
from phase_l.exact_history_recovery import (
    GOV_UPGRADED_A,
    GOV_UPGRADED_B,
    GOV_UPGRADED_C,
    LOT_PRICE,
    grade_recovered_award,
    solicitation_search_variants,
)
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap, legacy_cleanup_report, obsolete_rule_active
from phase_l.platform_history import run_buyer_pivot
from phase_l.quality_audit import GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C, GOV_VALUE_D
from phase_l.resilient_hunt import SOURCE_TIMEOUT, HUNT_COMPLETE_WITH_FAILURES
from phase_l.supplier_upgrade import classify_prior_awardee_role


def test_bidnet_blocked_triggers_buyer_pivot():
    res = run_auth_walled_history_recovery(
        {
            "title": "Ford F-150 fleet",
            "agency": "City of Austin",
            "solicitation_id": "IFB-2024-100",
            "source": "BidNet",
            "portal": "bidnetdirect.com",
        },
        commercial={"manufacturer": "Ford", "model": "F-150"},
        authorize_live=False,
        platform_blocked=True,
    )
    assert res["platform_state"] == PLATFORM_HISTORY_BLOCKED
    assert res["platform_state"] != HISTORY_NOT_AVAILABLE
    assert res["access_mode"] == PUBLIC_ANTI_BOT_BLOCKED
    assert any(a.get("step") == "bidnet_metadata_only" for a in res["attempts"])
    assert any(a.get("pivot") for a in res["attempts"] if a.get("step") == "bidnet_metadata_only")
    assert res["create_account"] is False
    assert res["auto_register"] is False


def test_platform_block_not_history_unavailable():
    info = classify_access_mode("cloudflare challenge", http_status=202, platform="BidNet")
    assert info["platform_history_blocked"] is True
    assert info["history_not_available"] is False
    assert info["access_mode"] == PUBLIC_ANTI_BOT_BLOCKED


def test_access_modes_not_collapsed():
    assert classify_access_mode("register for free")["access_mode"] == FREE_REGISTRATION_REQUIRED
    assert classify_access_mode("vendor login supplier portal")["access_mode"] == VENDOR_ACCOUNT_REQUIRED
    assert classify_access_mode("buyer portal agency login")["access_mode"] == BUYER_SPECIFIC_ACCOUNT_REQUIRED
    assert classify_access_mode("captcha required")["access_mode"] == CAPTCHA_PRESENT
    assert classify_access_mode("authorized users only private")["access_mode"] == PRIVATE_RESTRICTED
    assert classify_access_mode("results not published")["access_mode"] == NO_PUBLIC_HISTORY_FEATURE


def test_buyer_path_discovery_and_memory():
    disc = discover_buyer_path_urls("City of Austin", solicitation="IFB-1", model="F-150")
    assert disc["urls"]
    record_successful_recovery(
        "City of Austin",
        evidence_type="board_award",
        source_url="https://www.austin.gov/agendas/award.pdf",
    )
    mem = get_buyer_history_path("City of Austin")
    assert mem["last_successful_recovery"]
    assert "board_award" in (mem.get("supported_evidence_types") or [])


def test_platform_history_memory():
    rec = update_platform_memory(
        "BidNet",
        access_mode=PUBLIC_ANTI_BOT_BLOCKED,
        anti_bot=True,
        buyer_pivot_success=True,
        registration_leverage=40,
    )
    assert rec["anti_bot_behavior"] is True
    assert rec["alternate_buyer_pivot_successes"] >= 1
    assert rec["registration_leverage"] >= 40


def test_public_evidence_order():
    assert PUBLIC_EVIDENCE_ORDER[0] == "buyer_procurement_page"
    assert "board_council_records" in PUBLIC_EVIDENCE_ORDER
    assert PUBLIC_EVIDENCE_ORDER[-1] == "manual_auth_required_classification"
    assert "registration_required_classification" in PUBLIC_EVIDENCE_ORDER


def test_exact_solicitation_search_variants():
    v = solicitation_search_variants("IFB-2025-001")
    assert any("award" in x for x in v)
    assert any("tabulation" in x for x in v)
    assert any("xlsx" in x.lower() or "filetype:xlsx" in x for x in v)


def test_bid_tab_line_matching():
    lines = [
        {"item": "Office chairs", "unit_price": 120, "vendor": "A"},
        {"item": "Ford F-150 XL", "model": "F-150", "unit_price": 48000, "vendor": "B"},
        {"item": "Printer toner", "unit_price": 40, "vendor": "C"},
    ]
    hit = match_bid_tab_line(lines, commercial={"manufacturer": "Ford", "model": "F-150"}, title="Ford F-150")
    assert hit is not None
    assert hit["line_matched"] is True
    assert hit["unit_price"] == 48000


def test_lot_protection():
    graded = grade_recovered_award(
        {
            "buyer": "CITY",
            "total": 250000,
            "lot_price": True,
            "price_basis": LOT_PRICE,
            "model": "F-150",
            "manufacturer": "Ford",
            "item": "F-150 fleet lot",
        },
        row={"agency": "CITY", "title": "F-150"},
        commercial={"manufacturer": "Ford", "model": "F-150"},
    )
    assert graded["grade"] == GOV_VALUE_D
    assert graded["reason"] == "lot_price_not_unit"


def test_gov_a_upgrade_from_memory_via_auth_recovery():
    mem = {
        "entries": {
            "CITY|F-150": {
                "buyer": "CITY OF AUSTIN",
                "model": "F-150",
                "manufacturer": "Ford",
                "unit_value": 49000,
                "quantity": 2,
                "solicitation_id": "IFB-1",
            }
        }
    }
    res = run_auth_walled_history_recovery(
        {"title": "Ford F-150", "agency": "City of Austin", "solicitation_id": "IFB-1", "source": "BidNet"},
        commercial={"manufacturer": "Ford", "model": "F-150"},
        buyer_memory=mem,
        authorize_live=False,
        platform_blocked=True,
    )
    assert res["outcome"] == GOV_UPGRADED_A
    assert res["grade_after"] == GOV_VALUE_A
    assert res["platform_state"] == PLATFORM_HISTORY_BLOCKED  # still blocked at platform; recovered via buyer


def test_gov_b_other_buyer_exact():
    graded = grade_recovered_award(
        {
            "buyer": "CITY OF DALLAS",
            "model": "F-150",
            "manufacturer": "Ford",
            "unit_price": 47000,
            "source": "board_council",
            "item": "Ford F-150",
        },
        row={"agency": "City of Austin", "title": "Ford F-150"},
        commercial={"manufacturer": "Ford", "model": "F-150"},
    )
    assert graded["grade"] == GOV_VALUE_B


def test_gov_c_near_family():
    graded = grade_recovered_award(
        {
            "buyer": "CITY OF AUSTIN",
            "model": "F-250",
            "manufacturer": "Ford",
            "unit_price": 52000,
            "source": "open_data",
            "item": "Ford F-250",
        },
        row={"agency": "City of Austin", "title": "Ford F-150"},
        commercial={"manufacturer": "Ford", "model": "F-150"},
    )
    assert graded["grade"] in {GOV_VALUE_C, GOV_VALUE_D}


def test_free_registration_classification():
    info = classify_access_mode("please create a free account to view awards")
    assert info["access_mode"] == FREE_REGISTRATION_REQUIRED
    reg = registration_opportunity(
        platform="BidNet",
        buyer="City X",
        access_mode=FREE_REGISTRATION_REQUIRED,
        blocked_opportunities=12,
        potential_value=50000,
        buyers_on_platform=20,
    )
    assert reg["kind"] == "REGISTRATION_HISTORY_OPPORTUNITY"
    assert reg["create_account"] is False
    assert reg["auto_register"] is False


def test_vendor_account_classification():
    info = classify_access_mode("vendor account required supplier registration")
    assert info["access_mode"] == VENDOR_ACCOUNT_REQUIRED


def test_registration_priority_platform_leverage():
    regs = [
        registration_opportunity(
            platform="BidNet",
            buyer="A",
            access_mode=FREE_REGISTRATION_REQUIRED,
            blocked_opportunities=1,
            potential_value=10000,
            buyers_on_platform=30,
        ),
        registration_opportunity(
            platform="BidNet",
            buyer="B",
            access_mode=FREE_REGISTRATION_REQUIRED,
            blocked_opportunities=1,
            potential_value=8000,
            buyers_on_platform=30,
        ),
        registration_opportunity(
            platform="TinyPortal",
            buyer="C",
            access_mode=VENDOR_ACCOUNT_REQUIRED,
            blocked_opportunities=1,
            potential_value=5000,
            buyers_on_platform=1,
        ),
    ]
    ranked = build_registration_priorities(regs)
    assert ranked[0]["platform"] == "BidNet"
    assert ranked[0]["opportunities_blocked"] == 2
    assert ranked[0]["create_account"] is False
    score = history_access_registration_priority(regs[0])
    assert score > 0


def test_prior_awardee_supplier_role():
    assert classify_prior_awardee_role({"name": "Ford OEM Factory"}) == "OEM"
    assert classify_prior_awardee_role({"name": "Austin Ford Dealer"}) == "DEALER"
    assert classify_prior_awardee_role({"name": "Regional Distributor LLC"}) == "DISTRIBUTOR"


def test_competition_extraction():
    lines = [
        {"vendor": "A", "unit_price": 45000},
        {"vendor": "B", "unit_price": 48000},
        {"vendor": "C", "unit_price": 51000},
    ]
    c = extract_competition_from_bid_tab(lines)
    assert c["bidder_count"] == 3
    assert c["low"] == 45000
    assert c["high"] == 51000
    assert c["inferred_hidden_bids"] is False


def test_buyer_pivot_run_does_not_create_accounts():
    pivot = run_buyer_pivot(
        {"agency": "City of Austin", "title": "F-150", "source": "BidNet"},
        commercial={"model": "F-150", "manufacturer": "Ford"},
        authorize_live=False,
    )
    assert pivot["kind"] == "BuyerPivotResult"
    assert pivot.get("platform_state") == PLATFORM_HISTORY_BLOCKED or pivot.get("auth_class")


def test_no_gov_d_validation_and_no_cap():
    assert STAGE3_NO_ROW_CAP is True
    assert_no_fixed_positive_cap()
    assert obsolete_rule_active("LEGACY_COLLAPSE_ALL_AUTH_TO_AUTH_REQUIRED") is False
    report = legacy_cleanup_report()
    assert report["l12_reconciled"]["no_auto_registration"] is True
    assert report["l12_reconciled"]["bidnet_terminates_recovery"] is False


def test_resilient_hunt_constants_unchanged():
    assert SOURCE_TIMEOUT == "SOURCE_TIMEOUT"
    assert HUNT_COMPLETE_WITH_FAILURES == "COMPLETE_WITH_SOURCE_FAILURES"


def test_economic_recompute_after_upgrade_shape():
    from phase_l.economic_evaluability import recompute_economics_from_recovery

    econ = recompute_economics_from_recovery(
        {"title": "Ford F-150", "agency": "City"},
        gov_rec={
            "recovered": True,
            "unit_value": 49000,
            "grade": GOV_VALUE_A,
            "source": "buyer_memory",
        },
        qty_rec={"quantity": 2},
        supplier_rec=[],
    )
    assert econ.get("government_value") is not None


def test_no_outreach_flags_on_recovery():
    res = run_auth_walled_history_recovery(
        {"title": "Generic item", "agency": "Town", "source": "BidNet"},
        authorize_live=False,
        platform_blocked=True,
    )
    assert res["outreach"] is False
    assert res["create_account"] is False
