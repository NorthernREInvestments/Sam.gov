"""Bid eligibility / compliance / nonmanufacturer gate for deep deal research."""

from __future__ import annotations

from typing import Any

from deep_deal_constants import (
    COMP_FAIL,
    COMP_LIKELY,
    COMP_NA,
    COMP_NEEDS,
    COMP_PASS,
    COMP_UNKNOWN,
    NMR_HARD,
    NMR_NEEDS,
    NMR_PASS,
    NMR_POTENTIAL,
)


def _item(status: str, *, reason: str, provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "provenance": provenance or {"verification_status": "UNKNOWN"},
    }


def evaluate_nonmanufacturer_rule(
    *,
    set_aside: str | None = None,
    manufacturer_requirement: bool | None = None,
    domestic_manufacturer_required: bool | None = None,
    waiver_visible: bool | None = None,
    size_standard: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Explicit NMR handling — no legal conclusions beyond evidence."""
    ev = evidence or {}
    set_aside_status = set_aside or ev.get("set_aside")
    mfr_req = manufacturer_requirement if manufacturer_requirement is not None else ev.get("manufacturer_requirement")
    domestic = domestic_manufacturer_required if domestic_manufacturer_required is not None else ev.get("domestic_manufacturer_requirement")
    waiver = waiver_visible if waiver_visible is not None else ev.get("waiver_visible")

    potentially_applicable = False
    if set_aside_status and str(set_aside_status).upper() not in {"UNRESTRICTED", "NONE", "FULL_AND_OPEN", ""}:
        potentially_applicable = True

    if mfr_req is True and waiver is not True:
        status = NMR_HARD
        reason = "manufacturer_status_required_without_visible_waiver"
    elif potentially_applicable and domestic is True and waiver is not True:
        status = NMR_POTENTIAL
        reason = "set_aside_with_domestic_manufacturer_signals"
    elif potentially_applicable:
        status = NMR_NEEDS
        reason = "set_aside_present_nmr_implications_unknown"
    elif set_aside_status is None:
        status = NMR_NEEDS
        reason = "set_aside_status_unknown"
    else:
        status = NMR_PASS
        reason = "no_set_aside_nmr_signal_detected"

    return {
        "set_aside_status": set_aside_status,
        "manufacturer_requirement": mfr_req,
        "nonmanufacturer_rule_potentially_applicable": potentially_applicable,
        "waiver_visible": waiver,
        "domestic_manufacturer_requirement": domestic,
        "size_standard": size_standard or ev.get("size_standard"),
        "nmr_status": status,
        "reason": reason,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


def evaluate_bid_compliance(
    *,
    opportunity: dict[str, Any] | None = None,
    extracted_facts: dict[str, Any] | None = None,
    entity_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Structured bid-qualification gate.
    Does NOT treat future/expected entity status as current verified compliance.
    """
    opp = opportunity or {}
    facts = extracted_facts or {}
    entity = entity_status or {}
    categories: dict[str, dict[str, Any]] = {}

    # Entity / SAM / CAGE — if not verified complete, NEEDS_VERIFICATION
    if entity.get("sam_registered") is True and entity.get("cage_code"):
        categories["entity_eligibility"] = _item(COMP_PASS, reason="entity_sam_cage_verified", provenance={"verification_status": "VERIFIED"})
        categories["sam_registration"] = _item(COMP_PASS, reason="sam_registered")
        categories["cage"] = _item(COMP_PASS, reason=f"cage={entity.get('cage_code')}")
    else:
        categories["entity_eligibility"] = _item(COMP_NEEDS, reason="operating_entity_sam_cage_not_verified_as_complete")
        categories["sam_registration"] = _item(COMP_NEEDS, reason="sam_registration_not_confirmed_for_operating_entity")
        categories["cage"] = _item(COMP_NEEDS, reason="cage_not_confirmed")

    set_aside = facts.get("set_aside")
    if set_aside in (None,):
        categories["set_aside"] = _item(COMP_UNKNOWN, reason="set_aside_not_visible_in_available_text")
    elif str(set_aside).upper() in {"UNRESTRICTED", "FULL_AND_OPEN"}:
        categories["set_aside"] = _item(COMP_PASS, reason="unrestricted_or_full_and_open")
    else:
        categories["set_aside"] = _item(COMP_NEEDS, reason=f"set_aside={set_aside}_requires_eligibility_check")

    if facts.get("naics"):
        categories["naics"] = _item(COMP_NEEDS, reason=f"naics={facts.get('naics')}_size_standard_not_verified")
    else:
        categories["naics"] = _item(COMP_UNKNOWN, reason="naics_not_found")

    if facts.get("brand_name_only") is True:
        categories["manufacturer_reseller_authorization"] = _item(
            COMP_NEEDS,
            reason="brand_name_only_may_require_authorized_channel",
        )
    elif facts.get("brand_or_equal") is True:
        categories["manufacturer_reseller_authorization"] = _item(COMP_LIKELY, reason="brand_or_equal_allows_alternates")
    else:
        categories["manufacturer_reseller_authorization"] = _item(COMP_UNKNOWN, reason="authorization_requirements_unknown")

    categories["licenses"] = _item(COMP_UNKNOWN, reason="license_requirements_not_extracted")
    categories["bonding"] = _item(
        COMP_FAIL if facts.get("performance_bond_required") is True and entity.get("bonding_capacity") is not True else (
            COMP_NEEDS if facts.get("bid_bond_required") or facts.get("performance_bond_required") else COMP_UNKNOWN
        ),
        reason="bonding_capacity_not_verified" if (facts.get("bid_bond_required") or facts.get("performance_bond_required")) else "bonding_not_visible",
    )
    if facts.get("performance_bond_required") is True and entity.get("bonding_capacity") is not True:
        categories["bonding"] = _item(COMP_NEEDS, reason="performance_bond_required_capacity_unknown")

    categories["insurance"] = _item(
        COMP_NEEDS if facts.get("insurance_required") else COMP_UNKNOWN,
        reason="insurance_required_or_unknown",
    )
    categories["experience"] = _item(COMP_UNKNOWN, reason="past_performance_requirements_not_extracted")
    categories["past_performance"] = _item(COMP_UNKNOWN, reason="past_performance_not_extracted")
    categories["security_clearance"] = _item(COMP_NA, reason="no_clearance_signal") if not re_search_clearance(facts, opp) else _item(COMP_FAIL, reason="security_clearance_signal")
    categories["site_visit"] = _item(
        COMP_NEEDS if facts.get("site_visit_required") else COMP_UNKNOWN,
        reason="site_visit_required" if facts.get("site_visit_required") else "site_visit_unknown",
    )
    categories["buy_american"] = _item(
        COMP_NEEDS if facts.get("buy_american") else COMP_UNKNOWN,
        reason="buy_american_clause_visible" if facts.get("buy_american") else "buy_american_unknown",
    )
    categories["taa"] = _item(
        COMP_NEEDS if facts.get("taa") else COMP_UNKNOWN,
        reason="taa_clause_visible" if facts.get("taa") else "taa_unknown",
    )
    categories["delivery_capability"] = _item(COMP_UNKNOWN, reason="delivery_capability_not_verified")
    categories["warranty_obligations"] = _item(COMP_UNKNOWN, reason="warranty_not_extracted")
    categories["mandatory_services"] = _item(
        COMP_NEEDS if facts.get("installation_required") else COMP_UNKNOWN,
        reason="installation_or_services_may_be_mandatory" if facts.get("installation_required") else "mandatory_services_unknown",
    )
    categories["mandatory_installation"] = categories["mandatory_services"]
    categories["financial_capacity"] = _item(COMP_NEEDS, reason="working_capital_path_not_yet_verified")
    categories["oem_certification"] = _item(
        COMP_NEEDS if facts.get("brand_name_only") else COMP_UNKNOWN,
        reason="oem_letter_may_be_required" if facts.get("brand_name_only") else "oem_unknown",
    )

    nmr = evaluate_nonmanufacturer_rule(
        set_aside=set_aside,
        evidence={
            "manufacturer_requirement": facts.get("brand_name_only"),
            "domestic_manufacturer_requirement": facts.get("buy_american"),
            "waiver_visible": None,
        },
    )
    categories["nonmanufacturer_rule"] = _item(
        {
            NMR_PASS: COMP_PASS,
            NMR_NEEDS: COMP_NEEDS,
            NMR_POTENTIAL: COMP_NEEDS,
            NMR_HARD: COMP_FAIL,
        }.get(nmr["nmr_status"], COMP_UNKNOWN),
        reason=nmr["reason"],
    )

    hard = [k for k, v in categories.items() if v.get("status") == COMP_FAIL]
    needs = [k for k, v in categories.items() if v.get("status") == COMP_NEEDS]
    passes = [k for k, v in categories.items() if v.get("status") in {COMP_PASS, COMP_LIKELY, COMP_NA}]

    overall = COMP_FAIL if hard else (COMP_NEEDS if needs else COMP_PASS)

    return {
        "categories": categories,
        "overall_compliance": overall,
        "hard_blockers": hard,
        "needs_verification": needs,
        "pass_or_na": passes,
        "nonmanufacturer": nmr,
        "entity_status_note": "Future/expected entity status is NOT treated as current verified compliance",
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


def re_search_clearance(facts: dict[str, Any], opp: dict[str, Any]) -> bool:
    blob = f"{facts} {opp.get('title')} {opp.get('description')}".lower()
    return "security clearance" in blob or "top secret" in blob
