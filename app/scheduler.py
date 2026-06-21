import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app.config import settings

logger = logging.getLogger("marea.scheduler")

scheduler = BackgroundScheduler(timezone="UTC")


def setup_scheduler() -> BackgroundScheduler:
    from app.ingest.run_all import IngestAll

    ingestor = IngestAll()

    scheduler.add_job(
        ingestor.run_sync,
        trigger=CronTrigger(
            hour=settings.ingest_cron_hour,
            minute=settings.ingest_cron_minute,
            timezone="UTC",
        ),
        id="daily_ingest_all",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info(
        "Job diario registrado: %02d:%02d UTC (yfinance + crypto + on-chain)",
        settings.ingest_cron_hour,
        settings.ingest_cron_minute,
    )
    return scheduler
