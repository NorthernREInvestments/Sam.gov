"""Tests for BidNet/Euna abandonment + product-line validation + admission helpers."""

from __future__ import annotations

from anonymous_source_policy_v1 import (
    bidnet_abandoned,
    euna_abandoned,
    filter_out_abandoned_sources,
    is_bidnet_source_id,
)
from buyer_intelligence.product_line_validation_v1 import (
    COMMERCIALLY_SOURCEABLE_GENERIC,
    EXACT_PRODUCT_IDENTIFIED,
    INVALID_EXTRACTION,
    validate_product_line,
)


def test_bidnet_and_euna_abandoned_by_default(monkeypatch):
    monkeypatch.delenv("M3_BIDNET_ABANDONED", raising=False)
    monkeypatch.delenv("M3_EUNA_ABANDONED", raising=False)
    assert bidnet_abandoned() is True
    assert euna_abandoned() is True


def test_filter_removes_bidnet_rows(monkeypatch):
    monkeypatch.setenv("M3_BIDNET_ABANDONED", "true")
    monkeypatch.setenv("M3_EUNA_ABANDONED", "true")
    rows = [
        {"source_id": "network_bidnet_california", "platform_family": "BidNet", "list_url": "https://x"},
        {"source_id": "structured_socrata_nyc", "platform_family": "Socrata", "list_url": "https://y"},
        {"source_id": "exp_bonfire_x", "platform_family": "Bonfire", "adapter_family": "live_bonfire", "list_url": "https://z"},
    ]
    kept = filter_out_abandoned_sources(rows)
    assert len(kept) == 1
    assert kept[0]["source_id"] == "structured_socrata_nyc"
    assert is_bidnet_source_id("live_bidnet")


def test_rejects_insurance_boilerplate():
    v = validate_product_line(
        {
            "description": "Commercial General Liability Insurance Requirements shall be $5,000,000 each",
            "quantity": "5",
            "uom": "each",
        }
    )
    assert v["classification"] == INVALID_EXTRACTION
    assert v["commercially_usable"] is False


def test_rejects_section_number_as_qty():
    v = validate_product_line(
        {"description": "Part number/trade name or equal clause", "quantity": "13.3", "uom": "each"}
    )
    assert v["commercially_usable"] is False


def test_accepts_generic_paper_goods():
    v = validate_product_line(
        {
            "description": "Copy Paper 8.5x11 20lb White Reams",
            "quantity": 40,
            "uom": "ream",
        }
    )
    assert v["classification"] == COMMERCIALLY_SOURCEABLE_GENERIC
    assert v["commercially_usable"] is True


def test_accepts_exact_mfr_mpn():
    v = validate_product_line(
        {
            "description": "Penn Valley Pump Model 6DDSX76CNU-MK2",
            "manufacturer": "Penn Valley",
            "part_number": "6DDSX76CNU-MK2",
            "quantity": 2,
            "uom": "ea",
        }
    )
    assert v["classification"] == EXACT_PRODUCT_IDENTIFIED
    assert v["commercially_usable"] is True


def test_admission_targets_include_nyc_and_planetbids():
    from anonymous_source_admission_v1 import default_admission_targets

    ids = {t["source_id"] for t in default_admission_targets()}
    assert "admit_nyc_current_solicitations" in ids
    assert "admit_planetbids_sandiego" in ids
    assert "admit_caleprocure" in ids
