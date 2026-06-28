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
    """SAM.gov search + enrich calls per day (protect expiring API key credits)."""
    return _daily_limit("SAM_DAILY_API_BUDGET", 10)


def scheduled_naics_per_sync() -> int:
    """How many NAICS codes the 6am scheduled sync searches per run (rotates through the pool)."""
    return max(1, _daily_limit("SCHEDULED_NAICS_PER_SYNC", 2))


def scheduled_sync_batch_size() -> int:
    """Scheduled sync batch size capped by remaining SAM.gov budget."""
    snap = get_usage_snapshot()
    remaining = snap["sam_remaining"]
    if remaining <= 0:
        return 0
    return min(scheduled_naics_per_sync(), remaining)


def screen_daily_limit() -> int:
    """Claude screenings per day. 0 = unlimited (no daily cap)."""
    return _daily_limit("ANTHROPIC_DAILY_SCREEN_BUDGET", 0)


def enrich_on_sync_limit() -> int:
    return _daily_limit("ENRICH_ON_SYNC_LIMIT", 5)


def intake_on_sync_enabled() -> bool:
    raw = os.getenv("INTAKE_ON_SYNC", "true").strip().lower()
    return raw not in ("0", "false", "no")


def intake_per_sync_limit() -> int:
    return _daily_limit("INTAKE_PER_SYNC_LIMIT", 5)


def scrape_max_per_sync() -> int:
    """Max fully scraped contracts per NAICS sync. 0 = scrape every search result."""
    return _daily_limit("SCRAPE_MAX_PER_SYNC", 0)


def attachment_enrich_per_sync_limit() -> int:
    """How many matching contracts get SAM attachment lists loaded per sync / list refresh."""
    return _daily_limit("ATTACHMENT_ENRICH_PER_SYNC_LIMIT", 5)


def attachment_enrich_on_list_limit() -> int:
    return _daily_limit("ATTACHMENT_ENRICH_ON_LIST_LIMIT", 3)


def auto_screen_on_startup() -> bool:
    """Legacy flag — intake on startup uses INTAKE_ON_SYNC instead."""
    if os.getenv("AUTO_SCREEN_ON_STARTUP", "").strip():
        return os.getenv("AUTO_SCREEN_ON_STARTUP", "false").strip().lower() in ("1", "true", "yes")
    return intake_on_sync_enabled()


def sam_pdf_download_limit() -> int:
    """Separate daily cap for PDF bytes fetched from SAM.gov during Claude screening."""
    return _daily_limit("SAM_PDF_DOWNLOAD_BUDGET", 10)


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
    screens_unlimited = screen_limit == 0
    return {
        "sam_used_today": sam_used,
        "sam_daily_limit": sam_limit,
        "sam_remaining": max(0, sam_limit - sam_used),
        "sam_pdf_downloads_today": sam_pdf_used,
        "sam_pdf_download_limit": sam_pdf_limit,
        "sam_pdf_downloads_remaining": max(0, sam_pdf_limit - sam_pdf_used),
        "screens_used_today": screen_used,
        "screen_daily_limit": screen_limit,
        "screens_unlimited": screens_unlimited,
        "screens_remaining": None if screens_unlimited else max(0, screen_limit - screen_used),
        "auto_screen_on_startup": auto_screen_on_startup(),
        "enrich_on_sync_limit": enrich_on_sync_limit(),
        "intake_on_sync": intake_on_sync_enabled(),
        "intake_per_sync_limit": intake_per_sync_limit(),
        "scheduled_naics_per_sync": scheduled_naics_per_sync(),
        "attachment_enrich_per_sync_limit": attachment_enrich_per_sync_limit(),
        "attachment_enrich_on_list_limit": attachment_enrich_on_list_limit(),
    }


def can_spend_sam(credits: int = 1) -> bool:
    if credits <= 0:
        return True
    snap = get_usage_snapshot()
    return snap["sam_remaining"] >= credits


def can_download_screening_pdf() -> bool:
    limit = sam_pdf_download_limit()
    if limit == 0:
        return True
    snap = get_usage_snapshot()
    return snap["sam_pdf_downloads_remaining"] > 0


def record_sam_pdf_download() -> bool:
    """Record one SAM.gov-hosted PDF download during Claude screening."""
    session = SessionLocal()
    try:
        used = _get_usage(session, "sam_pdf")
        limit = sam_pdf_download_limit()
        if limit > 0 and used + 1 > limit:
            return False
        _set_usage(session, "sam_pdf", used + 1)
        session.commit()
        return True
    finally:
        session.close()


def can_screen() -> bool:
    limit = screen_daily_limit()
    if limit == 0:
        return True
    snap = get_usage_snapshot()
    return (snap["screens_remaining"] or 0) > 0


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
        if limit > 0 and used + 1 > limit:
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
