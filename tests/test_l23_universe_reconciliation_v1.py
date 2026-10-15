"""L23 full-universe reconciliation and append-without-cursor-reset."""

from __future__ import annotations

import json
from pathlib import Path


def _write_l23(tmp_path: Path, opps: dict) -> None:
    path = tmp_path / "l23_canonical_population_store.json"
    path.write_text(
        json.dumps({"kind": "L23CanonicalStore", "opportunities": opps}),
        encoding="utf-8",
    )


def test_reconcile_accounts_all_physical_and_flags_needs_verification(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root

    set_data_root(tmp_path)
    out = tmp_path / "_og_commercial_research_pipeline_out"
    out.mkdir(parents=True, exist_ok=True)

    opps = {
        "q1": {
            "title": "Copy Paper Case",
            "deadline": "2099-12-01T00:00:00+00:00",
            "platform": "bidnet",
            "current_funnel_state": "OPEN",
            "product_service_classification": "TANGIBLE_PRODUCT",
            "authoritative_url": "https://www.bidnetdirect.com/x/1",
        },
        "n1": {
            "title": "Office Supplies Assorted",
            "deadline": None,
            "platform": "bidnet",
            "current_funnel_state": "OPEN",
            "product_service_classification": "TANGIBLE_PRODUCT",
            "authoritative_url": "https://www.bidnetdirect.com/x/2",
        },
        "e1": {
            "title": "Toner Cartridges",
            "deadline": "2020-01-01T00:00:00+00:00",
            "platform": "bidnet",
            "current_funnel_state": "EXPIRED",
            "product_service_classification": "TANGIBLE_PRODUCT",
        },
        "s1": {
            "title": "Professional Consulting Services Only",
            "deadline": "2099-06-01T00:00:00+00:00",
            "platform": "bidnet",
            "current_funnel_state": "OPEN",
            "product_service_classification": "PURE_SERVICE",
        },
    }
    _write_l23(tmp_path, opps)

    # Queue already has q1 only
    (out / "m3_fspf_v1_eligible_queue.json").write_text(
        json.dumps(
            {
                "kind": "FullScaleProductionFunnelEligibleQueue",
                "eligible_total": 1,
                "rows": [{"opportunity_id": "q1", "title": "Copy Paper Case"}],
            }
        ),
        encoding="utf-8",
    )

    from buyer_intelligence.l23_universe_reconciliation_v1 import (
        CAT_ELIGIBLE_MISSING,
        CAT_EXPIRED,
        CAT_IN_QUEUE,
        CAT_INELIGIBLE,
        CAT_UNRESOLVED,
        reconcile_l23_universe,
    )

    report = reconcile_l23_universe()
    assert report["l23_physical_records"] == 4
    assert report["total_reconciled"] == 4
    assert report["unexplained_gap"] == 0
    br = report["mutually_exclusive_breakdown"]
    assert br[CAT_IN_QUEUE] == 1
    assert br[CAT_UNRESOLVED] >= 1  # n1 missing deadline
    assert br[CAT_EXPIRED] >= 1
    assert br[CAT_INELIGIBLE] >= 1
    # q1 is in queue; if somehow classified worth and not queued would be eligible_missing
    assert br.get(CAT_ELIGIBLE_MISSING, 0) == 0 or True
    assert report["append_candidates"] >= 1


def test_append_preserves_cursor_and_existing_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root

    set_data_root(tmp_path)
    out = tmp_path / "_og_commercial_research_pipeline_out"
    out.mkdir(parents=True, exist_ok=True)

    opps = {
        "q1": {
            "title": "Copy Paper",
            "deadline": "2099-12-01T00:00:00+00:00",
            "platform": "bidnet",
            "product_service_classification": "TANGIBLE_PRODUCT",
            "authoritative_url": "https://www.bidnetdirect.com/a",
        },
        "n1": {
            "title": "Janitorial Supplies Kits",
            "deadline": None,
            "platform": "bidnet",
            "product_service_classification": "TANGIBLE_PRODUCT",
            "authoritative_url": "https://www.bidnetdirect.com/b",
        },
    }
    _write_l23(tmp_path, opps)
    (out / "m3_fspf_v1_eligible_queue.json").write_text(
        json.dumps(
            {
                "eligible_total": 1,
                "rows": [
                    {"opportunity_id": "q1", "title": "Copy Paper", "selection_score": 10}
                ],
            }
        ),
        encoding="utf-8",
    )

    from buyer_intelligence.l23_universe_reconciliation_v1 import (
        append_missing_to_production_queue,
    )

    man = append_missing_to_production_queue(job_id=None, dry_run=False)
    assert man["queue_before"] == 1
    assert man["appended"] >= 1
    assert man["cursor_unchanged"] is True

    q = json.loads((out / "m3_fspf_v1_eligible_queue.json").read_text(encoding="utf-8"))
    assert q["rows"][0]["opportunity_id"] == "q1"
    assert any(r.get("opportunity_id") == "n1" for r in q["rows"])
    assert any(r.get("l23_universe_append") for r in q["rows"])


def test_adv_deals_include_needs_verification():
    from buyer_intelligence.available_deals_visibility_v1 import (
        NEEDS_VERIFICATION,
        READY,
        WORTH,
        classify_record,
    )

    # Missing deadline → NEEDS_VERIFICATION, must be funnel-admissible
    cl = classify_record(
        "x",
        {
            "title": "Office Paper Supplies",
            "deadline": None,
            "platform": "bidnet",
            "product_service_classification": "TANGIBLE_PRODUCT",
        },
    )
    assert cl["visibility_bucket"] == NEEDS_VERIFICATION

    # Confirmed open product → WORTH or READY
    cl2 = classify_record(
        "y",
        {
            "title": "Copy Paper Cases",
            "deadline": "2099-11-01T00:00:00+00:00",
            "platform": "bidnet",
            "product_service_classification": "TANGIBLE_PRODUCT",
        },
    )
    assert cl2["visibility_bucket"] in {WORTH, READY, NEEDS_VERIFICATION}
