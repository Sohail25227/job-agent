"""Daily scheduled runs.

Keep this process running (or point launchd/cron at ``cli.py run``) and the
agent works the job market while you work your job.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Config, load_config
from .db import init_db
from .fetch import crawl

log = logging.getLogger(__name__)


def start(config: Config | None = None) -> None:
    """Keep this process running to warm the cache on a schedule.

    Only useful where a process can stay alive. On a host whose free tier stops
    the container when idle, run ``cli.py crawl`` from an external scheduler
    instead - see the GitHub Actions workflow.
    """
    config = config or load_config()
    init_db(config)
    scheduler = BlockingScheduler(timezone=config.schedule.timezone)
    trigger = CronTrigger.from_crontab(config.schedule.cron, timezone=config.schedule.timezone)

    def job() -> None:
        log.info("scheduled crawl starting")
        try:
            stats = crawl(config)
            log.info("scheduled crawl finished: %s", stats.as_dict())
        except Exception as exc:
            log.error("scheduled crawl failed: %s", exc, exc_info=True)

    scheduler.add_job(job, trigger, id="daily", max_instances=1, coalesce=True,
                      misfire_grace_time=3600)
    now = datetime.now(ZoneInfo(config.schedule.timezone))
    log.info(
        "scheduler started | cron=%r timezone=%s | next run: %s",
        config.schedule.cron, config.schedule.timezone, trigger.get_next_fire_time(None, now),
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler stopped")
