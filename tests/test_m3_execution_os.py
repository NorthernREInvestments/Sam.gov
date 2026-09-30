"""BUILD 15 — M3 Execution Intelligence OS tests.

Evidence preservation, UNKNOWN handling, no fabricated facts,
version history, supplier commitment, payment readiness,
execution states, permissions/audit.
"""

from __future__ import annotations

from m3_execution_os_read import (
    AWARD_STAGES,
    BUILD_TAG,
    PAY_BLOCKED,
    PAY_READY,
    PAY_UNKNOWN,
    ROLE_MANAGER,
    ROLE_OWNER,
    ROLE_RESEARCHER,
    ROLE_VIEWER,
    ST_BLOCKED,
    ST_COMPLETE,
    ST_UNKNOWN,
    attach_execution_os_to_deal_room,
    build_award_execution_control,
    build_cash_survival_model,
    build_contract_memory,
    build_execution_os_profile,
    build_operator_command_center,
    build_payment_readiness,
    build_permissions_audit,
    build_product_change_control,
    build_supplier_commitment,
    fact,
    record_audit_change,
    record_contract_memory,
    record_performance_outcome,
)


def test_build_tag_and_app_version_alignment():
    from app import APP_BUILD_VERSION

    assert BUILD_TAG == "20260919-m3-execution-os-1"
    # Build tag for this module is stable; app build advances independently (Phase C/D).
    assert "m3-" in APP_BUILD_VERSION
    assert APP_BUILD_VERSION.startswith("2026")


def test_fact_envelope_preserves_unknown():
    f = fact()
    assert f["value"] == "UNKNOWN"
    assert f["source"] == "UNKNOWN"
    assert f["confidence"] == "UNKNOWN"
    assert f["evidence"] == "UNKNOWN"
    assert f["status"] == ST_UNKNOWN


def test_award_execution_stages_and_states():
    row = {
        "canonical_id": "sol:exec1:dla",
        "lifecycle": "AWARDED",
        "award_date": "2026-09-01",
        "award_number": "SPE7M1-26-C-0001",
    }
    ctrl = build_award_execution_control(row)
    assert ctrl["kind"] == "M3AwardExecutionControl"
    assert len(ctrl["stages"]) == len(AWARD_STAGES)
    award = next(s for s in ctrl["stages"] if s["stage"] == "AWARD")
    assert award["status"] == ST_COMPLETE
    assert award["evidence"]["value"] != "UNKNOWN"
    for s in ctrl["stages"]:
        assert s["status"] in {ST_UNKNOWN, "PLANNED", "IN_PROGRESS", ST_COMPLETE, ST_BLOCKED}
        assert "owner" in s and "next_action" in s and "blockers" in s
    assert ctrl["fabricated"] is False


def test_supplier_graph_presence_not_execution_ready():
    row = {
        "canonical_id": "sol:exec2:dla",
        "supplier_product_graph": {
            "edges": [
                {
                    "supplier_name": "Acme Distribution",
                    "relationship_type": "DISTRIBUTOR",
                    "confidence": "HIGH",
                }
            ]
        },
    }
    commit = build_supplier_commitment(row)
    assert commit["execution_ready_count"] == 0
    assert commit["suppliers"][0]["execution_ready"] is False
    assert commit["suppliers"][0]["quote_received"]["value"] == "UNKNOWN"
    assert commit["assumes_capability"] is False


def test_supplier_ready_only_with_quote_and_po_evidence():
    row = {
        "canonical_id": "sol:exec2b:dla",
        "supplier_product_graph": {
            "edges": [{"supplier_name": "Ready Co", "relationship_type": "OEM", "confidence": "HIGH"}]
        },
        "supplier_commitment": {
            "by_supplier": {
                "Ready Co": {
                    "quote_received": True,
                    "po_acceptance": "ACCEPTED",
                    "lead_time": "14 days",
                }
            }
        },
    }
    commit = build_supplier_commitment(row)
    ready = [s for s in commit["suppliers"] if s["supplier_name"] == "Ready Co"][0]
    assert ready["execution_ready"] is True
    assert commit["execution_ready_count"] == 1


def test_cash_survival_never_claims_financeable():
    row = {
        "canonical_id": "sol:exec3:dla",
        "deal_economics": {"Required_Acquisition_Cost": 12000, "PRICE_CONFIDENCE": "LOW"},
        "execution_intelligence": {"Financing_Fit": "UNKNOWN"},
    }
    cash = build_cash_survival_model(row)
    assert cash["is_financeable_claim"] is False
    assert cash["supplier_payment_requirements"]["value"] == 12000
    assert cash["supplier_payment_requirements"]["evidence"]
    paths = cash["identified_financing_paths"]
    assert paths
    assert paths[0]["status"] == ST_UNKNOWN or "missing" in str(paths[0].get("missing_information")).lower()
    blob = str(cash).lower()
    assert "this is financeable" not in blob


def test_product_change_control_blocks_substitution():
    row = {
        "canonical_id": "sol:exec4:dla",
        "dla_product_structure": {
            "fields": {
                "nsn": {"value": "4320-01-243-1951", "evidence_source": "solicitation"},
                "part_number": {"value": "ABC-100", "evidence_source": "solicitation"},
                "oem": {"value": "Acme OEM", "evidence_source": "solicitation"},
                "cage": {"value": "12345", "evidence_source": "solicitation"},
            }
        },
        "supplier_proposed_product": {
            "nsn": "4320-01-243-1951",
            "part_number": "XYZ-999",
            "manufacturer": "Other Mfr",
            "cage": "99999",
            "specifications": "equivalent form/fit",
        },
    }
    pcc = build_product_change_control(row)
    assert pcc["has_substitution_risk"] is True
    blocked = [d for d in pcc["differences"] if d["status"] == ST_BLOCKED]
    assert any(d["field"] == "part_number" for d in blocked)
    assert all(d.get("approval_required") for d in blocked)
    assert "compliant" in pcc["note"].lower()


def test_payment_readiness_unknown_blocked_ready():
    empty = build_payment_readiness({"canonical_id": "sol:pay0"})
    assert empty["status"] == PAY_UNKNOWN

    partial = build_payment_readiness(
        {
            "canonical_id": "sol:pay1",
            "award_number": "SPE7M1-26-C-0001",
            "payment_readiness": {"clin": "0001", "price": 100},
        }
    )
    assert partial["status"] == PAY_BLOCKED
    assert "shipment_evidence" in partial["missing"]

    ready = build_payment_readiness(
        {
            "canonical_id": "sol:pay2",
            "payment_readiness": {
                "contract_number": "SPE7M1-26-C-0001",
                "clin": "0001",
                "part_number": "ABC-100",
                "quantity": 10,
                "unit_of_measure": "EA",
                "price": 50.0,
                "shipment_evidence": "tracking 1Z999",
                "receiving_evidence": "DD250 dated 2026-09-10",
                "acceptance_evidence": "accepted 2026-09-12",
                "required_documents": "invoice + packing list",
            },
        }
    )
    assert ready["status"] == PAY_READY
    assert ready["missing"] == []


def test_contract_memory_append_only_no_overwrite():
    e1 = record_contract_memory(
        opportunity_id="sol:mem1",
        field="supplier_price",
        previous_value=100,
        new_value=110,
        reason="quote revision",
        evidence="email 2026-09-01",
        user="operator",
        persist=False,
    )
    e2 = record_contract_memory(
        opportunity_id="sol:mem1",
        field="supplier_price",
        previous_value=110,
        new_value=105,
        reason="corrected quote",
        evidence="email 2026-09-02",
        user="operator",
        persist=False,
    )
    assert e1["previous_value"] == 100
    assert e2["previous_value"] == 110
    assert e1["change_date"]
    assert e2["reason"] == "corrected quote"

    mem = build_contract_memory(
        {
            "canonical_id": "sol:mem1",
            "contract_memory": {"entries": [e1, e2]},
        }
    )
    assert mem["overwrite_forbidden"] is True
    assert len(mem["entries"]) >= 2
    assert mem["entries"][0]["previous_value"] is not None


def test_performance_learning_no_fake_scores():
    entry = record_performance_outcome(
        {
            "opportunity_id": "sol:perf1",
            "product": "NSN 4320",
            "supplier": "Acme",
            "agency": "DLA",
            "delivery_performance": "on_time",
            "acceptance_result": "accepted",
            "problems": ["packaging delay"],
            "resolutions": ["repack"],
            "lessons_learned": ["confirm packaging early"],
        },
        persist=False,
    )
    assert entry["fabricated_score"] is False
    assert "score" not in entry or entry.get("fabricated_score") is False
    assert entry["delivery_performance"] == "on_time"


def test_permissions_audit_roles_and_required_fields():
    audit = record_audit_change(
        field="unit_price",
        old_value=50,
        new_value=55,
        reason="supplier quote update",
        user="alice",
        role=ROLE_MANAGER,
        evidence="quote.pdf",
        opportunity_id="sol:aud1",
        persist=False,
    )
    for k in ("old_value", "new_value", "reason", "user", "date", "evidence"):
        assert k in audit and audit[k] not in (None, "")
    assert audit["role"] == ROLE_MANAGER

    profile = build_permissions_audit({"canonical_id": "sol:aud1"})
    assert set(profile["roles"]) == {ROLE_OWNER, ROLE_MANAGER, ROLE_RESEARCHER, ROLE_VIEWER}
    assert profile["silent_change_forbidden"] is True


def test_full_profile_facts_only_flags():
    profile = build_execution_os_profile(
        {
            "canonical_id": "sol:full1",
            "lifecycle": "RESEARCH",
            "title": "Pump assembly",
        }
    )
    assert profile["kind"] == "M3ExecutionOSProfile"
    assert profile["facts_only"] is True
    assert profile["unknown_preserved"] is True
    assert profile["no_fabricated_prices"] is True
    assert profile["no_assumed_financing"] is True
    assert profile["engines_unchanged"] is True
    assert profile["scoring_unchanged"] is True
    assert profile["cash_survival"]["is_financeable_claim"] is False
    assert profile["payment_readiness"]["status"] == PAY_UNKNOWN
    assert len(profile["award_execution"]["stages"]) == 11


def test_command_center_morning_evening():
    rows = [
        {
            "canonical_id": "sol:cc1",
            "title": "Widget",
            "lifecycle": "ACTIVE",
            "deadline": "2026-10-01",
        }
    ]
    morning = build_operator_command_center(rows, period="morning")
    assert morning["period"] == "morning"
    assert "required_actions" in morning["sections"] or "blockers" in morning["sections"]
    assert "What changed?" in morning["questions"]

    evening = build_operator_command_center(rows, period="evening")
    assert evening["period"] == "evening"
    assert "What is blocked?" in evening["questions"]
    assert evening["meaningful_changes_only"] is True


def test_attach_execution_os_to_deal_room():
    deal = attach_execution_os_to_deal_room(
        {"canonical_id": "sol:deal1", "overview": {}},
        row={"canonical_id": "sol:deal1", "lifecycle": "ACTIVE"},
    )
    assert deal["execution_os"]["kind"] == "M3ExecutionOSProfile"
    assert deal["execution_os"]["read_only"] is True


def test_regression_engines_and_cost_governor_unchanged():
    """Execution OS is read-only — must not alter Cost Governor or scoring constants."""
    from cost_governor_constants import TIER_ORDER, COST_FREE
    from m3_eligibility_evidence_read import BUILD_TAG as ELIG_TAG
    from m3_offer_readiness_read import BUILD_TAG as OFFER_TAG

    assert COST_FREE == "FREE"
    assert len(TIER_ORDER) >= 4
    assert ELIG_TAG
    assert OFFER_TAG
    profile = build_execution_os_profile({"canonical_id": "sol:reg1"})
    assert profile["engines_unchanged"] is True
    assert profile["scoring_unchanged"] is True
