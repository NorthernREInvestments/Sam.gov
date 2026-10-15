"""Process-wide BidNet Playwright mutex.

Opportunity workers and the purchasing-group access manager each use their own
browser client, but must not drive Chromium concurrently against the same
session store — that causes TimeoutError / lost auth mid-form.

Acquire before launching Playwright; release after close. Fast-path opportunity
work never takes this lock, so registration proceeds while first-pass defers.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

_lock = threading.RLock()
_holder: str | None = None
_holder_since: float | None = None


def browser_mutex_status() -> dict[str, Any]:
    return {
        "held": _lock._is_owned() if hasattr(_lock, "_is_owned") else (_holder is not None),
        "holder": _holder,
        "holder_since": _holder_since,
    }


def try_acquire_bidnet_browser(*, holder: str, timeout_s: float = 0.0) -> bool:
    """Non-blocking (timeout_s=0) or bounded acquire. Returns True if held."""
    global _holder, _holder_since
    import time

    ok = _lock.acquire(timeout=max(0.0, float(timeout_s)))
    if ok:
        _holder = str(holder or "unknown")[:80]
        _holder_since = time.time()
    return ok


def release_bidnet_browser(*, holder: str | None = None) -> None:
    global _holder, _holder_since
    with threading.Lock():
        # RLock release must be by the owning thread; ignore holder mismatch.
        try:
            if holder and _holder and holder != _holder:
                # Still release if this thread owns it (re-entrant safety).
                pass
            _holder = None
            _holder_since = None
            _lock.release()
        except RuntimeError:
            # not owned by this thread
            pass


@contextmanager
def bidnet_browser_slot(*, holder: str, timeout_s: float = 120.0) -> Iterator[bool]:
    """Context manager. Yields True if lock acquired, False if timed out."""
    got = try_acquire_bidnet_browser(holder=holder, timeout_s=timeout_s)
    try:
        yield got
    finally:
        if got:
            release_bidnet_browser(holder=holder)
