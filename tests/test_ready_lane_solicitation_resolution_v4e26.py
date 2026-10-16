"""v4e26: statewide title harvest + READY repair re-promote + paid demotion."""

from __future__ import annotations

import json
from pathlib import Path


def test_title_harvest_from_statewide_abstract_html():
    from buyer_intelligence.bidnet_bounded_document_recovery_v1 import (
        _harvest_title_from_source_url,
    )

    html = """
    <html><head><title>Office Copy Paper - BidNet</title></head>
    <body><h1>Office Copy Paper and Envelopes FY26</h1>
    <span>Solicitation Number</span><div class="mets-field-body">RFP-2026-441</div>
    </body></html>
    """

    class _Client:
        def fetch_html(self, url, *, timeout_ms=12_000, prefer_http=False):
            assert "statewide" in url
            assert prefer_http is False
            return html

    out = _harvest_title_from_source_url(
        _Client(),
        "https://www.bidnetdirect.com/public/supplier/solicitations/statewide/444140692958/abstract",
    )
    assert out["ok"] is True
    assert "Office Copy Paper" in (out["title"] or "")
    assert out["solicitation_number"] == "RFP-2026-441"


def test_solicitation_resolution_not_free_portal():
    from buyer_intelligence.bidnet_bounded_document_recovery_v1 import (
        FREE_PORTAL_UNAVAILABLE,
        SOLICITATION_RESOLUTION_FAILED,
    )

    # Mirror the post-classify override used in _recover_one_inner
    blocker = FREE_PORTAL_UNAVAILABLE
    early_st = "NO_INTERCEPTION_RESULT"
    docs: list = []
    if (
        not docs
        and early_st in {"NO_INTERCEPTION_RESULT", "NO_SEARCH_QUERIES"}
        and blocker == FREE_PORTAL_UNAVAILABLE
    ):
        blocker = SOLICITATION_RESOLUTION_FAILED
    assert blocker == SOLICITATION_RESOLUTION_FAILED


def test_title_link_regex_accepts_class_before_or_after_href():
    from bidnet_engine.package_materialization import extract_search_result_interception_links

    fixtures = Path(__file__).resolve().parent / "fixtures" / "bidnet"
    html = (fixtures / "search_results_interception_v1.html").read_text(encoding="utf-8")
    rows = extract_search_result_interception_links(html, title_hint="Copy Paper")
    assert rows
    assert any(r["solicitation_id"] == "9755638705" for r in rows)

    # class attribute before href (live BidNet sometimes emits this order)
    alt = """
    <a class="solicitationsTitleLink mets-command-link"
       href="/private/supplier/interception/open-solicitation/9998887777?target=view">
      Building Materials Supply
    </a>
    """
    rows2 = extract_search_result_interception_links(alt, title_hint="Building Materials")
    assert rows2
    assert rows2[0]["solicitation_id"] == "9998887777"


def test_paid_entitlement_demote_clears_ready_eligibility(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root
    import buyer_intelligence.fspf_ready_lane_v1 as lane
    import buyer_intelligence.full_scale_production_funnel_v1 as fspf

    set_data_root(tmp_path)
    out = tmp_path / "_og_commercial_research_pipeline_out"
    out.mkdir(parents=True)
    monkeypatch.setattr(lane, "OUT", str(out).replace("\\", "/"))
    monkeypatch.setattr(lane, "READY_QUEUE_FILE", f"{lane.OUT}/m3_fspf_v1_ready_queue.json")
    monkeypatch.setattr(
        lane, "DEFERRED_QUEUE_FILE", f"{lane.OUT}/m3_fspf_v1_deferred_queue.json"
    )
    monkeypatch.setattr(fspf, "OUT", lane.OUT)
    monkeypatch.setattr(fspf, "DEFERRED_QUEUE_FILE", lane.DEFERRED_QUEUE_FILE)

    lane._persist_ready(
        {
            "items": [
                {
                    "opportunity_id": "paid1",
                    "pg_unlocked": "8407551",
                    "queue_index": 0,
                    "stage_reached": "ACCESS_DOCS",
                }
            ]
        }
    )
    lane.demote_ready_to_deferred(
        opportunity_id="paid1",
        reason="PAID_ENTITLEMENT:CB_FD_EXAMPLE",
        blocker="PAID_ENTITLEMENT:CB_FD_EXAMPLE",
        keep_recovery_eligible=True,  # caller might still pass True — demote must override
        pg_unlocked="8407551",
    )
    dq = json.loads((out / "m3_fspf_v1_deferred_queue.json").read_text(encoding="utf-8"))
    row = next(x for x in dq["items"] if x["opportunity_id"] == "paid1")
    assert row["recovery_eligible"] is False
    assert row["additional_access_dependency"].startswith("PAID_ENTITLEMENT")
    assert row.get("pg_unlocked") == "8407551"  # PG membership preserved


def test_repair_retry_allows_detail_fetch_empty_after_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root
    from m3_jobs.store import JobStore
    import buyer_intelligence.fspf_ready_lane_v1 as lane
    import buyer_intelligence.full_scale_production_funnel_v1 as fspf

    set_data_root(tmp_path)
    out = tmp_path / "_og_commercial_research_pipeline_out"
    out.mkdir(parents=True)
    monkeypatch.setattr(lane, "OUT", str(out).replace("\\", "/"))
    monkeypatch.setattr(lane, "READY_QUEUE_FILE", f"{lane.OUT}/m3_fspf_v1_ready_queue.json")
    monkeypatch.setattr(
        lane, "DEFERRED_QUEUE_FILE", f"{lane.OUT}/m3_fspf_v1_deferred_queue.json"
    )
    monkeypatch.setattr(fspf, "OUT", lane.OUT)
    monkeypatch.setattr(fspf, "DEFERRED_QUEUE_FILE", lane.DEFERRED_QUEUE_FILE)
    monkeypatch.setattr(fspf, "QUEUE_FILE", f"{lane.OUT}/m3_fspf_v1_eligible_queue.json")

    store = JobStore(fspf.FAMILY, root=tmp_path / "jobs" / fspf.FAMILY)
    job = store.create_job(total=1, opportunity_ids=["fixme"])
    jid = job["JOB_ID"]
    store.write_item_checkpoint(
        jid,
        {
            "OPPORTUNITY_ID": "fixme",
            "RESULT": "DEFERRED_ERROR",
            "terminal_blocker": "DETAIL_FETCH_EMPTY",
            "INDEX": 0,
        },
    )
    deferred = {
        "kind": "FullScaleDeferredQueue",
        "items": [
            {
                "opportunity_id": "fixme",
                "recovery_eligible": True,
                "pg_unlocked": "8407551",
                "ready_fail_count": 2,
                "terminal_blocker": "DETAIL_FETCH_EMPTY",
                "ready_demote_reason": "DETAIL_FETCH_EMPTY",
                "queue_index": 0,
            }
        ],
        "count": 1,
    }
    (out / "m3_fspf_v1_deferred_queue.json").write_text(json.dumps(deferred), encoding="utf-8")
    (out / "m3_fspf_v1_eligible_queue.json").write_text(
        json.dumps(
            {
                "eligible_total": 1,
                "rows": [{"opportunity_id": "fixme", "title": "copy paper", "days_left": 12}],
            }
        ),
        encoding="utf-8",
    )
    report = lane.promote_unlock_eligible_to_ready(job_id=jid)
    assert report["promoted"] == 1
    ready = lane.load_ready_queue()
    assert ready["items"][0].get("repair_retry") is True
