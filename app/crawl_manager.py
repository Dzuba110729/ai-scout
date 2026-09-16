"""Единственная точка запуска обходов: и планировщик, и кнопка в интерфейсе идут сюда.

Раньше логика была продублирована в scheduler.py и routers/competitors.py, и две
копии успели разойтись. Здесь она одна и держит две выстраданные гарантии:

1. Отметка «обход идёт» ставится СИНХРОННО, до постановки фоновой задачи. Если
   ставить её внутри фоновой задачи, быстрый повторный клик по кнопке успевает
   проскочить проверку — задача-то ещё не стартовала — и обход уходит дважды.
2. Отметка снимается при ЛЮБОМ исходе, включая падение до начала обхода и отмену
   задачи. Иначе конкурент навсегда залипает в статусе «обход идёт», и все
   следующие запуски отбиваются как дубли.

Плюс глобальный лимит одновременных обходов (CRAWL_CONCURRENCY): без него десять
конкурентов поднимут десять браузеров разом и выест всю память сервера.
"""

import asyncio
import logging
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import Competitor
from app.pipeline import run_crawl_for_competitor

logger = logging.getLogger(__name__)

# Ссылки на живые фоновые задачи: без сильной ссылки сборщик мусора может убить
# задачу на полуслове (asyncio держит на неё только слабую ссылку).
_background_tasks: set[asyncio.Task] = set()

# competitor_id -> его текущая задача обхода (ручная, плановая или через "обойти всех").
# Нужна для stop() — без неё отменить конкретный обход снаружи нечем.
_tasks_by_competitor: dict[int, asyncio.Task] = {}

_semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple[int, asyncio.Semaphore]]" = (
    weakref.WeakKeyDictionary()
)


@dataclass(frozen=True)
class CrawlAllResult:
    started: int
    skipped_running: int
    skipped_paused: int


def _semaphore() -> asyncio.Semaphore:
    """Семафор привязан к текущему event loop.

    Примитивы asyncio нельзя переносить между циклами — модульная константа
    сломалась бы в тестах, где у каждого теста свой loop. Заодно перечитываем
    лимит, если его поменяли в настройках.
    """
    loop = asyncio.get_running_loop()
    limit = max(1, settings.crawl_concurrency)
    cached = _semaphores.get(loop)
    if cached is None or cached[0] != limit:
        cached = (limit, asyncio.Semaphore(limit))
        _semaphores[loop] = cached
    return cached[1]


def claim(db: Session, competitor: Competitor) -> bool:
    """Синхронно занимает конкурента под обход. False — обход уже идёт."""
    if competitor.is_crawling:
        return False

    competitor.last_crawl_started_at = datetime.now(UTC)
    competitor.last_crawl_finished_at = None
    db.add(competitor)
    db.commit()
    return True


def _release_if_still_crawling(db: Session, competitor_id: int) -> None:
    try:
        db.rollback()  # сессия могла остаться в сломанном состоянии после ошибки
        competitor = db.get(Competitor, competitor_id)
        if competitor is not None and competitor.is_crawling:
            competitor.last_crawl_finished_at = datetime.now(UTC)
            db.add(competitor)
            db.commit()
    except Exception:  # noqa: BLE001 — это последний рубеж, дальше отметку снять уже некому
        logger.exception("Не удалось снять отметку «обход идёт» с конкурента %s", competitor_id)


async def _run_claimed(competitor_id: int) -> None:
    """Выполняет уже занятый (claim) обход — в своей сессии БД и под общим лимитом.

    Регистрирует себя в _tasks_by_competitor независимо от того, кто её запустил
    (ручная кнопка через _spawn или плановый job через run_now) — так stop()
    одинаково работает для обоих путей.
    """
    task = asyncio.current_task()
    if task is not None:
        _tasks_by_competitor[competitor_id] = task
    try:
        async with _semaphore():
            db = SessionLocal()
            try:
                competitor = db.get(Competitor, competitor_id)
                if competitor is not None:
                    await run_crawl_for_competitor(db, competitor)
            except asyncio.CancelledError:
                logger.info("Обход конкурента %s остановлен вручную", competitor_id)
                raise
            except Exception:  # фоновая задача: ошибку логируем, наверх нести некуда
                logger.exception("Обход конкурента %s завершился ошибкой", competitor_id)
            finally:
                _release_if_still_crawling(db, competitor_id)
                db.close()
    finally:
        if _tasks_by_competitor.get(competitor_id) is task:
            del _tasks_by_competitor[competitor_id]


def _spawn(competitor_id: int) -> None:
    task = asyncio.get_running_loop().create_task(_run_claimed(competitor_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def stop(competitor_id: int) -> bool:
    """Отменяет обход конкурента, если он сейчас выполняется (или ждёт своей очереди
    на семафоре). False — обхода этого конкурента прямо сейчас нет.

    Отменяет именно нашу задачу, а не процесс сервера — см. CLAUDE.md/память про
    инцидент с убийством uvicorn ради остановки обхода."""
    task = _tasks_by_competitor.get(competitor_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True


def start(db: Session, competitor: Competitor) -> bool:
    """Ручной запуск: занимаем конкурента и уходим в фон. False — обход уже идёт.

    Занять и поставить задачу нужно из одного места, без await между ними: тогда
    второй клик гарантированно упирается в занятого конкурента.
    """
    if not claim(db, competitor):
        return False
    _spawn(competitor.id)
    return True


def start_all(db: Session) -> CrawlAllResult:
    """Запускает обход всех конкурентов, кроме поставленных на паузу.

    Обходы идут параллельно, но не больше CRAWL_CONCURRENCY одновременно —
    остальные ждут своей очереди на семафоре, а не в очереди на event loop.

    Наш собственный сайт сюда не попадает: кнопка называется «обойти всех
    конкурентов», и счётчики в ответе должны сходиться со списком конкурентов.
    Он обходится по расписанию и отдельной кнопкой в настройках.
    """
    competitors = (
        db.query(Competitor).filter(Competitor.is_own.is_(False)).order_by(Competitor.id).all()
    )

    started = 0
    skipped_running = 0
    skipped_paused = 0

    for competitor in competitors:
        if competitor.is_paused:
            skipped_paused += 1
        elif start(db, competitor):
            started += 1
        else:
            skipped_running += 1

    logger.info(
        "Запущен обход всех конкурентов: запущено %s, уже идёт %s, на паузе %s",
        started,
        skipped_running,
        skipped_paused,
    )
    return CrawlAllResult(started=started, skipped_running=skipped_running, skipped_paused=skipped_paused)


async def run_now(db: Session, competitor: Competitor) -> bool:
    """Плановый запуск: занимаем и выполняем прямо здесь.

    Фоновая задача тут не нужна — каждый конкурент это отдельная job планировщика,
    то есть уже отдельная asyncio-задача. False — конкурент на паузе или занят.
    """
    if competitor.is_paused:
        return False
    if not claim(db, competitor):
        return False

    await _run_claimed(competitor.id)
    return True
