"""Стартовый аудит: все уже опубликованные статьи конкурентов против наших (этап 6).

Тот же алгоритм, что в обходе (app/seo.py): карточка интента → кандидаты среди наших
статей → решение «написать / обновить / ничего», склейка одной темы у разных
конкурентов. Находки пишутся с kind="audit" и идут на лист «Стартовый аудит» таблицы.

Статей тысячи, а подписка Claude не безразмерная, поэтому аудит идёт порциями:
- после каждой порции — commit: прерванный аудит продолжается с того же места
  (статья с находкой в очередь больше не попадает);
- потолок вызовов ИИ за запуск (max_calls);
- лимит подписки (ClaudeLimitError) — пауза CLAUDE_CLI_LIMIT_PAUSE_MINUTES и дальше;
- ночной режим: работаем только в окне часов по Москве, остальное время спим.
Запуск — scripts/seo_audit.py.
"""

import asyncio
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.ai import analyze
from app.ai.topic_match import OwnArticlesIndex
from app.config import settings
from app.content_kind import is_info_url
from app.crawler.crawl import competitor_include_paths, path_included
from app.crawler.structure import PageStructure
from app.models import Competitor, Page, PageStructureRecord, SeoFinding
from app.seo import (
    FINDING_AUDIT,
    CardUpdate,
    SeoRunResult,
    build_cases,
    card_from_record,
    load_own_articles,
    match_cards,
    refresh_intent_cards,
)

logger = logging.getLogger(__name__)

# Статья без основного текста — пустышка (страница-каталог, 404): разбирать нечего.
_MIN_TEXT = 300


def structure_from_record(record: PageStructureRecord) -> PageStructure:
    return PageStructure(
        meta_title=record.meta_title,
        meta_description=record.meta_description,
        h1=record.h1,
        headings=[(int(level), str(text)) for level, text in record.headings or []],
        main_text=record.main_text or "",
    )


def _tracked_info_pages(db: Session, competitor: Competitor) -> list[Page]:
    paths = competitor_include_paths(competitor.include_paths, is_own=competitor.is_own)
    pages = (
        db.query(Page)
        .filter(Page.competitor_id == competitor.id, Page.is_removed.is_(False))
        .order_by(Page.url)
        .all()
    )
    return [page for page in pages if is_info_url(page.url) and path_included(page.url, paths)]


def audit_queue(db: Session, competitor: Competitor) -> list[Page]:
    """Статьи конкурента, которые ещё не разобраны (нет ни одной SEO-находки)."""
    done = {page_id for (page_id,) in db.query(SeoFinding.page_id).filter(SeoFinding.competitor_id == competitor.id)}
    return [
        page
        for page in _tracked_info_pages(db, competitor)
        if page.id not in done and page.structure is not None and len(page.structure.main_text or "") >= _MIN_TEXT
    ]


@dataclass
class AuditEstimate:
    competitor: str
    articles: int  # статьи в отслеживаемых инфоразделах
    ready: int  # со структурой, ещё не разобраны — можно аудировать сейчас
    without_structure: int  # структура появится после ближайшего обхода
    done: int
    calls: int  # примерная оценка вызовов ИИ на готовые статьи


def estimate_audit(db: Session, *, merge_share: float = 0.2) -> list[AuditEstimate]:
    """Сколько статей и вызовов ИИ впереди. merge_share — доля статей, которые склеятся
    с уже разобранной темой без ИИ (предварительно; уточнить по пилоту)."""
    result = []
    for competitor in db.query(Competitor).filter(Competitor.is_own.is_(False)).order_by(Competitor.id):
        pages = _tracked_info_pages(db, competitor)
        done = {pid for (pid,) in db.query(SeoFinding.page_id).filter(SeoFinding.competitor_id == competitor.id)}
        ready = [p for p in pages if p.id not in done and p.structure is not None]
        cards = sum(1 for p in ready if p.intent is None)
        compare = len(ready) * (1 - merge_share)
        calls = math.ceil(cards / max(1, settings.seo_intent_batch_size)) + math.ceil(
            compare / max(1, settings.seo_compare_batch_size)
        )
        result.append(
            AuditEstimate(
                competitor=competitor.name,
                articles=len(pages),
                ready=len(ready),
                without_structure=sum(1 for p in pages if p.id not in done and p.structure is None),
                done=sum(1 for p in pages if p.id in done),
                calls=calls,
            )
        )
    return result


async def audit_portion(
    db: Session, competitor: Competitor, pages: list[Page], own_index: OwnArticlesIndex | None
) -> SeoRunResult:
    """Одна порция статей: карточки (где их нет или статья поменялась) и решения."""
    items = [(page, structure_from_record(page.structure)) for page in pages]
    result = await refresh_intent_cards(db, items, limit=len(items))
    carded = {update.page.id for update in result.cards}
    updates = [
        CardUpdate(page=u.page, structure=u.structure, card=u.card, is_first=True) for u in result.cards
    ] + [
        CardUpdate(page=page, structure=structure, card=card_from_record(page.intent), is_first=False)
        for page, structure in items
        if page.id not in carded and page.intent is not None
    ]
    cases = build_cases(
        db, competitor.id, updates, kinds={u.page.url: FINDING_AUDIT for u in updates}, change_ids={}
    )
    if cases:
        result.merge(await match_cards(db, cases, own_index, limit=len(cases)))
    return result


@dataclass
class AuditStats:
    processed: int = 0
    calls: int = 0
    decisions: dict[str, int] = field(default_factory=dict)
    merged: int = 0
    failed: int = 0
    limit_pauses: int = 0
    stopped_by_calls: bool = False
    stopped_by_window: bool = False


@dataclass
class AuditProgress:
    competitor: str
    articles: int  # статьи в отслеживаемых инфоразделах
    done: int  # уже разобраны (есть SEO-находка)
    ready: int  # ждут ночного аудита
    waiting_structure: int  # без структуры — появится после ближайшего обхода


def audit_progress(db: Session) -> list[AuditProgress]:
    """Сколько статей конкурентов разобрано и сколько впереди — для экрана бота.

    Тот же отбор, что у audit_queue, но одним запросом на конкурента: у onlineschool-1
    ~3000 статей, ленивая подгрузка структуры по одной заметно тормозила бы кнопку."""
    result = []
    for competitor in db.query(Competitor).filter(Competitor.is_own.is_(False)).order_by(Competitor.id):
        paths = competitor_include_paths(competitor.include_paths, is_own=False)
        rows = (
            db.query(Page.id, Page.url, PageStructureRecord.id, func.length(PageStructureRecord.main_text))
            .outerjoin(PageStructureRecord, PageStructureRecord.page_id == Page.id)
            .filter(Page.competitor_id == competitor.id, Page.is_removed.is_(False))
            .all()
        )
        done_ids = {pid for (pid,) in db.query(SeoFinding.page_id).filter(SeoFinding.competitor_id == competitor.id)}
        progress = AuditProgress(competitor.name, 0, 0, 0, 0)
        for page_id, url, structure_id, text_len in rows:
            if not (is_info_url(url) and path_included(url, paths)):
                continue
            progress.articles += 1
            if page_id in done_ids:
                progress.done += 1
            elif structure_id is None:
                progress.waiting_structure += 1
            elif (text_len or 0) >= _MIN_TEXT:
                progress.ready += 1
        if progress.articles:
            result.append(progress)
    return result


def remaining_articles(db: Session, competitors: list[Competitor]) -> int:
    return sum(len(audit_queue(db, competitor)) for competitor in competitors)


def nightly_competitors(db: Session) -> list[Competitor]:
    """Кого аудировать ночью: конкуренты не на паузе и не в середине обхода."""
    return [
        competitor
        for competitor in db.query(Competitor).filter(Competitor.is_own.is_(False)).order_by(Competitor.id)
        if not competitor.is_paused and not competitor.is_crawling
    ]


_DECISION_LABELS = (("new", "написать новую"), ("update", "обновить нашу"), ("covered", "у нас раскрыто"))


def format_nightly_report(stats: "AuditStats", remaining: int, sheet_url: str | None) -> str:
    lines = [f"🌙 Ночной SEO-аудит статей конкурентов: разобрано {stats.processed}."]
    decided = [f"{label} — {stats.decisions[key]}" for key, label in _DECISION_LABELS if stats.decisions.get(key)]
    if decided:
        lines.append("Решения: " + ", ".join(decided) + ".")
    if stats.merged:
        lines.append(f"Те же темы, что у других конкурентов (склеены): {stats.merged}.")
    lines.append(f"Осталось разобрать: {remaining}." if remaining else "Все статьи конкурентов разобраны.")
    if stats.limit_pauses:
        lines.append("Упирались в лимит подписки Claude — ждали и продолжали.")
    if sheet_url:
        lines.append(f"Таблица, лист «Стартовый аудит»: {sheet_url}")
    return "\n".join(lines)

def in_night_window(now: datetime, start_hour: int, end_hour: int) -> bool:
    hour = now.astimezone(ZoneInfo(settings.digest_timezone)).hour
    if start_hour <= end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


async def run_audit(
    db: Session,
    competitors: list[Competitor],
    *,
    max_calls: int,
    portion: int = 12,
    night: tuple[int, int] | None = None,
    stop_outside_night: bool = False,
    max_articles: int | None = None,
    on_progress: Callable[[str], None] = print,
    sleep: Callable[[float], object] = asyncio.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(ZoneInfo("UTC")),
) -> AuditStats:
    """Аудит порциями до конца очереди, потолка вызовов ИИ или max_articles.

    night — окно часов по Москве; вне окна ждём его (ручной запуск) или, при
    stop_outside_night, заканчиваем (ночной запуск из планировщика)."""
    stats = AuditStats()
    own_articles = await asyncio.to_thread(load_own_articles, db)
    own_index = OwnArticlesIndex(own_articles) if own_articles else None
    on_progress(f"Наших статей для сравнения: {len(own_articles)}")

    for competitor in competitors:
        attempted: set[int] = set()
        while True:
            if stats.calls >= max_calls:
                stats.stopped_by_calls = True
                on_progress(f"Достигнут потолок вызовов ИИ за запуск: {max_calls}. Продолжение — тем же запуском.")
                return stats
            queue = [page for page in audit_queue(db, competitor) if page.id not in attempted]
            if max_articles is not None:
                queue = queue[: max(0, max_articles - stats.processed)]
            if not queue:
                break
            if night and not in_night_window(now(), *night):
                if stop_outside_night:
                    stats.stopped_by_window = True
                    on_progress("Ночное окно закончилось — продолжим следующей ночью")
                    return stats
                on_progress("Вне ночного окна — ждём 15 минут")
                await sleep(15 * 60)
                continue
            if pause := analyze._limit_pause_left():
                stats.limit_pauses += 1
                on_progress(f"Лимит подписки Claude — пауза {int(pause // 60) + 1} мин")
                await sleep(pause)
                continue
            chunk = queue[:portion]
            result = await audit_portion(db, competitor, chunk, own_index)
            db.commit()
            stats.calls += result.calls
            stats.merged += result.merged
            for decision, count in result.decisions.items():
                stats.decisions[decision] = stats.decisions.get(decision, 0) + count
            recorded = {finding.page_id for finding in result.findings}
            stats.processed += len(recorded)
            if not result.limit_hit:
                # Без ответа ИИ (не лимит) повторно в этом запуске не берём — иначе по кругу.
                failed = [page.id for page in chunk if page.id not in recorded]
                stats.failed += len(failed)
                attempted.update(failed)
            on_progress(
                f"{competitor.name}: разобрано {stats.processed}, вызовов ИИ {stats.calls}, "
                f"в очереди ещё {len(queue) - len(recorded)}"
            )
    return stats
