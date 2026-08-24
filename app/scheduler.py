"""Планировщик обхода конкурентов (APScheduler, без Celery/Redis — см. CLAUDE.md).

Расписание общее для всех конкурентов (ScheduleConfig, редактируется через UI),
но каждый конкурент обходится своей независимой job — APScheduler запускает их
как отдельные asyncio-задачи, поэтому конкуренты обходятся параллельно, а не
один за другим в общем цикле.
"""

import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.db import SessionLocal
from app.models import Competitor, ScheduleConfig
from app.pipeline import run_crawl_for_competitor

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

_JOB_PREFIX = "crawl_competitor_"


def _job_id(competitor_id: int) -> str:
    return f"{_JOB_PREFIX}{competitor_id}"


def get_schedule_config(db) -> ScheduleConfig:
    config = db.get(ScheduleConfig, 1)
    if config is None:
        config = ScheduleConfig(id=1, interval_days=7, interval_hours=0)
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def interval_timedelta(config: ScheduleConfig) -> timedelta:
    return timedelta(days=config.interval_days, hours=config.interval_hours)


async def _run_scheduled_crawl(competitor_id: int) -> None:
    db = SessionLocal()
    try:
        competitor = db.get(Competitor, competitor_id)
        if competitor is None:
            return
        if competitor.is_crawling:
            logger.info(
                "Плановый обход конкурента %s пропущен — уже выполняется другой обход", competitor.name
            )
            return
        try:
            await run_crawl_for_competitor(db, competitor)
        except Exception:
            db.rollback()
            logger.exception("Плановый обход конкурента %s завершился ошибкой", competitor.name)
        finally:
            config = get_schedule_config(db)
            competitor.next_crawl_at = datetime.now(UTC) + interval_timedelta(config)
            db.add(competitor)
            db.commit()
    finally:
        db.close()


def schedule_competitor(competitor_id: int, config: ScheduleConfig) -> None:
    scheduler.add_job(
        _run_scheduled_crawl,
        trigger=IntervalTrigger(days=config.interval_days, hours=config.interval_hours),
        id=_job_id(competitor_id),
        args=[competitor_id],
        replace_existing=True,
    )


def schedule_competitor_and_save_next_run(db, competitor: Competitor, config: ScheduleConfig) -> None:
    """schedule_competitor + сразу проставляет next_crawl_at, чтобы UI не ждал первого тика."""
    schedule_competitor(competitor.id, config)
    competitor.next_crawl_at = datetime.now(UTC) + interval_timedelta(config)
    db.add(competitor)
    db.commit()


def unschedule_competitor(competitor_id: int) -> None:
    job_id = _job_id(competitor_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


def reschedule_all(db) -> None:
    """Перечитывает ScheduleConfig и пересоздаёт job'ы всех активных конкурентов.

    Вызывается при старте приложения и при изменении расписания через UI.
    """
    config = get_schedule_config(db)
    interval = interval_timedelta(config)
    now = datetime.now(UTC)

    competitors = db.query(Competitor).filter(Competitor.is_paused.is_(False)).all()
    for competitor in competitors:
        schedule_competitor(competitor.id, config)
        competitor.next_crawl_at = now + interval
        db.add(competitor)
    db.commit()


def start_scheduler() -> None:
    db = SessionLocal()
    try:
        reschedule_all(db)
    finally:
        db.close()
    scheduler.start()
    logger.info("Планировщик запущен")
