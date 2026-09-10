"""Запуск обходов: защита от повторного запуска и общий лимит параллельности."""

import asyncio

import pytest

from app import crawl_manager
from app.models import Competitor


class _FakeQuery:
    def __init__(self, items: list[Competitor]):
        self._items = items

    def order_by(self, *_args):
        return self

    def all(self) -> list[Competitor]:
        return self._items


class _FakeSession:
    """Достаточно для crawl_manager: он только читает конкурента и коммитит отметки."""

    def __init__(self, competitors: list[Competitor]):
        self._by_id = {c.id: c for c in competitors}
        self.commits = 0

    def get(self, _model, pk):
        return self._by_id.get(pk)

    def query(self, _model):
        return _FakeQuery(list(self._by_id.values()))

    def add(self, _obj):
        pass

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


def _competitor(competitor_id: int, *, is_paused: bool = False) -> Competitor:
    return Competitor(
        id=competitor_id,
        name=f"Конкурент {competitor_id}",
        base_url=f"https://x{competitor_id}.ru",
        is_paused=is_paused,
    )


def _install(monkeypatch, session: _FakeSession, crawl) -> None:
    monkeypatch.setattr(crawl_manager, "SessionLocal", lambda: session)
    monkeypatch.setattr(crawl_manager, "run_crawl_for_competitor", crawl)


async def _drain_background_tasks() -> None:
    while crawl_manager._background_tasks:
        await asyncio.gather(*list(crawl_manager._background_tasks), return_exceptions=True)


def test_claim_marks_crawl_started():
    competitor = _competitor(1)
    session = _FakeSession([competitor])

    assert crawl_manager.claim(session, competitor) is True
    assert competitor.is_crawling is True


def test_second_claim_is_rejected_while_crawl_is_running():
    # Реальный баг: быстрый повторный клик по кнопке запускал обход дважды.
    competitor = _competitor(1)
    session = _FakeSession([competitor])

    assert crawl_manager.claim(session, competitor) is True
    assert crawl_manager.claim(session, competitor) is False


def test_claim_allowed_again_after_crawl_finished():
    competitor = _competitor(1)
    session = _FakeSession([competitor])

    crawl_manager.claim(session, competitor)
    crawl_manager._release_if_still_crawling(session, competitor.id)

    assert competitor.is_crawling is False
    assert crawl_manager.claim(session, competitor) is True


@pytest.mark.asyncio
async def test_flag_is_released_when_crawl_raises(monkeypatch):
    competitor = _competitor(1)
    session = _FakeSession([competitor])

    async def boom(_db, _competitor):
        raise RuntimeError("краулер упал")

    _install(monkeypatch, session, boom)
    crawl_manager.claim(session, competitor)

    await crawl_manager._run_claimed(competitor.id)

    # Иначе конкурент навсегда залипает в статусе «обход идёт»
    assert competitor.is_crawling is False


@pytest.mark.asyncio
async def test_flag_is_released_when_pipeline_returns_early(monkeypatch):
    competitor = _competitor(1)
    session = _FakeSession([competitor])

    async def does_nothing(_db, _competitor):
        return None

    _install(monkeypatch, session, does_nothing)
    crawl_manager.claim(session, competitor)

    await crawl_manager._run_claimed(competitor.id)

    assert competitor.is_crawling is False


@pytest.mark.asyncio
async def test_start_all_respects_global_concurrency_limit(monkeypatch):
    monkeypatch.setattr(crawl_manager.settings, "crawl_concurrency", 2)

    running = 0
    peak = 0

    async def slow_crawl(_db, competitor):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1

    competitors = [_competitor(i) for i in range(1, 6)]
    session = _FakeSession(competitors)
    _install(monkeypatch, session, slow_crawl)

    result = crawl_manager.start_all(session)
    await _drain_background_tasks()

    assert result.started == 5
    assert peak == 2  # пять конкурентов, но одновременно работают только два


@pytest.mark.asyncio
async def test_start_all_runs_competitors_in_parallel(monkeypatch):
    monkeypatch.setattr(crawl_manager.settings, "crawl_concurrency", 3)

    running = 0
    peak = 0

    async def slow_crawl(_db, _competitor):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1

    session = _FakeSession([_competitor(i) for i in range(1, 4)])
    _install(monkeypatch, session, slow_crawl)

    crawl_manager.start_all(session)
    await _drain_background_tasks()

    assert peak == 3  # обходы идут одновременно, а не по очереди


@pytest.mark.asyncio
async def test_start_all_skips_paused_and_already_running(monkeypatch):
    running_one = _competitor(1)
    paused_one = _competitor(2, is_paused=True)
    fresh_one = _competitor(3)

    session = _FakeSession([running_one, paused_one, fresh_one])

    async def noop(_db, _competitor):
        return None

    _install(monkeypatch, session, noop)
    crawl_manager.claim(session, running_one)

    result = crawl_manager.start_all(session)
    await _drain_background_tasks()

    assert result.started == 1
    assert result.skipped_running == 1
    assert result.skipped_paused == 1


@pytest.mark.asyncio
async def test_start_marks_flag_before_returning(monkeypatch):
    competitor = _competitor(1)
    session = _FakeSession([competitor])
    started = asyncio.Event()

    async def slow_crawl(_db, _competitor):
        started.set()
        await asyncio.sleep(0.02)

    _install(monkeypatch, session, slow_crawl)

    assert crawl_manager.start(session, competitor) is True
    # Отметка стоит уже здесь, до того как фоновая задача успела стартовать —
    # именно это и не даёт второму клику проскочить.
    assert started.is_set() is False
    assert competitor.is_crawling is True
    assert crawl_manager.start(session, competitor) is False

    await _drain_background_tasks()


@pytest.mark.asyncio
async def test_run_now_skips_paused_competitor(monkeypatch):
    competitor = _competitor(1, is_paused=True)
    session = _FakeSession([competitor])

    async def never(_db, _competitor):  # pragma: no cover — вызываться не должен
        raise AssertionError("обход конкурента на паузе запускаться не должен")

    _install(monkeypatch, session, never)

    assert await crawl_manager.run_now(session, competitor) is False
    assert competitor.is_crawling is False
