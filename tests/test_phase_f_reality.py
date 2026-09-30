"""Phase F — reality cases, UI consistency, language, and flow tests."""

from __future__ import annotations

from pathlib import Path

from execution_requirements.enrichment import enrich_deal_for_operator, owner_ready_banner_allowed
from m3_mobile_read_model import deal_room_summary, opportunity_card_summary
from validation_harness.runner import run_case, run_corpus
from validation_harness.seed_reality_corpus import main as seed_reality


def _seed():
    seed_reality()


def test_reality_corpus_seeded():
    _seed()
    from validation_harness.case_loader import load_cases

    reality = [c for c in load_cases() if c.get("validation_depth") == "reality" or str(c.get("case_id", "")).startswith("R_")]
    assert len(reality) >= 14
    classes = {c.get("reality_class") or c.get("source_type") for c in reality}
    assert "REALISTIC_FROZEN_FIXTURE" in classes or any(
        "REALISTIC" in str(x) for x in classes
    )
    assert "REAL_SOURCE_FIXTURE" in classes
    # Must not mislabel all as synthetic Phase E
    assert not all(c.get("source_type") == "SYNTHETIC_VALIDATION_FIXTURE" for c in reality)


def test_r13_r14_real_source_cases_not_ready():
    """Opp199 + Iowa REAL_SOURCE fixtures must load and stay not owner-ready until resolved."""
    _seed()
    for cid in ("R_13", "R_14"):
        r = run_case(cid)
        assert r["status"] in {"PASSED", "PARTIAL"}, (cid, r.get("gaps"))
        assert r["ready_for_owner_approval"] is False
        assert r["actuals"]["quantity_uom"]["has_quantity"] is True


def test_r11_good_deal_reaches_ready():
    _seed()
    r = run_case("R_11")
    assert r["ready_for_owner_approval"] is True
    assert r["status"] in {"PASSED", "PARTIAL"}


def test_r01_positive_commercial_ready():
    _seed()
    r = run_case("R_01")
    assert r["ready_for_owner_approval"] is True


def test_r10_financing_failure_blocks():
    _seed()
    r = run_case("R_10")
    assert r["ready_for_owner_approval"] is False
    blockers = (r.get("actuals") or {}).get("owner_readiness", {}).get("blockers") or []
    assert any("FINANCING" in str(b).upper() for b in blockers)


def test_r12_looks_good_blocked():
    _seed()
    r = run_case("R_12")
    assert r["ready_for_owner_approval"] is False
    assert (r.get("actuals") or {}).get("supplier", {}).get("supplier_validated") is False


def test_r02_dibbs_public_price_not_ready():
    _seed()
    r = run_case("R_02")
    assert r["ready_for_owner_approval"] is False


def test_r03_mil_pack_detected_not_false_ready():
    _seed()
    r = run_case("R_03")
    assert r["actuals"]["packaging"]["mil_std_2073_detected"] is True
    assert r["ready_for_owner_approval"] is False


def test_r06_amendment_applied():
    _seed()
    r = run_case("R_06")
    assert r["actuals"]["amendment"]["amendment_applied"] is True
    assert r["ready_for_owner_approval"] is False


def test_r07_estimate_not_guaranteed():
    _seed()
    r = run_case("R_07")
    assert r["actuals"]["quantity_uom"]["estimate_flagged_not_guaranteed"] is True


def test_r05_multi_clin_destinations():
    _seed()
    r = run_case("R_05")
    assert r["actuals"]["quantity_uom"]["multi_clin_destinations"] == 2


def test_reality_corpus_run_depth():
    _seed()
    run = run_corpus(validation_depth="reality")
    assert run["depth_breakdown"]["reality_cases"] >= 14
    # Critical false-ready must not appear on negative cases
    for r in run["results"]:
        if r["case_id"] in {"R_02", "R_03", "R_10", "R_12", "R_13", "R_14"}:
            assert r["ready_for_owner_approval"] is False


def test_positive_flow_surfaces_agree_r11():
    _seed()
    from validation_harness.case_loader import load_case

    case = load_case("R_11")
    row = {
        "canonical_id": "R_11",
        "title": case["case_name"],
        "description": case["source_fixture"],
        **(case.get("row_overrides") or {}),
    }
    card = opportunity_card_summary(row)
    deal = deal_room_summary(row)
    enriched = enrich_deal_for_operator(row, text=row["description"])
    assert card["ready_for_owner_approval"] is True
    assert deal["ready_for_owner_approval"] is True
    assert enriched["ready_for_owner_approval"] is True
    assert owner_ready_banner_allowed(deal) is True


def test_negative_flow_surfaces_agree_r12():
    _seed()
    from validation_harness.case_loader import load_case

    case = load_case("R_12")
    row = {
        "canonical_id": "R_12",
        "title": case["case_name"],
        "description": case["source_fixture"],
        **(case.get("row_overrides") or {}),
        "operator_workflow_state": "BID_PREPARATION",
    }
    card = opportunity_card_summary(row)
    deal = deal_room_summary(row)
    assert card["ready_for_owner_approval"] is False
    assert deal["ready_for_owner_approval"] is False
    assert owner_ready_banner_allowed(deal) is False


def test_ui_no_primary_stage_jargon():
    js = (Path(__file__).resolve().parents[1] / "static" / "m3-mobile.js").read_text(encoding="utf-8")
    # Primary operator labels must not teach Stage 0-5
    assert "Stage 0" not in js
    assert "Stage 1" not in js
    assert "LEVEL_4_UNKNOWN" not in js or "humanEvidenceLevel" in js
    # Ensure humanEvidenceLevel exists and READY authority remains gate-only
    assert "humanEvidenceLevel" in js
    assert "gateReady || allPass" not in js


def test_ui_blocker_map_has_financing_plain_english():
    js = (Path(__file__).resolve().parents[1] / "static" / "m3-mobile.js").read_text(encoding="utf-8")
    assert "Financing unresolved" in js
    assert "Supplier not validated" in js


def test_empty_dashboard_shape():
    from m3_mobile_read_model import mobile_dashboard_summary
    from m3_pipeline_store import M3PipelineStore
    import tempfile
    from pathlib import Path as P

    with tempfile.TemporaryDirectory() as td:
        store = M3PipelineStore(path=P(td) / "empty.json")
        dash = mobile_dashboard_summary(store)
        assert dash.get("kind") == "M3MobileDashboard"
        assert isinstance(dash.get("active_opportunities"), list)
        assert dash.get("active_count") == 0 or dash.get("active_count") is not None


def test_r04_uom_trap_has_quantity_signal():
    """UOM trap: HD must be detected; total pieces noted; never READY without resolution."""
    _seed()
    r = run_case("R_04")
    assert r["actuals"]["quantity_uom"]["has_quantity"] is True
    assert r["actuals"]["quantity_uom"]["hd_hundred_detected"] is True
    assert r["actuals"]["quantity_uom"]["total_pieces_noted"] is True
    assert r["ready_for_owner_approval"] is False
