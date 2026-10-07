"""Engine unit tests — no discovery retune, no live BidNet required."""

from bidnet_engine.fingerprint import classify_change, source_fingerprint
from bidnet_engine.invalidation import full_rerun_avoided, invalidated_stages
from bidnet_engine.priority import priority_class
from bidnet_engine.run import format_engine_report, select_workers


def test_fingerprint_no_change_and_deadline_only():
    row = {
        "stable_key": "id:1",
        "title": "Office supplies",
        "deadline": "2026-11-01",
        "status": "OPEN",
        "buyer": "City",
        "description": "paper towels",
    }
    a = source_fingerprint(row)
    b = source_fingerprint(dict(row))
    assert classify_change(a, b) == "NO_CHANGE"
    c = source_fingerprint({**row, "deadline": "2026-11-02"})
    assert classify_change(a, c) == "DEADLINE_ONLY"


def test_invalidation_selective():
    assert invalidated_stages("NO_CHANGE") == []
    assert "IDENTITY" not in invalidated_stages("DEADLINE_ONLY")
    assert full_rerun_avoided("DEADLINE_ONLY")
    assert full_rerun_avoided("NEW_AMENDMENT")
    assert "LINES" in invalidated_stages("LINE_RELEVANT_CHANGE")


def test_priority_prefers_common_goods():
    assert priority_class({"title": "Janitorial paper towels", "priority_score": 50}).startswith("P")
    hard = priority_class({"title": "Custom fabrication design-build", "priority_score": 40})
    easy = priority_class({"title": "MRO tools and PPE", "priority_score": 60, "package_state": "PACKAGE_ACQUIRED_BIDNET"})
    assert easy in {"P0_IMMEDIATE", "P1_HIGH"}
    assert hard in {"P2_NORMAL", "P3_LOW", "P1_HIGH"}


def test_select_workers_prefers_safe_fast():
    scaling = [
        {"workers": 1, "throughput_per_min": 2, "errors": 0, "auth_failures": 0},
        {"workers": 3, "throughput_per_min": 8, "errors": 0, "auth_failures": 0},
        {"workers": 5, "throughput_per_min": 14, "errors": 0, "auth_failures": 0},
        {"workers": 10, "throughput_per_min": 15, "errors": 0, "auth_failures": 0},
    ]
    # 5 is within 15% of 10 → prefer 5
    assert select_workers(scaling) == 5


def test_report_contains_required_headers():
    text = format_engine_report(
        {
            "build": "20261006-m3-bidnet-incremental-parallel-engine-v1",
            "run_id": "BNE-TEST",
            "runtime_s": 1,
            "PASS_FAIL": "PASS",
            "baseline": {"product_mixed_total": 13270, "previously_complete": 120, "processed_this_build": 0, "cumulative_complete": 120, "remaining": 13150},
            "before": {"throughput_per_min": 1.3, "stage_timing": {}, "primary_sink": "detail_fetch"},
            "concurrency": [{"workers": 1, "throughput_per_min": 2, "errors": 0, "auth_failures": 0}],
            "selected_workers": 3,
            "after": {"throughput_per_min": 12, "improvement_multiple": 9, "eta_remaining_hours": 18},
            "cache": {},
            "change_engine": {"NO_CHANGE": 100},
            "invalidation": {},
            "queue": {},
            "backlog": {},
            "documents": {},
            "stress_5000": {"input": 5000, "conservation_diff": 0},
            "twice_daily": {"can_process_source_delta": True},
            "resume_test": {"interrupted": True, "resumed": True, "duplicate_work": 0, "lost_records": 0, "PASS_FAIL": "PASS"},
            "performance": {},
            "safety": {"sam_calls": 0, "service_leakage": 0, "fg_leakage": 0, "fake_revenue": 0, "fake_prices": 0, "fixture_contamination": 0, "opportunity_conservation_diff": 0, "queue_conservation_diff": 0},
            "gates": {"existing_120_preserved": True},
            "BIDNET_INCREMENTAL_PARALLEL_PASS": "YES",
            "NEXT_RUN_ALLOWED": "RESUME_FULL_BASELINE",
            "answers": {"1_bottleneck": "detail_fetch"},
        }
    )
    assert "BIDNET INCREMENTAL/PARALLEL ENGINE SUMMARY" in text
    assert "SAM" in text
    assert "STRESS TEST — 5,000 CHANGED RECORDS" in text
