"""Этап 6 — стартовый аудит (app/seo_audit.py): очередь, продолжение после прерывания,
потолок вызовов ИИ, пауза при лимите подписки, ночное окно, оценка объёма."""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import seo, seo_audit
from app.ai import analyze
from app.ai.intent import IntentCard
from app.ai.topic_match import MatchResult
from app.config import settings
from app.db import Base
from app.models import ArticleIntent, Competitor, Page, PageStructureRecord, SeoFinding

TEXT = "Окончание — изменяемая часть слова. " * 20


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


def _site(db, name, *, is_own=False, include_paths=None):
    site = Competitor(name=name, base_url=f"https://{name}", is_own=is_own, include_paths=include_paths)
    db.add(site)
    db.flush()
    return site


def _article(db, site, path, *, text=TEXT, structure=True):
    page = Page(competitor_id=site.id, url=f"https://{site.name}{path}")
    db.add(page)
    db.flush()
    if structure:
        db.add(PageStructureRecord(page_id=page.id, h1=path, headings=[[2, "Раздел"]], main_text=text))
        db.flush()
    return page


@pytest.fixture
def sites(db):
    own = _site(db, "og1.ru", is_own=True)
    _article(db, own, "/polza/okonchanie")
    rival = _site(db, "skysmart.ru", include_paths="!/vpr")
    for i in range(5):
        _article(db, rival, f"/articles/a{i}")
    _article(db, rival, "/articles/no-structure", structure=False)
    _article(db, rival, "/articles/empty", text="мало")
    _article(db, rival, "/vpr/excluded")
    _article(db, rival, "/courses/ege")
    db.commit()
    return own, rival


def _fake_ai(monkeypatch, calls):
    async def cards(articles):
        calls.append("cards")
        return [IntentCard(topic=f"Тема {a.url}", main_intent=f"узнать {a.url}") for a in articles]

    async def match(cases):
        calls.append("match")
        return [MatchResult("covered", c[0].url, "full", [], "есть", [], {}) for _a, c in cases]

    monkeypatch.setattr(seo, "make_intent_cards", cards)
    monkeypatch.setattr(seo, "match_articles", match)
    monkeypatch.setattr(settings, "seo_topic_merge_threshold", 1.1)  # в тесте темы не склеиваем


def _run(db, rival, **kwargs):
    kwargs.setdefault("max_calls", 100)
    kwargs.setdefault("on_progress", lambda _msg: None)
    return asyncio.run(seo_audit.run_audit(db, [rival], **kwargs))


def test_queue_takes_only_tracked_articles_with_text(db, sites):
    _own, rival = sites

    queue = [page.url.rsplit("/", 1)[-1] for page in seo_audit.audit_queue(db, rival)]

    assert queue == ["a0", "a1", "a2", "a3", "a4"]


def test_audit_records_findings_and_continues_where_it_stopped(db, sites, monkeypatch):
    _own, rival = sites
    calls: list = []
    _fake_ai(monkeypatch, calls)
    monkeypatch.setattr(settings, "seo_intent_batch_size", 2)
    monkeypatch.setattr(settings, "seo_compare_batch_size", 2)

    first = _run(db, rival, portion=2, max_calls=2)  # одна порция: 1 вызов карточек + 1 сравнения

    assert first.stopped_by_calls and first.processed == 2
    assert db.query(SeoFinding).filter(SeoFinding.kind == "audit").count() == 2

    second = _run(db, rival, portion=2)

    assert second.processed == 3
    assert db.query(SeoFinding).count() == 5
    assert seo_audit.audit_queue(db, rival) == []


def test_article_with_card_but_without_decision_is_compared_without_new_card(db, sites, monkeypatch):
    _own, rival = sites
    page = seo_audit.audit_queue(db, rival)[0]
    db.add(ArticleIntent(page_id=page.id, content_hash=seo.intent_content_hash(seo_audit.structure_from_record(page.structure)), topic="T", main_intent="M"))
    db.commit()
    calls: list = []
    _fake_ai(monkeypatch, calls)

    _run(db, rival, portion=1, max_articles=1)

    assert calls == ["match"]  # карточка уже была — ИИ только для решения


def test_limit_of_subscription_pauses_and_goes_on(db, sites, monkeypatch):
    _own, rival = sites
    calls: list = []
    _fake_ai(monkeypatch, calls)
    real_cards = seo.make_intent_cards
    state = {"limited": True}

    async def cards_with_limit(articles):
        if state["limited"]:
            state["limited"] = False
            analyze._start_limit_pause()
            raise analyze.ClaudeLimitError("limit reached")
        return await real_cards(articles)

    monkeypatch.setattr(seo, "make_intent_cards", cards_with_limit)
    # После теста пауза должна снова быть нулевой — иначе соседние тесты «ждут лимит».
    monkeypatch.setattr(analyze, "_limit_pause_until", 0.0)
    slept: list = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        analyze._limit_pause_until = 0.0

    stats = _run(db, rival, portion=5, sleep=fake_sleep)

    assert stats.limit_pauses == 1 and slept
    assert stats.processed == 5


def test_night_mode_waits_outside_the_window(db, sites, monkeypatch):
    _own, rival = sites
    _fake_ai(monkeypatch, [])
    moments = iter([datetime(2026, 10, 19, 12, tzinfo=UTC), datetime(2026, 10, 19, 23, tzinfo=UTC)])
    slept: list = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    # 12:00 UTC = 15:00 МСК — вне окна 1–7; 23:00 UTC = 02:00 МСК — внутри.
    stats = _run(db, rival, portion=5, night=(1, 7), sleep=fake_sleep, now=lambda: next(moments, datetime(2026, 10, 19, 23, tzinfo=UTC)))

    assert slept == [15 * 60]
    assert stats.processed == 5


def test_estimate_counts_ready_articles_and_calls(db, sites, monkeypatch):
    monkeypatch.setattr(settings, "seo_intent_batch_size", 4)
    monkeypatch.setattr(settings, "seo_compare_batch_size", 3)

    (row,) = seo_audit.estimate_audit(db, merge_share=0.0)

    assert (row.articles, row.ready, row.without_structure, row.done) == (7, 6, 1, 0)
    assert row.calls == 2 + 2  # 6 карточек по 4 + 6 сравнений по 3
