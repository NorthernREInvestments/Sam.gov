"""Runtime resource guards — throttle before the container becomes unresponsive."""

from __future__ import annotations

import os
import threading
from typing import Any

try:
    import resource as _resource  # Unix
except ImportError:  # Windows
    _resource = None  # type: ignore


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def limits() -> dict[str, int]:
    return {
        "MAX_BROWSER_PROCESSES": _env_int("BIDNET_MAX_BROWSER_PROCESSES", 2),
        "MAX_BROWSER_CONTEXTS": _env_int("BIDNET_MAX_BROWSER_CONTEXTS", 4),
        "MAX_THREADS": _env_int("BIDNET_MAX_THREADS", 64),
        "MAX_MEMORY_PERCENT": _env_int("BIDNET_MAX_MEMORY_PERCENT", 85),
        "MAX_PENDING_TASKS": _env_int("BIDNET_MAX_PENDING_TASKS", 40),
    }


def thread_count() -> int:
    return threading.active_count()


def memory_percent() -> float | None:
    try:
        import psutil  # type: ignore

        return float(psutil.Process().memory_percent())
    except Exception:
        pass
    if _resource is not None:
        try:
            usage = _resource.getrusage(_resource.RUSAGE_SELF)
            # ru_maxrss is KB on Linux
            rss_kb = float(usage.ru_maxrss)
            # Without cgroup limit, return None rather than invent a percent.
            limit = os.environ.get("BIDNET_MEMORY_LIMIT_MB")
            if limit:
                return round(100.0 * rss_kb / (float(limit) * 1024.0), 1)
        except Exception:
            pass
    return None


def snapshot() -> dict[str, Any]:
    lim = limits()
    mem = memory_percent()
    threads = thread_count()
    return {
        "limits": lim,
        "threads": threads,
        "memory_percent": mem,
        "thread_pressure": threads >= lim["MAX_THREADS"],
        "memory_pressure": mem is not None and mem >= lim["MAX_MEMORY_PERCENT"],
    }


def should_throttle(*, pending_tasks: int = 0) -> tuple[bool, str]:
    snap = snapshot()
    lim = snap["limits"]
    if snap["thread_pressure"]:
        return True, f"threads={snap['threads']}>=MAX_THREADS={lim['MAX_THREADS']}"
    if snap["memory_pressure"]:
        return True, f"memory_percent={snap['memory_percent']}>=MAX_MEMORY_PERCENT={lim['MAX_MEMORY_PERCENT']}"
    if pending_tasks >= lim["MAX_PENDING_TASKS"]:
        return True, f"pending_tasks={pending_tasks}>=MAX_PENDING_TASKS={lim['MAX_PENDING_TASKS']}"
    return False, ""


def recommended_browser_workers(current: int) -> int:
    """Reduce browser concurrency under pressure."""
    throttle, _ = should_throttle()
    if throttle:
        return max(1, min(current, 1))
    return max(1, min(current, limits()["MAX_BROWSER_PROCESSES"]))
