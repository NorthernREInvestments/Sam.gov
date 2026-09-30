"""Phase L.14.1 repair + structured source audit tests."""

from __future__ import annotations

from pathlib import Path

from phase_l.acquisition_lanes import STAGE3_NO_ROW_CAP
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap
from phase_l.l141_repair import (
    BUILD,
    FAILURE_TAXONOMY,
    PARKED_FRAGILE_SOURCE,
    REQUIRED_L14_ARTIFACTS,
    REQUIRED_L14_DOCS,
    TIER_1,
    TIER_4,
    audit_dedupe,
    audit_l14_artifacts,
    audit_profile,
    classify_source_outcome,
    label_inventory_freshness,
    reconcile_sources_from_checkpoint,
    source_engineering_value_score,
    structured_source_inventory,
)
from phase_l.public_artifact_types import LAST_KNOWN_RECENT, LIVE_FRESH
from phase_l.product_page_resolution import EXACT_VERIFIED


def test_required_artifacts_and_docs_exist():
    audit = audit_l14_artifacts()
    assert audit["all_required_present"] is True
    assert not audit["missing_artifacts"]
    assert not audit["missing_docs"]
    assert "working-directory" in audit["filenotfound_explanation"]


def test_failure_taxonomy_zero_vs_failed():
    assert classify_source_outcome(records=11) == "SUCCESS"
    assert classify_source_outcome(records=0, timed_out=False, failed=False) == "ZERO_RESULTS"
    assert classify_source_outcome(records=0, timed_out=True) == "TIMEOUT"
    assert classify_source_outcome(records=0, stop_reason="CLOUDFLARE") == "ANTI_BOT"
    assert classify_source_outcome(records=0, stop_reason="AUTH_REQUIRED") == "AUTH_REQUIRED"
    assert "ZERO_RESULTS" in FAILURE_TAXONOMY
    assert "SUCCESS" in FAILURE_TAXONOMY


def test_collapse_math_and_not_dedupe():
    recon = reconcile_sources_from_checkpoint()
    c = recon["collapse_analysis"]
    assert c["math_check"] == c["live_runner_unique"] or c["math_check"] == sum(
        (c.get("productive_sources") or {}).values()
    )
    assert any(r["id"] == "LIVE_RUNNER_UNIQUE_NOT_INVENTORY" for r in c["root_causes"])
    assert any(r["id"] == "NOT_BAD_DEDUPE" for r in c["root_causes"])
    d = audit_dedupe()
    assert d["collapse_caused_by_dedupe"] is False


def test_structured_inventory_and_tiers():
    inv = structured_source_inventory()
    assert inv["count"] >= 10
    plats = {i["platform"] for i in inv["items"]}
    assert "SAM" in plats
    assert "OpenGov" in plats
    assert "BidNet" in plats
    assert any(i["access_tier"] == TIER_1 for i in inv["items"])
    assert any(i["implementation_status"] == PARKED_FRAGILE_SOURCE for i in inv["items"])


def test_source_value_score_prefers_api():
    api = {
        "source_name": "SAM API",
        "access_tier": TIER_1,
        "implementation_status": "ACTIVE",
        "auth": "FREE_API_KEY",
    }
    fragile = {
        "source_name": "Bonfire",
        "access_tier": TIER_4,
        "implementation_status": PARKED_FRAGILE_SOURCE,
        "auth": "FREE_ACCOUNT",
    }
    assert source_engineering_value_score(api, unique_rows=50)["score"] > source_engineering_value_score(
        fragile, unique_rows=5
    )["score"]


def test_freshness_labeling():
    rows = label_inventory_freshness(
        [
            {"notice_id": "A1", "source_portal": "fed_sam_public_search"},
            {"notice_id": "B1", "source_portal": "unk"},
        ]
    )
    assert rows[0]["inventory_freshness"] == LIVE_FRESH
    assert rows[1]["inventory_freshness"] == LAST_KNOWN_RECENT


def test_profile_audit_and_no_caps():
    p = audit_profile()
    assert p["kind_caps"]["NETWORK"] <= 4
    assert_no_fixed_positive_cap()
    assert STAGE3_NO_ROW_CAP is True
    assert EXACT_VERIFIED == "EXACT_VERIFIED"
    assert BUILD.startswith("20260928-m3-phase-l141")


def test_required_lists_match_spec():
    assert "l14_summary.json" in REQUIRED_L14_ARTIFACTS
    assert "phase_l14_regression.md" in REQUIRED_L14_DOCS
    root = Path(__file__).resolve().parents[1]
    for name in REQUIRED_L14_ARTIFACTS:
        assert (root / "artifacts" / "phase_l" / name).exists()
