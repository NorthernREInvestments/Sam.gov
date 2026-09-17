"""Source access recovery — taxonomy, backoff, DLA SAM fallback."""

from __future__ import annotations

from discovery.dla_fallback import probe_known_answer_status, search_sam_dla_product_opportunities
from discovery.source_backoff import compute_backoff_until, should_skip_source_for_backoff
from discovery.source_baseline import build_eligible_universe_snapshot, cluster_fix_priority
from discovery.source_failure_taxonomy import (
    ACCESS_FAILED,
    ACCESS_SUCCEEDED_PARSE_FAILED,
    BOT_CHALLENGE,
    PARSER_FAILURE,
    REGISTRATION_REQUIRED,
    classify_root_cause,
)


def test_classify_bot_vs_parser():
    bot = classify_root_cause({"ok": False, "source_stop_reason": "BOT_PROTECTED"})
    assert bot["primary"] == BOT_CHALLENGE
    assert bot["access_outcome"] == ACCESS_FAILED
    assert bot["external_access_block"] is True

    parse = classify_root_cause(
        {
            "ok": False,
            "source_stop_reason": "PARSER_FAILURE",
            "validation": {"failure_type": "PARSER_FAILURE", "http_status": 200},
            "request_meta": {"http_status": 200},
        }
    )
    assert parse["primary"] == PARSER_FAILURE
    assert parse["access_outcome"] == ACCESS_SUCCEEDED_PARSE_FAILED
    assert parse["software_fixable"] is True


def test_registration_backoff_long():
    until = compute_backoff_until(REGISTRATION_REQUIRED)
    assert until
    skip = should_skip_source_for_backoff(
        {
            "failure_class": REGISTRATION_REQUIRED,
            "last_failure_at": "2099-01-01T00:00:00+00:00",
            "backoff_hours": 168,
            "backoff_until": "2099-01-08T00:00:00+00:00",
        }
    )
    assert skip["skip"] is True
    assert skip["reason"] == "BACKOFF"


def test_baseline_snapshot_no_network():
    snap = build_eligible_universe_snapshot(per_source_metrics={})
    assert snap["eligible_count"] > 25
    assert "sources" in snap
    clusters = cluster_fix_priority(snap)
    assert isinstance(clusters, list)


def test_sam_dla_fallback_requires_authorize():
    out = search_sam_dla_product_opportunities(authorize_live=False)
    assert out["executed"] is False
    assert out["opportunities"] == []


def test_known_answer_probe_not_injection():
    from discovery.schema import CanonicalOpportunity

    discovered = [
        CanonicalOpportunity(
            external_id="SPE7M126T357A",
            source_id="fed_dla_sam_cross_publish",
            title="VALVE",
            solicitation_number="SPE7M126T357A",
        )
    ]
    probes = probe_known_answer_status(["SPE7M126T357A", "SPE7M126Q1500"], discovered=discovered)
    assert probes[0]["status"] == "OPEN_AND_FOUND"
    assert probes[1]["status"] == "UNVERIFIABLE"
