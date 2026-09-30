"""End-to-end operator dry-run smoke — real Iowa solicitation artifact."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BREAKTHROUGH = ROOT / "artifacts" / "m3_iowa_live_breakthrough.json"
SCRIPT = ROOT / "scripts" / "run_m3_final_operator_dry_run.py"


@pytest.mark.skipif(not BREAKTHROUGH.exists(), reason="Iowa breakthrough artifact missing")
def test_final_operator_dry_run_script_exists_and_loads():
    assert SCRIPT.exists()
    iowa = __import__("json").loads(BREAKTHROUGH.read_text(encoding="utf-8"))
    assert iowa.get("ok") is True
    assert "645-DOTRFB-3046-2027" in str(iowa.get("matched_solicitation") or iowa.get("target"))
    assert (iowa.get("line_item_count") or 0) > 10


@pytest.mark.skipif(not BREAKTHROUGH.exists(), reason="Iowa breakthrough artifact missing")
def test_dry_run_phases_core_modules():
    """Focused path without full HTTP Deal Room (slow) — core decision loop."""
    import json
    from m3_capital_requirement_gate_read import build_capital_requirement_assessment
    from m3_commercial_validation_read import build_commercial_economics_view, record_supplier_quote
    from m3_end_to_end import M3EndToEndOrchestrator
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_pursuit_readiness_read import build_pursuit_readiness_assessment
    from m3_operator_loop_read import operator_mark_done
    from m3_action_orchestration_read import create_action

    iowa = json.loads(BREAKTHROUGH.read_text(encoding="utf-8"))
    t = iowa["target"]
    record = {
        "title": t["title"],
        "solicitation_number": t["solicitation_number"],
        "agency": t["agency"],
        "source_id": t.get("source_id"),
        "canonical_id": t.get("canonical_id"),
        "description": "Wildflower seed — dry run test",
        "line_items": (iowa.get("line_items") or [])[:15],
        "documents": iowa.get("documents") or [],
        "package_access": "PUBLIC_DETAIL_PAGE",
        "status": "OPEN",
    }
    store = M3PipelineStore()
    try:
        restore_pipeline_store_from_db(store)
    except Exception:
        pass
    out = M3EndToEndOrchestrator(store=store).ingest_discovery_record(record, persist=True)
    cid = out["canonical_id"]
    row = store.get(cid)
    assert row
    assert row.get("cheap_screen_survive") is True or out.get("survived") is True

    econ = build_commercial_economics_view(row)
    assert econ["acquisition_cost"]["unknown"] is True
    assert econ["acquisition_cost"]["value"] == "UNKNOWN"

    with pytest.raises(ValueError):
        record_supplier_quote(
            {"opportunity_id": cid, "supplier": "X", "product": "Y", "unit_price": 1},
            persist=False,
        )

    cap = build_capital_requirement_assessment(row)
    assert cap["capital_vs_funding_source"]["funded"] is False
    assert cap["funding_path"]["financing_available_claim"] is False
    assert cap["OpenAI"] == 0

    pr = build_pursuit_readiness_assessment(row, ensure_actions=False)
    assert pr["overall"]["not_a_score"] is True
    assert pr["principles"]["does_not_predict_winners"] is True

    act = create_action(
        {
            "title": "Dry-run obtain quote",
            "action_type": "RESEARCH",
            "why": "test",
            "opportunity_id": cid,
            "trigger_source": f"test_dry:{cid}",
        },
        persist=True,
    )
    done = operator_mark_done({"action_id": act["action_id"], "evidence": ""}, persist=True)
    assert done.get("accepted") is not True
