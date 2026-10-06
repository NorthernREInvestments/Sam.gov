"""Gap-closure accounting. Discovery coverage thresholds stay at 90%."""

from pathlib import Path

from bidnet_gap_closure.accounting import (
    build_accounting,
    classify_found_row,
    explain_unlisted_residual,
    retry_targets,
)
from bidnet_gap_closure.identity import stable_bidnet_key
from bidnet_gap_closure.report import format_gap_report


def test_discovery_threshold_not_retuned():
    text = Path("bidnet_discovery/partitioned.py").read_text(encoding="utf-8")
    assert "int(0.90 * int(ui_reported))" in text
    assert "int(0.95 * int(ui_reported))" not in text
    sweep = Path("bidnet_full_production/sweep.py").read_text(encoding="utf-8")
    assert 'int(0.90 * int(discovery.get("reported_open") or 1))' in sweep
    assert 'int(0.95 * int(discovery.get("reported_open")' not in sweep


def test_stable_key_collapses_national_and_state_urls():
    national = {"detail_url": "https://www.bidnetdirect.com/solicitations/1234567", "raw_metadata": {}}
    state = {
        "detail_url": "https://www.bidnetdirect.com/michigan/solicitations/1234567",
        "raw_metadata": {"bidnet_internal_id": "1234567"},
    }
    assert stable_bidnet_key(national) == stable_bidnet_key(state) == "id:1234567"


def test_retry_targets_skip_finished_states_and_national():
    parts = [
        {"partition_id": "national", "pages": 151, "retrieved": 3775, "pagination_complete": False},
        {"partition_id": "state_MI", "pages": 0, "retrieved": 0, "error": "empty_or_rate_limited"},
        {"partition_id": "state_CA", "pages": 99, "retrieved": 2472},
        {"partition_id": "state_OK", "pages": 1, "retrieved": 25},
        {"partition_id": "state_NC", "pages": 1, "retrieved": 18},
    ]
    ids = [p["partition_id"] for p in retry_targets(parts)]
    assert ids == ["state_MI", "state_OK"]


def test_classify_rate_limited_state_and_national_tail():
    row = {"solicitation_id": "9990001", "status": "OPEN", "raw_metadata": {"bidnet_internal_id": "9990001"}}
    assert (
        classify_found_row(row, partition_id="state_MI", prior={"error": "empty_or_rate_limited", "pages": 0, "retrieved": 0})
        == "TEMPORARY_FETCH_FAILURE"
    )
    assert classify_found_row(row, partition_id="national", prior={"pages": 151, "retrieved": 3775}) == "PAGINATION_MISS"
    assert classify_found_row({"title": "no id"}, partition_id="state_MI", prior={"pages": 0, "retrieved": 0}) == "MALFORMED_RECORD"


def test_conservation_closes_and_unknown_gate():
    rows = [
        {"key": f"id:{i}", "classification": "TEMPORARY_FETCH_FAILURE", "recovered": True}
        for i in range(2402)
    ]
    rows.append({"key": "id:closed", "classification": "STALE_OR_CLOSED", "recovered": False})
    report = build_accounting(
        classifications=rows + explain_unlisted_residual(found=len(rows), fresh_reported=24372),
        harvested_keys_before=10,
        harvested_keys_after=10,
    )
    cons = report["conservation"]
    assert cons["diff"] == 0
    assert cons["expected"] == cons["harvested"] + cons["classified_missing"]
    assert cons["classified_missing"] == cons["recovered"] + cons["terminal"] + cons["still_missing"]
    assert report["classification_counts"]["UNKNOWN"] <= 24
    assert report["gates"]["no_threshold_changes"] is True
    assert "MUST = 0" in format_gap_report(report)


def test_covered_pagination_residual_is_duplicate_not_unknown():
    rows = [
        {"key": f"id:{i}", "classification": "PAGINATION_MISS", "recovered": True}
        for i in range(7)
    ]
    rows.extend(
        {"key": f"malformed:{i}", "classification": "MALFORMED_RECORD", "recovered": False}
        for i in range(95)
    )
    explained = explain_unlisted_residual(
        found=len(rows),
        fresh_reported=24028,
        duplicate_slots=2403,
    )
    report = build_accounting(
        classifications=rows + explained,
        fresh_reported=24028,
        harvested_keys_before=10,
        harvested_keys_after=10,
    )
    assert report["classification_counts"]["STALE_OR_CLOSED"] == 344
    assert report["classification_counts"]["UNKNOWN"] == 0
    assert report["classification_counts"]["DUPLICATE_OR_MERGED"] == 2403 - 7 - 95 - 344
    assert report["gates"]["unknown_le_1pct"] is True
    assert report["conservation"]["diff"] == 0
    assert report["true_coverage"]["valid_open_retrieved"] == 21969 + 7
    assert report["PASS_FAIL"] == "PASS"
    assert report["BIDNET_DISCOVERY_TRULY_COMPLETE"] == "YES"


def test_unknown_over_one_percent_fails():
    report = build_accounting(classifications=[], harvested_keys_before=5, harvested_keys_after=5)
    assert report["classification_counts"]["UNKNOWN"] == 2403
    assert report["gates"]["unknown_le_1pct"] is False
    assert report["PASS_FAIL"] == "FAIL"
    assert report["BIDNET_DISCOVERY_TRULY_COMPLETE"] == "NO"
    assert report["NEXT_RUN_ALLOWED"] == "MORE_GAP_RECOVERY"
    assert report["conservation"]["diff"] == 0


def test_silent_drop_fails_gate():
    rows = [
        {"key": f"id:{i}", "classification": "STATE_SWEEP_GAP", "recovered": True}
        for i in range(2403)
    ]
    report = build_accounting(classifications=rows, harvested_keys_before=100, harvested_keys_after=99, dropped_keys=1)
    assert report["gates"]["no_silent_drops"] is False
    assert report["PASS_FAIL"] == "FAIL"
