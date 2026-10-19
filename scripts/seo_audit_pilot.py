"""Пилот стартового аудита без записи в рабочую базу (этап 6).

Из рабочей базы только ЧИТАЕМ (транзакция read only): адреса наших статей og1.ru с
текстом последнего снимка и адреса статей конкурента. Всё остальное — в отдельной
SQLite в --work-dir: структура страниц (грузим обычным HTTP, как этап 2 в обходе),
карточки, решения. Дальше — ровно тот же код, что у настоящего аудита (run_audit),
и выгрузка строк таблицы в CSV и markdown.

    python scripts/seo_audit_pilot.py --competitor-id 3 --limit 50 --work-dir /tmp/pilot --out pilot/
"""

import argparse
import asyncio
import csv
import json
import logging
import random
import sys
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.content_kind import is_info_url
from app.crawler.crawl import competitor_include_paths
from app.crawler.structure import extract_structure
from app.db import Base
from app.integrations.seo_sheet import HEADER, build_sheet_rows, load_sheet_findings
from app.models import (
    ArticleIntent,
    Competitor,
    Page,
    PageSnapshot,
    PageStructureRecord,
    SeoFinding,
)
from app.seo_audit import run_audit

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"


def _read_prod(dsn: str, competitor_id: int, limit: int, seed: int) -> dict:
    """Только SELECT простым SQL: в рабочей базе ещё нет колонок новых миграций,
    а модели приложения уже знают о них."""
    engine = create_engine(dsn, connect_args={"options": "-c default_transaction_read_only=on"})
    with engine.connect() as conn:
        own = conn.execute(
            text("SELECT id, name, base_url FROM competitors WHERE is_own ORDER BY id LIMIT 1")
        ).one()
        rival = conn.execute(
            text("SELECT id, name, base_url FROM competitors WHERE id = :id"), {"id": competitor_id}
        ).one()
        rows = conn.execute(
            text(
                "SELECT DISTINCT ON (p.id) p.url, p.title, s.text_content FROM pages p "
                "LEFT JOIN page_snapshots s ON s.page_id = p.id "
                "WHERE p.competitor_id = :id AND NOT p.is_removed ORDER BY p.id, s.fetched_at DESC"
            ),
            {"id": own.id},
        ).all()
        own_pages = [
            {"url": url, "title": title, "text": snapshot or ""} for url, title, snapshot in rows if is_info_url(url)
        ]
        # Аудит берёт статьи по ТЕКУЩЕМУ фильтру разделов; для пилота — все инфостраницы
        # из базы (у skysmart /articles сейчас исключён, но в базе с сентября они есть).
        rival_urls = sorted(
            url
            for (url,) in conn.execute(
                text("SELECT url FROM pages WHERE competitor_id = :id AND NOT is_removed"), {"id": rival.id}
            )
            if is_info_url(url)
        )
    random.Random(seed).shuffle(rival_urls)
    return {
        "own": {"name": own.name, "base_url": own.base_url},
        "rival": {"name": rival.name, "base_url": rival.base_url},
        "own_pages": own_pages,
        "rival_urls": rival_urls[:limit],
    }


async def _fetch_structures(urls: list[str], cache_path: Path, concurrency: int, delay: float) -> dict[str, dict]:
    cache: dict[str, dict] = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    todo = [url for url in urls if url not in cache]
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": _UA}) as client:

        async def one(url: str) -> None:
            async with semaphore:
                try:
                    response = await client.get(url)
                    if response.status_code == 200:
                        cache[url] = extract_structure(response.text).to_dict()
                except httpx.HTTPError as exc:
                    print(f"Не загрузилась {url}: {exc}")
                await asyncio.sleep(delay)

        for start in range(0, len(todo), 200):
            await asyncio.gather(*(one(url) for url in todo[start : start + 200]))
            cache_path.write_text(json.dumps(cache, ensure_ascii=False))
            print(f"Структура: загружено {min(start + 200, len(todo))} из {len(todo)}")
    return cache


def _build_local_db(work_dir: Path, data: dict, structures: dict[str, dict]):
    db_path = work_dir / "pilot.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False)()
    if db.query(Competitor).count():
        return db  # продолжение прерванного пилота
    own = Competitor(name=data["own"]["name"], base_url=data["own"]["base_url"], is_own=True)
    rival = Competitor(name=data["rival"]["name"], base_url=data["rival"]["base_url"])
    db.add_all([own, rival])
    db.flush()
    for item in data["own_pages"]:
        page = Page(competitor_id=own.id, url=item["url"], title=item["title"])
        db.add(page)
        db.flush()
        db.add(PageSnapshot(page_id=page.id, content_hash="", text_content=item["text"]))
        _add_structure(db, page, structures.get(item["url"]))
    for url in data["rival_urls"]:
        page = Page(competitor_id=rival.id, url=url)
        db.add(page)
        db.flush()
        _add_structure(db, page, structures.get(url))
    db.commit()
    return db


def _add_structure(db, page: Page, data: dict | None) -> None:
    if not data:
        return
    db.add(
        PageStructureRecord(
            page_id=page.id,
            meta_title=data.get("meta_title"),
            meta_description=data.get("meta_description"),
            h1=data.get("h1"),
            headings=data.get("headings"),
            main_text=data.get("main_text") or "",
            main_text_length=len(data.get("main_text") or ""),
        )
    )


def _write_outputs(db, out_dir: Path, name: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = name.replace("https://", "").strip("/").replace("/", "_")
    rows = build_sheet_rows(load_sheet_findings(db, audit=True))
    with (out_dir / f"seo_audit_pilot_{slug}.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(HEADER)
        writer.writerows(rows)

    lines = [f"# Пилот стартового аудита: {name}", "", f"Тем: {len(rows)}.", ""]
    for finding in db.query(SeoFinding).order_by(SeoFinding.id):
        intent: ArticleIntent | None = finding.page.intent
        decision = finding.decision
        lines += [
            f"## {finding.page.url}",
            f"- Тема: {decision.topic} (тип: {decision.material_type})",
            f"- Основной интент: {intent.main_intent if intent else decision.main_intent}",
            f"- Подинтенты: {'; '.join(intent.subintents or []) if intent else ''}",
            f"- Решение: **{decision.decision}**" + (f" → {decision.our_url}" if decision.our_url else ""),
            f"- Добавить: {'; '.join(decision.add_subintents or []) or '—'}",
            f"- Аргумент: {decision.argument or '—'}",
            f"- Каннибализация: {', '.join(decision.cannibal_urls or []) or 'нет'}",
            "",
        ]
    (out_dir / f"seo_audit_pilot_{slug}.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Результаты: {out_dir}")


async def main(args: argparse.Namespace) -> None:
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    data_path = work_dir / "prod_extract.json"
    if data_path.exists():
        data = json.loads(data_path.read_text())
    else:
        data = _read_prod(args.prod_dsn or settings.database_url, args.competitor_id, args.limit, args.seed)
        data_path.write_text(json.dumps(data, ensure_ascii=False))
    print(f"Наших статей: {len(data['own_pages'])}, статей конкурента в пилоте: {len(data['rival_urls'])}")

    own_structures = await _fetch_structures(
        [p["url"] for p in data["own_pages"]], work_dir / "own_structures.json", concurrency=6, delay=0.2
    )
    rival_structures = await _fetch_structures(
        data["rival_urls"], work_dir / "rival_structures.json", concurrency=2, delay=1.0
    )
    db = _build_local_db(work_dir, data, {**own_structures, **rival_structures})
    rival = db.query(Competitor).filter(Competitor.is_own.is_(False)).one()
    rival.include_paths = None  # пилоту — все статьи из выборки
    assert competitor_include_paths(rival.include_paths, is_own=False) is not None
    stats = await run_audit(db, [rival], max_calls=args.max_calls, portion=args.portion)
    print(f"Пилот: разобрано {stats.processed}, вызовов ИИ {stats.calls}, решения {stats.decisions}, склеено {stats.merged}")
    _write_outputs(db, Path(args.out), data["rival"]["base_url"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--competitor-id", type=int, required=True)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--seed", type=int, default=19102026)
    parser.add_argument("--max-calls", type=int, default=40)
    parser.add_argument("--portion", type=int, default=12)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--out", default="pilot")
    parser.add_argument("--prod-dsn", default=None)
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main(parser.parse_args()))
