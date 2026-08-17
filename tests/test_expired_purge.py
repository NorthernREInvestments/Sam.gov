"""Past-due opportunities that were never pursued should be deleted."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from expired_purge_service import (
    contract_was_pursued,
    purge_expired_unpursued,
    remove_expired_unpursued_contracts,
)


def _contract(**kwargs) -> Contract:
    defaults = {
        "id": 1,
        "notice_id": "n1",
        "title": "Test",
        "status": "new",
        "due_date": date.today() - timedelta(days=1),
        "selected_sub_quote": None,
        "award_date": None,
        "period_of_performance_start": None,
        "analysis": {},
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_contract_was_pursued_false_for_new():
    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = None
    session.query.return_value.filter.return_value.limit.return_value.first.return_value = None
    assert contract_was_pursued(session, _contract(status="new")) is False
    assert contract_was_pursued(session, _contract(status="skipped")) is False
    assert contract_was_pursued(session, _contract(status="reviewing")) is False


def test_contract_was_pursued_true_for_bidding_and_manual_pursue():
    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = None
    session.query.return_value.filter.return_value.limit.return_value.first.return_value = None
    assert contract_was_pursued(session, _contract(status="bidding")) is True
    assert contract_was_pursued(session, _contract(status="submitted")) is True
    assert (
        contract_was_pursued(
            session,
            _contract(status="new", analysis={"csv_import": {"pursued_manually": True}}),
        )
        is True
    )


def test_remove_expired_keeps_pursued(monkeypatch):
    expired = _contract(notice_id="expired-new", status="new")
    pursued = _contract(notice_id="expired-bid", status="bidding")

    deleted: list = []

    class FakeQuery:
        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return [expired, pursued]

        def filter_by(self, **kwargs):
            return self

        def first(self):
            return None

        def limit(self, n):
            return self

    class FakeSession:
        def query(self, model):
            return FakeQuery()

        def delete(self, row):
            deleted.append(row)

        def flush(self):
            pass

    session = FakeSession()
    removed = remove_expired_unpursued_contracts(session)
    assert removed == 1
    assert deleted == [expired]


def test_purge_calls_csv_and_contracts(monkeypatch):
    calls = {"contracts": 0, "csv": 0, "queue": 0}

    monkeypatch.setattr(
        "expired_purge_service.remove_expired_unpursued_contracts",
        lambda session, today=None: calls.__setitem__("contracts", 2) or 2,
    )
    monkeypatch.setattr(
        "expired_purge_service.remove_expired_csv_rows",
        lambda session, today=None: calls.__setitem__("csv", 3) or 3,
    )
    monkeypatch.setattr(
        "csv_attachment_queue_service.clear_pending_queue_for_deleted_csv",
        lambda session: calls.__setitem__("queue", 1) or 1,
    )

    result = purge_expired_unpursued(MagicMock())
    assert result == {"contracts_removed": 2, "csv_removed": 3, "queue_cleared": 1}
    assert calls == {"contracts": 2, "csv": 3, "queue": 1}
