"""Ручная дозаправка ИИ-разбора для находок без него.

С 2026-09-18 дозаправка идёт автоматически в конце каждого обхода (не больше
AI_BACKFILL_MAX_PER_RUN за раз). Скрипт нужен, когда ждать следующего обхода не
хочется или пропусков накопилось больше лимита. Логика — в app/ai/backfill.py.

Запуск: python scripts/backfill_ai_analysis.py [--competitor "Skysmart"]
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai.backfill import backfill_missing_analyses, find_pending
from app.config import settings
from app.db import SessionLocal
from app.models import Competitor


async def main(competitor_name: str | None) -> None:
    db = SessionLocal()
    try:
        competitor_id = None
        if competitor_name:
            competitor = db.query(Competitor).filter(Competitor.name == competitor_name).one_or_none()
            if competitor is None:
                raise SystemExit(f"Конкурент «{competitor_name}» не найден")
            competitor_id = competitor.id

        pending = find_pending(db, competitor_id)
        if not pending:
            print("Находок без ИИ-разбора не найдено — дозаправлять нечего.")
            return

        print(f"Найдено находок без разбора: {len(pending)}. Начинаю (параллельность {settings.claude_cli_concurrency})...")

        def _report(url: str, ok: bool) -> None:
            print(f"  {'разобрано' if ok else 'не удалось'}: {url}")

        done, total = await backfill_missing_analyses(db, competitor_id, on_result=_report)
        print(f"Готово: разобрано {done} из {total}.")
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--competitor", help="Имя конкурента (как в базе) — иначе все")
    args = parser.parse_args()
    asyncio.run(main(args.competitor))
