"""Планировщик обходов (APScheduler, без Celery/Redis — см. CLAUDE.md).

Плановый обход — один общий цикл раз в интервал (ScheduleConfig, по умолчанию
неделя, меняется в UI и в боте): сначала наш сайт, до конца, и только потом все
конкуренты. Порядок важен: находки конкурентов сравниваются с нашим сайтом («есть
ли у нас такое»), и сравнивать нужно со свежей его версией. Раньше у каждого сайта
был свой независимый таймер, и конкурент мог обойтись раньше нашего сайта.

Когда будет следующий цикл, хранится в БД (ScheduleConfig.next_run_at), а не
только в памяти планировщика: иначе каждый перезапуск сервиса отодвигал бы обход
на полный интервал, и при перезапусках чаще раза в неделю он не наступал никогда.

Если наш сайт не открылся (нет интернета, сайт лёг), его обход повторяется через
OWN_SITE_RETRY_MINUTES, до OWN_SITE_RETRY_ATTEMPTS раз; конкуренты в плановом цикле
ждут удачного обхода нашего сайта, а если не помог ни один повтор — обходятся со
старым снимком. Раньше такой обход молча заканчивался «изменений нет» (2026-10-07/08).

Сам запуск обходов планировщик не делает: он идёт через app/crawl_manager.py —
там и защита от повторного запуска, и общий лимит одновременных обходов.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import crawl_manager
from app.config import settings
from app.db import SessionLocal
from app.models import Competitor, ScheduleConfig
from app.notifications.telegram import TelegramNotifier, format_unavailable_message
from app.own_site import get_own_site

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

CYCLE_JOB_ID = "crawl_cycle"
OWN_SITE_DAILY_JOB_ID = "own_site_daily"
DIGEST_JOB_ID = "weekly_digest"
CYCLE_RETRY_JOB_ID = "crawl_cycle_retry"
OWN_SITE_DAILY_RETRY_JOB_ID = "own_site_daily_retry"

_MOSCOW = ZoneInfo("Europe/Moscow")

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


async def _tell_owner(text: str) -> None:
    try:
        await TelegramNotifier().send(text)
    except Exception:
        logger.exception("Не удалось отправить уведомление в Telegram")


def _schedule_retry(job_id: str, func, attempt: int) -> datetime:
    """Разовый повтор через OWN_SITE_RETRY_MINUTES. Живёт только в памяти: при
    перезапуске сервиса повтор пропадёт — но плановый цикл в этом случае и так
    запустится заново (его время в БД не сдвигается, пока цикл не дошёл до конкурентов)."""
    run_at = datetime.now(UTC) + timedelta(minutes=settings.own_site_retry_minutes)
    scheduler.add_job(
        func,
        trigger=DateTrigger(run_date=run_at),
        kwargs={"attempt": attempt},
        id=job_id,
        replace_existing=True,
        misfire_grace_time=None,
    )
    return run_at


def _retry_note(run_at: datetime, then: str) -> str:
    return f"Попробую ещё раз в {run_at.astimezone(_MOSCOW):%H:%M} по Москве{then}."


async def _crawl_own_site(
    db, own: Competitor, *, attempt: int, retry_job_id: str, retry_func, then: str
) -> bool | None:
    """Обходит наш сайт.

    True — обход прошёл (или уже шёл); False — сайт не открылся, повтор назначен и
    владелец об этом знает; None — сайт не открылся, а повторы кончились."""
    result = await crawl_manager.run_now(db, own, quiet=True)
    if result is None:
        logger.info("Обход нашего сайта уже идёт — новый не запускаем")
        return True
    if result:
        return True

    if attempt < settings.own_site_retry_attempts:
        run_at = _schedule_retry(retry_job_id, retry_func, attempt + 1)
        logger.warning("Наш сайт не открылся, повтор %s в %s", attempt + 1, run_at.isoformat())
        await _tell_owner(format_unavailable_message(own.name, own.base_url, retry_note=_retry_note(run_at, then)))
        return False

    logger.warning("Наш сайт не открылся и после %s повторов", attempt)
    return None


async def run_crawl_cycle(attempt: int = 0) -> None:
    """Плановый цикл: наш сайт целиком, затем все конкуренты (кроме тех, что на паузе).

    attempt — номер повтора, если наш сайт в прошлый раз не открылся."""
    db = SessionLocal()
    try:
        own = get_own_site(db)
        if own is not None and not own.is_paused:
            logger.info("Плановый цикл: обходим наш сайт%s", f" (повтор {attempt})" if attempt else "")
            own_ok = await _crawl_own_site(
                db,
                own,
                attempt=attempt,
                retry_job_id=CYCLE_RETRY_JOB_ID,
                retry_func=run_crawl_cycle,
                then=", конкурентов обойду после него",
            )
            if own_ok is False:
                # Время цикла в БД не трогаем: если сервис перезапустят до повтора,
                # просроченный цикл запустится заново, и конкуренты не потеряются.
                return
            if own_ok is None:
                await _tell_owner(
                    format_unavailable_message(
                        own.name,
                        own.base_url,
                        retry_note=(
                            f"Повторы ({attempt}) не помогли. Конкурентов обхожу сейчас, а сравниваю "
                            "с прошлым снимком нашего сайта."
                        ),
                    )
                )
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


async def run_own_site_daily(attempt: int = 0) -> None:
    """Ежедневный обход нашего сайта — только новые и изменившиеся страницы (так
    устроен любой не полный обход, см. plan_crawl). Нужен, чтобы находки конкурента
    сравнивались с актуальным нашим сайтом, даже если обход конкурента запустили
    вручную посреди недели. Пишет в Telegram, только если на сайте что-то поменялось
    или сайт не открылся."""
    db = SessionLocal()
    try:
        own = get_own_site(db)
        if own is None or own.is_paused:
            return
        logger.info("Ежедневный обход нашего сайта%s", f" (повтор {attempt})" if attempt else "")
        own_ok = await _crawl_own_site(
            db,
            own,
            attempt=attempt,
            retry_job_id=OWN_SITE_DAILY_RETRY_JOB_ID,
            retry_func=run_own_site_daily,
            then="",
        )
        if own_ok is None:
            await _tell_owner(
                format_unavailable_message(
                    own.name,
                    own.base_url,
                    retry_note=f"Повторы ({attempt}) не помогли. Следующая попытка — в обычное время завтра.",
                )
            )
    except Exception:
        logger.exception("Ежедневный обход нашего сайта завершился ошибкой")
    finally:
        db.close()


def schedule_own_site_daily() -> None:
    if not settings.own_site_daily_enabled:
        return
    scheduler.add_job(
        run_own_site_daily,
        trigger=CronTrigger(hour=settings.own_site_daily_hour, timezone=settings.own_site_daily_timezone),
        id=OWN_SITE_DAILY_JOB_ID,
        replace_existing=True,
        # Мак спал в назначенный час — обойти, как только проснулся.
        misfire_grace_time=None,
        coalesce=True,
        max_instances=1,
    )


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
        trigger=CronTrigger(
            day_of_week=settings.digest_day_of_week, hour=settings.digest_hour, timezone=settings.digest_timezone
        ),
        id=DIGEST_JOB_ID,
        replace_existing=True,
    )


SEO_AUDIT_JOB_ID = "seo_audit_nightly"


async def run_seo_audit_nightly() -> None:
    """Стартовый аудит статей конкурентов порциями, ночью по Москве (app/seo_audit.py).

    Пишет в Telegram, только если что-то разобрал. Когда очередь пуста — молчит:
    новые статьи разбирает уже сам обход.
    """
    from app.integrations.seo_sheet import export_audit
    from app.seo_audit import format_nightly_report, nightly_competitors, remaining_articles, run_audit

    db = SessionLocal()
    try:
        competitors = nightly_competitors(db)
        if not remaining_articles(db, competitors):
            return
        stats = await run_audit(
            db,
            competitors,
            max_calls=settings.seo_audit_nightly_max_calls,
            night=(settings.seo_audit_night_start_hour, settings.seo_audit_night_end_hour),
            stop_outside_night=True,
            on_progress=logger.info,
        )
        if not stats.processed:
            return
        try:
            sheet_url = await asyncio.to_thread(export_audit, db)
            db.commit()
        except Exception:
            logger.exception("Не удалось выгрузить лист «Стартовый аудит»")
            sheet_url = None
        await _tell_owner(format_nightly_report(stats, remaining_articles(db, competitors), sheet_url))
    except Exception:
        logger.exception("Ночной SEO-аудит завершился ошибкой")
    finally:
        db.close()


def schedule_seo_audit() -> None:
    if not settings.seo_audit_nightly_enabled or not settings.seo_enabled:
        return
    scheduler.add_job(
        run_seo_audit_nightly,
        trigger=CronTrigger(hour=settings.seo_audit_night_start_hour, timezone=settings.digest_timezone),
        id=SEO_AUDIT_JOB_ID,
        replace_existing=True,
        # Мак спал в час ночи — догоним, если проснулся ещё до конца окна (окно
        # проверяет сам аудит), иначе просто следующей ночью.
        misfire_grace_time=None,
        coalesce=True,
        max_instances=1,
    )


def start_scheduler() -> None:
    db = SessionLocal()
    try:
        next_run = schedule_cycle(db)
    finally:
        db.close()
    schedule_digest()
    schedule_own_site_daily()
    schedule_seo_audit()
    scheduler.start()
    logger.info("Планировщик запущен, ближайший плановый цикл обходов: %s", next_run.isoformat())
