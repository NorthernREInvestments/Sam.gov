"""Phase L.10 exact evidence workflow tests."""

from __future__ import annotations

from collections import Counter

from phase_l.acquisition_lanes import DEEP_RESEARCH_NO_FIXED_COUNT, STAGE3_NO_ROW_CAP
from phase_l.buyer_history_workflow import (
    BUYER_HISTORY_SEARCH_ORDER,
    PRODUCT_HISTORY_SEARCH_ORDER,
    run_gov_value_upgrade_loop,
)
from phase_l.canonical_workflow import (
    DISCOVERED,
    EVIDENCE_EXHAUSTED,
    SOURCE_VERIFIED,
    CanonicalOpportunityWorkflow,
    classify_identity_state,
)
from phase_l.discovery_audit import GAP_PLATFORM_ENUMERATION, audit_discovery_coverage, generate_discovery_gaps
from phase_l.history_graphs import link_product_history, link_supplier, normalize_award_tabulation, query_product_history
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap, obsolete_rule_active
from phase_l.platform_history import detect_platform, fetch_platform_history, platform_history_inventory
from phase_l.quality_audit import (
    GOV_VALUE_A,
    GOV_VALUE_C,
    GOV_VALUE_D,
    RECON_ONLY_CATEGORY_BENCHMARK,
    SECONDARY_QUOTE_TARGET,
    SUPPLIER_A,
    SUPPLIER_D,
    VALIDATED_QUOTE_TARGET,
    audit_quote_positive,
    confidence_matrix,
    grade_government_value,
    grade_quantity,
    grade_supplier,
    revise_profit_tiers,
)
from phase_l.supplier_upgrade import SUPPLIER_DISCOVERY_ORDER, run_supplier_upgrade_loop, validate_supplier_product_fit


def test_state_machine_transitions_and_audit_trail():
    wf = CanonicalOpportunityWorkflow("T1")
    assert wf.state == DISCOVERED
    wf.transition(
        SOURCE_VERIFIED,
        rule_id="L10_SOURCE_AUTHORITATIVE_RESOLVED",
        evidence_used=["url"],
        evidence_source="https://sam.gov/x",
        confidence="HIGH",
    )
    assert wf.state == SOURCE_VERIFIED
    assert len(wf.audit_trail) == 1
    assert wf.audit_trail[0]["prior_state"] == DISCOVERED
    assert wf.audit_trail[0]["rule_id"] == "L10_SOURCE_AUTHORITATIVE_RESOLVED"


def test_illegal_transition_raises():
    wf = CanonicalOpportunityWorkflow("T2")
    try:
        wf.transition(EVIDENCE_EXHAUSTED, rule_id="BAD")
        assert False, "should raise"
    except ValueError:
        pass


def test_buyer_and_product_history_order():
    assert BUYER_HISTORY_SEARCH_ORDER[0] == "same_buyer_exact_model"
    assert PRODUCT_HISTORY_SEARCH_ORDER[-1] == "category_benchmark_last_resort"


def test_category_benchmark_last_resort_and_model_band_to_c():
    d = grade_government_value(
        {
            "state": "GOV_VALUE_RANGE",
            "tier": "C",
            "unit_value": 45000,
            "source": "GOVERNMENT_CATEGORY_BENCHMARK_RECON:generic_vehicle",
        },
        commercial={"manufacturer": "Ford"},
    )
    assert d["grade"] == GOV_VALUE_D
    c = grade_government_value(
        {
            "state": "GOV_VALUE_RANGE",
            "tier": "C",
            "unit_value": 48000,
            "source": "GOVERNMENT_CATEGORY_BENCHMARK_RECON:light_truck_award_band",
        },
        commercial={"manufacturer": "Ford", "model": "F-150"},
    )
    assert c["grade"] == GOV_VALUE_C
    assert c.get("upgraded_from_benchmark") is True


def test_gov_d_cannot_validate():
    audit = audit_quote_positive(
        {
            "title": "Fleet vehicles",
            "agency": "City",
            "solicitation_id": "X1",
            "original_posting_url": "https://sam.gov/opp/x/view",
        },
        commercial={"manufacturer": "Ford"},
        gov={
            "state": "GOV_VALUE_RANGE",
            "unit_value": 45000,
            "source": "GOVERNMENT_CATEGORY_BENCHMARK_RECON:generic_vehicle",
        },
        suppliers=[
            {
                "supplier_domain": "ford.com",
                "source_type": "OEM",
                "authorization_state": "AUTHORIZED_CONFIRMED",
                "product_fit": "EXACT",
                "exact_product_evidence": True,
            }
        ],
        qty_info={"quantity": 2, "quality": "EXACT"},
        max_buy={"supplier_quote_target": 35000},
        qdep={"tiers": {"quote_dependent_positive": True, "ge_10k": True}},
        original={"original_source_verified": True, "original_posting_url": "https://sam.gov/opp/x/view"},
        deadline_days=20,
        attempt_upgrades=False,
    )
    assert audit["gov_grade"] == GOV_VALUE_D
    assert audit["quality_state"] == RECON_ONLY_CATEGORY_BENCHMARK


def test_gov_upgrade_model_band_can_secondary():
    audit = audit_quote_positive(
        {
            "title": "Two Ford F-150 fleet trucks",
            "agency": "City",
            "solicitation_id": "X2",
            "original_posting_url": "https://sam.gov/opp/y/view",
        },
        commercial={"manufacturer": "Ford", "model": "F-150"},
        gov={
            "state": "GOV_VALUE_RANGE",
            "unit_value": 48000,
            "source": "GOVERNMENT_CATEGORY_BENCHMARK_RECON:light_truck_award_band",
        },
        suppliers=[
            {
                "supplier_domain": "ford.com",
                "source_type": "OEM",
                "authorization_state": "AUTHORIZED_CONFIRMED",
                "product_fit": "EXACT",
                "exact_product_evidence": True,
            }
        ],
        qty_info={"quantity": 2, "quality": "EXACT"},
        max_buy={"supplier_quote_target": 40000},
        qdep={"tiers": {"quote_dependent_positive": True, "ge_10k": True}},
        original={"original_source_verified": True, "original_posting_url": "https://sam.gov/opp/y/view"},
        deadline_days=20,
        attempt_upgrades=False,
    )
    assert audit["gov_grade"] == GOV_VALUE_C
    assert audit["quality_state"] == SECONDARY_QUOTE_TARGET
    assert audit["quality_state"] != VALIDATED_QUOTE_TARGET


def test_supplier_exact_fit_and_generic_cannot_validate():
    fit = validate_supplier_product_fit(
        {"supplier_domain": "ford.com", "source_type": "OEM"},
        commercial={"manufacturer": "Ford", "model": "F-150"},
    )
    assert fit["exact_model_mpn_evidence"] is True
    assert (
        grade_supplier(
            {
                "supplier_domain": "ford.com",
                "source_type": "OEM",
                "authorization_state": "AUTHORIZED_CONFIRMED",
                "product_fit": "EXACT",
                "exact_product_evidence": True,
            },
            commercial={"manufacturer": "Ford", "model": "F-150"},
        )
        == SUPPLIER_A
    )
    audit = audit_quote_positive(
        {
            "title": "Exact NSN valve",
            "agency": "DOD",
            "solicitation_id": "S9",
            "original_posting_url": "https://sam.gov/opp/z/view",
        },
        commercial={"mpn": "1274M99P01"},
        gov={"state": "GOV_VALUE_EXACT", "tier": "A", "unit_value": 5000, "source": "historical_award_unit_price"},
        suppliers=[{"supplier_domain": "grainger.com", "source_type": "DISTRIBUTOR", "product_fit": "FAMILY"}],
        qty_info={"quantity": 1, "quality": "EXACT"},
        max_buy={"supplier_quote_target": 4000},
        qdep={"tiers": {"quote_dependent_positive": True}},
        original={"original_source_verified": True, "original_posting_url": "https://sam.gov/opp/z/view"},
        deadline_days=20,
        attempt_upgrades=False,
    )
    assert audit["supplier_grade"] == SUPPLIER_D
    assert audit["quality_state"] != VALIDATED_QUOTE_TARGET


def test_quantity_no_unit_only_total_profit_inflation():
    tiers = revise_profit_tiers(
        qdep_tiers={"quote_dependent_positive": True, "ge_25k": True, "ge_50k": True},
        quantity_grade="QUANTITY_C_UNIT_ONLY",
        max_buy={"supplier_quote_target": 1000},
        gov_unit=2000,
    )
    assert tiers["ge_25k"] is False
    assert tiers["ge_50k"] is False
    assert tiers["basis"] == "UNIT_MARGIN_POSITIVE"


def test_configuration_and_identity():
    assert classify_identity_state({"mpn": "ABC"}, {}) == "IDENTITY_EXACT"
    assert classify_identity_state({"manufacturer": "Ford", "model": "F-150"}, {}) == "IDENTITY_STRONG"


def test_platform_history_and_award_normalization():
    inv = platform_history_inventory()
    assert "BidNet" in inv["adapters"]
    assert detect_platform({"url": "https://www.bidnetdirect.com/x"}) == "BidNet"
    aw = normalize_award_tabulation(
        {"buyer": "City", "vendor": "Acme", "unit_price": 100, "quantity": 2, "solicitation_id": "1"}
    )
    assert aw["buyer"] == "City"
    assert aw["unit_price"] == 100
    res = fetch_platform_history({"agency": "City", "url": "https://sam.gov/opp/1", "historical_unit_price": 50})
    assert res["platform"] == "SAM"


def test_product_and_supplier_graph():
    link_product_history(
        product_key="F-150",
        manufacturer="Ford",
        mpn_model="F-150",
        buyer="City A",
        price=48000,
        date="2025-01-01",
    )
    q = query_product_history("F-150")
    assert q["hit"] is True
    link_supplier(supplier="ford.com", manufacturer="Ford", exact_model="F-150", authorization="AUTHORIZED_CONFIRMED")


def test_evidence_exhausted_and_no_fixed_caps():
    assert STAGE3_NO_ROW_CAP and DEEP_RESEARCH_NO_FIXED_COUNT
    assert assert_no_fixed_positive_cap(500) is True
    assert obsolete_rule_active("LEGACY_LOOSE_CATEGORY_BENCHMARK_AS_VALIDATED") is False
    loop = run_gov_value_upgrade_loop(
        {"title": "Generic office supplies", "agency": "Town"},
        commercial={},
    )
    assert loop["EVIDENCE_EXHAUSTED"] or loop["grade_after"] in {GOV_VALUE_D, "GOV_VALUE_UNKNOWN"}
    assert loop["grade_after"] != GOV_VALUE_A


def test_supplier_upgrade_order():
    assert SUPPLIER_DISCOVERY_ORDER[0] == "oem_direct"
    loop = run_supplier_upgrade_loop(
        {"title": "Ford F-150"},
        commercial={"manufacturer": "Ford", "model": "F-150"},
        suppliers=[{"supplier_domain": "ford.com", "source_type": "OEM"}],
    )
    assert loop["authorization_checked"] is True


def test_discovery_gaps_generated():
    gaps = generate_discovery_gaps(
        platform_counts=Counter({"SAM": 100}),
        state_counts=Counter({"TX": 2}),
        report_sources=[{"source_platform": "SAM", "opportunities_returned": 100, "award_history_access": 0}],
        attach_missing=80,
        rows_n=100,
    )
    cats = {g["category"] for g in gaps}
    assert GAP_PLATFORM_ENUMERATION in cats
    audit = audit_discovery_coverage(
        [{"source": "SAM", "title": "Truck", "agency": "City", "our_bid_access": "YES", "url": "https://sam.gov/1"}]
    )
    assert audit["kind"] == "DiscoveryCoverageAudit"
    assert audit["national_coverage_claimed"] is False


def test_economic_confidence_matrix():
    m = confidence_matrix(GOV_VALUE_D, SUPPLIER_A)
    assert m["economic_confidence"] == "RECON_ONLY"
    m2 = confidence_matrix(GOV_VALUE_A, SUPPLIER_A)
    assert m2["economic_confidence"] == "HIGH"
    assert m2["allows_validated"] is True


def test_original_solicitation_preserved_no_outreach():
    audit = audit_quote_positive(
        {
            "title": "Switch Pressure",
            "agency": "DOD",
            "solicitation_id": "SPRTA126Q0448",
            "original_posting_url": "https://sam.gov/opp/46ef1429127d4fdf9973a8688628e04b/view",
        },
        commercial={"mpn": "1274M99P01", "nsn": "5930012154689"},
        gov={"state": "GOV_VALUE_EXACT", "tier": "A", "unit_value": 5937, "source": "historical_award_unit_price"},
        suppliers=[
            {
                "supplier_domain": "geaerospace.com",
                "source_type": "OEM",
                "authorization_state": "AUTHORIZED_LIKELY",
                "product_fit": "EXACT",
                "exact_product_evidence": True,
            }
        ],
        qty_info={"quantity": 1, "quality": "UNIT_ONLY", "unit_only": True},
        max_buy={"supplier_quote_target": 5000},
        qdep={"tiers": {"quote_dependent_positive": True}},
        original={
            "original_source_verified": True,
            "original_posting_url": "https://sam.gov/opp/46ef1429127d4fdf9973a8688628e04b/view",
        },
        deadline_days=30,
        attempt_upgrades=False,
    )
    assert audit["gov_grade"] == GOV_VALUE_A


def test_grade_quantity_exact():
    g = grade_quantity({"quantity": 5, "quality": "EXACT"})
    assert g["grade"] == "QUANTITY_A_EXACT"
