"""Research novelty + rotation acceptance tests."""

from __future__ import annotations

from buyer_intelligence.research_novelty_engine_v1 import (
    evidence_version_hash,
    record_research_attempt,
    should_revisit,
    task_fingerprint,
)


def test_identical_task_suppressed_on_unchanged_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    oid = "test_novelty_oid_001"
    eh = "abc123hash"
    record_research_attempt(
        oid,
        task="RESEARCH_PUBLIC_ACQUISITION_PRICE",
        method="public_web_deterministic",
        sources=["cached"],
        status="NO_NEW_EVIDENCE",
        evidence_hash=eh,
    )
    gate = should_revisit(
        oid,
        task="RESEARCH_PUBLIC_ACQUISITION_PRICE",
        method="public_web_deterministic",
        sources=["cached"],
        current_evidence_hash=eh,
    )
    assert gate["allowed"] is False
    assert "SUPPRESS" in gate["reason"] or "DIMINISHING" in gate["reason"] or "IDENTICAL" in gate["reason"]


def test_new_evidence_allows_reresearch(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    oid = "test_novelty_oid_002"
    record_research_attempt(
        oid,
        task="RESEARCH_PUBLIC_ACQUISITION_PRICE",
        method="public_web_deterministic",
        sources=["cached"],
        status="NO_NEW_EVIDENCE",
        evidence_hash="oldhash",
    )
    gate = should_revisit(
        oid,
        task="RESEARCH_PUBLIC_ACQUISITION_PRICE",
        method="public_web_deterministic",
        sources=["cached"],
        current_evidence_hash="newhash",
        new_documents=True,
    )
    assert gate["allowed"] is True


def test_task_fingerprint_stable():
    a = task_fingerprint("T", "M", ["b", "a"], "eh")
    b = task_fingerprint("T", "M", ["a", "b"], "eh")
    assert a == b


def test_evidence_hash_changes_with_mpn():
    e1 = {"TITLE": "x", "DEADLINE": "2026-12-01", "LINES": [{"MPN": "A", "QTY": 1}]}
    e2 = {"TITLE": "x", "DEADLINE": "2026-12-01", "LINES": [{"MPN": "B", "QTY": 1}]}
    assert evidence_version_hash(e1) != evidence_version_hash(e2)
