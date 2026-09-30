"""Phase F — remaining UI torture, transitions, history, next-action, enum audit."""

from __future__ import annotations

from pathlib import Path

from execution_requirements.enrichment import enrich_deal_for_operator
from operator_workflow.transitions import evaluate_transition
from validation_harness.seed_reality_corpus import main as seed_reality


ROOT = Path(__file__).resolve().parents[1]


def test_fg008_next_action_is_executable_for_blocked_supplier():
    """F-G008: next action must tell VA what to do — not only 'Validate supplier'."""
    seed_reality()
    from validation_harness.case_loader import load_case

    case = load_case("R_12")
    row = {
        "canonical_id": "R_12",
        "title": case["case_name"],
        "description": case["source_fixture"],
        **(case.get("row_overrides") or {}),
    }
    enriched = enrich_deal_for_operator(row, text=row["description"])
    nxt = str(enriched.get("operator_next_action") or "")
    assert enriched["ready_for_owner_approval"] is False
    assert len(nxt) > 40
    # Must mention quote / supplier / not escalate in actionable way
    low = nxt.lower()
    assert any(k in low for k in ("quote", "supplier", "formal", "packaging", "financing", "submission", "validate"))
    assert "stage 0" not in low


def test_fg008_ready_deal_next_action_points_to_owner():
    seed_reality()
    from validation_harness.case_loader import load_case

    case = load_case("R_11")
    row = {
        "canonical_id": "R_11",
        "description": case["source_fixture"],
        **(case.get("row_overrides") or {}),
    }
    enriched = enrich_deal_for_operator(row, text=row["description"])
    assert enriched["ready_for_owner_approval"] is True
    assert "owner" in str(enriched.get("operator_next_action") or "").lower()


def test_state_transitions_legal_and_illegal():
    ok = evaluate_transition("BID_PREPARATION", "SUBMITTED", submission_evidenced=True)
    assert ok["legal"] is True
    bad = evaluate_transition("BID_PREPARATION", "SUBMITTED", submission_evidenced=False)
    assert bad["legal"] is False

    exp = evaluate_transition("EXPIRED", "BID_PREPARATION")
    assert exp["legal"] is False
    exp_ok = evaluate_transition("EXPIRED", "BID_PREPARATION", renewed_solicitation=True)
    assert exp_ok["legal"] is True

    rej = evaluate_transition("REJECTED", "QUALIFIED")
    assert rej["legal"] is False and rej["requires_override"] is True
    rej_ok = evaluate_transition("REJECTED", "QUALIFIED", override_audited=True)
    assert rej_ok["legal"] is True

    award = evaluate_transition("AWARDED", "ORDERING")
    assert award["legal"] is True

    paid = evaluate_transition("DELIVERED", "PAID")
    assert paid["legal"] is False
    paid_ok = evaluate_transition("DELIVERED", "PAID", acceptance_evidenced=True, invoice_evidenced=True)
    assert paid_ok["legal"] is True


def test_history_ui_preserves_unknown_not_invented():
    js = (ROOT / "static" / "m3-mobile.js").read_text(encoding="utf-8")
    assert "Outcome notes" in js
    assert 'o.lesson || o.loss_reason || o.why_waiting || "UNKNOWN"' in js
    assert "No closed outcomes yet" in (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_empty_and_error_fallback_copy_present():
    js = (ROOT / "static" / "m3-mobile.js").read_text(encoding="utf-8")
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "No opportunities in this filter" in js or "No active opportunities" in js
    assert "Unable to load pipeline" in js
    assert "Unable to load history" in js
    assert "Nothing needs attention right now" in html
    assert "No closed outcomes yet" in html
    assert "No operator actions queued" in html


def test_primary_ui_enum_leakage_audit():
    """Internal Stage/Luna/Terra and raw readiness enums must not drive primary labels."""
    js = (ROOT / "static" / "m3-mobile.js").read_text(encoding="utf-8")
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    for forbidden in ("Stage 0", "Stage 1", "Stage 2", "Stage 3", "Stage 4", "Stage 5", "Luna", "Terra"):
        assert forbidden not in js, forbidden
    assert 'LEVEL_4_UNKNOWN")' not in js  # no direct display fallback
    assert "humanEvidenceLevel" in js
    assert "View as Owner" in html
    assert "not authentication or access control" in html.lower() or "not access control" in html


def test_responsive_layout_hooks_exist():
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    assert "@media" in css
    assert "m3-mobile" in css or "m3-nav" in css
    # Functional sanity: nav + ready banner classes present
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "m3-nav-btn" in html
    assert "m3-deal-ready-banner" in html or "m3-ready-banner" in html


def test_dashboard_buckets_blocked_ready_submitted():
    from operator_workflow.summary import build_operator_dashboard_payload

    rows = [
        {
            "canonical_id": "blocked-1",
            "title": "Blocked",
            "description": "RFQ Part X Quantity: 1 EA",
            "execution_critical_blockers": ["SUPPLIER_NOT_VALIDATED"],
            "ready_for_owner_approval": False,
            "owner_approval_gate": {"ready_for_owner_approval": False},
        },
        {
            "canonical_id": "ready-1",
            "title": "Ready",
            "description": "RFQ Part Y Quantity: 2 EA",
            "ready_for_owner_approval": True,
            "owner_approval_gate": {"ready_for_owner_approval": True},
            "operator_workflow_state": "BID_PREPARATION",
        },
        {
            "canonical_id": "sub-1",
            "title": "Submitted",
            "lifecycle": "SUBMITTED",
            "Deal_state": "SUBMITTED",
            "award_lifecycle_status": "SUBMITTED",
            "ready_for_owner_approval": False,
            "owner_approval_gate": {"ready_for_owner_approval": False},
        },
    ]
    # Without re-enrich (would rebuild gate from text); use enrich=False and pre-set fields
    dash = build_operator_dashboard_payload(rows, enrich=False)
    assert dash["kind"] == "OperatorDashboardPrep"
    ids = {w["canonical_id"] for w in dash["active_work"]}
    assert "blocked-1" in ids or "ready-1" in ids or "sub-1" in ids
    ready_count = sum(1 for w in dash["active_work"] if w.get("ready_for_owner_approval") is True)
    assert ready_count >= 1


def test_owner_vs_operator_view_copy():
    js = (ROOT / "static" / "m3-mobile.js").read_text(encoding="utf-8")
    assert "View as Owner" in js or "View mode: Owner" in js
    assert "not access control" in js.lower() or "not authentication" in js.lower()
    assert "View as Operator" in (ROOT / "static" / "index.html").read_text(encoding="utf-8")
