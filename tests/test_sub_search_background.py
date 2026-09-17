"""Sub search should not block contract screening."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from sqlalchemy.orm import Session

from database import SessionLocal
from models import Contract
from sub_finder import ensure_sub_search_before_screening, maybe_start_background_sub_search


def _make_contract(session: Session, notice_id: str | None = None) -> Contract:
    nid = notice_id or f"test-bg-sub-search-{uuid4().hex[:10]}"
    session.rollback()
    existing = session.query(Contract).filter_by(notice_id=nid).one_or_none()
    if existing:
        session.delete(existing)
        session.commit()
    row = Contract(
        notice_id=nid,
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
    notice_id = f"test-bg-sub-search-{uuid4().hex[:10]}"
    try:
        row = _make_contract(session, notice_id=notice_id)
        result = ensure_sub_search_before_screening(session, row)
        assert result["started_background"] is True
        assert result["in_progress"] is True
        assert started == [row.notice_id]
    finally:
        session.rollback()
        session.query(Contract).filter_by(notice_id=notice_id).delete()
        session.commit()
        session.close()


def test_maybe_start_skips_when_complete(monkeypatch):
    session = SessionLocal()
    notice_id = f"test-bg-sub-search-{uuid4().hex[:10]}"
    try:
        row = _make_contract(session, notice_id=notice_id)
        row.sub_search_status = "complete"
        session.commit()
        result = maybe_start_background_sub_search(session, row)
        assert result["started"] is False
        assert result["status"] == "complete"
    finally:
        session.rollback()
        session.query(Contract).filter_by(notice_id=notice_id).delete()
        session.commit()
        session.close()
