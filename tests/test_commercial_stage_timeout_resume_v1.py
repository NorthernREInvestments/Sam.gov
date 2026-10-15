"""Commercial funnel: extract survives later timeouts; resume at pricing."""

from __future__ import annotations

from unittest.mock import patch


def test_classify_timeout_after_extract_resumes_at_price():
    from buyer_intelligence.opportunity_funnel_orchestrator_v1 import (
        OUTCOME_EXTERNAL_DEPENDENCY,
        STAGE_EXTRACT,
        STAGE_PRICE,
        STAGE_SOURCE,
        classify_process_one_outcome,
    )

    c = classify_process_one_outcome(
        {
            "terminal_blocker": "PER_OPPORTUNITY_TIMEOUT",
            "bounded_timed_out": True,
            "documents_downloaded": 1,
            "line_count": 1,
            "complete_spec": True,
            "supplier_status": "SOURCE_UNAVAILABLE",
            "source_outcome": "NO_COMPLIANT_SUPPLIER_FOUND",
            "econ_state": "MARGIN_UNVERIFIED",
            "economics_complete": False,
        }
    )
    assert c["outcome"] == OUTCOME_EXTERNAL_DEPENDENCY
    assert c["defer"] is True
    assert c["commercial_resume"] is True
    assert c["software_defect_key"] is None
    assert c["last_completed_stage"] in {STAGE_EXTRACT, STAGE_SOURCE}
    assert c["next_stage"] == STAGE_PRICE
    assert c["stage_reached"] in {STAGE_SOURCE, STAGE_PRICE}


def test_classify_commercial_budget_deferred():
    from buyer_intelligence.opportunity_funnel_orchestrator_v1 import (
        OUTCOME_EXTERNAL_DEPENDENCY,
        STAGE_PRICE,
        classify_process_one_outcome,
    )

    c = classify_process_one_outcome(
        {
            "terminal_blocker": "COMMERCIAL_STAGE_BUDGET_DEFERRED",
            "commercial_budget_deferred": True,
            "documents_downloaded": 2,
            "line_count": 3,
            "complete_spec": True,
            "last_completed_stage": "EXTRACT",
            "next_stage": "PRICE",
        }
    )
    assert c["outcome"] == OUTCOME_EXTERNAL_DEPENDENCY
    assert c["next_stage"] == STAGE_PRICE
    assert c["commercial_resume"] is True


def test_soft_watchdog_preserves_complete_package():
    from buyer_intelligence.bidnet_bounded_document_recovery_v1 import (
        COMPLETE_PACKAGE,
        recover_one_bounded,
    )

    fake = {
        "opportunity_id": "x",
        "primary_blocker": COMPLETE_PACKAGE,
        "complete_spec": True,
        "documents": [{"name": "a.pdf", "local_path": "/tmp/a.pdf"}],
        "line_items": [{"description": "Copy Paper", "quantity": 10}],
    }

    with patch(
        "buyer_intelligence.bidnet_bounded_document_recovery_v1._recover_one_inner",
        return_value=dict(fake),
    ):
        with patch(
            "buyer_intelligence.bidnet_bounded_document_recovery_v1.threading.Timer"
        ) as timer_cls:
            # Fire watchdog immediately so soft_exceeded is True after return.
            def _fake_timer(interval, fn):
                class T:
                    daemon = False

                    def start(self_inner):
                        fn()

                    def cancel(self_inner):
                        return None

                return T()

            timer_cls.side_effect = _fake_timer
            row = recover_one_bounded(
                {"opportunity_id": "x"},
                auth_client=object(),
                l23={},
                timeout_s=0.01,
                isolate_process=False,
            )
    assert row["primary_blocker"] == COMPLETE_PACKAGE
    assert row.get("timed_out") is False
    assert row.get("docs_budget_exceeded_after_success") is True
    assert row.get("documents")
    assert row.get("line_items")


def test_process_one_skips_docs_on_commercial_resume():
    from buyer_intelligence.commercial_funnel_execution_v1 import process_one
    import httpx

    item = {
        "opportunity_id": "resume-oid",
        "title": "Hammermill Copy Paper",
        "authoritative_url": "https://www.bidnetdirect.com/example",
        "commercial_resume": True,
        "next_stage": "PRICE",
        "complete_spec": True,
        "documents_downloaded": 1,
        "line_items": [
            {
                "description": "Hammermill Copy Paper 8.5x11",
                "quantity": 40,
                "mpn": "150010",
                "manufacturer": "Hammermill",
            }
        ],
        "recovered_documents": [
            {"document_name": "spec.pdf", "local_path": "/data/spec.pdf", "local_path_set": True}
        ],
    }

    with patch(
        "buyer_intelligence.commercial_funnel_execution_v1._research_public_prices_for_lines",
        return_value={
            "ok": False,
            "prices": [],
            "failures": [{"failure_class": "NO_PUBLIC_PRICE"}],
            "distributors": [],
            "verified_public_unit_price": None,
            "elapsed_s": 0.1,
            "price_evidence_count": 0,
        },
    ) as price_mock:
        with httpx.Client() as client:
            row = process_one(
                item,
                client=client,
                evidence_index={"by_opportunity": {}},
                auth_status={"authenticated": False},
                l23_rec={},
                auth_client=None,
            )
    assert row.get("skipped_docs_recovery") is True
    assert row.get("fetch_via") == "prior_checkpoint_commercial_resume"
    assert row.get("complete_spec") is True
    assert row.get("line_count") == 1
    assert price_mock.called
    assert row.get("last_completed_stage") == "EXTRACT"
    assert row.get("terminal_blocker") in {
        "PRICE_UNAVAILABLE_AFTER_SEARCH",
        "MISSING_ACQUISITION_AND_GOV_PRICE",
        "AWAITING_OWNER_APPROVED_OUTREACH",
    }


def test_process_one_defers_pricing_when_budget_exhausted():
    from buyer_intelligence.commercial_funnel_execution_v1 import process_one
    import httpx
    import time

    item = {
        "opportunity_id": "budget-oid",
        "title": "Office Supplies",
        "authoritative_url": "https://example.com/x",
        "commercial_resume": True,
        "next_stage": "PRICE",
        "complete_spec": True,
        "documents_downloaded": 1,
        "line_items": [
            {"description": "Ballpoint Pens Blue Medium Point Box", "quantity": 100}
        ],
        "recovered_documents": [
            {"document_name": "a.pdf", "local_path": "/data/a.pdf", "local_path_set": True}
        ],
    }

    # Force remain < min by making t0 appear far in the past via patched time.
    real_time = time.time

    class Clock:
        def __init__(self):
            self.t = real_time()

        def __call__(self):
            return self.t

    clock = Clock()

    with patch("buyer_intelligence.commercial_funnel_execution_v1.time") as tmod:
        tmod.time = clock
        # Advance clock after process_one starts: first call is t0, then bump.
        calls = {"n": 0}
        base = real_time()

        def _tick():
            calls["n"] += 1
            # After start, pretend 95s elapsed (wall=100, min_price=18 → defer)
            return base if calls["n"] == 1 else base + 95.0

        tmod.time = _tick
        with patch(
            "buyer_intelligence.commercial_funnel_execution_v1._research_public_prices_for_lines"
        ) as price_mock:
            with httpx.Client() as client:
                row = process_one(
                    item,
                    client=client,
                    evidence_index={"by_opportunity": {}},
                    auth_status={},
                    l23_rec={},
                    auth_client=None,
                )
    assert price_mock.called is False
    assert row.get("commercial_budget_deferred") is True
    assert row.get("terminal_blocker") == "COMMERCIAL_STAGE_BUDGET_DEFERRED"
    assert row.get("next_stage") == "PRICE"
    assert row.get("complete_spec") is True


def test_unverified_margin_not_economics_complete():
    from buyer_intelligence.commercial_funnel_execution_v1 import assess_economics

    econ = assess_economics(
        {
            "historical_unit_price": None,
            "public_unit_price": None,
            "primary_quantity": 10,
            "awardable_scope": {"award_unit": "INDIVIDUAL_LINE"},
        }
    )
    assert econ["econ_state"] == "MARGIN_UNVERIFIED"
    assert econ.get("acquisition_unit") is None


def test_defer_preserves_commercial_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root

    set_data_root(tmp_path)
    from buyer_intelligence import full_scale_production_funnel_v1 as fspf

    fspf._defer_opportunity(
        opportunity_id="comm-1",
        stage="SOURCE",
        outcome="EXTERNAL_DEPENDENCY",
        defect_key=None,
        shared_access_key="pg:8409951",
        blocker="COMMERCIAL_STAGE_BUDGET_DEFERRED",
        index=5,
        recovery_eligible=True,
        pg_unlocked="8409951",
        next_stage="PRICE",
        last_completed_stage="EXTRACT",
        commercial_resume=True,
    )
    dq = fspf._load_deferred_queue()
    item = dq["items"][0]
    assert item["commercial_resume"] is True
    assert item["next_stage"] == "PRICE"
    assert item["last_completed_stage"] == "EXTRACT"
    assert item["recovery_eligible"] is True

    fspf._defer_opportunity(
        opportunity_id="comm-1",
        stage="SOURCE",
        outcome="EXTERNAL_DEPENDENCY",
        defect_key=None,
        shared_access_key="pg:8409951",
        blocker="PRICE_UNAVAILABLE_AFTER_SEARCH",
        index=5,
        # omit commercial_resume → preserve
    )
    item2 = fspf._load_deferred_queue()["items"][0]
    assert item2["commercial_resume"] is True
    assert item2["next_stage"] == "PRICE"


def test_slim_checkpoint_persists_line_items():
    from buyer_intelligence.full_scale_production_funnel_v1 import _slim_checkpoint

    slim = _slim_checkpoint(
        {
            "opportunity_id": "oid-1",
            "complete_spec": True,
            "documents_downloaded": 1,
            "document_count": 1,
            "line_count": 1,
            "line_items": [{"description": "Widget A", "quantity": 5}],
            "recovered_documents": [
                {"document_name": "a.pdf", "local_path": "/x/a.pdf", "local_path_set": True}
            ],
            "last_completed_stage": "EXTRACT",
            "next_stage": "PRICE",
            "commercial_resume": True,
            "source_outcome": "NO_COMPLIANT_SUPPLIER_FOUND",
            "economics_complete": False,
            "stage_timings": {"docs_recovery_s": 40.0},
        },
        result="DEFERRED_EXTERNAL",
        index=1,
        prior=None,
    )
    assert slim["line_items"]
    assert slim["recovered_documents"]
    assert slim["next_stage"] == "PRICE"
    assert slim["commercial_resume"] is True
    assert slim["last_completed_stage"] == "EXTRACT"
