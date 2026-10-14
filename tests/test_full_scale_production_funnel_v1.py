"""Regression: full-scale funnel queue + result mapping + AUTO_SEND hard false."""

from __future__ import annotations

from buyer_intelligence.full_scale_production_funnel_v1 import (
    AUTO_SEND,
    BUILD,
    FAMILY,
    SLICE_SIZE,
    _map_result,
    _priority_score,
    _slim_checkpoint,
)


def test_auto_send_hard_false():
    assert AUTO_SEND is False


def test_slice_is_internal_not_pilot_cap():
    assert SLICE_SIZE >= 10
    assert SLICE_SIZE <= 100
    assert "full_scale" in FAMILY
    assert "full-scale-production-funnel" in BUILD


def test_priority_prefers_near_deadline_common_products():
    near = {
        "research_priority_score": 10,
        "product": "Copy Paper letter size",
        "days_left": 10,
        "authoritative_url": "https://www.bidnetdirect.com/x",
        "product_class": "PRODUCT",
        "common_product": True,
    }
    far = {
        "research_priority_score": 10,
        "product": "Consulting services engagement",
        "days_left": 200,
        "authoritative_url": "https://example.com/x",
        "product_class": "MIXED",
    }
    assert _priority_score(near) > _priority_score(far)


def test_item_should_skip_preserves_success_allows_pending_retry():
    from buyer_intelligence.full_scale_production_funnel_v1 import _item_should_skip

    assert _item_should_skip({"RESULT": "SUCCESS"}) is True
    assert _item_should_skip({"RESULT": "PENDING_RETRY", "RETRY_COUNT": 0}) is False
    assert _item_should_skip({"RESULT": "PENDING_RETRY", "RETRY_COUNT": 99}) is True
    assert _item_should_skip(
        {
            "RESULT": "NO_PACKAGE",
            "RETRYABLE": True,
            "RETRY_COUNT": 0,
            "terminal_blocker": "ABSTRACT_FETCH_FAILED_OR_WAF",
        }
    ) is False


def test_map_result_success_and_timeout():
    assert (
        _map_result(
            {
                "complete_spec": True,
                "document_count": 4,
                "line_count": 44,
            }
        )
        == "SUCCESS"
    )
    assert (
        _map_result(
            {
                "bounded_timed_out": True,
                "complete_spec": False,
            }
        )
        == "TIMEOUT"
    )
    assert (
        _map_result(
            {
                "terminal_blocker": "AWAITING_OWNER_APPROVED_OUTREACH",
                "complete_spec": True,
            }
        )
        == "OWNER_ACTION_REQUIRED"
    )


def test_slim_checkpoint_counts_fully_specified_lines():
    row = {
        "opportunity_id": "BN-1",
        "title": "Copy Paper",
        "document_count": 4,
        "documents_downloaded": 4,
        "line_count": 2,
        "complete_spec": True,
        "line_items": [
            {"description": "20# white letter", "quantity": "40", "source": "spreadsheet"},
            {"description": "title only", "quantity": None, "source": "title_fallback"},
            {"description": "legal pads", "quantity": "10", "source": "spreadsheet"},
        ],
        "distributors": [{"name": "Staples Business Advantage"}],
        "rfq": {"status": "AWAITING_OWNER_APPROVED_OUTREACH", "ready_to_send": False},
        "econ_state": "MARGIN_UNVERIFIED",
    }
    slim = _slim_checkpoint(row, result="SUCCESS", index=0)
    assert slim["RESULT"] == "SUCCESS"
    assert slim["fully_specified_product_lines"] == 2
    assert slim["rfq_ready_to_send"] is False
    assert slim["financeable"] is False
    assert slim["bid_ready"] is False


def test_build_eligible_queue_no_cap(tmp_path, monkeypatch):
    """Queue builder must not artificially cap at 100."""
    import buyer_intelligence.full_scale_production_funnel_v1 as m

    rows = []
    for i in range(250):
        rows.append(
            {
                "opportunity_id": f"OID-{i}",
                "funnel_class": "LIVE_ELIGIBLE",
                "product_class": "PRODUCT",
                "product": f"Office paper supply lot {i}",
                "days_left": 15,
                "authoritative_url": "https://www.bidnetdirect.com/sol/" + str(i),
                "deadline_status": "OPEN_CONFIRMED",
                "common_product": True,
                "research_priority_score": 50,
            }
        )

    def fake_data_path(*parts: object) -> str:
        bits: list[str] = []
        for p in parts:
            bits.extend(str(p).replace("\\", "/").split("/"))
        return str(tmp_path.joinpath(*[b for b in bits if b]))

    monkeypatch.setattr(m, "data_path", fake_data_path)
    out = tmp_path / "_og_commercial_research_pipeline_out"
    out.mkdir(parents=True, exist_ok=True)
    import json

    (out / "m3_fcf_v1_classifications.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")

    q = m.build_eligible_queue(force_rebuild=True)
    assert q["eligible_total"] == 250
    assert len(q["rows"]) == 250
    assert q["eligible_total"] > 100
