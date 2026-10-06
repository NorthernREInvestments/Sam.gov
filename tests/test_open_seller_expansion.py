"""Unit tests for open alternate seller expansion."""

from __future__ import annotations

from open_seller_expansion.fingerprint import build_fingerprint, enrich_upc_from_html, normalize_mpn
from open_seller_expansion.models import HARD_FOCUS, HONEST_BASELINE_V3, IDENTITY_REFERENCE_DOMAINS
from open_seller_expansion.pools import is_identity_reference, seller_pool_for_item


def test_baseline_frozen_at_46():
    assert HONEST_BASELINE_V3 == 46


def test_identity_reference_domains():
    assert is_identity_reference("www.grainger.com")
    assert is_identity_reference("https://www.homedepot.com/p/x")
    assert not is_identity_reference("acmetools.com")
    assert "bradyid.com" in IDENTITY_REFERENCE_DOMAINS


def test_fingerprint_short_mpn_requires_mfr():
    fp = build_fingerprint(
        {
            "benchmark_id": "mro-crc-05005",
            "manufacturer": "CRC",
            "mpn": "05005",
            "description": "CRC Brakleen brake parts cleaner",
            "category": "mro",
        }
    )
    assert fp["short_mpn"] is True
    assert fp["requires_mfr_family"] is True
    assert fp["normalized_mpn"] == "05005"
    assert "CRC" in fp["canonical_title"]


def test_upc_enrichment():
    html = '<script type="application/ld+json">{"gtin12":"071247490040"}</script>'
    fp = enrich_upc_from_html(html, {"upc": None})
    assert fp.get("upc") == "071247490040"


def test_normalize_mpn():
    assert normalize_mpn("LF777M2-QT") == "LF777M2QT"


def test_seller_pool_skips_identity_refs():
    pool = seller_pool_for_item(
        {"manufacturer": "Watts", "mpn": "LF777M2-QT", "category": "plumbing", "benchmark_id": "easy-watts-lf777m2"}
    )
    assert pool
    assert "supplyhouse.com" not in pool
    assert "grainger.com" not in pool
    # should prefer open plumbing/tool alts
    assert any(d in pool for d in ("activeplumbing.com", "plumbingsupply.com", "parts-hvac.com", "faucet.com"))


def test_hard_focus_includes_known_cases():
    assert "easy-watts-lf777m2" in HARD_FOCUS
    assert "easy-brady-121943" in HARD_FOCUS
    assert "easy-makita-b-45580" in HARD_FOCUS
