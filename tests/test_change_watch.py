"""Change-order detection + 30-day closed retention (no live network)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from m3.research.change_watch import (
    CLOSED_RETENTION_DAYS,
    acknowledge_change,
    apply_change_event_to_package,
    compare_fingerprints,
    is_bid_protected,
    package_doc_fingerprint,
    retention_30d_decision,
)


def test_fingerprint_and_new_amendment_detected():
    base = package_doc_fingerprint(
        [
            {"source_url": "https://ex.test/sol.pdf", "content_hash": "aaa", "is_amendment": False},
        ]
    )
    updated = package_doc_fingerprint(
        [
            {"source_url": "https://ex.test/sol.pdf", "content_hash": "aaa", "is_amendment": False},
            {
                "source_url": "https://ex.test/addendum1.pdf",
                "content_hash": "bbb",
                "is_amendment": True,
            },
        ]
    )
    cmp = compare_fingerprints(base, updated)
    assert cmp["changed"] is True
    assert "new_document_urls" in cmp["reasons"] or "amendment_count_changed" in cmp["reasons"]
    assert any("addendum1" in u for u in cmp["added_urls"])


def test_baseline_only_is_not_a_change():
    cur = package_doc_fingerprint([{"source_url": "https://ex.test/a.pdf", "is_amendment": False}])
    cmp = compare_fingerprints(None, cur)
    assert cmp["changed"] is False
    assert cmp["baseline_only"] is True


def test_similar_part_unchanged():
    fp = package_doc_fingerprint(
        [{"source_url": "https://ex.test/a.pdf", "content_hash": "x", "is_amendment": False}]
    )
    cmp = compare_fingerprints(fp, fp)
    assert cmp["changed"] is False


def test_retention_30d_no_change_purges():
    now = datetime(2026, 12, 31, tzinfo=timezone.utc)
    close = datetime(2026, 11, 30, tzinfo=timezone.utc)
    d = retention_30d_decision(deadline_at=close, now=now, bid_protected=False, change_unresolved=False)
    assert d["purge_eligible"] is True
    assert CLOSED_RETENTION_DAYS == 30


def test_retention_within_30d_keeps():
    now = datetime(2026, 12, 15, tzinfo=timezone.utc)
    close = datetime(2026, 11, 30, tzinfo=timezone.utc)
    d = retention_30d_decision(deadline_at=close, now=now)
    assert d["purge_eligible"] is False


def test_change_after_close_extends_anchor():
    now = datetime(2026, 12, 20, tzinfo=timezone.utc)
    close = datetime(2026, 11, 30, tzinfo=timezone.utc)
    change = datetime(2026, 12, 5, tzinfo=timezone.utc)
    # 30d after change = 2027-01-04 — still keep on 12/20
    d = retention_30d_decision(
        deadline_at=close, now=now, last_change_at=change, change_unresolved=False
    )
    assert d["purge_eligible"] is False
    assert "2027-01-04" in (d["purge_after"] or "")


def test_bid_protected_and_unresolved_change_never_purge():
    close = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert retention_30d_decision(deadline_at=close, now=now, bid_protected=True)["purge_eligible"] is False
    assert (
        retention_30d_decision(deadline_at=close, now=now, change_unresolved=True)["purge_eligible"]
        is False
    )


def test_bid_protected_from_opportunity_dict():
    assert is_bid_protected({"status": "pursuing"}) is True
    assert is_bid_protected({"status": "saved", "raw_evidence": {"bid_submitted": True}}) is True
    assert is_bid_protected({"status": "saved", "raw_evidence": {}}) is False


def test_apply_change_event_roundtrip():
    class _Pkg:
        amendment_summary = {}

    pkg = _Pkg()
    fp1 = package_doc_fingerprint([{"source_url": "https://ex.test/a.pdf", "is_amendment": False}])
    r1 = apply_change_event_to_package(
        pkg, compare_fingerprints(None, fp1), fingerprint=fp1  # type: ignore[arg-type]
    )
    assert r1["status"] == "baseline_recorded"
    fp2 = package_doc_fingerprint(
        [
            {"source_url": "https://ex.test/a.pdf", "is_amendment": False},
            {"source_url": "https://ex.test/change.pdf", "is_amendment": True},
        ]
    )
    prev = pkg.amendment_summary["change_watch"]["fingerprint"]
    r2 = apply_change_event_to_package(
        pkg, compare_fingerprints(prev, fp2), fingerprint=fp2  # type: ignore[arg-type]
    )
    assert r2["status"] == "CHANGE_DETECTED"
    assert pkg.amendment_summary["change_watch"]["change_unresolved"] is True
    acknowledge_change(pkg)  # type: ignore[arg-type]
    assert pkg.amendment_summary["change_watch"]["change_unresolved"] is False
