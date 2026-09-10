"""Черновик уже загруженных страниц на уровне pipeline: свежесть, запись, очистка.

Отдельно от tests/test_resume_cache.py, который проверяет сам цикл обхода в
crawl.py на заглушках. Здесь — работа с настоящей (временной, in-memory) базой,
потому что хуки читают и пишут в PageFetchCache напрямую.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base
from app.models import Competitor, PageFetchCache
from app.pipeline import (
    _clear_fetch_cache,
    _fetch_cache_lookup,
    _fetch_cache_store,
    _on_urls_discovered,
)

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    competitor = Competitor(name="Конкурент", base_url="https://rival.ru")
    session.add(competitor)
    session.commit()

    yield session, competitor.id
    session.close()


def test_fresh_cache_entry_is_reused(db, monkeypatch):
    session, competitor_id = db
    monkeypatch.setattr(settings, "crawl_resume_max_age_hours", 12.0)
    session.add(
        PageFetchCache(
            competitor_id=competitor_id,
            url="https://rival.ru/a",
            title="Заголовок",
            text_content="Текст",
            fetched_at=NOW - timedelta(hours=1),
        )
    )
    session.commit()

    lookup = _fetch_cache_lookup(session, competitor_id)
    with _frozen_now(monkeypatch, NOW):
        result = asyncio.run(lookup("https://rival.ru/a"))

    assert result == ("Заголовок", "Текст")


def test_stale_cache_entry_is_ignored(db, monkeypatch):
    session, competitor_id = db
    monkeypatch.setattr(settings, "crawl_resume_max_age_hours", 12.0)
    session.add(
        PageFetchCache(
            competitor_id=competitor_id,
            url="https://rival.ru/a",
            title="Старый",
            text_content="Старый текст",
            fetched_at=NOW - timedelta(hours=13),  # старше порога в 12 часов
        )
    )
    session.commit()

    lookup = _fetch_cache_lookup(session, competitor_id)
    with _frozen_now(monkeypatch, NOW):
        result = asyncio.run(lookup("https://rival.ru/a"))

    assert result is None


def test_missing_cache_entry_returns_none(db):
    session, competitor_id = db

    lookup = _fetch_cache_lookup(session, competitor_id)
    result = asyncio.run(lookup("https://rival.ru/never-fetched"))

    assert result is None


def test_store_creates_a_new_row(db):
    session, competitor_id = db

    store = _fetch_cache_store(session, competitor_id)
    asyncio.run(store("https://rival.ru/a", "Заголовок", "Текст"))

    rows = session.query(PageFetchCache).filter(PageFetchCache.competitor_id == competitor_id).all()
    assert len(rows) == 1
    assert rows[0].url == "https://rival.ru/a"
    assert rows[0].text_content == "Текст"


def test_store_updates_an_existing_row_instead_of_duplicating(db):
    session, competitor_id = db
    store = _fetch_cache_store(session, competitor_id)
    asyncio.run(store("https://rival.ru/a", "Старый", "Старый текст"))

    asyncio.run(store("https://rival.ru/a", "Новый", "Новый текст"))

    rows = session.query(PageFetchCache).filter(PageFetchCache.competitor_id == competitor_id).all()
    assert len(rows) == 1
    assert rows[0].text_content == "Новый текст"


def test_urls_discovered_sets_total_on_the_competitor(db):
    session, competitor_id = db

    on_discovered = _on_urls_discovered(session, competitor_id)
    asyncio.run(on_discovered(187))

    competitor = session.get(Competitor, competitor_id)
    assert competitor.crawl_pages_total == 187


def test_clear_removes_only_this_competitor_cache_and_resets_total(db):
    session, competitor_id = db
    other = Competitor(name="Другой конкурент", base_url="https://other.ru", crawl_pages_total=5)
    session.add(other)
    session.flush()

    session.add(PageFetchCache(competitor_id=competitor_id, url="https://rival.ru/a", text_content="т"))
    session.add(PageFetchCache(competitor_id=other.id, url="https://other.ru/a", text_content="т"))
    session.get(Competitor, competitor_id).crawl_pages_total = 42
    session.commit()

    _clear_fetch_cache(session, competitor_id)
    session.commit()

    assert session.query(PageFetchCache).filter(PageFetchCache.competitor_id == competitor_id).count() == 0
    assert session.query(PageFetchCache).filter(PageFetchCache.competitor_id == other.id).count() == 1
    assert session.get(Competitor, competitor_id).crawl_pages_total is None
    assert session.get(Competitor, other.id).crawl_pages_total == 5


class _frozen_now:
    """Подменяет datetime.now(UTC) внутри app.pipeline на фиксированный момент —
    иначе тест на «свежесть» зависел бы от реального времени прогона."""

    def __init__(self, monkeypatch, when: datetime):
        self._monkeypatch = monkeypatch
        self._when = when

    def __enter__(self):
        import app.pipeline as pipeline_module

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return self._when

        self._monkeypatch.setattr(pipeline_module, "datetime", _FixedDatetime)

    def __exit__(self, *exc_info):
        return False
