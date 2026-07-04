"""Track USAspending API calls avoided when watchlist fingerprint supplies pricing."""

from __future__ import annotations

from typing import Any

# Typical get_regional_benchmark path: regional benchmarks + predecessor lookup.
CALLS_PER_SKIPPED_CONTRACT = 2

_saved_notice_ids: set[str] = set()
_calls_saved = 0


def reset_usaspending_savings() -> None:
    global _calls_saved
    _saved_notice_ids.clear()
    _calls_saved = 0


def record_usaspending_skip(contract: Any, *, calls: int | None = None) -> bool:
    """Record one contract whose USAspending lookups were skipped. Returns False if already counted."""
    global _calls_saved
    notice_id = str(getattr(contract, "notice_id", None) or "").strip()
    if not notice_id or notice_id in _saved_notice_ids:
        return False
    _saved_notice_ids.add(notice_id)
    _calls_saved += int(calls if calls is not None else CALLS_PER_SKIPPED_CONTRACT)
    return True


def get_usaspending_savings() -> dict[str, int]:
    return {
        "usaspending_calls_saved": _calls_saved,
        "usaspending_skipped_contracts": len(_saved_notice_ids),
    }
