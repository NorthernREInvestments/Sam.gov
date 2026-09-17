"""Regression: screening must not touch expired detached Contract rows."""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from database import SessionLocal
from intake import run_full_analysis
from models import Contract


def _make_contract(session: Session) -> Contract:
    row = Contract(
        notice_id="test-detach-screen",
        title="Grounds",
        agency="US Air Force",
        location="Columbus, MS",
        naics_code="561720",
        due_date=date.today(),
        status="new",
        sam_raw={"descriptionText": "Grounds maintenance"},
        analysis={"screening_stage": "text", "text_score": 8},
    )
    session.add(row)
    session.commit()
    return session.query(Contract).filter_by(notice_id=row.notice_id).one()


def test_run_full_analysis_reloads_row_before_ai_screen(monkeypatch):
    """After commit+close, screen_contract must receive a fresh expunged row."""
    session = SessionLocal()
    try:
        row = _make_contract(session)
        seen: dict[str, object] = {}

        def fake_screen(contract, **kwargs):
            seen["contract"] = contract
            # Expired detached rows raise on attribute access after commit+close.
            _ = contract.title
            _ = contract.sam_raw
            return {"score": 8, "pursue": True, "screening_stage": "full"}

        monkeypatch.setattr("screening_pipeline.has_attachments_ready", lambda *a, **k: True)
        monkeypatch.setattr("attachment_pipeline.ensure_attachments_from_database", lambda *a, **k: True)
        monkeypatch.setattr("sub_finder.ensure_sub_type_from_pdfs", lambda *a, **k: None)
        monkeypatch.setattr("sub_finder.ensure_sub_search_before_screening", lambda *a, **k: None)
        monkeypatch.setattr("sub_finder.subs_context_for_screening", lambda *a, **k: None)
        monkeypatch.setattr("api_budget.can_screen", lambda: True)
        monkeypatch.setattr("intake.can_screen", lambda: True)
        monkeypatch.setattr("api_budget.record_screen_usage", lambda: True)
        monkeypatch.setattr("intake.record_screen_usage", lambda: True)
        monkeypatch.setattr("intake.screen_contract", fake_screen)
        monkeypatch.setattr("pws_fields.apply_pws_extraction", lambda *a, **k: None)
        monkeypatch.setattr("pws_fields.contract_pws_missing", lambda *a, **k: False)
        monkeypatch.setattr("screening_pipeline.finalize_full_analysis", lambda *a, **k: None)
        monkeypatch.setattr("prior_contract_extract.merge_prior_contract_hints", lambda *a, **k: None)
        monkeypatch.setattr("prior_contract_extract.prior_contract_hints_complete", lambda *a, **k: True)
        monkeypatch.setattr("submission_package.apply_submission_package", lambda *a, **k: {})

        result = run_full_analysis(row, prior=row.analysis, session=session)

        assert result.get("skipped") is False
        assert seen.get("contract") is not None
        assert seen["contract"] is not row
    finally:
        session.query(Contract).filter_by(notice_id="test-detach-screen").delete()
        session.commit()
        session.close()


def test_force_full_analysis_contract_accepts_session(monkeypatch):
    from intake import force_full_analysis_contract

    session = SessionLocal()
    try:
        row = _make_contract(session)
        monkeypatch.setattr(
            "intake.run_full_analysis",
            lambda *a, **k: {"notice_id": row.notice_id, "skipped": False},
        )
        monkeypatch.setattr("intake._try_begin_intake", lambda _notice_id: True)
        monkeypatch.setattr("intake._end_intake", lambda _notice_id: None)
        monkeypatch.setattr("screening_pipeline.needs_text_screening", lambda _analysis: False)

        result = force_full_analysis_contract(row, session=session)
        assert result["notice_id"] == row.notice_id
    finally:
        session.query(Contract).filter_by(notice_id="test-detach-screen").delete()
        session.commit()
        session.close()
