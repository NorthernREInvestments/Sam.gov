"""Phase G provenance + funnel unit tests (no live network)."""

from __future__ import annotations

from phase_g.provenance import (
    PROVENANCE_FROZEN_REAL_SOURCE,
    PROVENANCE_LIVE_SOURCE,
    counts_as_live_proof,
    normalize_provenance,
    stamp_live_source,
)


def test_provenance_classes_distinct():
    assert normalize_provenance("LIVE_SOURCE") == PROVENANCE_LIVE_SOURCE
    assert normalize_provenance("REAL_SOURCE_FIXTURE") == PROVENANCE_FROZEN_REAL_SOURCE
    assert counts_as_live_proof("LIVE_SOURCE") is True
    assert counts_as_live_proof("REAL_SOURCE_FIXTURE") is False
    assert counts_as_live_proof("REALISTIC_FROZEN_FIXTURE") is False


def test_stamp_live_source_overrides_fixture_masquerade():
    row = {"canonical_id": "x", "source_type": "REAL_SOURCE_FIXTURE", "title": "Widget"}
    stamp_live_source(row, retrieved_at="2026-09-24T00:00:00+00:00", source_system="sam.gov")
    assert row["phase_g_provenance"] == PROVENANCE_LIVE_SOURCE
    assert row["source_provenance"] == PROVENANCE_LIVE_SOURCE
    assert row["live_source_system"] == "sam.gov"


def test_operator_action_priority_prefers_identity_before_generic_held():
    from phase_g.live_batch import _blocker_bucket, _operator_action

    blockers = [
        "ECONOMICS_INCOMPLETE_OR_UNKNOWN",
        "SUPPLIER_NOT_VALIDATED",
        "QUOTE_NOT_EXECUTABLE",
        "FINANCING_INCOMPATIBLE_OR_UNKNOWN",
        "PRODUCT_IDENTITY_UNCONFIRMED",
        "QUANTITY_UOM_UNCONFIRMED",
    ]
    action = _operator_action(
        {
            "ready_for_owner_approval": False,
            "execution_critical_blockers": blockers,
            "operator_next_action": "Economics must be explicitly complete",
        },
        rejected=False,
    )
    assert action == "VERIFY_PRODUCT_IDENTITY"
    assert _blocker_bucket(blockers, {}) == "identity_uom"


def test_evaluate_live_row_rejects_obvious_service_without_network():
    from phase_g.live_batch import evaluate_live_row

    row = {
        "canonical_id": "sol:svc",
        "title": "Architectural Consulting Services FY26",
        "description": "professional consulting staffing",
        "source_id": "fed_sam_contract_opportunities",
        "status": "OPEN",
    }
    ev = evaluate_live_row(row)
    assert ev["phase_g_provenance"] == PROVENANCE_LIVE_SOURCE
    assert ev["rejected"] is True or ev["is_product"] is False
    assert ev["ready_for_owner_approval"] is not True
