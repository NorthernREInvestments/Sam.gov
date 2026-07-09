"""Sub search should not block contract screening."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from sqlalchemy.orm import Session

from database import SessionLocal
from models import Contract
from sub_finder import ensure_sub_search_before_screening, maybe_start_background_sub_search


def _make_contract(session: Session) -> Contract:
    row = Contract(
        notice_id="test-bg-sub-search",
        title="Grounds maintenance",
        agency="US Air Force",
        location="Columbus, MS",
        naics_code="561720",
        due_date=date.today(),
        status="reviewing",
        sam_raw={"descriptionText": "Grounds"},
        analysis={},
    )
    session.add(row)
    session.commit()
    return session.query(Contract).filter_by(notice_id=row.notice_id).one()


def test_ensure_sub_search_starts_background(monkeypatch):
    started: list[str] = []

    monkeypatch.setattr(
        "screening_pipeline.has_attachments_ready",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "sub_finder.start_background_sub_search",
        lambda notice_id, **kw: started.append(notice_id),
    )

    session = SessionLocal()
    try:
        row = _make_contract(session)
        result = ensure_sub_search_before_screening(session, row)
        assert result["started_background"] is True
        assert result["in_progress"] is True
        assert started == [row.notice_id]
    finally:
        session.query(Contract).filter_by(notice_id="test-bg-sub-search").delete()
        session.commit()
        session.close()


def test_maybe_start_skips_when_complete(monkeypatch):
    session = SessionLocal()
    try:
        row = _make_contract(session)
        row.sub_search_status = "complete"
        session.commit()
        result = maybe_start_background_sub_search(session, row)
        assert result["started"] is False
        assert result["status"] == "complete"
    finally:
        session.query(Contract).filter_by(notice_id="test-bg-sub-search").delete()
        session.commit()
        session.close()
