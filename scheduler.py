"""Tiered SAM.gov sync scheduler."""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger("govtracker.scheduler")

scheduler = BackgroundScheduler()


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("America/Denver")


def run_daily_sync() -> None:
    from sync import sync_scheduled_naics

    logger.info("Starting scheduled tiered SAM.gov sync")
    try:
        result = sync_scheduled_naics()
        logger.info(
            "Scheduled sync done: mode=%s, %s",
            result.get("mode"),
            result.get("fetch_status"),
        )
    except Exception:
        logger.exception("Scheduled daily sync failed")


def run_amendment_check() -> None:
    from database import SessionLocal
    from amendment_monitor import check_all_amendments

    session = SessionLocal()
    try:
        result = check_all_amendments(session)
        logger.info("Amendment check: %s", result)
    except Exception:
        logger.exception("Amendment check failed")
    finally:
        session.close()


def configure_scheduler() -> None:
    from settings_store import get_scheduler_settings

    settings = get_scheduler_settings()
    if not settings["enabled"]:
        if scheduler.running:
            for job_id in ("daily_sam_sync", "amendment_monitor"):
                job = scheduler.get_job(job_id)
                if job:
                    scheduler.remove_job(job_id)
        logger.info("Scheduler disabled in settings")
        return

    hour = settings["hour"]
    minute = settings["minute"]
    tz = _timezone(settings["timezone"])
    trigger = CronTrigger(hour=hour, minute=minute, timezone=tz)

    if scheduler.running:
        job = scheduler.get_job("daily_sam_sync")
        if job:
            scheduler.reschedule_job("daily_sam_sync", trigger=trigger)
        else:
            scheduler.add_job(
                run_daily_sync,
                trigger,
                id="daily_sam_sync",
                replace_existing=True,
            )
        scheduler.add_job(
            run_amendment_check,
            CronTrigger(hour="*/6", timezone=tz),
            id="amendment_monitor",
            replace_existing=True,
        )
    else:
        scheduler.add_job(
            run_daily_sync,
            trigger,
            id="daily_sam_sync",
            replace_existing=True,
        )
        scheduler.add_job(
            run_amendment_check,
            CronTrigger(hour="*/6", timezone=tz),
            id="amendment_monitor",
            replace_existing=True,
        )
        scheduler.start()

    logger.info(
        "Scheduler configured: tiered sync at %02d:%02d %s (T1 daily, T2 Mon/Wed/Fri, T3 Sun)",
        hour,
        minute,
        tz,
    )


def start_scheduler() -> None:
    configure_scheduler()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def scheduler_status() -> dict:
    from naics_labels import tiers_for_scheduled_sync
    from settings_store import get_naics_codes_for_tiers, get_scheduler_settings

    settings = get_scheduler_settings()
    if not settings["enabled"]:
        return {"enabled": False, "running": False, **settings}

    scheduled_tiers = tiers_for_scheduled_sync()
    scheduled_pool = get_naics_codes_for_tiers(scheduled_tiers)
    from api_budget import scheduled_naics_per_sync

    job = scheduler.get_job("daily_sam_sync") if scheduler.running else None
    return {
        "enabled": True,
        "running": scheduler.running,
        "hour": settings["hour"],
        "minute": settings["minute"],
        "timezone": settings["timezone"],
        "next_run": job.next_run_time.isoformat() if job and job.next_run_time else None,
        "scheduled_tiers": scheduled_tiers,
        "scheduled_pool_size": len(scheduled_pool),
        "scheduled_per_sync": scheduled_naics_per_sync(),
        "tier_schedule": "Tier 1 daily · Tier 2 Mon/Wed/Fri · Tier 3 Sunday · rotates a few codes per run",
    }
