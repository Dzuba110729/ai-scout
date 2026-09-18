"""Дозаправка ИИ-разбора для находок, оставшихся без него (app/ai/backfill.py).

Вызовы claude -p подменены заглушками: проверяем, что дозаправка берёт только
находки без разбора, уважает лимит и не теряет успехи при отказах CLI.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai import backfill as backfill_module
from app.ai.analyze import AiAnalysisResult, ClaudeCliError
from app.ai.backfill import backfill_missing_analyses, find_pending
from app.db import Base
from app.models import AiAnalysis, ChangeType, Competitor, Page, PageChange, PageSnapshot


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


def _competitor(db, name: str) -> Competitor:
    competitor = Competitor(name=name, base_url=f"https://{name}.ru")
    db.add(competitor)
    db.commit()
    return competitor


def _change(db, competitor: Competitor, url: str, *, analyzed: bool = False) -> PageChange:
    page = Page(competitor_id=competitor.id, url=url, title=url)
    db.add(page)
    db.flush()
    snapshot = PageSnapshot(page_id=page.id, content_hash="h", text_content=f"текст {url}")
    db.add(snapshot)
    db.flush()
    change = PageChange(page_id=page.id, change_type=ChangeType.NEW, new_snapshot_id=snapshot.id)
    db.add(change)
    db.flush()
    if analyzed:
        db.add(AiAnalysis(page_change_id=change.id, category="оффер", raw_response={}))
    db.commit()
    return change


def _ok(url: str) -> AiAnalysisResult:
    return AiAnalysisResult(category="оффер", usp=None, cta=None, summary=url, raw_response={"url": url})


def test_find_pending_returns_only_changes_without_analysis(db):
    rival = _competitor(db, "rival")
    other = _competitor(db, "other")
    _change(db, rival, "https://rival.ru/a", analyzed=True)
    missing = _change(db, rival, "https://rival.ru/b")
    _change(db, other, "https://other.ru/c")

    pending = find_pending(db, rival.id)

    assert [change.id for change, _page in pending] == [missing.id]
    assert len(find_pending(db)) == 2


@pytest.mark.asyncio
async def test_backfill_stores_analysis_for_pending_changes(db, monkeypatch):
    rival = _competitor(db, "rival")
    change = _change(db, rival, "https://rival.ru/b")
    seen: list[str] = []

    async def fake_analyze(diff):
        seen.append(diff.url)
        return _ok(diff.url)

    monkeypatch.setattr(backfill_module, "analyze_page_change", fake_analyze)

    done, attempted = await backfill_missing_analyses(db, rival.id)

    assert (done, attempted) == (1, 1)
    assert seen == ["https://rival.ru/b"]
    assert db.query(AiAnalysis).filter_by(page_change_id=change.id).one().summary == "https://rival.ru/b"
    assert find_pending(db, rival.id) == []


@pytest.mark.asyncio
async def test_backfill_keeps_successes_when_cli_fails_for_some(db, monkeypatch):
    rival = _competitor(db, "rival")
    _change(db, rival, "https://rival.ru/ok")
    _change(db, rival, "https://rival.ru/fail")

    async def fake_analyze(diff):
        if diff.url.endswith("fail"):
            raise ClaudeCliError("CLI недоступен")
        return _ok(diff.url)

    monkeypatch.setattr(backfill_module, "analyze_page_change", fake_analyze)

    done, attempted = await backfill_missing_analyses(db, rival.id)

    assert (done, attempted) == (1, 2)
    assert [page.url for _change, page in find_pending(db, rival.id)] == ["https://rival.ru/fail"]


@pytest.mark.asyncio
async def test_backfill_limit_caps_attempts_and_takes_oldest_first(db, monkeypatch):
    rival = _competitor(db, "rival")
    for i in range(5):
        _change(db, rival, f"https://rival.ru/p{i}")
    seen: list[str] = []

    async def fake_analyze(diff):
        seen.append(diff.url)
        return _ok(diff.url)

    monkeypatch.setattr(backfill_module, "analyze_page_change", fake_analyze)

    done, attempted = await backfill_missing_analyses(db, rival.id, limit=2)

    assert (done, attempted) == (2, 2)
    assert sorted(seen) == ["https://rival.ru/p0", "https://rival.ru/p1"]
    assert len(find_pending(db, rival.id)) == 3


@pytest.mark.asyncio
async def test_backfill_with_nothing_pending_does_not_call_cli(db, monkeypatch):
    rival = _competitor(db, "rival")
    _change(db, rival, "https://rival.ru/a", analyzed=True)

    async def fake_analyze(diff):
        raise AssertionError("CLI не должен вызываться")

    monkeypatch.setattr(backfill_module, "analyze_page_change", fake_analyze)

    assert await backfill_missing_analyses(db, rival.id) == (0, 0)
