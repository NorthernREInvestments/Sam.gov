"""GovTracker loads every gs_watchlist row — no GovSpend UI filter mirroring."""

from __future__ import annotations

from gs_watchlist_service import _watchlist_order_by, watchlist_status


def test_watchlist_order_by_uses_available_columns():
    order = _watchlist_order_by(frozenset({"priority", "id"}))
    assert "priority" in order.lower()
    assert "id ASC" in order
    assert "expected_repost_start" not in order

    full = _watchlist_order_by(
        frozenset({"priority", "expected_repost_start", "expiration_date", "id"})
    )
    assert "expected_repost_start NULLS LAST" in full
    assert "expiration_date NULLS LAST" in full


def test_watchlist_status_monitors_all_rows():
    status = watchlist_status()
    assert status.get("monitors_all_rows") is True
    assert "status_filter" not in status
    assert "priorities" not in status


def test_load_watching_targets_sql_has_no_status_filter(monkeypatch):
    captured: dict[str, str] = {}

    class _Result:
        def mappings(self):
            return self

        def all(self):
            return []

    class _Session:
        def execute(self, statement, params=None):
            captured["sql"] = str(statement)
            return _Result()

        def close(self):
            pass

    import gs_watchlist_service as svc

    svc.clear_watchlist_cache()
    monkeypatch.setattr(svc, "_resolved_table_cached", lambda: "gs_watchlist")
    monkeypatch.setattr(
        svc,
        "_watchlist_table_columns",
        lambda: frozenset({"id", "priority", "status", "contract_name"}),
    )
    monkeypatch.setattr(svc, "_select_columns", lambda: ["id", "contract_name", "priority", "status"])
    monkeypatch.setattr(svc, "SharedReadSession", lambda: _Session())

    svc._load_watching_targets_cached.cache_clear()
    svc._load_watching_targets_cached("gs_watchlist")

    sql = captured.get("sql", "").lower()
    assert " where " not in sql
    assert "= any" not in sql
