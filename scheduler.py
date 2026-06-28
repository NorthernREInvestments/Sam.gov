"""Daily SAM.gov sync scheduler."""

from __future__ import annotations

import logging
import os
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger("govtracker.scheduler")

scheduler = BackgroundScheduler()


def _enabled() -> bool:
    return os.getenv("SCHEDULER_ENABLED", "true").strip().lower() not in ("0", "false", "no")


def _timezone() -> ZoneInfo:
    name = os.getenv("SCHEDULER_TIMEZONE", "America/Denver").strip()
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("America/Denver")


def run_daily_sync() -> None:
    from sync import sync_all_naics

    logger.info("Starting scheduled daily SAM.gov sync")
    try:
        result = sync_all_naics()
        logger.info(
            "Scheduled sync done: %s API calls, %s new, %s total in DB",
            result["api_calls"],
            result["new"],
            result["total_in_db"],
        )
    except Exception:
        logger.exception("Scheduled daily sync failed")


def start_scheduler() -> None:
    if not _enabled():
        logger.info("Scheduler disabled via SCHEDULER_ENABLED")
        return
    if scheduler.running:
        return

    hour = int(os.getenv("DAILY_REFRESH_HOUR", "6"))
    minute = int(os.getenv("DAILY_REFRESH_MINUTE", "0"))
    tz = _timezone()

    scheduler.add_job(
        run_daily_sync,
        CronTrigger(hour=hour, minute=minute, timezone=tz),
        id="daily_sam_sync",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started: daily sync at %02d:%02d %s", hour, minute, tz)


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def scheduler_status() -> dict:
    if not _enabled():
        return {"enabled": False, "running": False}

    job = scheduler.get_job("daily_sam_sync")
    return {
        "enabled": True,
        "running": scheduler.running,
        "hour": int(os.getenv("DAILY_REFRESH_HOUR", "6")),
        "minute": int(os.getenv("DAILY_REFRESH_MINUTE", "0")),
        "timezone": str(_timezone()),
        "next_run": job.next_run_time.isoformat() if job and job.next_run_time else None,
    }
