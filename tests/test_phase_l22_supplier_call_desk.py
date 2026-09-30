"""Phase L.22 supplier call desk tests."""

from __future__ import annotations

import json
from pathlib import Path

from discovery.sam_api_parked import sam_api_park_status
from phase_l.bidnet_parked import BIDNET_AUTH_HISTORY_PARKED
from phase_l.l22_supplier_call_desk import (
    ANSWERED,
    ASKED_NO_ANSWER,
    AUTO_CALL,
    AUTO_SEND_SUPPLIER_OUTREACH,
    BUILD,
    CALL_COMPLETE,
    CALL_FIRST,
    CALL_IN_PROGRESS,
    CRITICAL_QUESTION_IDS,
    FOLLOW_UP_REQUIRED,
    MISSING_COST_INPUTS,
    MUST_ASK,
    NOT_ASKED,
    PRICE_LOOKS_GOOD,
    QUOTE_PROMISED,
    QUOTE_RECEIVED,
    VERIFIED_ACQUISITION_PRICE,
    add_written_quote,
    build_dynamic_questions,
    build_supplier_call_sheet,
    build_today_calls,
    compare_opportunity_suppliers,
    complete_call,
    compute_live_economics,
    load_l21_call_targets,
    missing_critical_answers,
    new_call_session,
    opening_script,
    opportunity_call_progress,
    record_answer,
    resume_call,
    save_call_session,
    supplier_call_priority,
)
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap
from phase_l.quote_readiness import INTERNAL_FIELDS_NEVER_SUPPLIER

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
DOCS = ROOT / "docs"


def _sample_row_supplier():
    targets = load_l21_call_targets()
    assert targets
    row = targets[0]
    sup = (row.get("suppliers") or [None])[0]
    assert sup
    return row, sup


def test_auto_comms_hard_false():
    assert AUTO_SEND_SUPPLIER_OUTREACH is False
    assert AUTO_CALL is False


def test_dynamic_questions_types_and_bands():
    qs = build_dynamic_questions(
        {"manufacturer": "Apple", "model": "iPad 11", "quantity": 5, "delivery_destination": "LA"},
        {"supplier_domain": "apple.com"},
    )
    types = {q["answer_type"] for q in qs}
    assert "currency" in types and "dropdown" in types and "yes_no_unknown" in types
    assert any(q["priority_band"] == MUST_ASK for q in qs)
    assert all(q["state"] == NOT_ASKED for q in qs)
    assert CRITICAL_QUESTION_IDS <= {q["question_id"] for q in qs}


def test_question_answer_entry_and_states():
    row, sup = _sample_row_supplier()
    sheet = build_supplier_call_sheet(row, sup)
    qs = build_dynamic_questions(row.get("requirement_packet") or {}, sup)
    sess = new_call_session(sheet, qs)
    assert sess["status"] == CALL_IN_PROGRESS
    ans = record_answer(sess, "unit_price", 499.0, owner_note="rep quoted each")
    assert ans["kind"] == "SupplierCallAnswer"
    assert ans["owner_note"] == "rep quoted each"
    q = next(x for x in sess["questions"] if x["question_id"] == "unit_price")
    assert q["state"] == ANSWERED and q["answer_recorded"] is True and q["asked"] is True
    record_answer(sess, "lead_time", None, asked=True)
    q2 = next(x for x in sess["questions"] if x["question_id"] == "lead_time")
    assert q2["state"] == ASKED_NO_ANSWER
    record_answer(sess, "coo", "US", follow_up_required=True)
    q3 = next(x for x in sess["questions"] if x["question_id"] == "coo")
    assert q3["state"] == FOLLOW_UP_REQUIRED


def test_freeform_and_question_notes():
    row, sup = _sample_row_supplier()
    sheet = build_supplier_call_sheet(row, sup)
    sess = new_call_session(sheet, build_dynamic_questions(row.get("requirement_packet") or {}, sup))
    sess["call_notes"] = "Salesperson seemed knowledgeable; call back Tuesday."
    record_answer(sess, "payment_terms", "PREPAID", owner_note="Finance may approve third-party PO funding.")
    assert "call back Tuesday" in sess["call_notes"]
    a = sess["answers"][-1]
    assert "third-party" in (a.get("owner_note") or "")


def test_partial_save_resume_multiple_sessions():
    row, sup = _sample_row_supplier()
    sheet = build_supplier_call_sheet(row, sup)
    qs = build_dynamic_questions(row.get("requirement_packet") or {}, sup)
    s1 = new_call_session(sheet, qs)
    record_answer(s1, "unit_price", 100)
    s1["call_notes"] = "partial"
    saved = save_call_session(s1)
    assert saved["ok"] and saved["saved_at"]
    resumed = resume_call(s1["session_id"])
    assert resumed["session"]["call_notes"] == "partial"
    assert any(q["question_id"] == "unit_price" for q in resumed["questions_already_answered"])
    # Second session does not overwrite first
    s2 = new_call_session(sheet, qs)
    assert s2["session_id"] != s1["session_id"]
    save_call_session(s2)
    assert resume_call(s1["session_id"])["session"]["call_notes"] == "partial"


def test_call_timeline_and_followup():
    row, sup = _sample_row_supplier()
    sheet = build_supplier_call_sheet(row, sup)
    sess = new_call_session(sheet, build_dynamic_questions(row.get("requirement_packet") or {}, sup))
    record_answer(sess, "product_exact", "YES")
    assert any(e.get("event") == "answer_recorded" for e in sess["timeline"])
    for qid in CRITICAL_QUESTION_IDS:
        record_answer(sess, qid, "YES" if qid != "unit_price" else 50.0)
    record_answer(sess, "freight_included", "YES")
    record_answer(sess, "freight_amount", 0)
    result = complete_call(
        sess,
        outcome=QUOTE_RECEIVED,
        follow_up={"follow_up_required": True, "follow_up_date": "2026-10-01", "promised_quote_date": "2026-09-30", "reason": "confirm writing"},
    )
    assert result["ok"]
    assert sess["status"] == CALL_COMPLETE
    assert sess["follow_up"]["promised_quote_date"] == "2026-09-30"
    assert sess["next_action"]


def test_missing_answer_validation_and_override():
    row, sup = _sample_row_supplier()
    sheet = build_supplier_call_sheet(row, sup)
    sess = new_call_session(sheet, build_dynamic_questions(row.get("requirement_packet") or {}, sup))
    missing = missing_critical_answers(sess)
    assert missing
    bad = complete_call(sess, outcome=QUOTE_PROMISED)
    assert bad["ok"] is False and bad["still_needed"]
    ok = complete_call(
        sess,
        outcome=QUOTE_PROMISED,
        override_reason="Supplier will include freight and terms in written quote tomorrow.",
    )
    assert ok["ok"] and ok["override"]
    assert sess["completion_override_reason"]


def test_live_economics_and_freight_guard():
    row, sup = _sample_row_supplier()
    sheet = build_supplier_call_sheet(row, sup)
    sess = new_call_session(sheet, build_dynamic_questions(row.get("requirement_packet") or {}, sup))
    record_answer(sess, "unit_price", 1000)
    econ = compute_live_economics(sess, row)
    assert econ["status"] == MISSING_COST_INPUTS  # freight unknown
    assert econ.get("freight") is None
    record_answer(sess, "freight_included", "YES")
    econ2 = compute_live_economics(sess, row)
    assert econ2["freight"] == 0.0
    assert econ2.get("freight_double_count_guard") is True
    assert econ2["owner_only"] is True
    # Negative price rejected
    from phase_l.l22_supplier_call_desk import validate_price_inputs

    assert "negative_unit_price" in validate_price_inputs(-1, 1, 0, 0)


def test_written_quote_overrides_verbal():
    row, sup = _sample_row_supplier()
    sheet = build_supplier_call_sheet(row, sup)
    sess = new_call_session(sheet, build_dynamic_questions(row.get("requirement_packet") or {}, sup))
    record_answer(sess, "unit_price", 999, source="PHONE")
    verbal_n = len(sess["verbal_answers"])
    res = add_written_quote(
        sess,
        {
            "quote_number": "Q-1",
            "unit_price": 850,
            "freight": 40,
            "freight_included": False,
            "expiration": "2026-10-15",
            "terms": "NET_30",
            "reference_path": "quotes/Q-1.pdf",
        },
        row,
    )
    assert res["ok"] and res["verbal_preserved"]
    assert len(sess["verbal_answers"]) >= verbal_n
    assert sess["written_quote"]["unit_price"] == 850
    amap_unit = next(a for a in sess["answers"] if a["question_id"] == "unit_price")
    assert amap_unit["answer_value"] == 850
    assert amap_unit["source"] == "WRITTEN_QUOTE"
    assert sess["outcome"] == QUOTE_RECEIVED


def test_verified_acquisition_not_fabricated_without_quote():
    row, sup = _sample_row_supplier()
    sheet = build_supplier_call_sheet(row, sup)
    sess = new_call_session(sheet, build_dynamic_questions(row.get("requirement_packet") or {}, sup))
    record_answer(sess, "unit_price", 100)
    compute_live_economics(sess, row)
    assert sess.get("verified_acquisition_price") is None
    assert sess.get("verified_positive") is None


def test_supplier_comparison_and_progress():
    row, sup = _sample_row_supplier()
    sheet = build_supplier_call_sheet(row, sup)
    sess = new_call_session(sheet, build_dynamic_questions(row.get("requirement_packet") or {}, sup))
    record_answer(sess, "unit_price", 100)
    record_answer(sess, "freight_included", "YES")
    compute_live_economics(sess, row)
    sess["outcome"] = QUOTE_RECEIVED
    cmp_ = compare_opportunity_suppliers(row, [sess])
    assert cmp_["kind"] == "SupplierComparison"
    assert cmp_["not_cheapest_only"] is True
    prog = opportunity_call_progress(row, [sess])
    assert "suppliers contacted" in prog["summary"]
    assert prog["auto_bid"] is False


def test_today_calls_and_priority():
    targets = load_l21_call_targets()
    sheets = []
    for row in targets[:3]:
        for s in row.get("suppliers") or []:
            sheets.append(build_supplier_call_sheet(row, s))
    today = build_today_calls(sheets, [])
    assert today["kind"] == "TODAYS_SUPPLIER_CALLS"
    assert today["count"] >= 1
    assert "NOT_CALLED" in today["filters_supported"]
    pri = supplier_call_priority(
        {"supplier_grade": "SUPPLIER_A", "authorization_state": "AUTHORIZED_CONFIRMED", "product_fit": "EXACT", "source_type": "OEM"},
        contact={"contact_path_status": "RFQ_FORM_PUBLIC"},
    )
    assert pri["band"] in {CALL_FIRST, "CALL_SECOND", "CALL_THIRD", "BACKUP"}


def test_opening_script_no_internal_leak():
    script = opening_script({"manufacturer": "Ford", "model": "PPI", "quantity": 2, "delivery_destination": "Cook County"})
    assert "pricing and availability" in script.lower()
    for field in ("max_buy", "break_even", "expected profit"):
        assert field not in script.lower()
    for field in INTERNAL_FIELDS_NEVER_SUPPLIER:
        assert field.lower() not in script.lower()


def test_no_external_actions():
    assert_no_fixed_positive_cap()
    assert sam_api_park_status()["calls_consumed"] == 0
    assert BIDNET_AUTH_HISTORY_PARKED
    assert AUTO_CALL is False


def test_artifacts_after_run():
    if not (OUT / "l22_summary.json").exists():
        return
    s = json.loads((OUT / "l22_summary.json").read_text(encoding="utf-8"))
    assert s.get("verdict") in {
        "PHASE_L22_SUPPLIER_CALL_DESK_READY",
        "PHASE_L22_PARTIAL_SUPPLIER_CALL_DESK",
        "PHASE_L22_SUPPLIER_CALL_DESK_FAILED",
    }
    assert s.get("auto_call") is False
    assert s.get("written_quote_ingestion") == "YES"
    assert s.get("build") == BUILD
    for name in (
        "l22_supplier_call_sheets.json",
        "l22_dynamic_questions.json",
        "l22_answer_schema.json",
        "l22_call_sessions.json",
        "l22_call_outcomes.json",
        "l22_followup_model.json",
        "l22_supplier_comparison.json",
        "l22_today_calls.json",
        "l22_summary.json",
    ):
        assert (OUT / name).exists(), name
    for doc in (
        "phase_l22_supplier_call_desk.md",
        "phase_l22_answer_capture.md",
        "phase_l22_notes_and_sessions.md",
        "phase_l22_live_economics.md",
        "phase_l22_followups.md",
        "phase_l22_supplier_comparison.md",
        "phase_l22_mobile_owner_workflow.md",
        "phase_l22_legacy_cleanup.md",
        "phase_l22_regression.md",
    ):
        assert (DOCS / doc).exists(), doc
