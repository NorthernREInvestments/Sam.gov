"""Process-wide BidNet Playwright lease (TTL-based).

Opportunity workers and the purchasing-group access manager each use their own
browser client, but must not drive Chromium concurrently against the same
session store. A TTL lease replaces a bare RLock so a hung worker cannot
permanently starve the other lane.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

_lock = threading.Lock()
_holder: str | None = None
_until: float = 0.0
DEFAULT_TTL_S = 150.0


def browser_mutex_status() -> dict[str, Any]:
    now = time.time()
    with _lock:
        held = bool(_holder) and _until > now
        return {
            "held": held,
            "holder": _holder if held else None,
            "until": _until if held else None,
            "remaining_s": round(max(0.0, _until - now), 1) if held else 0.0,
        }


def try_acquire_bidnet_browser(
    *,
    holder: str,
    timeout_s: float = 0.0,
    ttl_s: float = DEFAULT_TTL_S,
) -> bool:
    """Acquire or steal an expired lease. Returns True if this holder owns it."""
    global _holder, _until
    deadline = time.time() + max(0.0, float(timeout_s))
    name = str(holder or "unknown")[:80]
    ttl = max(30.0, float(ttl_s))
    while True:
        now = time.time()
        with _lock:
            if _holder is None or _until <= now or _holder == name:
                _holder = name
                _until = now + ttl
                return True
        if now >= deadline:
            return False
        time.sleep(0.25)


def release_bidnet_browser(*, holder: str | None = None) -> None:
    global _holder, _until
    with _lock:
        if holder and _holder and holder != _holder:
            return
        _holder = None
        _until = 0.0


def refresh_bidnet_browser_lease(*, holder: str, ttl_s: float = DEFAULT_TTL_S) -> bool:
    """Extend lease while long browser work continues."""
    global _until
    with _lock:
        if _holder != str(holder or "")[:80]:
            return False
        _until = time.time() + max(30.0, float(ttl_s))
        return True


@contextmanager
def bidnet_browser_slot(
    *,
    holder: str,
    timeout_s: float = 120.0,
    ttl_s: float = DEFAULT_TTL_S,
) -> Iterator[bool]:
    got = try_acquire_bidnet_browser(holder=holder, timeout_s=timeout_s, ttl_s=ttl_s)
    try:
        yield got
    finally:
        if got:
            release_bidnet_browser(holder=holder)
