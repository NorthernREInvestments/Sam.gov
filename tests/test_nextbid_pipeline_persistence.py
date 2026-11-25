"""Prove durable NextBid response persistence + reload (no live API)."""
from __future__ import annotations

from m3.research.nextbid_acquire import nextbid_file_text
from m3.research.nextbid_procurement_pipeline import (
    PIPELINE_TEST_ARTIFACT,
    PIPELINE_TEST_NB_ID,
    build_one_call_approval_package,
    ingest_simulated_or_authorized_read_file,
    plan_cheapest_retrieval,
    process_offline_opportunity,
    reconcile_quote_ready_counts,
    select_test_opportunity,
)
from m3.research.nextbid_response_store import (
    load_by_request_id,
    persist_paid_response,
    verify_persisted_response,
)


def test_persist_markdown_raw_text_complete_no_truncation(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setattr(
        "m3.research.nextbid_response_store.get_settings",
        lambda: type("S", (), {"data_root": lambda self=None: root})(),
    )
    raw = "# Notice\n\n" + ("line\n" * 5000) + "Quantity: 12 EA widgets\n"
    assert len(raw) > 20000
    body = {"ok": False, "error": "non_json", "status_code": 200, "raw_text": raw}
    meta = persist_paid_response(
        method="GET",
        path="/opportunities/NBTEST/files/doc.md",
        params=None,
        endpoint="read_file",
        nb_id="NBTEST",
        status_code=200,
        body=body,
        raw_text=raw,
        content_type="text/markdown",
        original_bytes=raw.encode("utf-8"),
    )
    assert meta["persistence_ok"]
    row = load_by_request_id(meta["request_id"])
    assert row is not None
    assert row["body"]["raw_text"] == raw
    assert len(row["body"]["raw_text"]) == len(raw)
    assert row["original_bytes_sha256"] == meta["original_bytes_sha256"]
    verify = verify_persisted_response(meta["request_id"], expected_content_hash=meta["content_hash"])
    assert verify["ok"]
    assert nextbid_file_text(row["body"]) == raw


def test_never_overwrite_thicker_with_thinner(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setattr(
        "m3.research.nextbid_response_store.get_settings",
        lambda: type("S", (), {"data_root": lambda self=None: root})(),
    )
    path = "/opportunities/NBTEST2/files/a.md"
    thick = {"ok": False, "error": "non_json", "raw_text": "THICK " * 500}
    thin = {"ok": False, "error": "non_json", "raw_text": "thin"}
    m1 = persist_paid_response(
        method="GET",
        path=path,
        params=None,
        endpoint="read_file",
        nb_id="NBTEST2",
        status_code=200,
        body=thick,
        raw_text=thick["raw_text"],
    )
    m2 = persist_paid_response(
        method="GET",
        path=path,
        params=None,
        endpoint="read_file",
        nb_id="NBTEST2",
        status_code=200,
        body=thin,
        raw_text=thin["raw_text"],
    )
    assert m1["request_id"] == m2["request_id"]
    row = load_by_request_id(m1["request_id"])
    assert "THICK" in row["body"]["raw_text"]
    assert len(row["body"]["raw_text"]) > len(thin["raw_text"])


def test_extraction_failure_does_not_drop_body(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setattr(
        "m3.research.nextbid_response_store.get_settings",
        lambda: type("S", (), {"data_root": lambda self=None: root})(),
    )
    raw = "Unparseable but must survive\nQuantity: 3 EA\n"
    body = {"ok": False, "error": "non_json", "raw_text": raw}
    meta = persist_paid_response(
        method="GET",
        path="/opportunities/NBX/files/x.md",
        params=None,
        endpoint="read_file",
        nb_id="NBX",
        status_code=200,
        body=body,
        raw_text=raw,
    )
    row = load_by_request_id(meta["request_id"])
    assert row["body"]["raw_text"] == raw


def test_pipeline_selects_propane_not_furniture():
    sel = select_test_opportunity()
    assert sel["nb_id"] == PIPELINE_TEST_NB_ID
    assert sel["rejected_alternative"]["nb_id"] == "NB00321439"
    assert "document0.md" in sel["known_artifact"]["path"]


def test_cheapest_plan_blocks_already_retrieved_document0():
    """After owner-authorized read_file, planner must not propose the same artifact again."""
    plan = plan_cheapest_retrieval(PIPELINE_TEST_NB_ID)
    assert plan["killswitch_locked"] is True
    assert plan["proposed_metered_call"] is None
    assert plan["stop_without_call"] == "artifact_already_retained_locally"
    assert plan["already_retained_artifact"]["path"].endswith(".document0.md")


def test_offline_process_and_ingest_fixture_replay(tmp_path, monkeypatch):
    offline = process_offline_opportunity(PIPELINE_TEST_NB_ID)
    assert offline["api_calls"] == 0
    assert offline["owner_card"]["bid_status"] == "BID STATUS UNVERIFIED"
    assert "live_open_status_UNVERIFIED" in offline["extraction"]["unknowns"]

    root = tmp_path / "data"
    (root / "solicitation_full_text").mkdir(parents=True)
    monkeypatch.setattr(
        "m3.research.nextbid_response_store.get_settings",
        lambda: type("S", (), {"data_root": lambda self=None: root})(),
    )
    monkeypatch.setattr(
        "m3.research.full_text_store.get_settings",
        lambda: type("S", (), {"data_root": lambda self=None: root})(),
    )
    fixture = (
        "# Bulk Propane Fuel Supply CS-2026-QQ-02\n\n"
        "The City of Pembroke Pines seeks quotes to furnish and deliver bulk liquid propane.\n"
        "Estimated annual quantity: 12,000 gallons.\n"
        "Unit of measure: GAL.\n"
        "Delivery to the City's 5,000-gallon stationary storage tank.\n"
        "Quotes must be submitted electronically via OpenGov.\n"
    )
    result = ingest_simulated_or_authorized_read_file(
        nb_id=PIPELINE_TEST_NB_ID,
        path=PIPELINE_TEST_ARTIFACT,
        raw_text=fixture,
        job_id="fixture-preflight",
    )
    assert result["verify_reload"]["ok"]
    assert result["reloaded_raw_text_chars"] == len(fixture)
    assert result["api_calls"] == 0
    qty = result["extraction"]["quantity_extract"]
    assert float(qty.get("quantity") or 0) == 12000.0


def test_one_call_package_does_not_spend():
    pkg = build_one_call_approval_package()
    assert pkg["killswitch_locked"] is True
    assert pkg["max_credit_cost"] == 1
    assert pkg["execution_gate"] == "OWNER_EXPLICIT_AUTHORIZATION_REQUIRED"
    assert pkg["opportunity_id"] == PIPELINE_TEST_NB_ID


def test_reconcile_2_vs_10():
    r = reconcile_quote_ready_counts()
    assert r["fourteen_audit_YES"] == 2
    assert r["population_accounting_YES"] == 10
    assert r["do_not_inflate"] is True
