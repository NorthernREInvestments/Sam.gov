"""Phase L.13 — public artifact recovery tests."""

from __future__ import annotations

from phase_l.acquisition_lanes import STAGE3_NO_ROW_CAP
from phase_l.canonical_workflow import (
    CanonicalOpportunityWorkflow,
    PUBLIC_ARTIFACT_RECOVERY,
    REGISTRATION_REQUIRED,
    run_public_artifact_recovery_branch,
    should_run_public_artifact_recovery,
)
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap, legacy_cleanup_report, obsolete_rule_active
from phase_l.public_artifact_index import (
    apply_observed_pattern,
    classify_artifact_type,
    get_cached_discovery,
    record_observed_url_pattern,
    set_cached_discovery,
)
from phase_l.public_artifact_recovery import (
    BidNetPublicArtifactAdapter,
    exact_artifact_link,
    reject_wrong_solicitation,
    run_public_artifact_recovery,
)
from phase_l.public_artifact_search import build_artifact_queries
from phase_l.public_artifact_types import (
    FORBIDDEN_ACTIONS,
    FREE_REGISTRATION_STILL_REQUIRED,
    PUBLIC_ARTIFACT_RECOVERED,
    PUBLIC_METADATA_ONLY,
    SOLICITATION_PRINT_VIEW,
    AWARD_PDF,
    BID_TAB,
)
from phase_l.quality_audit import GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C, GOV_VALUE_D
from phase_l.exact_history_recovery import grade_recovered_award
from phase_l.resilient_hunt import SOURCE_TIMEOUT, HUNT_COMPLETE_WITH_FAILURES


def test_bidnet_blocked_triggers_public_artifact_recovery():
    assert should_run_public_artifact_recovery(
        access_mode="PUBLIC_ANTI_BOT_BLOCKED", platform_blocked=True
    )
    row = {
        "solicitation_id": "444171205790",
        "buyer": "Illinois",
        "title": "Bridge Inspection Crane Truck",
        "original_solicitation_url": (
            "https://www.bidnetdirect.com/public/supplier/solicitations/statewide/"
            "444171205790/abstract?purchasingGroupId=88021351&origin=1"
        ),
        "source": "BidNet",
    }
    res = run_public_artifact_recovery(row, authorize_live=False, platform_blocked=True)
    assert res["branch"] == "PUBLIC_ARTIFACT_RECOVERY"
    assert res["platform"] == "BidNet"
    assert res["captcha_bypass"] is False
    assert res["id_brute_force"] is False
    assert res["create_account"] is False
    assert res["artifacts_found"] >= 1  # seeded abstract URL linked by solicitation
    assert res["outcome"] in {
        PUBLIC_METADATA_ONLY,
        FREE_REGISTRATION_STILL_REQUIRED,
        PUBLIC_ARTIFACT_RECOVERED,
        "NO_PUBLIC_ARTIFACT_FOUND",
        "EVIDENCE_EXHAUSTED",
    }


def test_exact_solicitation_query():
    qs = build_artifact_queries(platform="BidNet", solicitation="444171205790", buyer="Illinois")
    assert any('site:bidnetdirect.com "444171205790"' in q["query"] for q in qs)
    assert any("print" in q["query"] for q in qs)
    assert any("award" in q["query"] for q in qs)
    assert any("filetype:pdf" in q["query"] for q in qs)


def test_public_print_and_award_classification():
    assert classify_artifact_type(
        "https://www.bidnetdirect.com/public/x/444/print", "solicitation abstract"
    ) == SOLICITATION_PRINT_VIEW
    assert classify_artifact_type("https://example.com/award-print-pdf.pdf", "award") == AWARD_PDF
    assert classify_artifact_type("https://example.com/bid-tab.xlsx", "tab") == BID_TAB


def test_exact_artifact_linking_and_rejection():
    sid = "444171205790"
    url = f"https://www.bidnetdirect.com/public/supplier/solicitations/statewide/{sid}/abstract"
    ok = exact_artifact_link(url=url, solicitation=sid, buyer="Illinois", title="Crane Truck")
    assert ok["linked"] is True
    assert "solicitation_number_in_artifact" in ok["exact_match_basis"]

    bad = exact_artifact_link(
        url="https://www.bidnetdirect.com/public/supplier/solicitations/statewide/999999999999/abstract",
        solicitation=sid,
        buyer="Illinois",
        page_text="unrelated office chairs",
    )
    assert bad["linked"] is False
    assert reject_wrong_solicitation(
        "https://www.bidnetdirect.com/public/supplier/solicitations/statewide/999999999999/abstract",
        sid,
    )


def test_no_id_brute_force_in_adapter():
    adapter = BidNetPublicArtifactAdapter()
    row = {"solicitation_id": "444171205790", "title": "Truck"}
    seeds = adapter.seed_urls(row)
    # Without observed pattern or original URL, must not invent attachment IDs
    assert not any("/public/attachments/" in u and "guess" in u for u in seeds)
    assert "CAPTCHA_SOLVE" in FORBIDDEN_ACTIONS
    assert "ID_BRUTE_FORCE" in FORBIDDEN_ACTIONS


def test_observed_pattern_instantiation_only():
    record_observed_url_pattern(
        "BidNet",
        url="https://www.bidnetdirect.com/public/supplier/solicitations/statewide/444171205790/abstract",
        artifact_type=SOLICITATION_PRINT_VIEW,
        solicitation="444171205790",
        discovery_method="test",
        access_status="PUBLIC",
    )
    applied = apply_observed_pattern(
        {
            "url_structure": "https://www.bidnetdirect.com/public/supplier/solicitations/statewide/{SOLICITATION}/abstract"
        },
        "111222333444",
    )
    assert applied and "111222333444" in applied
    assert apply_observed_pattern({"url_structure": "https://example.com/static"}, "1") is None


def test_cache_behavior():
    set_cached_discovery("BidNet", "ABC", "exact_solicitation_search", urls=["https://example.com/a"], provider="test")
    cached = get_cached_discovery("BidNet", "ABC", "exact_solicitation_search")
    assert cached and cached["urls"] == ["https://example.com/a"]


def test_bid_tab_line_and_gov_promotion_from_artifact_fields():
    from phase_l.auth_history_recovery import match_bid_tab_line

    lines = [
        {"item": "Office chair", "unit_price": 100, "vendor": "A"},
        {"item": "Ford F-150", "model": "F-150", "unit_price": 48000, "vendor": "B"},
    ]
    hit = match_bid_tab_line(lines, commercial={"manufacturer": "Ford", "model": "F-150"}, title="F-150")
    assert hit and hit["unit_price"] == 48000
    graded = grade_recovered_award(
        {
            "buyer": "Illinois",
            "model": "F-150",
            "manufacturer": "Ford",
            "unit_price": 48000,
            "quantity": 1,
            "source": "public_artifact:BID_TAB",
            "item": "Ford F-150",
            "vendor": "B",
        },
        row={"agency": "Illinois", "title": "Ford F-150"},
        commercial={"manufacturer": "Ford", "model": "F-150"},
    )
    assert graded["grade"] == GOV_VALUE_A


def test_gov_b_other_buyer_from_artifact():
    graded = grade_recovered_award(
        {
            "buyer": "Georgia",
            "model": "F-150",
            "manufacturer": "Ford",
            "unit_price": 47000,
            "source": "public_artifact:AWARD_PDF",
            "item": "Ford F-150",
        },
        row={"agency": "Illinois", "title": "Ford F-150"},
        commercial={"manufacturer": "Ford", "model": "F-150"},
    )
    assert graded["grade"] == GOV_VALUE_B


def test_registration_still_required_when_no_seed():
    res = run_public_artifact_recovery(
        {"title": "Generic", "agency": "Town", "source": "BidNet", "solicitation_id": "NOMATCH999"},
        authorize_live=False,
        platform_blocked=True,
    )
    assert res["outcome"] in {
        FREE_REGISTRATION_STILL_REQUIRED,
        "NO_PUBLIC_ARTIFACT_FOUND",
        PUBLIC_METADATA_ONLY,
        "EVIDENCE_EXHAUSTED",
    }
    if res["outcome"] == FREE_REGISTRATION_STILL_REQUIRED:
        assert res["registration_opportunity"]
        assert res["registration_opportunity"]["create_account"] is False


def test_canonical_workflow_integration():
    wf = CanonicalOpportunityWorkflow(opportunity_id="444171205790")
    from phase_l.canonical_workflow import SOURCE_VERIFIED, PRODUCT_CONFIRMED, IDENTITY_RESOLVED

    wf.transition(SOURCE_VERIFIED, rule_id="t", force=True)
    wf.transition(PRODUCT_CONFIRMED, rule_id="t", force=True)
    wf.transition(IDENTITY_RESOLVED, rule_id="t", force=True)
    row = {
        "solicitation_id": "444171205790",
        "buyer": "Illinois",
        "title": "Crane Truck",
        "original_solicitation_url": (
            "https://www.bidnetdirect.com/public/supplier/solicitations/statewide/"
            "444171205790/abstract?purchasingGroupId=88021351&origin=1"
        ),
    }
    result = run_public_artifact_recovery_branch(wf, row, authorize_live=False, platform_blocked=True)
    assert result["branch"] == "PUBLIC_ARTIFACT_RECOVERY"
    assert any(t.get("next_state") == PUBLIC_ARTIFACT_RECOVERY for t in wf.audit_trail)
    assert wf.state in {REGISTRATION_REQUIRED, "EVIDENCE_EXHAUSTED", "GOV_VALUE_RESEARCHED", PUBLIC_ARTIFACT_RECOVERY}


def test_no_captcha_bypass_flags_and_caps():
    assert STAGE3_NO_ROW_CAP is True
    assert_no_fixed_positive_cap()
    assert obsolete_rule_active("LEGACY_ID_BRUTE_FORCE_ARTIFACTS") is False
    assert obsolete_rule_active("LEGACY_CAPTCHA_BYPASS") is False
    report = legacy_cleanup_report()
    assert report["l13_reconciled"]["no_id_brute_force"] is True
    assert report["l13_reconciled"]["public_artifact_before_registration"] is True


def test_resilient_hunt_unchanged():
    assert SOURCE_TIMEOUT == "SOURCE_TIMEOUT"
    assert HUNT_COMPLETE_WITH_FAILURES == "COMPLETE_WITH_SOURCE_FAILURES"


def test_supplier_awardee_extraction_role():
    from phase_l.supplier_upgrade import classify_prior_awardee_role

    assert classify_prior_awardee_role({"name": "Ford Authorized Dealer"}) == "DEALER"


def test_no_outreach():
    res = run_public_artifact_recovery(
        {"title": "X", "solicitation_id": "1", "source": "BidNet"},
        authorize_live=False,
    )
    assert res["outreach"] is False
