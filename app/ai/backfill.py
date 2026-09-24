"""Дозаправка: ИИ-разбор для находок, которые остались без него.

Зачем нужна: при первом полном обходе конкурента (см. CLAUDE.md про инкрементальный
обход) может разом найтись гораздо больше изменений, чем при обычном еженедельном
обходе (1-5 штук) — например, 177 у Skysmart с первого прогона. Личная подписка
Claude Code не рассчитана на такой залп вызовов `claude -p`, и часть анализов
не проходит (таймаут/лимит). Обычный повторный обход это НЕ чинит: раз содержимое
страниц с прошлого раза не изменилось, инкрементальный обход их просто пропускает —
находки уже записаны, а старые пропуски обход сам не пересматривает.

Поэтому дозаправка вызывается автоматически в конце каждого обхода
(app.pipeline.run_crawl_for_competitor) и доступна вручную через
scripts/backfill_ai_analysis.py. Безопасно запускать повторно — трогает только
находки, у которых ещё нет разбора.
"""

import asyncio
import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.ai.analyze import ClaudeCliError, analyze_page_change
from app.config import settings
from app.crawler.diff import ChangeType, PageDiff
from app.models import AiAnalysis, Page, PageChange
from app.models import ChangeType as DbChangeType

logger = logging.getLogger(__name__)

_DB_TO_CHANGE_TYPE = {
    DbChangeType.NEW: ChangeType.NEW,
    DbChangeType.CHANGED: ChangeType.CHANGED,
    DbChangeType.REMOVED: ChangeType.REMOVED,
}


def find_pending(db: Session, competitor_id: int | None = None) -> list[tuple[PageChange, Page]]:
    """Находки без ИИ-разбора — старые первыми, чтобы давние пропуски не ждали вечно."""
    query = (
        db.query(PageChange, Page)
        .join(Page, Page.id == PageChange.page_id)
        .outerjoin(AiAnalysis, AiAnalysis.page_change_id == PageChange.id)
        .filter(AiAnalysis.id.is_(None))
        .order_by(PageChange.id)
    )
    if competitor_id is not None:
        query = query.filter(Page.competitor_id == competitor_id)
    return query.all()


def to_page_diff(page: Page, change: PageChange) -> PageDiff:
    old_text = change.old_snapshot.text_content if change.old_snapshot else None
    new_text = change.new_snapshot.text_content if change.new_snapshot else None
    return PageDiff(
        url=page.url,
        change_type=_DB_TO_CHANGE_TYPE[change.change_type],
        old_text=old_text,
        new_text=new_text,
    )


async def backfill_missing_analyses(
    db: Session,
    competitor_id: int | None = None,
    *,
    limit: int | None = None,
    on_result: Callable[[str, bool], None] | None = None,
) -> tuple[int, int]:
    """Дозаправляет находки без разбора. Возвращает (разобрано, всего попыток).

    limit — не больше стольких за раз: если CLI недоступен вовсе, не нужно
    долбить его сотнями попыток за один прогон. Каждый успешный разбор коммитится
    отдельно — упавший на середине процесс не теряет уже сделанное.
    """
    pending = find_pending(db, competitor_id)
    if limit is not None:
        pending = pending[: max(0, limit)]
    if not pending:
        return 0, 0

    semaphore = asyncio.Semaphore(max(1, settings.claude_cli_concurrency))

    async def _one(change: PageChange, page: Page) -> bool:
        diff = to_page_diff(page, change)
        async with semaphore:
            try:
                analysis = await analyze_page_change(diff)
            except (ClaudeCliError, ValueError) as exc:
                logger.warning("Дозаправка ИИ-анализа не удалась для %s: %s", page.url, exc)
                if on_result:
                    on_result(page.url, False)
                return False
        # db.add/commit — после await, синхронно: параллельной записи в Session нет.
        db.add(
            AiAnalysis(
                page_change_id=change.id,
                category=analysis.category,
                usp=analysis.usp,
                cta=analysis.cta,
                summary=analysis.summary,
                importance=analysis.importance,
                importance_reason=analysis.importance_reason,
                raw_response=analysis.raw_response,
            )
        )
        db.commit()
        if on_result:
            on_result(page.url, True)
        return True

    results = await asyncio.gather(*(_one(change, page) for change, page in pending))
    return sum(results), len(pending)
