"""Track and enforce daily API usage budgets (SAM.gov + AI screening)."""

from __future__ import annotations
from application_clock import now_utc, today_local

import os
from datetime import date
from typing import Any

from database import SessionLocal
from models import AppSetting

# Legacy usage key kept for same-day continuity after Anthropic → OpenAI migration.
_AI_USAGE_KEYS = ("ai_screen", "anthropic_screen")


def _today() -> str:
    return today_local().isoformat()


def _daily_limit(env_key: str, default: int) -> int:
    raw = os.getenv(env_key, str(default)).strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def sam_daily_limit() -> int:
    """SAM.gov search + enrich calls per day (protect expiring API key credits).

    Prefer SAM_API_CALL_LIMIT when set; fall back to SAM_DAILY_API_BUDGET (default 10).
    """
    if os.getenv("SAM_API_CALL_LIMIT") is not None:
        return _daily_limit("SAM_API_CALL_LIMIT", 10)
    return _daily_limit("SAM_DAILY_API_BUDGET", 10)


def scheduled_naics_per_sync() -> int:
    """How many NAICS codes the scheduled sync searches per run (1 = deep focus on one code)."""
    return max(1, _daily_limit("SCHEDULED_NAICS_PER_SYNC", 1))


def scheduled_sync_batch_size() -> int:
    """Scheduled sync batch size capped by remaining SAM.gov budget."""
    snap = get_usage_snapshot()
    remaining = snap["sam_remaining"]
    if remaining <= 0:
        return 0
    return min(scheduled_naics_per_sync(), remaining)


def screen_daily_limit() -> int:
    """
    AI screenings per day.
    Prefer AI_DAILY_SCREEN_BUDGET; fall back to legacy ANTHROPIC_DAILY_SCREEN_BUDGET if set.
    Default 25 (not unlimited) to protect OpenAI spend.
    """
    if os.getenv("AI_DAILY_SCREEN_BUDGET") is not None:
        return _daily_limit("AI_DAILY_SCREEN_BUDGET", 25)
    if os.getenv("ANTHROPIC_DAILY_SCREEN_BUDGET") is not None:
        return _daily_limit("ANTHROPIC_DAILY_SCREEN_BUDGET", 25)
    return 25


def enrich_on_sync_limit() -> int:
    return _daily_limit("ENRICH_ON_SYNC_LIMIT", 5)


def intake_on_sync_enabled() -> bool:
    """Default OFF — prevent automatic paid AI on every sync unless explicitly enabled."""
    raw = os.getenv("INTAKE_ON_SYNC", "false").strip().lower()
    return raw in ("1", "true", "yes")


def auto_screen_on_contract_detail() -> bool:
    """
    When true, opening an unscored contract detail may trigger paid AI screening.
    Default OFF — user/explicit workflow should trigger screening.
    """
    raw = os.getenv("AUTO_SCREEN_ON_CONTRACT_DETAIL", "false").strip().lower()
    return raw in ("1", "true", "yes")


def is_ai_api_blocked(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if "credit balance" in msg:
        return True
    if "insufficient_quota" in msg or "insufficient quota" in msg:
        return True
    if "authentication" in msg and "api" in msg:
        return True
    if "invalid_api_key" in msg or "incorrect api key" in msg:
        return True
    return False


# Backward-compatible alias
is_anthropic_api_blocked = is_ai_api_blocked


class AIPipelineHalt(Exception):
    """Legacy — errors return per-contract results; queue always continues."""

    def __init__(self, reason: str, *, notice_id: str | None = None, detail: str | None = None):
        self.reason = reason
        self.notice_id = notice_id
        self.detail = detail
        super().__init__(detail or reason)


ClaudePipelineHalt = AIPipelineHalt


def ai_intake_allowed() -> bool:
    """Automatic AI intake/repair (from stored PDFs) when sync intake is on and budget allows."""
    if not intake_on_sync_enabled():
        return False
    return can_screen()


claude_intake_allowed = ai_intake_allowed


def intake_per_sync_limit() -> int | None:
    """Max AI intakes per sync. 0 = unlimited within daily screen budget."""
    raw = _daily_limit("INTAKE_PER_SYNC_LIMIT", 0)
    return None if raw == 0 else raw


def scrape_max_per_sync() -> int:
    """Max fully scraped contracts per NAICS sync. 0 = scrape every search result."""
    return _daily_limit("SCRAPE_MAX_PER_SYNC", 0)


def attachment_enrich_per_sync_limit() -> int | None:
    """Max attachment scrapes per manual sync. 0 = use all remaining SAM budget."""
    raw = _daily_limit("ATTACHMENT_ENRICH_PER_SYNC_LIMIT", 0)
    return None if raw == 0 else raw


def attachment_enrich_on_list_limit() -> int:
    return _daily_limit("ATTACHMENT_ENRICH_ON_LIST_LIMIT", 3)


def scheduled_sync_attachments_only() -> bool:
    """True during the configured attachments-only window (inclusive on both ends)."""
    until = scheduled_sync_attachments_only_until()
    if until is not None:
        today = today_local()
        start = scheduled_sync_attachments_only_from()
        if start is not None and today < start:
            return False
        return today <= until
    raw = os.getenv("SCHEDULED_SYNC_ATTACHMENTS_ONLY", "false").strip().lower()
    return raw in ("1", "true", "yes")


def scheduled_sync_attachments_only_from() -> date | None:
    """First calendar day (inclusive) for attachments-only 6am syncs."""
    raw = os.getenv("SCHEDULED_SYNC_ATTACHMENTS_ONLY_FROM", "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def scheduled_sync_attachments_only_until() -> date | None:
    raw = os.getenv("SCHEDULED_SYNC_ATTACHMENTS_ONLY_UNTIL", "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def sam_pdf_download_limit() -> int:
    return _daily_limit("SAM_PDF_DOWNLOAD_BUDGET", 0)


def auto_screen_on_startup() -> bool:
    raw = os.getenv("AUTO_SCREEN_ON_STARTUP", "false").strip().lower()
    return raw in ("1", "true", "yes")


def _usage_key(prefix: str) -> str:
    return f"{prefix}_usage_{_today()}"


def _get_usage(session, prefix: str) -> int:
    row = session.query(AppSetting).filter_by(key=_usage_key(prefix)).first()
    if not row or not row.value:
        return 0
    try:
        return max(0, int(row.value))
    except ValueError:
        return 0


def _get_ai_screen_usage(session) -> int:
    """Read AI screen usage; merge legacy anthropic_screen counter for the same day."""
    total = 0
    for prefix in _AI_USAGE_KEYS:
        total += _get_usage(session, prefix)
    return total


def _set_usage(session, prefix: str, value: int) -> None:
    key = _usage_key(prefix)
    row = session.query(AppSetting).filter_by(key=key).first()
    if row:
        row.value = str(value)
    else:
        session.add(AppSetting(key=key, value=str(value)))


def _usage_counts() -> dict[str, int]:
    """Read daily usage counters only — never calls can_screen / get_usage_snapshot."""
    session = SessionLocal()
    try:
        return {
            "sam_used_today": _get_usage(session, "sam_api"),
            "sam_pdf_downloads_today": _get_usage(session, "sam_pdf"),
            "screens_used_today": _get_ai_screen_usage(session),
        }
    finally:
        session.close()


def get_usage_snapshot() -> dict[str, Any]:
    from csv_attachment_policy import sam_attachments_csv_only_snapshot

    counts = _usage_counts()
    sam_used = counts["sam_used_today"]
    sam_pdf_used = counts["sam_pdf_downloads_today"]
    screen_used = counts["screens_used_today"]

    sam_limit = sam_daily_limit()
    sam_pdf_limit = sam_pdf_download_limit()
    screen_limit = screen_daily_limit()
    screens_unlimited = screen_limit == 0
    snapshot = {
        "sam_used_today": sam_used,
        "sam_daily_limit": sam_limit,
        "sam_remaining": max(0, sam_limit - sam_used),
        "sam_pdf_downloads_today": sam_pdf_used,
        "sam_pdf_download_limit": sam_pdf_limit,
        "sam_pdf_downloads_remaining": max(0, sam_pdf_limit - sam_pdf_used) if sam_pdf_limit else None,
        "screens_used_today": screen_used,
        "screen_daily_limit": screen_limit,
        "screens_unlimited": screens_unlimited,
        "screens_remaining": None if screens_unlimited else max(0, screen_limit - screen_used),
        "auto_screen_on_startup": auto_screen_on_startup(),
        "auto_screen_on_contract_detail": auto_screen_on_contract_detail(),
        "enrich_on_sync_limit": enrich_on_sync_limit(),
        "intake_on_sync": intake_on_sync_enabled(),
        # Computed after counts — must not re-enter get_usage_snapshot (RecursionError)
        "ai_intake_allowed": _ai_intake_allowed_from_counts(screen_used=screen_used, screen_limit=screen_limit),
        "claude_intake_allowed": False,  # set below to same value
        "intake_per_sync_limit": intake_per_sync_limit(),
        "scheduled_naics_per_sync": scheduled_naics_per_sync(),
        "attachment_enrich_per_sync_limit": attachment_enrich_per_sync_limit(),
        "attachment_enrich_on_list_limit": attachment_enrich_on_list_limit(),
        "scheduled_sync_attachments_only": scheduled_sync_attachments_only(),
        "scheduled_sync_attachments_only_from": (
            scheduled_sync_attachments_only_from().isoformat()
            if scheduled_sync_attachments_only_from()
            else None
        ),
        "scheduled_sync_attachments_only_until": (
            scheduled_sync_attachments_only_until().isoformat()
            if scheduled_sync_attachments_only_until()
            else None
        ),
    }
    snapshot["claude_intake_allowed"] = snapshot["ai_intake_allowed"]
    try:
        from ai_cost_budget import get_cost_snapshot

        snapshot["ai_cost"] = get_cost_snapshot()
    except Exception:
        snapshot["ai_cost"] = None
    snapshot.update(sam_attachments_csv_only_snapshot())
    return snapshot


def _ai_intake_allowed_from_counts(*, screen_used: int, screen_limit: int) -> bool:
    if not intake_on_sync_enabled():
        return False
    return _can_screen_from_counts(screen_used=screen_used, screen_limit=screen_limit)


def _can_screen_from_counts(*, screen_used: int, screen_limit: int) -> bool:
    """Legacy count budget AND dollar monthly budget must allow spend — no snapshot recursion."""
    if screen_limit != 0 and max(0, screen_limit - screen_used) <= 0:
        return False
    try:
        from ai_cost_budget import get_cost_snapshot

        cost = get_cost_snapshot()
        if float(cost.get("monthly_remaining_usd") or 0) <= 0:
            return False
    except Exception:
        pass
    return True


def can_spend_sam(credits: int = 1) -> bool:
    if credits <= 0:
        return True
    counts = _usage_counts()
    return max(0, sam_daily_limit() - counts["sam_used_today"]) >= credits


def can_download_screening_pdf() -> bool:
    limit = sam_pdf_download_limit()
    if limit == 0:
        return True
    counts = _usage_counts()
    remaining = max(0, limit - counts["sam_pdf_downloads_today"])
    return remaining > 0


def record_sam_pdf_download() -> bool:
    """Record one SAM.gov-hosted PDF download during AI screening."""
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
    """Legacy count budget AND dollar monthly budget must allow spend."""
    counts = _usage_counts()
    return _can_screen_from_counts(
        screen_used=counts["screens_used_today"],
        screen_limit=screen_daily_limit(),
    )

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
        used = _get_ai_screen_usage(session)
        limit = screen_daily_limit()
        if limit > 0 and used + 1 > limit:
            return False
        # Write only to the new key going forward.
        current_new = _get_usage(session, "ai_screen")
        _set_usage(session, "ai_screen", current_new + 1)
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
            f"Daily AI screening budget exhausted "
            f"({snap['screens_used_today']}/{snap['screen_daily_limit']} used today)."
        )
        super().__init__(detail)


def require_sam_budget(credits: int = 1) -> None:
    if not can_spend_sam(credits):
        raise SamBudgetExceeded()


def require_screen_budget() -> None:
    if not can_screen():
        raise ScreenBudgetExceeded()
