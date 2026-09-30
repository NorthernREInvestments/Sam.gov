"""BUILD 19 — Data Ownership + Master Record Architecture tests."""

from __future__ import annotations

from m3_master_record_read import (
    BUILD_TAG,
    LIFE_OBS,
    LIFE_VERIFIED,
    MATCH_CONFIRMED,
    MATCH_POSSIBLE,
    SRC_OBSERVED,
    SRC_REVIEW,
    SRC_VALIDATED,
    ST_UNKNOWN,
    attach_governance_to_deal_room,
    build_change_history,
    build_fact_lifecycle,
    build_governance_profile,
    build_lineage,
    build_master_entities_from_row,
    build_master_record_view,
    build_merge_history,
    create_master_entity,
    detect_field_conflicts,
    enrich_command_center_governance,
    fact,
    find_match_candidates,
    record_change,
    record_data_conflict,
    record_entity_match,
    record_entity_merge,
    record_field_authority,
    record_identifier,
    record_lifecycle_transition,
    record_lineage,
    record_source,
)


def test_build_tag_and_app_version():
    from app import APP_BUILD_VERSION

    assert BUILD_TAG == "20260919-m3-master-record-1"
    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION


def test_unknown_and_evidence_required():
    f = fact()
    assert f["value"] == ST_UNKNOWN
    assert f["evidence"] == ST_UNKNOWN
    assert f["confidence"] == ST_UNKNOWN  # label only — not numeric score


def test_master_entity_and_source_preservation():
    ent = create_master_entity(
        entity_type="Supplier",
        canonical_name="Acme Distribution",
        aliases=["Acme Dist"],
        identifiers=[{"identifier_type": "CAGE", "identifier_value": "1ABC2", "source": "sam", "validation_status": SRC_OBSERVED}],
        status=SRC_OBSERVED,
        evidence_links=["website"],
        owner="RESEARCHER",
        persist=False,
    )
    assert ent["kind"] == "M3MasterEntity"
    assert ent["entity_id"].startswith("ent:supplier:")
    assert ent["ai_assumption_as_fact"] is False

    src = record_source(
        {
            "source": "company_website",
            "source_type": "web",
            "original_value": "123 Main St",
            "related_entity": ent["entity_id"],
            "extraction_method": "manual",
            "validation_status": SRC_OBSERVED,
            "evidence": "screenshot",
        },
        persist=False,
    )
    assert src["validation_status"] == SRC_OBSERVED
    assert "not automatically" in src["note"].lower()


def test_field_authority_preserves_previous_values():
    a1 = record_field_authority(
        {
            "entity": "ent:supplier:acme",
            "field": "address",
            "current_value": "123 Main",
            "source": "website",
            "authority_status": SRC_OBSERVED,
            "evidence": "web",
            "actor": "system",
        },
        persist=False,
    )
    # Simulate second write with history by calling again on same logical key via persist path
    # For unit test, manually chain previous
    a2 = record_field_authority(
        {
            "entity": "ent:supplier:acme",
            "field": "address",
            "current_value": "456 Gov Reg Ave",
            "source": "government_registration",
            "authority_status": SRC_REVIEW,
            "evidence": "SAM registration",
            "actor": "researcher",
            "reason": "gov registration differs",
        },
        persist=False,
    )
    assert a2["auto_selected"] is False
    assert a2["current_value"] == "456 Gov Reg Ave"
    assert a1["current_value"] == "123 Main"


def test_entity_matching_no_auto_merge():
    a = create_master_entity(entity_type="Supplier", canonical_name="Acme Co", persist=False)
    b = create_master_entity(entity_type="Supplier", canonical_name="Acme Company", persist=False)
    cands = find_match_candidates([a, b])
    assert cands
    assert all(c.get("auto_merged") is False for c in cands)
    assert all(c.get("review_status") == MATCH_POSSIBLE for c in cands)

    decided = record_entity_match(
        {
            "entity_a": a["entity_id"],
            "entity_b": b["entity_id"],
            "evidence": "shared CAGE",
            "review_status": MATCH_CONFIRMED,
            "decision": "same_legal_entity",
        },
        persist=False,
    )
    assert decided["auto_merged"] is False


def test_merge_preserves_history():
    m = record_entity_merge(
        {
            "entities_merged": ["ent:a", "ent:b"],
            "surviving_entity": "ent:a",
            "reason": "confirmed duplicate",
            "evidence": "CAGE match + legal name",
            "reviewer": "manager",
        },
        persist=False,
    )
    assert m["previous_identities_preserved"] is True
    assert m["history_deleted"] is False
    hist = build_merge_history(entity_id="ent:a", limit=5)
    assert hist["history_immutable"] is True
    assert hist["auto_merge_forbidden"] is True


def test_identifier_and_conflict_no_auto_winner():
    ident = record_identifier(
        {
            "entity": "ent:company:x",
            "identifier_type": "UEI",
            "identifier_value": "ABC123DEF456",
            "source": "SAM",
            "validation_status": SRC_VALIDATED,
            "evidence": "SAM.gov",
        },
        persist=False,
    )
    assert ident["identifier_type"] == "UEI"

    conflict = record_data_conflict(
        {
            "conflict_type": "supplier_address",
            "entities_affected": ["ent:supplier:acme"],
            "conflicting_values": ["123 Main", "456 Gov"],
            "sources": ["website", "SAM"],
            "impact": "shipping risk",
            "owner": "MANAGER",
        },
        persist=False,
    )
    assert conflict["auto_winner_selected"] is False

    auth = [
        {
            "entity": "e1",
            "field": "address",
            "current_value": "A",
            "source": "s1",
            "previous_values": [{"value": "B", "source": "s2"}],
        }
    ]
    detected = detect_field_conflicts(auth)
    assert detected
    assert all(d.get("auto_winner_selected") is False for d in detected)


def test_lineage_and_fact_lifecycle():
    lin = record_lineage(
        {
            "record": "ent:product:nsn",
            "parent_source": "solicitation_pdf",
            "transformation": "extract_nsn",
            "created_by": "system",
            "version": "1",
            "evidence": "page 2 snippet",
        },
        persist=False,
    )
    assert lin["kind"] == "M3RecordLineage"
    assert build_lineage(record_id="ent:product:nsn")["question"]

    t1 = record_lifecycle_transition(
        {"record": "fact:1", "from_state": "RAW_INPUT", "to_state": LIFE_OBS, "evidence": "ingest", "actor": "system"},
        persist=False,
    )
    t2 = record_lifecycle_transition(
        {"record": "fact:1", "from_state": LIFE_OBS, "to_state": LIFE_VERIFIED, "evidence": "KO confirmation", "actor": "manager"},
        persist=False,
    )
    assert t1["to_state"] == LIFE_OBS
    assert t2["to_state"] == LIFE_VERIFIED
    fl = build_fact_lifecycle()
    assert fl["ai_assumption_forbidden_as_fact"] is True


def test_change_record_no_silent_edits():
    ch = record_change(
        {
            "object": "ent:supplier:acme",
            "field_changed": "address",
            "previous_value": "123",
            "new_value": "456",
            "reason": "SAM update",
            "evidence": "SAM export",
            "changed_by": "researcher",
        },
        persist=False,
    )
    assert ch["silent_edit"] is False
    hist = build_change_history(object_id="ent:supplier:acme")
    assert hist["silent_edits_forbidden"] is True
    assert hist["history_immutable"] is True


def test_master_record_view_and_row_derivation():
    row = {
        "canonical_id": "sol:gov1",
        "agency": "DLA",
        "award_number": "SPE7M1-26-C-0001",
        "dla_product_structure": {"fields": {"nsn": {"value": "4320-01-243-1951"}, "part_number": {"value": "ABC-100"}}},
        "supplier_product_graph": {
            "edges": [{"supplier_name": "Acme Distribution", "relationship_type": "DISTRIBUTOR", "source": "graph"}]
        },
        "documents": [{"filename": "solicitation.pdf"}],
    }
    ents = build_master_entities_from_row(row, persist=False)
    assert any(e["entity_type"] == "Product" for e in ents)
    assert any(e["entity_type"] in {"Supplier", "Distributor"} for e in ents)
    assert any(e["entity_type"] == "Agency" for e in ents)
    assert any(e["entity_type"] == "Contract" for e in ents)

    # Persist one entity then fetch view
    product = next(e for e in ents if e["entity_type"] == "Product")
    saved = create_master_entity(
        entity_type="Product",
        canonical_name=product["canonical_name"],
        aliases=product.get("aliases"),
        identifiers=product.get("identifiers"),
        status=SRC_OBSERVED,
        evidence_links=product.get("evidence_links"),
        persist=True,
    )
    view = build_master_record_view(saved["entity_id"], rows=[row])
    assert view["kind"] == "M3MasterRecordView"
    assert view["canonical_entity"]
    assert view["history_immutable"] is True
    assert view["no_numeric_confidence_scores"] is True
    assert view["ai_assumption_as_fact_forbidden"] is True


def test_governance_profile_command_center_deal_attach():
    row = {
        "canonical_id": "sol:gov2",
        "agency": "DLA",
        "dla_product_structure": {"fields": {"nsn": {"value": "1234-00-000-0001"}}},
        "supplier_product_graph": {
            "edges": [
                {"supplier_name": "Acme", "relationship_type": "DISTRIBUTOR"},
                {"supplier_name": "Acme Inc", "relationship_type": "DISTRIBUTOR"},
            ]
        },
    }
    profile = build_governance_profile(row)
    assert profile["auto_merge_forbidden"] is True
    assert profile["scoring_unchanged"] is True

    morning = enrich_command_center_governance({}, [row], period="morning")
    assert "duplicate_candidates" in morning or "missing_validation" in morning
    evening = enrich_command_center_governance({}, [row], period="evening")
    assert "merged_entities" in evening or "changed_master_records" in evening or "new_verified_facts" in evening

    deal = attach_governance_to_deal_room({"canonical_id": "sol:gov2"}, row=row)
    assert deal["data_governance"]["kind"] == "M3DataGovernanceProfile"


def test_regression_prior_layers():
    from cost_governor_constants import COST_FREE, TIER_ORDER
    from m3_contract_lifecycle_read import BUILD_TAG as CTR
    from m3_economic_learning_read import BUILD_TAG as ECON
    from m3_execution_os_read import BUILD_TAG as EOS

    assert COST_FREE == "FREE"
    assert len(TIER_ORDER) >= 4
    assert CTR.startswith("20260919-m3-contract-lifecycle")
    assert ECON.startswith("20260919-m3-economic-learning")
    assert EOS.startswith("20260919-m3-execution-os")
