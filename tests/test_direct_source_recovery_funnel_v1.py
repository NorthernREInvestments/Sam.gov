"""Tests for BidNet-abandon funnel routing + historical recovery helpers."""

from __future__ import annotations

from historical_direct_source_recovery_v1 import (
    DIRECT_SOURCE_DOCUMENTS_RECOVERED,
    OFFICIAL_SOURCE_NOT_FOUND,
    SOLICITATION_EXPIRED,
    _classify_after_resolve,
    _is_bidnet_rec,
)
from phase_l.l23_full_population_funnel import canonical_id_for


def test_bidnet_rec_detection():
    assert _is_bidnet_rec({"platform": "live_bidnet", "title": "x"})
    assert _is_bidnet_rec({"authoritative_url": "https://www.bidnetdirect.com/x"})
    assert not _is_bidnet_rec({"platform": "Socrata", "authoritative_url": "https://data.cityofnewyork.us/x"})


def test_classify_documents_recovered():
    cls = _classify_after_resolve(
        {"deadline": "2099-01-01"},
        {"official_source_resolved": True, "resolved_opportunity_url": "https://x"},
        {"status": "VALID_FREE_PACKAGE_FOUND", "documents": [{"name": "a.pdf"}]},
    )
    assert cls == DIRECT_SOURCE_DOCUMENTS_RECOVERED


def test_classify_expired():
    cls = _classify_after_resolve(
        {"deadline": "2020-01-01"},
        {"official_source_resolved": True},
        {},
    )
    assert cls == SOLICITATION_EXPIRED


def test_classify_not_found():
    cls = _classify_after_resolve(
        {"buyer": "City of X", "deadline": "2099-01-01"},
        {},
        {},
    )
    assert cls == OFFICIAL_SOURCE_NOT_FOUND


def test_canonical_id_keeps_different_sols_separate():
    a = {"buyer": "City of Austin", "solicitation_number": "RFP-2026-001", "title": "Copy Paper"}
    b = {"buyer": "City of Austin", "solicitation_number": "RFP-2026-002", "title": "Copy Paper"}
    assert canonical_id_for(a) != canonical_id_for(b)


def test_canonical_id_same_sol_buyer_merges():
    a = {"buyer": "City of Austin", "solicitation_number": "RFP-2026-001", "title": "Copy Paper Lot 1"}
    b = {"buyer": "City of Austin", "solicitation_number": "RFP-2026-001", "title": "Copy Paper Lot 1 Amended"}
    assert canonical_id_for(a) == canonical_id_for(b)


def test_product_line_validation_rejects_boilerplate():
    from buyer_intelligence.product_line_validation_v1 import INVALID_EXTRACTION, validate_product_line

    v = validate_product_line(
        {"description": "Workers Compensation Insurance shall be provided", "quantity": 1, "uom": "ea"}
    )
    assert v["classification"] == INVALID_EXTRACTION


def test_reclassify_abandoned_pg_skips_when_bidnet_active(monkeypatch):
    from buyer_intelligence import full_scale_production_funnel_v1 as fspf

    monkeypatch.setattr(
        "anonymous_source_policy_v1.bidnet_abandoned",
        lambda: False,
    )
    out = fspf.reclassify_abandoned_bidnet_pg_blockers("job-x")
    assert out.get("skipped") is True
    assert out.get("reason") == "bidnet_not_abandoned"
