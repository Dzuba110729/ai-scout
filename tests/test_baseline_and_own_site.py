"""Первый обход — точка отсчёта, и итог обхода нашего сайта.

Весь run_crawl_for_competitor на временной SQLite: сам обход, ИИ, отчёт и Telegram
подменены, чтобы проверить, что именно пайплайн пишет в базу и что отправляет.
"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import bot_views, pipeline
from app.ai.analyze import AiAnalysisResult
from app.crawler.crawl import CrawlResult
from app.crawler.diff import ChangeType, PageDiff
from app.db import Base
from app.models import Competitor, PageChange, PageSnapshot, SessionStatus
from app.notifications.telegram import format_own_site_finished


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    state = {"pages": {}, "ai_calls": 0, "sent": [], "reports": []}

    async def fake_crawl(_db, competitor, _previous, _force_full):
        return CrawlResult(base_url=competitor.base_url, pages=dict(state["pages"]), urls_total=len(state["pages"]))

    async def fake_analyze(page_diff):
        state["ai_calls"] += 1
        return AiAnalysisResult(
            category=None, usp=None, cta=None, summary=f"Суть {page_diff.url}", raw_response={}, importance="medium"
        )

    async def fake_report(_db, _competitor, _run_at, rows, notes):
        state["reports"].append((rows, notes))
        return "https://docs/report"

    async def fake_notify(_notifier, _db, competitor, text, *, page_change_id, buttons=None):
        state["sent"].append((text, buttons))

    async def no_recheck(diffs, previous_by_url):
        return {}

    monkeypatch.setattr(pipeline, "_crawl_competitor", fake_crawl)
    monkeypatch.setattr(pipeline, "analyze_page_change", fake_analyze)
    monkeypatch.setattr(pipeline, "_write_run_report", fake_report)
    monkeypatch.setattr(pipeline, "_notify", fake_notify)
    monkeypatch.setattr(pipeline, "_recheck_missing_pages", no_recheck)
    monkeypatch.setattr(pipeline, "_backfill_missing_analyses", lambda *_: asyncio.sleep(0, result=(0, 0)))

    yield db, state
    db.close()


def _run(db, competitor):
    asyncio.run(pipeline.run_crawl_for_competitor(db, competitor))


def test_first_crawl_is_a_baseline_without_findings(env):
    db, state = env
    rival = Competitor(name="Фоксфорд", base_url="https://foxford.ru", status=SessionStatus.ACTIVE)
    db.add(rival)
    db.commit()
    state["pages"] = {f"https://foxford.ru/p{i}": f"текст {i}" for i in range(50)}

    _run(db, rival)

    assert db.query(PageChange).count() == 0
    assert db.query(PageSnapshot).count() == 50  # текст сохранён — следующему обходу есть с чем сравнить
    assert state["ai_calls"] == 0
    rows, notes = state["reports"][0]
    assert rows == []
    assert "точку отсчёта" in notes[-1]
    assert "Первый обход «Фоксфорд» завершён" in state["sent"][-1][0]


def test_second_crawl_reports_only_real_changes(env):
    db, state = env
    rival = Competitor(name="Фоксфорд", base_url="https://foxford.ru", status=SessionStatus.ACTIVE)
    db.add(rival)
    db.commit()
    state["pages"] = {"https://foxford.ru/a": "цена 4900", "https://foxford.ru/b": "о школе"}
    _run(db, rival)

    state["pages"] = {"https://foxford.ru/a": "цена 3900", "https://foxford.ru/b": "о школе", "https://foxford.ru/c": "новое"}
    _run(db, rival)

    kinds = sorted(c.change_type.value for c in db.query(PageChange).all())
    assert kinds == ["changed", "new"]
    assert state["ai_calls"] == 2
    assert "Изменений найдено: 2" in state["sent"][-1][0]


def test_own_site_crawl_reports_what_changed_on_our_site(env):
    db, state = env
    own = Competitor(name="Наш сайт", base_url="https://og1.ru", is_own=True, status=SessionStatus.ACTIVE)
    db.add(own)
    db.commit()

    state["pages"] = {"https://og1.ru/ege": "ЕГЭ", "https://og1.ru/oge": "ОГЭ"}
    _run(db, own)
    assert "Первый обход нашего сайта https://og1.ru завершён" in state["sent"][-1][0]

    state["pages"] = {"https://og1.ru/ege": "ЕГЭ со скидкой", "https://og1.ru/oge": "ОГЭ"}
    _run(db, own)

    text, buttons = state["sent"][-1]
    assert "Изменилось: ✏️ 1" in text
    assert "https://og1.ru/ege" in text
    assert "Суть https://og1.ru/ege" in text
    assert buttons[0][0]["callback_data"] == f"chg:c{own.id}:all:1:n"
    assert state["reports"] == []  # отчёт в Google Docs — только по конкурентам

    # изменения нашего сайта видны в его ленте, но не в общей ленте конкурентов
    assert bot_views.load_changes(db, competitor_id=own.id).total == 1
    assert bot_views.load_changes(db).total == 0


def test_own_site_without_changes():
    assert "ничего не изменилось" in format_own_site_finished("https://og1.ru", [])


def test_own_site_long_list_is_trimmed():
    items = [
        (PageDiff(url=f"https://og1.ru/p{i}", change_type=ChangeType.NEW, new_text="t"), None) for i in range(14)
    ]

    text = format_own_site_finished("https://og1.ru", items)

    assert "Изменилось: 🆕 14" in text
    assert "https://og1.ru/p9" in text and "https://og1.ru/p10" not in text
    assert "И ещё 4" in text


# ---------------------------------------------------------------- расписание

from datetime import UTC, datetime, timedelta  # noqa: E402

from app.scheduler import plan_next_run  # noqa: E402

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
WEEK = timedelta(days=7)


def test_restart_keeps_saved_schedule():
    # Раньше каждый перезапуск сервиса ставил «через неделю от сейчас» — и при
    # перезапусках чаще раза в неделю плановый обход не наступал никогда.
    saved = NOW + timedelta(days=3)

    assert plan_next_run(saved, WEEK, NOW, reset=False) == saved


def test_overdue_cycle_runs_soon_after_start():
    planned = plan_next_run(NOW - timedelta(days=2), WEEK, NOW, reset=False)

    assert NOW < planned <= NOW + timedelta(minutes=5)


def test_first_setup_and_interval_change_start_from_now():
    assert plan_next_run(None, WEEK, NOW, reset=False) == NOW + WEEK
    assert plan_next_run(NOW + timedelta(days=3), WEEK, NOW, reset=True) == NOW + WEEK
