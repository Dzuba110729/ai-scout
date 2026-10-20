"""Какие разделы сайта конкурента отслеживать (competitors.include_paths).

В боте и вебе этой настройки нет — раньше её меняли запросом к базе. Без --apply
скрипт только показывает, что изменится и сколько страниц добавится или уйдёт по
карте сайта (один запрос к карте, без обхода):

    python scripts/set_include_paths.py --competitor skysmart.ru --paths "!/repetitors, !/vpr"
    python scripts/set_include_paths.py --competitor skysmart.ru --paths "!/repetitors, !/vpr" --apply

Открытый заново раздел следующий обход запомнит как точку отсчёта — без тысячи
«новых страниц» (см. CLAUDE.md, этап 2). --paths "" — весь сайт.
"""

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crawler.crawl import competitor_include_paths, discover_sitemap_entries, path_included
from app.db import SessionLocal
from app.models import Competitor


def _section(url: str) -> str:
    parts = [part for part in url.split("/")[3:] if part]
    return "/" + parts[0] if parts else "/"


async def main(args: argparse.Namespace) -> None:
    db = SessionLocal()
    try:
        matches = [
            c
            for c in db.query(Competitor).all()
            if args.competitor in (c.name, str(c.id)) or args.competitor in c.base_url
        ]
        if len(matches) != 1:
            raise SystemExit(f"Нужен ровно один сайт, найдено: {[c.name for c in matches]}")
        competitor = matches[0]
        new_value = args.paths.strip() or None
        print(f"{competitor.name}: сейчас «{competitor.include_paths or 'весь сайт'}» → станет «{new_value or 'весь сайт'}»")

        if not competitor.cookies_only:
            entries = await discover_sitemap_entries(competitor.base_url, competitor.sitemap_url)
            old = competitor_include_paths(competitor.include_paths, is_own=competitor.is_own)
            new = competitor_include_paths(new_value, is_own=competitor.is_own)
            added = Counter(_section(e.url) for e in entries if path_included(e.url, new) and not path_included(e.url, old))
            removed = Counter(_section(e.url) for e in entries if path_included(e.url, old) and not path_included(e.url, new))
            print(f"По карте сайта добавится страниц: {sum(added.values())} {dict(added.most_common(8))}")
            print(f"Перестанем отслеживать: {sum(removed.values())} {dict(removed.most_common(8))}")
        else:
            print("Сайт обходится только по кукам — карту сайта без них не читаем, оценки нет.")

        if not args.apply:
            print("Ничего не изменено. Чтобы применить — добавьте --apply.")
            return
        competitor.include_paths = new_value
        db.commit()
        print("Сохранено. Изменение подхватит следующий обход (перезапуск не нужен).")
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--competitor", required=True, help="имя, id или домен")
    parser.add_argument("--paths", required=True, help="разделы через запятую, «!/раздел» — исключить")
    parser.add_argument("--apply", action="store_true", help="сохранить (без него — только показать)")
    asyncio.run(main(parser.parse_args()))
