"""Lightweight performance timing for acquisition/discovery (Build 002F)."""

from __future__ import annotations

import statistics
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

_lock = threading.Lock()
_samples: dict[str, list[float]] = {}


def reset_perf() -> None:
    with _lock:
        _samples.clear()


@contextmanager
def timed(name: str) -> Iterator[None]:
    t0 = time.perf_counter()
    try:
        yield
    finally:
        dt = time.perf_counter() - t0
        with _lock:
            _samples.setdefault(name, []).append(dt)


def record(name: str, seconds: float) -> None:
    with _lock:
        _samples.setdefault(name, []).append(float(seconds))


def _stats(vals: list[float]) -> dict[str, Any]:
    if not vals:
        return {"n": 0}
    s = sorted(vals)
    n = len(s)
    p95_idx = min(n - 1, max(0, int(0.95 * (n - 1))))
    return {
        "n": n,
        "avg_s": round(statistics.fmean(s), 4),
        "median_s": round(statistics.median(s), 4),
        "p95_s": round(s[p95_idx], 4),
        "max_s": round(s[-1], 4),
        "sum_s": round(sum(s), 3),
    }


def snapshot() -> dict[str, Any]:
    with _lock:
        data = {k: list(v) for k, v in _samples.items()}
    return {k: _stats(v) for k, v in sorted(data.items())}
