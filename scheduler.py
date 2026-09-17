"""Tiered SAM.gov sync scheduler + M3 incremental discovery cadence."""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

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
            "Scheduled sync done: mode=%s, %s, usaspending_saved=%s",
            result.get("mode"),
            result.get("fetch_status"),
            result.get("usaspending_calls_saved", 0),
        )
    except Exception:
        logger.exception("Scheduled daily sync failed")
        return

    try:
        from database import SessionLocal
        from expired_purge_service import purge_expired_unpursued

        session = SessionLocal()
        try:
            purge = purge_expired_unpursued(session)
            if purge.get("contracts_removed") or purge.get("csv_removed"):
                session.commit()
                logger.info(
                    "Expired purge: contracts=%s csv=%s",
                    purge.get("contracts_removed"),
                    purge.get("csv_removed"),
                )
        finally:
            session.close()
    except Exception:
        logger.exception("Expired unpursued purge failed")

    from autopilot_service import run_scheduled_autopilot
    from pricing_backfill_service import start_background_pricing_backfill

    run_scheduled_autopilot()
    start_background_pricing_backfill()

    try:
        from csv_attachment_queue_service import run_scheduled_csv_attachment_queue

        queue_result = run_scheduled_csv_attachment_queue()
        if not queue_result.get("skipped"):
            logger.info(
                "CSV attachment queue: completed=%s failed=%s waiting_for_budget=%s",
                queue_result.get("completed"),
                queue_result.get("failed"),
                queue_result.get("waiting_for_budget"),
            )
    except Exception:
        logger.exception("Scheduled CSV attachment queue failed")


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


def configure_m3_discovery_job() -> None:
    """Interval discovery inside the web process — no separate Railway worker required."""
    from m3_discovery_service import discovery_enabled, discovery_interval_minutes, scheduled_discovery_tick

    if not discovery_enabled():
        if scheduler.running:
            job = scheduler.get_job("m3_incremental_discovery")
            if job:
                scheduler.remove_job("m3_incremental_discovery")
        logger.info("M3 discovery scheduler disabled (M3_DISCOVERY_ENABLED=false)")
        return

    minutes = discovery_interval_minutes()
    trigger = IntervalTrigger(minutes=minutes)
    if not scheduler.running:
        scheduler.start()
    scheduler.add_job(
        scheduled_discovery_tick,
        trigger,
        id="m3_incremental_discovery",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("M3 discovery scheduler: every %s minutes (server-side)", minutes)


def configure_m3_research_job() -> None:
    """Interval research queue drain — reuses M3EndToEndOrchestrator.advance."""
    from m3_research_service import research_enabled, research_interval_minutes, scheduled_research_tick

    if not research_enabled():
        if scheduler.running:
            job = scheduler.get_job("m3_research_queue")
            if job:
                scheduler.remove_job("m3_research_queue")
        logger.info("M3 research scheduler disabled (M3_RESEARCH_ENABLED=false)")
        return

    minutes = research_interval_minutes()
    trigger = IntervalTrigger(minutes=minutes)
    if not scheduler.running:
        scheduler.start()
    scheduler.add_job(
        scheduled_research_tick,
        trigger,
        id="m3_research_queue",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("M3 research scheduler: every %s minutes (server-side)", minutes)


def configure_scheduler() -> None:
    from settings_store import get_scheduler_settings

    settings = get_scheduler_settings()
    if not settings["enabled"]:
        if scheduler.running:
            for job_id in ("daily_sam_sync", "amendment_monitor"):
                job = scheduler.get_job(job_id)
                if job:
                    scheduler.remove_job(job_id)
        logger.info("Legacy SAM scheduler disabled in settings")
        # M3 discovery/research still run independently of legacy SAM sync toggle
        configure_m3_discovery_job()
        configure_m3_research_job()
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

    configure_m3_discovery_job()
    configure_m3_research_job()

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
    m3_job = scheduler.get_job("m3_incremental_discovery") if scheduler.running else None
    m3_research_job = scheduler.get_job("m3_research_queue") if scheduler.running else None
    m3_info = {}
    try:
        from m3_discovery_service import discovery_enabled, discovery_interval_minutes, discovery_status

        m3_info = {
            "m3_discovery_enabled": discovery_enabled(),
            "m3_discovery_interval_minutes": discovery_interval_minutes(),
            "m3_discovery_next_run": m3_job.next_run_time.isoformat() if m3_job and m3_job.next_run_time else None,
            "m3_discovery_status": discovery_status().get("status"),
        }
    except Exception:
        m3_info = {"m3_discovery_enabled": False}
    try:
        from m3_research_service import research_enabled, research_interval_minutes, research_status

        m3_info.update(
            {
                "m3_research_enabled": research_enabled(),
                "m3_research_interval_minutes": research_interval_minutes(),
                "m3_research_next_run": (
                    m3_research_job.next_run_time.isoformat()
                    if m3_research_job and m3_research_job.next_run_time
                    else None
                ),
                "m3_research_status": research_status().get("status"),
            }
        )
    except Exception:
        m3_info["m3_research_enabled"] = False

    if not settings["enabled"]:
        return {"enabled": False, "running": scheduler.running, **settings, **m3_info}

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
        **m3_info,
    }
