"""Tests for hard-miss recovery + denominator audit."""

from __future__ import annotations

from hard_miss_recovery.models import BUILD, DEAD_PRIMARY, SHORT_MPN_IDS, STRAT_SHORT_MPN
from hard_miss_recovery.route import classify_strategy, specialist_domains_for
from hard_miss_recovery.url_guards import is_collision_prone_mpn, reject_wrong_pack_blob


def test_build_id():
    assert BUILD.startswith("20261004-m3-hard-miss-recovery")


def test_short_mpn_strategy():
    item = {"benchmark_id": "light-sylvania-40771", "mpn": "40771", "manufacturer": "Sylvania", "category": "lighting"}
    assert classify_strategy(item, ["grainger.com"]) == STRAT_SHORT_MPN
    assert is_collision_prone_mpn("40771")


def test_specialists_skip_dead_primary():
    item = {"category": "plumbing", "manufacturer": "Watts", "mpn": "LF777M2-QT"}
    doms = specialist_domains_for(item)
    assert doms
    assert not any(d in DEAD_PRIMARY for d in doms)


def test_pack_guard():
    assert reject_wrong_pack_blob("Oatey gallon PVC cement 128-fl-oz", pack=1, price=178.5)
    assert not reject_wrong_pack_blob("Oatey 8 oz PVC cement", pack=1, price=8.5)
