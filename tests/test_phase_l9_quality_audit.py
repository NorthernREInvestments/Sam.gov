"""Phase L.9 positive quality audit tests."""

from __future__ import annotations

from phase_l.acquisition_lanes import DEEP_RESEARCH_NO_FIXED_COUNT, STAGE3_NO_ROW_CAP
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap
from phase_l.product_page_resolution import EXACT_VERIFIED
from phase_l.quality_audit import (
    CONFIG_C_PARTIAL,
    GOV_VALUE_A,
    GOV_VALUE_B,
    GOV_VALUE_C,
    GOV_VALUE_D,
    PROMISING_NEEDS_SUPPLIER_CONFIRMATION,
    QUANTITY_A_EXACT,
    QUANTITY_C_UNIT_ONLY,
    RECON_ONLY_CATEGORY_BENCHMARK,
    RECON_ONLY_SUPPLIER_SEED,
    SECONDARY_QUOTE_TARGET,
    SUPPLIER_A,
    SUPPLIER_B,
    SUPPLIER_C,
    SUPPLIER_D,
    UNIT_MARGIN_POSITIVE,
    VALIDATED_QUOTE_TARGET,
    audit_quote_positive,
    confidence_matrix,
    grade_configuration,
    grade_government_value,
    grade_quantity,
    grade_supplier,
    revise_profit_tiers,
)


def test_gov_grades_a_b_c_d():
    a = grade_government_value(
        {"state": "GOV_VALUE_EXACT", "tier": "A", "unit_value": 50000, "source": "historical_award_unit_price"}
    )
    assert a["grade"] == GOV_VALUE_A
    b = grade_government_value(
        {"state": "GOV_VALUE_STRONG", "tier": "B", "unit_value": 40000, "source": "budget"}
    )
    assert b["grade"] == GOV_VALUE_B
    c = grade_government_value(
        {"state": "GOV_VALUE_COMPARABLE", "tier": "C", "unit_value": 35000, "source": "family"},
        commercial={"model": "F-150"},
    )
    assert c["grade"] == GOV_VALUE_C
    d = grade_government_value(
        {
            "state": "GOV_VALUE_RANGE",
            "tier": "C",
            "unit_value": 45000,
            "source": "GOVERNMENT_CATEGORY_BENCHMARK_RECON:fleet",
        }
    )
    assert d["grade"] == GOV_VALUE_D
    assert d["is_category_benchmark"] is True


def test_category_benchmark_cannot_validate_alone():
    audit = audit_quote_positive(
        {
            "title": "Fleet vehicles package",
            "agency": "City",
            "solicitation_id": "X1",
            "original_posting_url": "https://sam.gov/opp/x/view",
        },
        commercial={"manufacturer": "Ford"},
        gov={
            "state": "GOV_VALUE_RANGE",
            "tier": "C",
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
    assert audit["quality_state"] != VALIDATED_QUOTE_TARGET


def test_supplier_grades_and_seed_cannot_validate():
    assert grade_supplier(
        {
            "supplier_domain": "ford.com",
            "source_type": "OEM",
            "authorization_state": "AUTHORIZED_CONFIRMED",
            "product_fit": "EXACT",
            "exact_product_evidence": True,
        },
        commercial={"manufacturer": "Ford", "model": "F-150"},
    ) == SUPPLIER_A
    assert (
        grade_supplier(
            {"supplier_domain": "grainger.com", "source_type": "DISTRIBUTOR", "product_fit": "FAMILY"},
            commercial={},
        )
        == SUPPLIER_D
    )
    audit = audit_quote_positive(
        {
            "title": "NSN valve exact prior award",
            "agency": "DOD",
            "solicitation_id": "S2",
            "original_posting_url": "https://sam.gov/opp/y/view",
            "quantity": 10,
            "uom": "each",
        },
        commercial={"mpn": "V-1"},
        gov={"state": "GOV_VALUE_EXACT", "tier": "A", "unit_value": 500, "source": "historical_award_unit_price"},
        suppliers=[{"supplier_domain": "grainger.com", "source_type": "DISTRIBUTOR", "product_fit": "FAMILY"}],
        qty_info={"quantity": 10, "quality": "EXACT"},
        max_buy={"supplier_quote_target": 400},
        qdep={"tiers": {"quote_dependent_positive": True, "ge_5k": True}},
        original={"original_source_verified": True},
        deadline_days=30,
        attempt_upgrades=False,
    )
    assert audit["supplier_grade"] == SUPPLIER_D
    assert audit["quality_state"] == RECON_ONLY_SUPPLIER_SEED


def test_authorization_and_validated_gate():
    audit = audit_quote_positive(
        {
            "title": "2027 Ford F-150 Police Responder",
            "agency": "County Sheriff",
            "solicitation_id": "V1",
            "quantity": 2,
            "uom": "vehicle",
            "place_of_performance": "TX",
            "original_posting_url": "https://sam.gov/opp/v1/view",
        },
        commercial={"manufacturer": "Ford", "model": "F-150 Police Responder"},
        gov={
            "state": "GOV_VALUE_EXACT",
            "tier": "A",
            "unit_value": 58000,
            "source": "historical_award_unit_price",
            "match_rationale": "exact",
        },
        history={"identity_match_type": "EXACT", "award_date": "2026-01-15"},
        suppliers=[
            {
                "supplier_domain": "ford.com",
                "source_type": "OEM",
                "authorization_state": "AUTHORIZED_CONFIRMED",
                "authorized_status": "AUTHORIZED_CONFIRMED",
                "product_fit": "EXACT",
                "exact_product_evidence": True,
            }
        ],
        qty_info={"quantity": 2, "quality": "EXACT"},
        max_buy={"supplier_quote_target": 45000, "thresholds": {"MAX_BUY_FOR_10K_PROFIT": 40000}},
        qdep={"tiers": {"quote_dependent_positive": True, "ge_10k": True, "ge_5k": True}},
        freight={"status": "FREIGHT_LOW_CONFIDENCE_RESERVE", "amount": 2000},
        original={"original_source_verified": True, "original_posting_url": "https://sam.gov/opp/v1/view"},
        deadline_days=21,
        attempt_upgrades=False,
    )
    assert audit["gov_grade"] == GOV_VALUE_A
    assert audit["supplier_grade"] in {SUPPLIER_A, SUPPLIER_B}
    assert audit["quality_state"] == VALIDATED_QUOTE_TARGET
    assert audit["send_authorized"] is False


def test_quantity_unit_only_suppresses_total_profit():
    rev = revise_profit_tiers(
        qdep_tiers={"quote_dependent_positive": True, "ge_25k": True, "ge_5k": True},
        quantity_grade=QUANTITY_C_UNIT_ONLY,
        max_buy={"supplier_quote_target": 40000},
        gov_unit=50000,
    )
    assert rev["basis"] == UNIT_MARGIN_POSITIVE
    assert rev["ge_25k"] is False
    assert rev["positive"] is True
    g = grade_quantity({"quantity": 5, "quality": "EXACT"})
    assert g["grade"] == QUANTITY_A_EXACT


def test_configuration_mismatch_and_secondary():
    cfg = grade_configuration(
        {"title": "Ford F-150 with upfit warranty package installation"},
        {"manufacturer": "Ford", "model": "F-150"},
    )
    assert cfg["grade"] == CONFIG_C_PARTIAL
    assert cfg["bare_vs_loaded_risk"] is True


def test_confidence_matrix_and_secondary():
    m = confidence_matrix(GOV_VALUE_A, SUPPLIER_A)
    assert m["allows_validated"] is True
    m2 = confidence_matrix(GOV_VALUE_C, SUPPLIER_A)
    assert m2.get("allows_secondary") is True
    m3 = confidence_matrix(GOV_VALUE_D, SUPPLIER_A)
    assert m3["allows_validated"] is False


def test_secondary_quote_target():
    audit = audit_quote_positive(
        {
            "title": "Bobcat ToolCat UW56",
            "agency": "City PW",
            "solicitation_id": "SEC1",
            "quantity": 1,
            "original_posting_url": "https://sam.gov/opp/s/view",
        },
        commercial={"manufacturer": "Bobcat", "model": "ToolCat UW56"},
        gov={"state": "GOV_VALUE_STRONG", "tier": "B", "unit_value": 65000, "source": "budget"},
        suppliers=[
            {"supplier_domain": "dealer1.example", "source_type": "DEALER", "product_fit": "FAMILY", "authorization_state": "AUTHORIZATION_UNKNOWN"},
            {"supplier_domain": "dealer2.example", "source_type": "DEALER", "product_fit": "FAMILY"},
        ],
        qty_info={"quantity": 1, "quality": "EXACT"},
        max_buy={"supplier_quote_target": 50000},
        qdep={"tiers": {"quote_dependent_positive": True, "ge_10k": True}},
        original={"original_source_verified": True},
        deadline_days=20,
        attempt_upgrades=False,
    )
    assert audit["gov_grade"] == GOV_VALUE_B
    assert audit["supplier_grade"] in {SUPPLIER_C, SUPPLIER_B}
    assert audit["quality_state"] in {SECONDARY_QUOTE_TARGET, PROMISING_NEEDS_SUPPLIER_CONFIRMATION, VALIDATED_QUOTE_TARGET}


def test_deadline_expiration_and_no_caps_no_outreach():
    audit = audit_quote_positive(
        {"title": "Truck", "solicitation_id": "E1", "original_posting_url": "https://sam.gov/x"},
        commercial={"manufacturer": "Ford", "model": "F-150"},
        gov={"state": "GOV_VALUE_EXACT", "tier": "A", "unit_value": 48000, "source": "historical_award_unit_price"},
        suppliers=[
            {
                "supplier_domain": "ford.com",
                "source_type": "OEM",
                "authorization_state": "AUTHORIZED_CONFIRMED",
                "product_fit": "EXACT",
                "exact_product_evidence": True,
            }
        ],
        qty_info={"quantity": 1, "quality": "EXACT"},
        max_buy={"supplier_quote_target": 38000},
        qdep={"tiers": {"quote_dependent_positive": True}},
        original={"original_source_verified": True},
        deadline_days=-1,
        attempt_upgrades=False,
    )
    assert audit["quality_state"] == "HARD_BLOCKED"
    assert_no_fixed_positive_cap(228)
    assert_no_fixed_positive_cap(5)
    assert STAGE3_NO_ROW_CAP and DEEP_RESEARCH_NO_FIXED_COUNT
    assert EXACT_VERIFIED == "EXACT_VERIFIED"


def test_original_source_preserved():
    from phase_l.original_solicitation import resolve_original_solicitation

    o = resolve_original_solicitation(
        {"title": "X", "agency": "City", "solicitation_id": "1", "original_posting_url": "https://sam.gov/opp/1/view"}
    )
    assert o["original_source_verified"] is True


def test_freight_material_blocker_and_confidence_ranking():
    from phase_l.quality_audit import (
        FREIGHT_MATERIAL_UNRESOLVED,
        FREIGHT_LOW_IMPACT,
        PROMISING_NEEDS_FREIGHT,
        CONFIG_A_EXACT,
        confidence_adjusted_opportunity_value,
        grade_freight,
        quote_priority_score_v2,
    )

    fr = grade_freight(
        {"title": "Skid steer loader delivery"},
        {"status": FREIGHT_MATERIAL_UNRESOLVED, "amount": None},
        headroom=3000,
    )
    assert fr == FREIGHT_MATERIAL_UNRESOLVED
    audit = audit_quote_positive(
        {
            "title": "Bobcat S770 skid steer",
            "agency": "County",
            "solicitation_id": "FR1",
            "original_posting_url": "https://sam.gov/opp/fr1/view",
            "quantity": 1,
        },
        commercial={"manufacturer": "Bobcat", "model": "S770"},
        gov={"state": "GOV_VALUE_STRONG", "tier": "B", "unit_value": 62000, "source": "budget"},
        suppliers=[
            {
                "supplier_domain": "bobcat.com",
                "source_type": "OEM",
                "authorization_state": "AUTHORIZED_LIKELY",
                "product_fit": "EXACT",
                "exact_product_evidence": True,
            }
        ],
        qty_info={"quantity": 1, "quality": "EXACT"},
        max_buy={"supplier_quote_target": 50000},
        qdep={"tiers": {"quote_dependent_positive": True, "ge_10k": True}},
        freight={"status": FREIGHT_MATERIAL_UNRESOLVED},
        original={"original_source_verified": True},
        deadline_days=25,
        attempt_upgrades=False,
    )
    assert audit["quality_state"] in {
        PROMISING_NEEDS_FREIGHT,
        SECONDARY_QUOTE_TARGET,
        VALIDATED_QUOTE_TARGET,
    }
    # Speculative high $D bench should not outrank strong exact mid-$ via confidence adjust
    weak = confidence_adjusted_opportunity_value(
        profit_tier={"positive": True, "ge_100k": True},
        gov_grade=GOV_VALUE_D,
        supplier_grade=SUPPLIER_D,
        quantity_grade=QUANTITY_C_UNIT_ONLY,
        config_grade=CONFIG_C_PARTIAL,
        freight=FREIGHT_MATERIAL_UNRESOLVED,
        deadline_days=30,
    )
    strong = confidence_adjusted_opportunity_value(
        profit_tier={"positive": True, "ge_10k": True, "ge_5k": True},
        gov_grade=GOV_VALUE_A,
        supplier_grade=SUPPLIER_A,
        quantity_grade=QUANTITY_A_EXACT,
        config_grade=CONFIG_A_EXACT,
        freight=FREIGHT_LOW_IMPACT,
        deadline_days=30,
    )
    assert strong["ConfidenceAdjustedOpportunityValue"] > weak["ConfidenceAdjustedOpportunityValue"]
    s_weak = quote_priority_score_v2(
        cav=weak,
        gov_grade=GOV_VALUE_D,
        supplier_grade=SUPPLIER_D,
        quantity_grade=QUANTITY_C_UNIT_ONLY,
        config_grade=CONFIG_C_PARTIAL,
        freight=FREIGHT_MATERIAL_UNRESOLVED,
        deadline_days=30,
        eligibility_ok=True,
        original_complete=True,
    )
    s_strong = quote_priority_score_v2(
        cav=strong,
        gov_grade=GOV_VALUE_A,
        supplier_grade=SUPPLIER_A,
        quantity_grade=QUANTITY_A_EXACT,
        config_grade=CONFIG_A_EXACT,
        freight=FREIGHT_LOW_IMPACT,
        deadline_days=30,
        eligibility_ok=True,
        original_complete=True,
    )
    assert (s_strong.get("score") or 0) >= (s_weak.get("score") or 0)


def test_recon_only_supplier_seed_state():
    assert RECON_ONLY_SUPPLIER_SEED == "RECON_ONLY_SUPPLIER_SEED"
    assert RECON_ONLY_CATEGORY_BENCHMARK == "RECON_ONLY_CATEGORY_BENCHMARK"
