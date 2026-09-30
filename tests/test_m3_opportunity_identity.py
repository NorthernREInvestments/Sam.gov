"""BUILD 1 — Opportunity Identity Graph foundation tests."""

from __future__ import annotations

from m3_opportunity_identity import (
    RESOLVED,
    UNRESOLVED,
    OpportunityIdentityResolver,
    compute_pipeline_canonical_id,
)
from solicitation_identity import identity_key


def test_sam_opportunity_resolves_correctly():
    r = OpportunityIdentityResolver()
    notice = "f72c4b22c0874fdea7bd26a475f290a4"
    ident = r.resolve(
        source_id="fed_sam_contract_opportunities",
        notice_id=notice,
        external_id=notice,
        solicitation_number="SPE7M126T9999",
    )
    assert ident["resolution_status"] == RESOLVED
    assert ident["opportunity_uid"] == f"opp:notice:{notice}"
    assert ident["notice_id"] == notice
    assert r.identity_count() == 1


def test_dla_opportunity_resolves_correctly():
    r = OpportunityIdentityResolver()
    notice = "a1b2c3d4e5f6789012345678abcdef01"
    row = {
        "source_id": "fed_sam_contract_opportunities",
        "notice_id": notice,
        "external_id": notice,
        "solicitation_number": "SPE4A726R0808",
        "agency": "DEFENSE LOGISTICS AGENCY",
        "title": "NSN 1680-01-482-4674 BALLSCREW",
        "is_dla": True,
    }
    pipe_id = identity_key(row)
    row["canonical_id"] = pipe_id
    ident = r.resolve_pipeline_row(row)
    assert ident["resolution_status"] == RESOLVED
    assert ident["notice_id"] == notice
    assert ident["solicitation_number"] == "SPE4A726R0808"
    assert ident["pipeline_canonical_id"] == pipe_id
    # Same DLA notice via discovered path
    disc = r.resolve_discovered(
        {
            "source_id": "fed_sam_contract_opportunities",
            "external_id": notice,
            "notice_id": notice,
            "solicitation_number": "SPE4A726R0808",
            "agency": "DEFENSE LOGISTICS AGENCY",
            "jurisdiction": "FEDERAL",
        }
    )
    assert disc["opportunity_uid"] == ident["opportunity_uid"]
    assert r.identity_count() == 1


def test_pipeline_row_resolves_to_same_identity():
    r = OpportunityIdentityResolver()
    notice = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    sam = r.resolve(notice_id=notice, source_id="fed_sam_contract_opportunities", external_id=notice)
    row = {
        "canonical_id": identity_key(
            {
                "solicitation_number": "SPE7M026T1234",
                "agency": "DLA",
                "external_id": notice,
                "source_id": "fed_sam_contract_opportunities",
            }
        ),
        "notice_id": notice,
        "external_id": notice,
        "source_id": "fed_sam_contract_opportunities",
        "solicitation_number": "SPE7M026T1234",
        "agency": "DLA",
    }
    # Ensure pipeline canonical matches helper
    assert row["canonical_id"] == compute_pipeline_canonical_id(row)
    pipe = r.resolve_pipeline_row(row)
    assert pipe["opportunity_uid"] == sam["opportunity_uid"]
    assert r.identity_count() == 1


def test_contract_promotion_preserves_identity():
    r = OpportunityIdentityResolver()
    notice = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    row = {
        "canonical_id": "sol:SPE7M026T5555:dla",
        "notice_id": notice,
        "external_id": notice,
        "source_id": "fed_sam_contract_opportunities",
        "solicitation_number": "SPE7M026T5555",
        "agency": "DLA",
    }
    before = r.resolve_pipeline_row(row)
    after = r.register_contract_promotion(pipeline_row=row, contract_id=42, contract_notice_id=notice)
    assert after["opportunity_uid"] == before["opportunity_uid"]
    assert after["contract_id"] == 42
    # Resolve via contract alone
    via_contract = r.resolve_contract({"id": 42, "notice_id": notice, "title": "Pump"})
    assert via_contract["opportunity_uid"] == before["opportunity_uid"]
    assert r.identity_count() == 1


def test_duplicate_resolution_does_not_create_duplicates():
    r = OpportunityIdentityResolver()
    notice = "cccccccccccccccccccccccccccccccc"
    a = r.resolve(notice_id=notice, external_id=notice, source_id="fed_sam")
    b = r.resolve(notice_id=notice, external_id=notice, source_id="fed_sam")
    c = r.resolve(notice_id=notice.upper(), pipeline_id="sol:SPE1:x")
    assert a["opportunity_uid"] == b["opportunity_uid"] == c["opportunity_uid"]
    assert r.identity_count() == 1
    assert len(r.by_uid[a["opportunity_uid"]]["aliases"]) >= 2


def test_unknown_identifiers_remain_unresolved():
    r = OpportunityIdentityResolver()
    out = r.resolve()
    assert out["resolution_status"] == UNRESOLVED
    assert out["opportunity_uid"] is None
    assert r.identity_count() == 0
    out2 = r.resolve(source_id="fed_sam")  # source alone insufficient
    assert out2["resolution_status"] == UNRESOLVED
    assert r.identity_count() == 0
