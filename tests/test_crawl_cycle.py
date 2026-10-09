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

    async def fake_run_now(_db, competitor, quiet=False):
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

    async def fake_run_now(_db, competitor, quiet=False):
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


def test_daily_run_crawls_only_own_site_quietly(db, monkeypatch):
    calls = []

    async def fake_run_now(_db, competitor, quiet=False):
        calls.append((competitor.name, quiet))
        return True

    def fail_start_all(_db):
        raise AssertionError("ежедневный обход не должен трогать конкурентов")

    monkeypatch.setattr(scheduler_module.crawl_manager, "run_now", fake_run_now)
    monkeypatch.setattr(scheduler_module.crawl_manager, "start_all", fail_start_all)

    asyncio.run(scheduler_module.run_own_site_daily())

    assert calls == [("Наш сайт", True)]


def test_daily_own_site_job_runs_at_moscow_hour():
    scheduler_module.schedule_own_site_daily()
    try:
        job = scheduler_module.scheduler.get_job(scheduler_module.OWN_SITE_DAILY_JOB_ID)
        assert str(job.trigger.timezone) == "Europe/Moscow"
        assert "hour='5'" in str(job.trigger)
    finally:
        scheduler_module.scheduler.remove_all_jobs()


@pytest.fixture
def sent(monkeypatch):
    messages = []

    async def fake_tell(text):
        messages.append(text)

    monkeypatch.setattr(scheduler_module, "_tell_owner", fake_tell)
    yield messages
    scheduler_module.scheduler.remove_all_jobs()


def test_cycle_waits_for_own_site_and_retries_when_it_did_not_open(db, monkeypatch, sent):
    competitors_started = []

    async def failed_run_now(_db, competitor, quiet=False):
        return False

    monkeypatch.setattr(scheduler_module.crawl_manager, "run_now", failed_run_now)
    monkeypatch.setattr(
        scheduler_module.crawl_manager, "start_all", lambda _db: competitors_started.append(1)
    )
    saved_next_run = scheduler_module.get_schedule_config(db).next_run_at

    asyncio.run(scheduler_module.run_crawl_cycle())

    assert competitors_started == []  # конкуренты ждут удачного обхода нашего сайта
    job = scheduler_module.scheduler.get_job(scheduler_module.CYCLE_RETRY_JOB_ID)
    assert job is not None and job.kwargs == {"attempt": 1}
    assert "Попробую ещё раз" in sent[-1] and "конкурентов обойду после него" in sent[-1]
    # время цикла в БД не сдвинулось: при перезапуске сервиса цикл запустится заново
    db.expire_all()
    assert scheduler_module.get_schedule_config(db).next_run_at == saved_next_run


def test_cycle_crawls_competitors_after_last_failed_retry(db, monkeypatch, sent):
    competitors_started = []

    async def failed_run_now(_db, competitor, quiet=False):
        return False

    def fake_start_all(_db):
        competitors_started.append(1)
        return scheduler_module.crawl_manager.CrawlAllResult(started=1, skipped_running=0, skipped_paused=0)

    monkeypatch.setattr(scheduler_module.crawl_manager, "run_now", failed_run_now)
    monkeypatch.setattr(scheduler_module.crawl_manager, "start_all", fake_start_all)
    monkeypatch.setattr(scheduler_module.settings, "own_site_retry_attempts", 3)

    asyncio.run(scheduler_module.run_crawl_cycle(attempt=3))

    assert competitors_started == [1]
    assert scheduler_module.scheduler.get_job(scheduler_module.CYCLE_RETRY_JOB_ID) is None
    assert "с прошлым снимком" in sent[-1]


def test_daily_run_retries_own_site_that_did_not_open(db, monkeypatch, sent):
    async def failed_run_now(_db, competitor, quiet=False):
        return False

    monkeypatch.setattr(scheduler_module.crawl_manager, "run_now", failed_run_now)

    asyncio.run(scheduler_module.run_own_site_daily())

    job = scheduler_module.scheduler.get_job(scheduler_module.OWN_SITE_DAILY_RETRY_JOB_ID)
    assert job is not None and job.kwargs == {"attempt": 1}
    assert "Попробую ещё раз" in sent[-1]


def test_digest_runs_at_moscow_hour():
    scheduler_module.schedule_digest()
    try:
        job = scheduler_module.scheduler.get_job(scheduler_module.DIGEST_JOB_ID)
        assert str(job.trigger.timezone) == "Europe/Moscow"
    finally:
        scheduler_module.scheduler.remove_all_jobs()
