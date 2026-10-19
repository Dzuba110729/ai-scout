"""Этап 4 — статья конкурента против наших статей (app/ai/topic_match.py, app/seo.py):
кандидаты без ИИ, разбор решения ИИ, склейка одной темы у разных конкурентов,
изменённые статьи — только с новыми подинтентами."""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import seo
from app.ai.intent import IntentCard
from app.ai.topic_match import (
    CompetitorArticle,
    MatchResult,
    OwnArticle,
    OwnArticlesIndex,
    TextIndex,
    build_match_prompt,
    parse_match_response,
)
from app.config import settings
from app.crawler.structure import PageStructure
from app.db import Base
from app.models import Competitor, Page, PageStructureRecord, SeoFinding, TopicDecision

OKONCHANIE = IntentCard(
    topic="Окончание слова в русском языке",
    main_intent="узнать, что такое окончание слова и как его найти",
    subintents=["виды окончаний", "нулевое окончание", "как определить окончание"],
)
GOTOVNOST = IntentCard(
    topic="Готовность ребёнка к школе",
    main_intent="проверить, готов ли ребёнок к первому классу",
    subintents=["признаки готовности", "возраст поступления"],
)


def _own(url, title, text):
    return OwnArticle(url=url, title=title, headings=[(2, title)], text=text)


def _article(url, card, added=None):
    return CompetitorArticle(url=url, h1=card.topic, card=card, text=card.main_intent, added_subintents=added)


def test_text_index_finds_closest_documents():
    index = TextIndex([("Окончание слова", "окончание морфема"), ("Готовность к школе", "первый класс ребёнок")])

    assert [n for n, _s in index.search("Нулевое окончание", "окончание слова", 2)] == [0]
    assert index.search("Погода", "дождь", 2) == []


def test_candidates_come_from_own_articles_by_meaning_words():
    own = [
        _own("https://og1.ru/polza/okonchanie", "Что такое окончание слова", "окончание изменяемая часть слова"),
        _own("https://og1.ru/polza/shkola", "Готов ли ребёнок к школе", "первый класс готовность"),
    ]

    candidates = OwnArticlesIndex(own).candidates(_article("https://x.ru/a", OKONCHANIE), 3)

    assert candidates[0].url == "https://og1.ru/polza/okonchanie"


def test_parse_keeps_only_our_candidate_urls_and_fixes_inconsistent_answers():
    own = _own("https://og1.ru/polza/okonchanie", "Окончание", "текст")
    cases = [(_article("https://x.ru/a", OKONCHANIE), [own]), (_article("https://x.ru/b", GOTOVNOST), [own])]
    raw = """{"results": [
      {"n": 1, "decision": "update", "our_url": "https://og1.ru/polza/okonchanie", "coverage": "partial",
       "add_subintents": ["двойные окончания"], "argument": "Нет раздела про двойные окончания.",
       "cannibalization_urls": ["https://og1.ru/polza/okonchanie", "https://og1.ru/vydumannaya"]},
      {"n": 2, "decision": "update", "our_url": "https://og1.ru/vydumannaya", "add_subintents": ["x"]}
    ]}"""

    first, second = parse_match_response(raw, cases)

    assert (first.decision, first.our_url, first.add_subintents) == (
        "update",
        "https://og1.ru/polza/okonchanie",
        ["двойные окончания"],
    )
    assert first.cannibalization_urls == ["https://og1.ru/polza/okonchanie"]
    # «обновить» несуществующую у нас статью — значит, писать новую
    assert (second.decision, second.our_url, second.coverage) == ("new", None, "none")


def test_prompt_shows_competitor_intent_candidates_and_added_subintents():
    own = _own("https://og1.ru/polza/okonchanie", "Окончание", "Окончание — часть слова")
    prompt = build_match_prompt([(_article("https://x.ru/a", OKONCHANIE, added=["двойные окончания"]), [own])])

    assert "### Статья конкурента 1" in prompt
    assert "Основной интент: узнать, что такое окончание" in prompt
    assert "добавлены подинтенты: двойные окончания" in prompt
    assert "- Наша статья https://og1.ru/polza/okonchanie" in prompt


# --- склейка и запись решений в базу ---


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


def _site(db, name, *, is_own=False):
    site = Competitor(name=name, base_url=f"https://{name}", is_own=is_own)
    db.add(site)
    db.flush()
    return site


def _page(db, site, path, structure=None):
    page = Page(competitor_id=site.id, url=f"https://{site.name}{path}")
    db.add(page)
    db.flush()
    if structure:
        db.add(
            PageStructureRecord(
                page_id=page.id,
                h1=structure.h1,
                headings=[list(h) for h in structure.headings],
                main_text=structure.main_text,
                main_text_length=len(structure.main_text),
            )
        )
        db.flush()
    return page


def _update(page, card, *, is_first=True, added=()):
    return seo.CardUpdate(
        page=page, structure=PageStructure(h1=card.topic, main_text=card.main_intent), card=card,
        is_first=is_first, added_subintents=list(added),
    )


def _fake_match(calls, decision="update"):
    async def fake(cases):
        calls.append([article.url for article, _candidates in cases])
        return [
            MatchResult(decision, candidates[0].url, "partial", ["двойные окончания"], "аргумент", [], {})
            for _article, candidates in cases
        ]

    return fake


def _process(db, site, updates, kinds=None):
    cases = seo.build_cases(db, site.id, updates, kinds=kinds or {}, change_ids={})
    own_index = OwnArticlesIndex(seo.load_own_articles(db))
    return asyncio.run(seo.match_cards(db, cases, own_index, limit=10))


def test_same_topic_at_two_competitors_is_one_decision_and_one_ai_call(db, monkeypatch):
    own = _site(db, "og1.ru", is_own=True)
    _page(db, own, "/polza/okonchanie", PageStructure(h1="Окончание слова", main_text="Окончание — часть слова"))
    sky, fox = _site(db, "skysmart.ru"), _site(db, "foxford.ru")
    calls: list = []
    monkeypatch.setattr(seo, "match_articles", _fake_match(calls))

    first = _process(db, sky, [_update(_page(db, sky, "/articles/okonchanie"), OKONCHANIE)])
    second = _process(db, fox, [_update(_page(db, fox, "/wiki/okonchanie"), OKONCHANIE)])

    assert calls == [["https://skysmart.ru/articles/okonchanie"]]  # вторая — склейка без ИИ
    assert first.decisions == {"update": 1} and second.merged == 1
    decision = db.query(TopicDecision).one()
    assert (decision.decision, decision.our_url) == ("update", "https://og1.ru/polza/okonchanie")
    assert sorted(f.competitor_id for f in decision.findings) == [sky.id, fox.id]


def test_two_articles_of_one_site_are_two_topics_and_unrelated_topic_without_candidates_is_new(db, monkeypatch):
    own = _site(db, "og1.ru", is_own=True)
    _page(db, own, "/polza/okonchanie", PageStructure(h1="Окончание слова", main_text="Окончание — часть слова"))
    sky = _site(db, "skysmart.ru")
    calls: list = []
    monkeypatch.setattr(seo, "match_articles", _fake_match(calls))
    weather = IntentCard(topic="Погода летом", main_intent="узнать прогноз", subintents=["дождь"])

    result = _process(
        db,
        sky,
        [
            _update(_page(db, sky, "/articles/a"), OKONCHANIE),
            _update(_page(db, sky, "/articles/b"), OKONCHANIE),
            _update(_page(db, sky, "/articles/c"), weather),
        ],
    )

    # Две статьи одного сайта на одну тему — не склейка (это две темы у конкурента).
    assert calls == [["https://skysmart.ru/articles/a", "https://skysmart.ru/articles/b"]]
    assert result.decisions == {"update": 2, "new": 1} and result.merged == 0
    decisions = db.query(TopicDecision).all()
    assert [d.decision for d in decisions if d.topic == "Погода летом"] == ["new"]  # похожих у нас нет — без ИИ
    assert sum(1 for d in decisions if d.topic == OKONCHANIE.topic) == 2


def test_known_topic_of_the_same_site_is_not_merged_again(db, monkeypatch):
    own = _site(db, "og1.ru", is_own=True)
    _page(db, own, "/polza/okonchanie", PageStructure(h1="Окончание слова", main_text="Окончание — часть слова"))
    sky = _site(db, "skysmart.ru")
    calls: list = []
    monkeypatch.setattr(seo, "match_articles", _fake_match(calls))

    _process(db, sky, [_update(_page(db, sky, "/articles/a"), OKONCHANIE)])
    second = _process(db, sky, [_update(_page(db, sky, "/articles/a-copy"), OKONCHANIE)])

    assert len(calls) == 2 and second.merged == 0
    assert db.query(TopicDecision).count() == 2


def test_changed_article_is_compared_again_only_with_new_subintents(db, monkeypatch):
    own = _site(db, "og1.ru", is_own=True)
    _page(db, own, "/polza/okonchanie", PageStructure(h1="Окончание слова", main_text="Окончание — часть слова"))
    sky = _site(db, "skysmart.ru")
    page = _page(db, sky, "/articles/okonchanie")
    calls: list = []
    monkeypatch.setattr(seo, "match_articles", _fake_match(calls, decision="covered"))
    _process(db, sky, [_update(page, OKONCHANIE)])

    nothing_new = _process(db, sky, [_update(page, OKONCHANIE, is_first=False)], kinds={page.url: "changed"})
    assert nothing_new.findings == [] and len(calls) == 1

    monkeypatch.setattr(seo, "match_articles", _fake_match(calls, decision="update"))
    result = _process(
        db, sky, [_update(page, OKONCHANIE, is_first=False, added=["двойные окончания"])], kinds={page.url: "changed"}
    )

    assert len(calls) == 2
    assert result.decisions == {"update": 1}
    finding = db.query(SeoFinding).order_by(SeoFinding.id.desc()).first()
    assert (finding.kind, finding.added_subintents) == ("changed", ["двойные окончания"])
    assert db.query(TopicDecision).one().decision == "update"  # та же тема, решение обновлено


def test_limit_and_silent_ai_leave_articles_for_audit(db, monkeypatch):
    own = _site(db, "og1.ru", is_own=True)
    _page(db, own, "/polza/okonchanie", PageStructure(h1="Окончание слова", main_text="Окончание — часть слова"))
    _page(db, own, "/polza/shkola", PageStructure(h1="Готовность к школе", main_text="первый класс готовность"))
    sky = _site(db, "skysmart.ru")
    cases = seo.build_cases(
        db,
        sky.id,
        [_update(_page(db, sky, "/articles/a"), OKONCHANIE), _update(_page(db, sky, "/articles/b"), GOTOVNOST)],
        kinds={},
        change_ids={},
    )
    monkeypatch.setattr(settings, "seo_compare_batch_size", 1)

    result = asyncio.run(seo.match_cards(db, cases, OwnArticlesIndex(seo.load_own_articles(db)), limit=1))

    assert result.not_compared == 2  # одна сверх лимита, вторая — ИИ промолчал (conftest)
    assert db.query(SeoFinding).count() == 0
    assert "Не успели — 2" in seo.format_seo_note(result)
