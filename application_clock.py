"""Centralized timezone-aware application clock for M3.

Production uses SystemClock (OS/runtime). Tests and historical simulation use FrozenClock.
All deadline and run-time logic must obtain "now" through this module — never invent a
hard-coded production current date.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, Generator, Iterator
from zoneinfo import ZoneInfo

# --- Clock modes ---
CLOCK_SYSTEM = "SYSTEM"
CLOCK_FROZEN_TEST = "FROZEN_TEST"
CLOCK_HISTORICAL_SIMULATION = "HISTORICAL_SIMULATION"


class ApplicationClock(ABC):
    """Authoritative abstraction for application current time."""

    @property
    @abstractmethod
    def mode(self) -> str:
        ...

    @abstractmethod
    def now_utc(self) -> datetime:
        """Timezone-aware UTC datetime."""
        ...

    def now_in_timezone(self, tz: str | ZoneInfo) -> datetime:
        zone = ZoneInfo(tz) if isinstance(tz, str) else tz
        return self.now_utc().astimezone(zone)

    def now_local(self) -> datetime:
        """Current time in the operating system's local timezone."""
        return self.now_utc().astimezone()

    def today_in_timezone(self, tz: str | ZoneInfo) -> date:
        return self.now_in_timezone(tz).date()

    def today_local(self) -> date:
        """Calendar date in the OS local timezone (replaces date.today())."""
        return self.now_local().date()

    def knowledge_cutoff_at(self) -> datetime | None:
        """Simulation cutoff when historical mode is active; else None."""
        return None

    def isoformat_utc(self) -> str:
        return self.now_utc().isoformat()

    def unix_timestamp(self) -> float:
        """Epoch seconds from authoritative clock (for tokens / age checks)."""
        return self.now_utc().timestamp()


class SystemClock(ApplicationClock):
    """Production clock — always reads the operating system / runtime."""

    @property
    def mode(self) -> str:
        return CLOCK_SYSTEM

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock(ApplicationClock):
    """Deterministic clock for tests and historical simulation."""

    def __init__(
        self,
        as_of: datetime,
        *,
        mode: str = CLOCK_FROZEN_TEST,
    ) -> None:
        if as_of.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        if mode not in {CLOCK_FROZEN_TEST, CLOCK_HISTORICAL_SIMULATION}:
            raise ValueError(f"unsupported FrozenClock mode: {mode}")
        self._as_of = as_of.astimezone(timezone.utc)
        self._mode = mode

    @property
    def mode(self) -> str:
        return self._mode

    def now_utc(self) -> datetime:
        return self._as_of

    def knowledge_cutoff_at(self) -> datetime | None:
        if self._mode == CLOCK_HISTORICAL_SIMULATION:
            return self._as_of
        return None


_clock: ApplicationClock = SystemClock()


def get_clock() -> ApplicationClock:
    return _clock


def set_clock(clock: ApplicationClock) -> ApplicationClock:
    global _clock
    if not isinstance(clock, ApplicationClock):
        raise TypeError("clock must be an ApplicationClock")
    _clock = clock
    return _clock


def reset_clock() -> ApplicationClock:
    """Restore production SystemClock."""
    return set_clock(SystemClock())


def now_utc() -> datetime:
    return get_clock().now_utc()


def now_in_timezone(tz: str | ZoneInfo) -> datetime:
    return get_clock().now_in_timezone(tz)


def now_local() -> datetime:
    return get_clock().now_local()


def today_in_timezone(tz: str | ZoneInfo) -> date:
    return get_clock().today_in_timezone(tz)


def today_local() -> date:
    return get_clock().today_local()


def clock_mode() -> str:
    return get_clock().mode


def knowledge_cutoff_at() -> datetime | None:
    return get_clock().knowledge_cutoff_at()


def unix_timestamp() -> float:
    return get_clock().unix_timestamp()


@contextmanager
def use_clock(clock: ApplicationClock) -> Iterator[ApplicationClock]:
    previous = get_clock()
    set_clock(clock)
    try:
        yield clock
    finally:
        set_clock(previous)


@contextmanager
def freeze_time(
    as_of: datetime,
    *,
    mode: str = CLOCK_FROZEN_TEST,
) -> Iterator[FrozenClock]:
    with use_clock(FrozenClock(as_of, mode=mode)) as clock:
        assert isinstance(clock, FrozenClock)
        yield clock


def start_run_metadata(*, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Begin run metadata for a substantial M3 run."""
    started = now_utc()
    meta = {
        "run_started_at": started.isoformat(),
        "run_completed_at": None,
        "clock_mode": clock_mode(),
        "knowledge_cutoff_at": (
            knowledge_cutoff_at().isoformat() if knowledge_cutoff_at() else None
        ),
    }
    if extra:
        meta.update(extra)
    return meta


def complete_run_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    out = dict(meta)
    out["run_completed_at"] = now_utc().isoformat()
    out["clock_mode"] = clock_mode()
    return out
