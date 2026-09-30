"""Regression: live persist must not poison the discovery session."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from discovery.live_runner import _safe_commit_session, _safe_persist_opportunity


def test_safe_persist_rolls_back_on_upsert_failure():
    session = MagicMock()
    metrics: dict = {"persist_failures": 0, "persist_errors": []}
    opp = SimpleNamespace(external_id="X1")

    with patch(
        "discovery.live_runner.upsert_canonical_opportunity",
        side_effect=RuntimeError("row boom"),
    ):
        ok = _safe_persist_opportunity(session, opp, metrics=metrics, sid="state_ne")

    assert ok is False
    assert metrics["persist_failures"] == 1
    assert metrics["persist_errors"][0]["source_id"] == "state_ne"
    session.rollback.assert_called()


def test_safe_commit_rolls_back_on_commit_failure():
    session = MagicMock()
    session.commit.side_effect = RuntimeError("commit boom")
    metrics: dict = {"commit_failures": 0, "commit_errors": []}

    _safe_commit_session(session, metrics=metrics, sid="state_wy")

    assert metrics["commit_failures"] == 1
    session.rollback.assert_called()
