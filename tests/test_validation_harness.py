"""Phase E — validation harness targeted tests."""

from __future__ import annotations

import json
from pathlib import Path

from validation_harness import BUILD_TAG, list_cases, load_case, load_cases, run_case, run_corpus
from validation_harness.gap_classifier import classify_gap
from validation_harness.gap_register import GapRegister
from validation_harness.readiness_tests import adversarial_cases, false_rejection_cases
from validation_harness.reports import coverage_report, write_run_artifacts
from validation_harness.severity import severity_for
from validation_harness.models import SOURCE_TYPE_SYNTHETIC


def test_build_tag():
    assert BUILD_TAG.startswith("20260922-m3-validation-harness")


def test_corpus_seeded_and_loadable():
    # Ensure corpus exists (idempotent seed)
    from validation_harness.seed_corpus import main as seed

    seed()
    ids = list_cases()
    assert len(ids) >= 16
    case = load_case("CASE_001")
    assert case["source_type"] == SOURCE_TYPE_SYNTHETIC
    assert "expected" in case
    assert "SYNTHETIC" in (case.get("source_fixture") or case.get("notes") or "")


def test_comparator_severity_and_classification():
    assert classify_gap("packaging", "packaging.mil_std") == "PACKAGING_ERROR"
    assert classify_gap("owner_readiness", "ready", false_readiness=True) == "FALSE_READINESS"
    assert severity_for("owner_readiness.ready_for_owner_approval", "FALSE_READINESS", false_readiness=True) == "CRITICAL"
    assert severity_for("quantity_uom.quantity", "QUANTITY_UOM_ERROR") == "CRITICAL"


def test_run_single_case_deterministic():
    from validation_harness.seed_corpus import main as seed

    seed()
    r1 = run_case("CASE_005")
    r2 = run_case("CASE_005")
    assert r1["case_id"] == "CASE_005"
    assert r1["status"] in {"PASSED", "PARTIAL", "FAILED"}
    assert r1["status"] == r2["status"]
    assert r1["actuals"]["packaging"]["mil_std_2073_detected"] is True
    assert r1["ready_for_owner_approval"] is False


def test_idiq_estimate_not_guaranteed():
    from validation_harness.seed_corpus import main as seed

    seed()
    r = run_case("CASE_008")
    assert r["actuals"]["quantity_uom"]["estimate_flagged_not_guaranteed"] is True
    assert r["actuals"]["financing"]["financing_assumed"] is False


def test_multi_clin_destinations():
    from validation_harness.seed_corpus import main as seed

    seed()
    r = run_case("CASE_007")
    assert r["actuals"]["quantity_uom"]["multi_clin_destinations"] == 2
    assert r["actuals"]["shipping_delivery"]["fob_destination"] is True
    assert r["actuals"]["shipping_delivery"]["fob_origin"] is True


def test_amendment_case():
    from validation_harness.seed_corpus import main as seed

    seed()
    r = run_case("CASE_011")
    assert r["actuals"]["amendment"]["amendment_applied"] is True
    assert r["ready_for_owner_approval"] is False


def test_wawf_and_payment_state_separation():
    from validation_harness.seed_corpus import main as seed

    seed()
    r = run_case("CASE_012")
    assert r["actuals"]["invoice_payment"]["wawf_required"] is True
    assert r["actuals"]["post_award"]["states_note_distinct"] is True


def test_false_readiness_adversarial_suite():
    cases = adversarial_cases()
    assert len(cases) >= 5
    for c in cases:
        r = run_case(c)
        assert r["ready_for_owner_approval"] is False, c["case_id"]
        assert r["status"] in {"PASSED", "PARTIAL"}  # must not falsely become FAILED only due to extra blockers wording


def test_false_rejection_mil_pack_detected_not_assumed_finance():
    for c in false_rejection_cases():
        r = run_case(c)
        if c["case_id"] == "ADV_FALSE_REJ_MIL_PACK":
            assert r["actuals"]["packaging"]["mil_std_2073_detected"] is True
        assert r["actuals"]["financing"]["financing_assumed"] is False


def test_report_and_gap_register(tmp_path: Path):
    from validation_harness.seed_corpus import main as seed

    seed()
    run = run_corpus(case_ids=["CASE_001", "CASE_005", "CASE_012"])
    assert run["case_count"] == 3
    paths = write_run_artifacts(run, root=tmp_path)
    assert Path(paths["summary"]).exists()
    assert Path(paths["gaps"]).exists()
    assert Path(paths["report"]).exists()
    summary = json.loads(Path(paths["summary"]).read_text(encoding="utf-8"))
    assert "coverage" in summary
    cov = coverage_report(run["results"])
    assert "Packaging" in cov["covered"] or "Product Identity" in cov["covered"]
    reg = GapRegister(tmp_path / "gap_register.json")
    reg.ingest(run.get("gaps") or [], run_id=run["run_id"])
    reg.save()
    assert reg.path.exists()
    # second ingest preserves history
    reg2 = GapRegister(tmp_path / "gap_register.json")
    before = len(reg2._data.get("gaps") or {})
    reg2.ingest(run.get("gaps") or [], run_id=run["run_id"] + "-2")
    after = len(reg2._data.get("gaps") or {})
    assert after >= before


def test_golden_truth_not_authored_by_m3():
    """Sanity: expected blocks exist in fixtures independently of actuals."""
    from validation_harness.seed_corpus import main as seed

    seed()
    case = load_case("CASE_002")
    assert "expected" in case and case["expected"]
    # Running M3 must not mutate the case file expected section
    before = json.dumps(case["expected"], sort_keys=True)
    run_case(case)
    after = json.dumps(load_case("CASE_002")["expected"], sort_keys=True)
    assert before == after
