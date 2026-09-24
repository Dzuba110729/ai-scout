"""Плановый цикл обходов: сначала наш сайт целиком, потом конкуренты."""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import scheduler as scheduler_module
from app.db import Base
from app.models import Competitor, SessionStatus


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(scheduler_module, "SessionLocal", factory)
    session = factory()
    session.add_all(
        [
            Competitor(name="Наш сайт", base_url="https://og1.ru", is_own=True, status=SessionStatus.ACTIVE),
            Competitor(name="Фоксфорд", base_url="https://foxford.ru", status=SessionStatus.ACTIVE),
        ]
    )
    session.commit()
    yield session
    session.close()


def test_cycle_crawls_own_site_to_the_end_before_competitors(db, monkeypatch):
    events = []

    async def fake_run_now(_db, competitor):
        events.append(f"start {competitor.name}")
        await asyncio.sleep(0.01)
        events.append(f"done {competitor.name}")
        return True

    def fake_start_all(_db):
        events.append("start competitors")
        return scheduler_module.crawl_manager.CrawlAllResult(started=1, skipped_running=0, skipped_paused=0)

    monkeypatch.setattr(scheduler_module.crawl_manager, "run_now", fake_run_now)
    monkeypatch.setattr(scheduler_module.crawl_manager, "start_all", fake_start_all)

    asyncio.run(scheduler_module.run_crawl_cycle())

    assert events == ["start Наш сайт", "done Наш сайт", "start competitors"]
    assert scheduler_module.get_schedule_config(db).next_run_at is not None


def test_paused_own_site_is_skipped(db, monkeypatch):
    own = db.query(Competitor).filter_by(is_own=True).one()
    own.is_paused = True
    db.commit()
    calls = []

    async def fake_run_now(_db, competitor):
        calls.append(competitor.name)
        return True

    monkeypatch.setattr(scheduler_module.crawl_manager, "run_now", fake_run_now)
    monkeypatch.setattr(
        scheduler_module.crawl_manager,
        "start_all",
        lambda _db: scheduler_module.crawl_manager.CrawlAllResult(started=1, skipped_running=0, skipped_paused=0),
    )

    asyncio.run(scheduler_module.run_crawl_cycle())

    assert calls == []
