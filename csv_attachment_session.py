"""Track SAM API calls used during a CSV upload / attachment-queue session."""

from __future__ import annotations

_session_calls = 0


def reset_csv_attachment_session() -> None:
    global _session_calls
    _session_calls = 0


def record_csv_attachment_session_calls(calls: int = 1) -> None:
    global _session_calls
    if calls > 0:
        _session_calls += int(calls)


def get_csv_attachment_session_calls() -> int:
    return _session_calls
