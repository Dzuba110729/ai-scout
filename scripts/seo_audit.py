"""Стартовый аудит статей конкурентов против наших (этап 6). Логика — app/seo_audit.py.

Сначала оценка объёма (ничего не меняет, ИИ не зовёт):
    python scripts/seo_audit.py --estimate

Аудит порциями (прерывать можно в любой момент — продолжит с того же места):
    python scripts/seo_audit.py --competitor skysmart.ru --max-calls 100
    python scripts/seo_audit.py --night 1-7 --max-calls 300 --sheet   # только ночью по Москве

--max-calls — потолок вызовов `claude -p` за запуск (подписка общая с обходами).
При лимите подписки скрипт сам ждёт CLAUDE_CLI_LIMIT_PAUSE_MINUTES и продолжает.
--sheet — в конце выгрузить лист «Стартовый аудит» в SEO-таблицу.

Статьи без структуры (H1, оглавление, текст — этап 2) аудировать нельзя: структура
появляется после ближайшего обхода сайта с новым кодом.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.db import SessionLocal
from app.integrations.seo_sheet import export_audit
from app.models import Competitor
from app.seo_audit import estimate_audit, run_audit


def _night(value: str) -> tuple[int, int]:
    start, _, end = value.partition("-")
    return int(start), int(end)


def _pick(db, names: list[str]) -> list[Competitor]:
    query = db.query(Competitor).filter(Competitor.is_own.is_(False)).order_by(Competitor.id)
    competitors = query.all()
    if not names:
        return [c for c in competitors if not c.is_paused]
    picked = [c for c in competitors if c.name in names or str(c.id) in names or c.base_url.rstrip("/").endswith(tuple(names))]
    if not picked:
        raise SystemExit(f"Конкуренты не найдены: {', '.join(names)}")
    return picked


def print_estimate(db, calls_per_night: int) -> None:
    rows = estimate_audit(db)
    total_calls = sum(row.calls for row in rows)
    print("Конкурент | статей в инфоразделах | готово к аудиту | без структуры | уже разобрано | вызовов ИИ ≈")
    for row in rows:
        print(f"{row.competitor} | {row.articles} | {row.ready} | {row.without_structure} | {row.done} | {row.calls}")
    nights = -(-total_calls // max(1, calls_per_night))
    print(f"Итого вызовов ИИ ≈ {total_calls}; при {calls_per_night} вызовах за ночь — ночей: {nights}.")


async def main(args: argparse.Namespace) -> None:
    db = SessionLocal()
    try:
        if args.estimate:
            print_estimate(db, args.calls_per_night)
            return
        stats = await run_audit(
            db,
            _pick(db, args.competitor),
            max_calls=args.max_calls,
            portion=args.portion,
            night=_night(args.night) if args.night else None,
            max_articles=args.max_articles,
        )
        print(
            f"Готово: разобрано статей {stats.processed}, вызовов ИИ {stats.calls}, решения {stats.decisions}, "
            f"склеено с известными темами {stats.merged}, без ответа ИИ {stats.failed}."
        )
        if args.sheet:
            url = export_audit(db)
            db.commit()
            print(f"Лист «Стартовый аудит»: {url or 'не выгружен (Google не настроен или Sheets API выключен)'}")
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--estimate", action="store_true", help="только оценка объёма")
    parser.add_argument("--competitor", action="append", default=[], help="имя, id или домен (можно несколько)")
    parser.add_argument("--max-calls", type=int, default=100, help="потолок вызовов claude -p за запуск")
    parser.add_argument("--max-articles", type=int, default=None, help="разобрать не больше стольких статей")
    parser.add_argument("--portion", type=int, default=12, help="статей в порции (commit после каждой)")
    parser.add_argument("--night", default=None, help="окно часов по Москве, например 1-7")
    parser.add_argument("--calls-per-night", type=int, default=150, help="для оценки: вызовов за ночь")
    parser.add_argument("--sheet", action="store_true", help="выгрузить лист «Стартовый аудит»")
    logging.basicConfig(level=settings.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main(parser.parse_args()))
