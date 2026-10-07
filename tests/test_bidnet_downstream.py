"""Downstream census conservation. Discovery code is not retuned."""

from bidnet_downstream.census import format_downstream_report, priority_score
from bidnet_downstream.models import VALID_OPEN_TARGET
from universe_pass.classify import classify_universe_opportunity


def test_deep_eligibility_and_package_mapping():
    from bidnet_downstream.deep import _eligibility_from_text, _map_detail, _map_package

    assert _map_detail("DETAIL_OK") == "DETAIL_COMPLETE"
    assert _map_package("PACKAGE_TERMINAL_OTHER") == "PACKAGE_TERMINAL"
    assert _eligibility_from_text("office supplies", "PACKAGE_ACQUIRED_BIDNET") == "ELIGIBILITY_CLEAR"
    assert _eligibility_from_text("bid bond required", "PACKAGE_ACQUIRED_BIDNET") == "ELIGIBILITY_BLOCKED"
    assert _eligibility_from_text("unknown", "PACKAGE_RETRYABLE") == "ELIGIBILITY_UNKNOWN"


def test_collapsed_list_identities_conserve_the_valid_open_target():
    from bidnet_downstream.census import account_list_identities

    identity = account_list_identities(15071)
    assert identity["collapsed_duplicate_list_identities"] == 6905
    assert identity["accounted"] == 21976
    assert identity["diff"] == 0


def test_harvest_window_excludes_older_bidnet_rows():
    from bidnet_downstream.census import freeze_valid_open_corpus

    store = {
        "new": {
            "platform": "bidnet",
            "title": "Paper towels",
            "authoritative_url": "https://www.bidnetdirect.com/solicitations/2718752162",
            "updated_at": "2026-10-06T17:00:00+00:00",
            "freshness": "FRESH",
        },
        "old": {
            "platform": "bidnet",
            "title": "Old chairs",
            "authoritative_url": "https://www.bidnetdirect.com/solicitations/1111111111",
            "updated_at": "2026-09-01T17:00:00+00:00",
            "freshness": "FRESH",
        },
    }
    rows, meta = freeze_valid_open_corpus(store)
    assert [r["stable_key"] for r in rows] == ["id:2718752162"]
    assert meta["open_bidnet_records_seen"] == 2


def test_classifier_maps_tangible_and_service():
    product = classify_universe_opportunity({"title": "Janitorial supplies and paper towels", "description": "case quantity"})
    service = classify_universe_opportunity({"title": "Professional consulting services", "description": "advisory only"})
    assert product["class"] == "TANGIBLE_PRODUCT"
    assert service["class"] == "PURE_SERVICE"
    assert priority_score({"title": "Office furniture", "buyer": "City", "solicitation_event_id": "1"}, "PRODUCT") > 0
    assert priority_score({"title": "Office furniture"}, "SERVICE") == 0


def test_report_conservation_block():
    report = {
        "build": "20261006-m3-bidnet-downstream-processing-v1",
        "run_id": "BDS-TEST",
        "runtime_s": 1,
        "input_valid_open": VALID_OPEN_TARGET,
        "corpus_hash": "abc",
        "completed": True,
        "checkpoint_resume": True,
        "PASS_FAIL": "PASS",
        "classification": {
            "PRODUCT": 10000,
            "MIXED_PRODUCT_MATERIAL": 2000,
            "SERVICE": 5000,
            "CONSTRUCTION": 4000,
            "UNKNOWN": 976,
        },
        "classification_accounted": VALID_OPEN_TARGET,
        "classification_diff": 0,
        "product_pipeline": {
            "product_plus_mixed": 12000,
            "detail_attempted": 0,
            "DETAIL_COMPLETE": 0,
            "DETAIL_PARTIAL": 0,
            "DETAIL_LOCKED": 0,
            "DETAIL_EXTERNAL_SOURCE": 0,
            "DETAIL_RETRYABLE": 12000,
            "DETAIL_TERMINAL": 0,
        },
        "package": {"PACKAGE_RETRYABLE": 12000, "PACKAGE_ACQUIRED_BIDNET": 0, "PACKAGE_ACQUIRED_OFFICIAL_SOURCE": 0},
        "eligibility": {"ELIGIBILITY_UNKNOWN": 12000},
        "lines": {},
        "identity": {},
        "material_coverage": {},
        "revenue": {},
        "acquisition": {},
        "quote_pipeline": {},
        "basket": {"ready": 0, "not_ready": 12000},
        "economics": {"ready": 0, "not_ready": 12000},
        "top_25_readiness": [],
        "bottlenecks": [],
        "safety": {"sam_calls": 0, "service_leakage": 0, "fg_leakage": 0, "fake_revenue": 0, "fake_prices": 0},
        "conservation": {"input": VALID_OPEN_TARGET, "opportunity_diff": 0},
        "gates": {
            "input_corpus_fixed_at_21976": True,
            "classification_accounted_100": True,
            "no_silent_drops": True,
            "no_contamination": True,
            "no_fake_economics": True,
            "canonical_pipeline_used": True,
            "checkpoint_resume": True,
            "ui_synchronized": True,
        },
        "BIDNET_DOWNSTREAM_PASS": "YES",
        "NEXT_RUN_ALLOWED": "BIDNET_DEEP_COMPLETION",
    }
    text = format_downstream_report(report)
    assert "BIDNET DOWNSTREAM SUMMARY" in text
    assert "SAM calls: 0" in text
    assert "Classification diff: 0" in text
    assert sum(report["classification"].values()) == VALID_OPEN_TARGET
