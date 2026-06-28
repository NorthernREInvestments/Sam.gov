"""Track and enforce daily API usage budgets (SAM.gov + Claude screening)."""

from __future__ import annotations

import os
from datetime import date
from typing import Any

from database import SessionLocal
from models import AppSetting


def _today() -> str:
    return date.today().isoformat()


def _daily_limit(env_key: str, default: int) -> int:
    raw = os.getenv(env_key, str(default)).strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def sam_daily_limit() -> int:
    return _daily_limit("SAM_DAILY_API_BUDGET", 150)


def screen_daily_limit() -> int:
    return _daily_limit("ANTHROPIC_DAILY_SCREEN_BUDGET", 10)


def auto_screen_on_startup() -> bool:
    return os.getenv("AUTO_SCREEN_ON_STARTUP", "false").strip().lower() in ("1", "true", "yes")


def enrich_on_sync_limit() -> int:
    return _daily_limit("ENRICH_ON_SYNC_LIMIT", 5)


def sam_pdf_download_limit() -> int:
    """Separate daily cap for PDF bytes fetched during Claude screening."""
    return _daily_limit("SAM_PDF_DOWNLOAD_BUDGET", 25)


def _usage_key(prefix: str) -> str:
    return f"{prefix}_usage_{_today()}"


def _get_usage(session, prefix: str) -> int:
    row = session.get(AppSetting, _usage_key(prefix))
    if not row:
        return 0
    try:
        return max(0, int(row.value))
    except ValueError:
        return 0


def _set_usage(session, prefix: str, value: int) -> None:
    key = _usage_key(prefix)
    row = session.get(AppSetting, key)
    if row:
        row.value = str(value)
    else:
        session.add(AppSetting(key=key, value=str(value)))


def get_usage_snapshot() -> dict[str, Any]:
    session = SessionLocal()
    try:
        sam_used = _get_usage(session, "sam_api")
        sam_pdf_used = _get_usage(session, "sam_pdf")
        screen_used = _get_usage(session, "anthropic_screen")
    finally:
        session.close()

    sam_limit = sam_daily_limit()
    sam_pdf_limit = sam_pdf_download_limit()
    screen_limit = screen_daily_limit()
    return {
        "sam_used_today": sam_used,
        "sam_daily_limit": sam_limit,
        "sam_remaining": max(0, sam_limit - sam_used),
        "sam_pdf_downloads_today": sam_pdf_used,
        "sam_pdf_download_limit": sam_pdf_limit,
        "sam_pdf_downloads_remaining": max(0, sam_pdf_limit - sam_pdf_used),
        "screens_used_today": screen_used,
        "screen_daily_limit": screen_limit,
        "screens_remaining": max(0, screen_limit - screen_used),
        "auto_screen_on_startup": auto_screen_on_startup(),
        "enrich_on_sync_limit": enrich_on_sync_limit(),
    }


def can_spend_sam(credits: int = 1) -> bool:
    if credits <= 0:
        return True
    snap = get_usage_snapshot()
    return snap["sam_remaining"] >= credits


def can_download_screening_pdf() -> bool:
    snap = get_usage_snapshot()
    return snap["sam_pdf_downloads_remaining"] > 0


def record_sam_pdf_download() -> bool:
    """Record one SAM.gov-hosted PDF download during Claude screening."""
    session = SessionLocal()
    try:
        used = _get_usage(session, "sam_pdf")
        limit = sam_pdf_download_limit()
        if used + 1 > limit:
            return False
        _set_usage(session, "sam_pdf", used + 1)
        session.commit()
        return True
    finally:
        session.close()


def can_screen() -> bool:
    snap = get_usage_snapshot()
    return snap["screens_remaining"] > 0


def record_sam_usage(credits: int = 1) -> bool:
    """Record SAM.gov API usage. Returns False if budget would be exceeded."""
    if credits <= 0:
        return True
    session = SessionLocal()
    try:
        used = _get_usage(session, "sam_api")
        limit = sam_daily_limit()
        if used + credits > limit:
            return False
        _set_usage(session, "sam_api", used + credits)
        session.commit()
        return True
    finally:
        session.close()


def record_screen_usage() -> bool:
    session = SessionLocal()
    try:
        used = _get_usage(session, "anthropic_screen")
        limit = screen_daily_limit()
        if used + 1 > limit:
            return False
        _set_usage(session, "anthropic_screen", used + 1)
        session.commit()
        return True
    finally:
        session.close()


class SamBudgetExceeded(Exception):
    def __init__(self, message: str | None = None):
        snap = get_usage_snapshot()
        detail = message or (
            f"SAM.gov daily API budget exhausted "
            f"({snap['sam_used_today']}/{snap['sam_daily_limit']} used today)."
        )
        super().__init__(detail)


class ScreenBudgetExceeded(Exception):
    def __init__(self, message: str | None = None):
        snap = get_usage_snapshot()
        detail = message or (
            f"Daily Claude screening budget exhausted "
            f"({snap['screens_used_today']}/{snap['screen_daily_limit']} used today)."
        )
        super().__init__(detail)


def require_sam_budget(credits: int = 1) -> None:
    if not can_spend_sam(credits):
        raise SamBudgetExceeded()


def require_screen_budget() -> None:
    if not can_screen():
        raise ScreenBudgetExceeded()
