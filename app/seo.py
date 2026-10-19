"""SEO-разбор инфостатей во время обхода: карточки интента (этап 3) и решения
«написать / обновить / ничего» по сравнению с нашими статьями (этап 4).

Обход (app/pipeline.py) зовёт process_info_articles с новыми и изменёнными статьями
конкурента (или нашего сайта) — у каждой уже есть структура из этапа 2. Всё, что
не успели за обход (лимиты seo_*_max_per_run), доберёт стартовый аудит
(scripts/seo_audit.py) тем же кодом.

ИИ-вызовы гоняются пачками и параллельно (до claude_cli_concurrency), а запись в БД —
синхронно после ответа, как и в остальном обходе: одну Session нельзя дёргать из
нескольких операций разом.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.ai.analyze import ClaudeCliError, ClaudeLimitError
from app.ai.intent import ArticleInput, IntentCard, intent_content_hash, make_intent_cards
from app.config import settings
from app.crawler.structure import PageStructure
from app.models import ArticleIntent, Page

logger = logging.getLogger(__name__)


@dataclass
class CardUpdate:
    page: Page
    structure: PageStructure
    card: IntentCard
    # Карточки у статьи раньше не было (новая статья или первый разбор).
    is_first: bool
    # Подинтенты, которых не было в прошлой версии статьи (по мнению ИИ).
    added_subintents: list[str] = field(default_factory=list)


@dataclass
class SeoRunResult:
    cards: list[CardUpdate] = field(default_factory=list)
    cards_skipped: int = 0  # не уложились в лимит или ИИ не ответил — доберёт аудит
    limit_hit: bool = False  # упёрлись в лимит подписки Claude


def card_from_record(record: ArticleIntent) -> IntentCard:
    return IntentCard(
        topic=record.topic,
        main_intent=record.main_intent,
        subintents=list(record.subintents or []),
        entities=list(record.entities or []),
        relations=[tuple(item) for item in record.relations or []],
        material_type=record.material_type or "другое",
        raw_response=record.raw_response or {},
    )


def _store_card(db: Session, page: Page, card: IntentCard, content_hash: str) -> None:
    record = page.intent
    if record is None:
        record = ArticleIntent(page_id=page.id)
        db.add(record)
        page.intent = record
    record.content_hash = content_hash
    record.topic = card.topic
    record.main_intent = card.main_intent
    record.subintents = card.subintents
    record.entities = card.entities
    record.relations = [list(item) for item in card.relations]
    record.material_type = card.material_type
    record.raw_response = card.raw_response
    record.updated_at = datetime.now(UTC)


def needs_card(page: Page, structure: PageStructure) -> bool:
    """Карточки нет или статья с тех пор поменялась по существу."""
    return page.intent is None or page.intent.content_hash != intent_content_hash(structure)


def _batches(items: list, size: int) -> list[list]:
    size = max(1, size)
    return [items[start : start + size] for start in range(0, len(items), size)]


async def refresh_intent_cards(
    db: Session, items: list[tuple[Page, PageStructure]], *, limit: int
) -> SeoRunResult:
    """Карточки для статей, у которых их нет или содержимое поменялось. Не больше limit."""
    result = SeoRunResult()
    todo = [(page, structure) for page, structure in items if needs_card(page, structure)]
    if limit < len(todo):
        result.cards_skipped = len(todo) - max(0, limit)
        todo = todo[: max(0, limit)]
    if not todo:
        return result

    semaphore = asyncio.Semaphore(max(1, settings.claude_cli_concurrency))

    async def _one(batch: list[tuple[Page, PageStructure]]) -> list[IntentCard | None] | None:
        articles = [
            ArticleInput(
                url=page.url,
                structure=structure,
                previous_subintents=list(page.intent.subintents or []) if page.intent else None,
            )
            for page, structure in batch
        ]
        async with semaphore:
            if result.limit_hit:
                return None
            try:
                return await make_intent_cards(articles)
            except ClaudeLimitError as exc:
                result.limit_hit = True
                logger.warning("Карточки интента: лимит подписки Claude — %s", exc)
            except (ClaudeCliError, ValueError) as exc:
                logger.warning("Карточки интента для %s статей не получены: %s", len(batch), exc)
            return None

    batches = _batches(todo, settings.seo_intent_batch_size)
    answers = await asyncio.gather(*(_one(batch) for batch in batches))
    for batch, cards in zip(batches, answers, strict=True):
        for (page, structure), card in zip(batch, cards or [None] * len(batch), strict=True):
            if card is None:
                result.cards_skipped += 1
                continue
            is_first = page.intent is None
            _store_card(db, page, card, intent_content_hash(structure))
            result.cards.append(
                CardUpdate(
                    page=page,
                    structure=structure,
                    card=card,
                    is_first=is_first,
                    added_subintents=[] if is_first else card.added_subintents,
                )
            )
    return result


async def process_info_articles(
    db: Session, items: list[tuple[Page, PageStructure]], *, is_own: bool
) -> SeoRunResult:
    """SEO-разбор новых и изменённых статей одного обхода."""
    if not settings.seo_enabled or not items:
        return SeoRunResult()
    return await refresh_intent_cards(db, items, limit=settings.seo_intent_max_per_run)


def format_seo_note(result: SeoRunResult) -> str | None:
    if not result.cards and not result.cards_skipped:
        return None
    note = f"SEO: разобрано инфостатей (интент, подинтенты) — {len(result.cards)}."
    if result.cards_skipped:
        note += f" Не успели — {result.cards_skipped}, доберём стартовым аудитом или в следующий раз."
    if result.limit_hit:
        note += " Упёрлись в лимит подписки Claude."
    return note
