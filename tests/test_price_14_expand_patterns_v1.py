"""Tests for price-14 expand patterns."""

from __future__ import annotations

from price_14_expand_patterns.models import BUILD, READY_14_IDS
from price_14_expand_patterns.patterns import preferred_domains_for_item, ensure_pattern_db
from price_14_expand_patterns.price_url import _pack_compatible


def test_build():
    assert BUILD.startswith("20261005-m3-price-14-expand-patterns")
    assert len(READY_14_IDS) == 14


def test_pack_rejects_case_vs_pair():
    item = {"expected_uom": "PR", "expected_pack": 1, "description": "3M 2097 P100 particulate filter pair"}
    ok, reason = _pack_compatible(
        item,
        title="Not Available P100 Particulate Filter 50 pk/cs",
        url="https://www.platt.com/p/0321658/3m/not-available-p100-particulate-filter-50-pk-cs-2-pk-100-cs/mmm2097",
        price=950.61,
    )
    assert not ok
    assert "pack" in reason


def test_preferred_domains_hp_quill():
    ensure_pattern_db()
    prefs = preferred_domains_for_item({"manufacturer": "HP", "mpn": "CF410A", "category": "office"})
    domains = [p.get("domain") for p in prefs]
    assert "quill.com" in domains
