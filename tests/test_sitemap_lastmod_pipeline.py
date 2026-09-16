"""Когда обход обязан быть полным (см. CLAUDE.md про инкрементальный обход) и как
дата страницы из sitemap сохраняется в Page.sitemap_lastmod по ходу обычного обхода."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.crawler.diff import ChangeType, PageDiff
from app.db import Base
from app.models import Competitor, Page
from app.pipeline import _apply_page_diff, _needs_full_crawl

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
OLD = datetime(2026, 1, 1, tzinfo=UTC)
NEW = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    competitor = Competitor(name="Конкурент", base_url="https://rival.ru")
    session.add(competitor)
    session.commit()

    yield session, competitor

    session.close()


def _frozen_now(monkeypatch, when: datetime):
    import app.pipeline as pipeline_module

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return when

    monkeypatch.setattr(pipeline_module, "datetime", _FixedDatetime)


def test_competitor_never_crawled_needs_a_full_crawl():
    competitor = Competitor(name="Новый", base_url="https://rival.ru")

    assert _needs_full_crawl(competitor) is True


def test_recently_fully_crawled_competitor_does_not_need_another(monkeypatch):
    competitor = Competitor(name="Конкурент", base_url="https://rival.ru", last_full_crawl_at=NOW)
    _frozen_now(monkeypatch, NOW + timedelta(days=1))

    assert _needs_full_crawl(competitor) is False


def test_full_crawl_is_repeated_after_the_recheck_period(monkeypatch):
    competitor = Competitor(name="Конкурент", base_url="https://rival.ru", last_full_crawl_at=NOW)
    _frozen_now(monkeypatch, NOW + timedelta(days=31))

    assert _needs_full_crawl(competitor) is True


def test_new_page_gets_its_sitemap_lastmod_stored(db):
    session, competitor = db
    page_diff = PageDiff(url="https://rival.ru/new", change_type=ChangeType.NEW, new_text="текст")

    page = _apply_page_diff(session, competitor, page_diff, {}, {"https://rival.ru/new": NEW})

    assert page.sitemap_lastmod == NEW


def test_changed_page_updates_its_sitemap_lastmod(db):
    session, competitor = db
    page = Page(competitor_id=competitor.id, url="https://rival.ru/a", sitemap_lastmod=OLD)
    session.add(page)
    session.flush()
    previous_by_url = {"https://rival.ru/a": (page, "старый текст")}
    page_diff = PageDiff(
        url="https://rival.ru/a", change_type=ChangeType.CHANGED, old_text="старый текст", new_text="новый текст"
    )

    _apply_page_diff(session, competitor, page_diff, previous_by_url, {"https://rival.ru/a": NEW})

    assert page.sitemap_lastmod == NEW


def test_removed_page_keeps_its_last_known_sitemap_lastmod(db):
    session, competitor = db
    page = Page(competitor_id=competitor.id, url="https://rival.ru/gone", sitemap_lastmod=OLD)
    session.add(page)
    session.flush()
    previous_by_url = {"https://rival.ru/gone": (page, "было")}
    page_diff = PageDiff(url="https://rival.ru/gone", change_type=ChangeType.REMOVED, old_text="было")

    _apply_page_diff(session, competitor, page_diff, previous_by_url, {})

    assert page.sitemap_lastmod == OLD
