"""Планировщик обходов (APScheduler, без Celery/Redis — см. CLAUDE.md).

Плановый обход — один общий цикл раз в интервал (ScheduleConfig, по умолчанию
неделя, меняется в UI и в боте): сначала наш сайт, до конца, и только потом все
конкуренты. Порядок важен: находки конкурентов сравниваются с нашим сайтом («есть
ли у нас такое»), и сравнивать нужно со свежей его версией. Раньше у каждого сайта
был свой независимый таймер, и конкурент мог обойтись раньше нашего сайта.

Когда будет следующий цикл, хранится в БД (ScheduleConfig.next_run_at), а не
только в памяти планировщика: иначе каждый перезапуск сервиса отодвигал бы обход
на полный интервал, и при перезапусках чаще раза в неделю он не наступал никогда.

Сам запуск обходов планировщик не делает: он идёт через app/crawl_manager.py —
там и защита от повторного запуска, и общий лимит одновременных обходов.
"""

import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import crawl_manager
from app.config import settings
from app.db import SessionLocal
from app.models import Competitor, ScheduleConfig
from app.own_site import get_own_site

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

CYCLE_JOB_ID = "crawl_cycle"
DIGEST_JOB_ID = "weekly_digest"

# Просроченный цикл (Мак спал, сервис лежал) запускается вскоре после старта —
# не в первую же секунду, а когда сервис поднялся целиком.
_OVERDUE_DELAY = timedelta(minutes=2)


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


def _aware(value: datetime | None) -> datetime | None:
    # Postgres отдаёт даты с часовым поясом, SQLite (тесты) — без; считаем такие UTC.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def plan_next_run(saved: datetime | None, interval: timedelta, now: datetime, *, reset: bool) -> datetime:
    """Когда запускать следующий цикл.

    reset=False (старт сервиса): сохранённое время уважаем, просроченное — вскоре
    после старта. reset=True (сменили интервал) или времени ещё нет — через
    интервал от сейчас.
    """
    saved = _aware(saved)
    if reset or saved is None:
        return now + interval
    if saved <= now + _OVERDUE_DELAY:
        return now + _OVERDUE_DELAY
    return saved


def _show_next_run(db, next_run: datetime) -> None:
    """next_crawl_at у сайтов — только для показа в UI и боте («следующий обход»):
    у всех, кто не на паузе, это время ближайшего цикла."""
    for competitor in db.query(Competitor).filter(Competitor.is_paused.is_(False)).all():
        competitor.next_crawl_at = None if competitor.cookies_only else next_run
        db.add(competitor)


async def run_crawl_cycle() -> None:
    """Плановый цикл: наш сайт целиком, затем все конкуренты (кроме тех, что на паузе)."""
    db = SessionLocal()
    try:
        own = get_own_site(db)
        if own is not None and not own.is_paused:
            logger.info("Плановый цикл: обходим наш сайт")
            if not await crawl_manager.run_now(db, own):
                # Обход нашего сайта уже идёт (запущен вручную) — ждать его не будем:
                # конкурентов сравним с тем, что уже сохранено.
                logger.info("Плановый цикл: обход нашего сайта уже идёт — сразу к конкурентам")
            db.expire_all()

        result = crawl_manager.start_all(db)
        logger.info(
            "Плановый цикл: конкуренты — запущено %s, уже шли %s, на паузе %s",
            result.started,
            result.skipped_running,
            result.skipped_paused,
        )

        job = scheduler.get_job(CYCLE_JOB_ID)
        config = get_schedule_config(db)
        next_run = job.next_run_time if job is not None else datetime.now(UTC) + interval_timedelta(config)
        config.next_run_at = next_run
        db.add(config)
        _show_next_run(db, next_run)
        db.commit()
    except Exception:
        logger.exception("Плановый цикл обходов завершился ошибкой")
    finally:
        db.close()


def schedule_cycle(db, *, reset: bool = False) -> datetime:
    """Ставит (или переставляет) плановый цикл. Возвращает время ближайшего запуска."""
    config = get_schedule_config(db)
    interval = interval_timedelta(config)
    next_run = plan_next_run(config.next_run_at, interval, datetime.now(UTC), reset=reset)

    scheduler.add_job(
        run_crawl_cycle,
        trigger=IntervalTrigger(days=config.interval_days, hours=config.interval_hours, start_date=next_run),
        id=CYCLE_JOB_ID,
        replace_existing=True,
        # Мак спал в момент срабатывания — обойти, как только проснулся, а не
        # молча ждать ещё интервал.
        misfire_grace_time=None,
        coalesce=True,
        max_instances=1,
    )

    config.next_run_at = next_run
    db.add(config)
    _show_next_run(db, next_run)
    db.commit()
    return next_run


def sync_next_run(db, competitor: Competitor) -> None:
    """Новый или снятый с паузы сайт попадает в ближайший общий цикл — отдельного
    таймера у него нет. Проставляем время цикла, чтобы UI и бот его показывали."""
    competitor.next_crawl_at = _aware(get_schedule_config(db).next_run_at)
    db.add(competitor)
    db.commit()


def set_interval(db, days: int, hours: int) -> ScheduleConfig:
    """Меняет интервал плановых обходов — и из веба, и из бота.

    ValueError — интервал нулевой или отрицательный.
    """
    if days < 0 or hours < 0 or (days == 0 and hours == 0):
        raise ValueError("Интервал должен быть больше нуля")

    config = get_schedule_config(db)
    config.interval_days = days
    config.interval_hours = hours
    db.add(config)
    db.commit()

    schedule_cycle(db, reset=True)
    db.refresh(config)
    return config


async def _run_digest() -> None:
    # Импорт здесь, а не наверху: app.digest тянет bot_views, а тот — этот модуль.
    from app.digest import send_weekly_digest

    await send_weekly_digest()


def schedule_digest() -> None:
    if not settings.digest_enabled:
        return
    scheduler.add_job(
        _run_digest,
        trigger=CronTrigger(day_of_week=settings.digest_day_of_week, hour=settings.digest_hour),
        id=DIGEST_JOB_ID,
        replace_existing=True,
    )


def start_scheduler() -> None:
    db = SessionLocal()
    try:
        next_run = schedule_cycle(db)
    finally:
        db.close()
    schedule_digest()
    scheduler.start()
    logger.info("Планировщик запущен, ближайший плановый цикл обходов: %s", next_run.isoformat())
