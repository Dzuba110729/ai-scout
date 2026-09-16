"""Дозаправка: ИИ-разбор для находок, которые остались без него.

Зачем нужен: при первом полном обходе конкурента (см. CLAUDE.md про инкрементальный
обход) может разом найтись гораздо больше изменений, чем при обычном еженедельном
обходе (1-5 штук) — например, 177 у Skysmart с первого прогона. Личная подписка
Claude Code не рассчитана на такой залп вызовов `claude -p` подряд, и часть анализов
не проходит (таймаут/лимит). Обычный повторный обход это НЕ чинит: раз содержимое
страниц с прошлого раза не изменилось, инкрементальный обход их просто пропустит —
находки уже записаны, и обычный обход не пересматривает старые пропуски.

Этот скрипт находит все PageChange без AiAnalysis и дозаправляет их напрямую,
без нового обхода сайта. Безопасно запускать повторно — трогает только находки,
у которых ещё нет разбора.

Запуск: python scripts/backfill_ai_analysis.py [--competitor "Skysmart"]
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai.analyze import ClaudeCliError, analyze_page_change
from app.config import settings
from app.crawler.diff import ChangeType, PageDiff
from app.db import SessionLocal
from app.models import AiAnalysis, Competitor, Page, PageChange
from app.models import ChangeType as DbChangeType

_DB_TO_CHANGE_TYPE = {
    DbChangeType.NEW: ChangeType.NEW,
    DbChangeType.CHANGED: ChangeType.CHANGED,
    DbChangeType.REMOVED: ChangeType.REMOVED,
}


def _to_page_diff(page: Page, change: PageChange) -> PageDiff:
    old_text = change.old_snapshot.text_content if change.old_snapshot else None
    new_text = change.new_snapshot.text_content if change.new_snapshot else None
    return PageDiff(
        url=page.url,
        change_type=_DB_TO_CHANGE_TYPE[change.change_type],
        old_text=old_text,
        new_text=new_text,
    )


async def _backfill_one(db, semaphore: asyncio.Semaphore, page: Page, change: PageChange) -> bool:
    async with semaphore:
        try:
            analysis = await analyze_page_change(_to_page_diff(page, change))
        except (ClaudeCliError, ValueError) as exc:
            print(f"  не удалось: {page.url} — {exc}")
            return False

    db.add(
        AiAnalysis(
            page_change_id=change.id,
            category=analysis.category,
            usp=analysis.usp,
            cta=analysis.cta,
            summary=analysis.summary,
            raw_response=analysis.raw_response,
        )
    )
    db.commit()
    print(f"  разобрано: {page.url}")
    return True


async def main(competitor_name: str | None) -> None:
    db = SessionLocal()
    try:
        query = (
            db.query(PageChange, Page)
            .join(Page, Page.id == PageChange.page_id)
            .outerjoin(AiAnalysis, AiAnalysis.page_change_id == PageChange.id)
            .filter(AiAnalysis.id.is_(None))
        )
        if competitor_name:
            competitor = db.query(Competitor).filter(Competitor.name == competitor_name).one_or_none()
            if competitor is None:
                raise SystemExit(f"Конкурент «{competitor_name}» не найден")
            query = query.filter(Page.competitor_id == competitor.id)

        pending = query.all()
        if not pending:
            print("Находок без ИИ-разбора не найдено — дозаправлять нечего.")
            return

        print(f"Найдено находок без разбора: {len(pending)}. Начинаю (параллельность {settings.claude_cli_concurrency})...")

        semaphore = asyncio.Semaphore(max(1, settings.claude_cli_concurrency))
        results = await asyncio.gather(
            *(_backfill_one(db, semaphore, page, change) for change, page in pending)
        )

        done = sum(results)
        print(f"Готово: разобрано {done} из {len(pending)}.")
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--competitor", help="Имя конкурента (как в базе) — иначе все")
    args = parser.parse_args()
    asyncio.run(main(args.competitor))
