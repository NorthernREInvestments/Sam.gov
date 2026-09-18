"""Tiered SAM.gov sync scheduler + M3 incremental discovery cadence."""

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
    """Discovery at 06:00 and 14:00 in configured timezone — not hourly."""
    from m3_discovery_service import discovery_enabled, scheduled_discovery_tick
    from settings_store import get_scheduler_settings

    if not discovery_enabled():
        if scheduler.running:
            job = scheduler.get_job("m3_incremental_discovery")
            if job:
                scheduler.remove_job("m3_incremental_discovery")
        logger.info("M3 discovery scheduler disabled (M3_DISCOVERY_ENABLED=false)")
        return

    settings = get_scheduler_settings()
    tz = _timezone(settings["timezone"])
    # Explicit dual daily runs — replaces previous hourly IntervalTrigger
    trigger = CronTrigger(hour="6,14", minute=0, timezone=tz)
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
    logger.info("M3 discovery scheduler: 06:00 and 14:00 %s (hourly disabled)", tz)


def configure_m3_research_job() -> None:
    """Portfolio deal analysis at 06:05 and 14:05 — progressive research, not equal-depth."""
    from settings_store import get_scheduler_settings

    try:
        from m3_research_service import research_enabled
    except Exception:
        research_enabled = lambda: True  # noqa: E731

    # Remove legacy interval research queue if present
    if scheduler.running:
        legacy = scheduler.get_job("m3_research_queue")
        if legacy:
            scheduler.remove_job("m3_research_queue")

    if not research_enabled():
        if scheduler.running:
            job = scheduler.get_job("m3_portfolio_cycle")
            if job:
                scheduler.remove_job("m3_portfolio_cycle")
        logger.info("M3 portfolio/research scheduler disabled")
        return

    settings = get_scheduler_settings()
    tz = _timezone(settings["timezone"])
    trigger = CronTrigger(hour="6,14", minute=5, timezone=tz)

    def _portfolio_tick() -> None:
        try:
            from m3_portfolio_deal_analysis import scheduled_portfolio_tick

            result = scheduled_portfolio_tick()
            logger.info(
                "M3 portfolio cycle: promotions=%s cvw=%s ftx=%s",
                (result.get("SUMMARY") or {}).get("Research_promotions"),
                (result.get("AFTER") or {}).get("commercial_verification_worthy"),
                (result.get("AFTER") or {}).get("first_transaction_candidates"),
            )
        except Exception:
            logger.exception("M3 portfolio cycle failed")

    if not scheduler.running:
        scheduler.start()
    scheduler.add_job(
        _portfolio_tick,
        trigger,
        id="m3_portfolio_cycle",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("M3 portfolio scheduler: 06:05 and 14:05 %s (hourly research disabled)", tz)


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
    m3_research_job = scheduler.get_job("m3_portfolio_cycle") if scheduler.running else None
    m3_info = {}
    try:
        from m3_discovery_service import discovery_enabled, discovery_status

        m3_info = {
            "m3_discovery_enabled": discovery_enabled(),
            "m3_discovery_schedule": "06:00,14:00",
            "m3_discovery_interval_minutes": None,
            "m3_hourly_schedule_disabled": True,
            "m3_discovery_next_run": m3_job.next_run_time.isoformat() if m3_job and m3_job.next_run_time else None,
            "m3_discovery_status": discovery_status().get("status"),
        }
    except Exception:
        m3_info = {"m3_discovery_enabled": False}
    try:
        from m3_research_service import research_enabled

        m3_info.update(
            {
                "m3_research_enabled": research_enabled(),
                "m3_portfolio_schedule": "06:05,14:05",
                "m3_research_interval_minutes": None,
                "m3_research_next_run": (
                    m3_research_job.next_run_time.isoformat()
                    if m3_research_job and m3_research_job.next_run_time
                    else None
                ),
                "m3_portfolio_job": "m3_portfolio_cycle",
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
