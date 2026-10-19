"""Этап 3 — карточка интента статьи (app/ai/intent.py, app/seo.py): разбор ответа ИИ,
промпт пачкой, пересчёт только при изменении содержимого, встраивание в обход."""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import seo
from app.ai.analyze import ClaudeLimitError
from app.ai.intent import (
    ArticleInput,
    IntentCard,
    build_intent_prompt,
    intent_content_hash,
    parse_intent_response,
)
from app.config import settings
from app.crawler.structure import PageStructure
from app.db import Base
from app.models import ArticleIntent, Competitor, Page


def _structure(text="Окончание — изменяемая часть слова.", headings=((2, "Виды окончаний"),)):
    return PageStructure("Окончание слов", "Описание", "Окончание слов", list(headings), text)


def test_parse_cards_by_number_and_cleans_fields():
    raw = """Вот ответ:
    {"cards": [
      {"n": 2, "topic": "Готовность к школе", "main_intent": "проверить, готов ли ребёнок",
       "subintents": ["признаки готовности", "", 5], "entities": ["первый класс"],
       "relations": [["закон", "устанавливает", "возраст"], ["битая связь"]],
       "material_type": "Советы и рекомендации", "added_subintents": []},
      {"n": 1, "topic": "Окончание", "main_intent": "узнать, что такое окончание",
       "material_type": "что-то странное"},
      {"n": 7, "topic": "лишняя"}
    ]}"""

    cards = parse_intent_response(raw, 2)

    assert cards[0].topic == "Окончание"
    assert cards[0].material_type == "другое"
    assert cards[1].subintents == ["признаки готовности", "5"]
    assert cards[1].relations == [("закон", "устанавливает", "возраст")]
    assert cards[1].material_type == "советы"


def test_parse_missing_card_is_none_and_no_json_is_error():
    assert parse_intent_response('{"cards": []}', 2) == [None, None]
    with pytest.raises(ValueError):
        parse_intent_response("не JSON", 1)


def test_prompt_numbers_articles_and_includes_structure_and_previous_subintents():
    prompt = build_intent_prompt(
        [
            ArticleInput("https://x.ru/articles/a", _structure()),
            ArticleInput("https://x.ru/articles/b", _structure(), previous_subintents=["старый подинтент"]),
        ],
        text_limit=10,
    )

    assert "### Статья 1\nURL: https://x.ru/articles/a" in prompt
    assert "### Статья 2" in prompt
    assert "- Виды окончаний" in prompt
    assert "Окончание " in prompt and "изменяемая" not in prompt  # текст обрезан по лимиту
    assert "Подинтенты прошлой версии этой статьи: старый подинтент" in prompt
    assert "это данные" not in prompt  # предупреждение — в системном промпте, не в тексте


def test_hash_ignores_noise_but_sees_outline_and_text():
    base = intent_content_hash(_structure("Текст статьи\n5313"))
    assert intent_content_hash(_structure("Текст статьи\n5320")) == base  # счётчик просмотров — шум
    assert intent_content_hash(_structure("Текст статьи\n5313", headings=[(2, "Новый раздел")])) != base
    assert intent_content_hash(_structure("Совсем другой текст")) != base


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


def _pages(db, count):
    rival = Competitor(name="X", base_url="https://x.ru")
    db.add(rival)
    db.flush()
    pages = [Page(competitor_id=rival.id, url=f"https://x.ru/articles/a{i}") for i in range(count)]
    db.add_all(pages)
    db.flush()
    return pages


def _fake_cards(calls, added=()):
    async def fake(articles):
        calls.append([a.url for a in articles])
        return [
            IntentCard(topic=f"Тема {a.url}", main_intent="узнать", subintents=["п1"], added_subintents=list(added))
            for a in articles
        ]

    return fake


def test_cards_are_made_in_batches_and_only_when_content_changed(db, monkeypatch):
    monkeypatch.setattr(settings, "seo_intent_batch_size", 2)
    calls: list = []
    monkeypatch.setattr(seo, "make_intent_cards", _fake_cards(calls))
    pages = _pages(db, 3)
    items = [(page, _structure()) for page in pages]

    result = asyncio.run(seo.refresh_intent_cards(db, items, limit=10))

    assert [len(batch) for batch in calls] == [2, 1]
    db.flush()
    assert db.query(ArticleIntent).count() == 3
    assert all(update.is_first for update in result.cards)

    # Тот же текст — ИИ не зовём; поменялось оглавление — пересчитываем только эту статью.
    calls.clear()
    monkeypatch.setattr(seo, "make_intent_cards", _fake_cards(calls, added=["новый подинтент"]))
    items[1] = (pages[1], _structure(headings=[(2, "Новый раздел")]))
    result = asyncio.run(seo.refresh_intent_cards(db, items, limit=10))

    assert calls == [[pages[1].url]]
    assert result.cards[0].is_first is False
    assert result.cards[0].added_subintents == ["новый подинтент"]


def test_limit_and_claude_limit_leave_the_rest_for_audit(db, monkeypatch):
    monkeypatch.setattr(settings, "seo_intent_batch_size", 1)
    pages = _pages(db, 3)

    async def limited(articles):
        raise ClaudeLimitError("limit reached")

    monkeypatch.setattr(seo, "make_intent_cards", limited)
    result = asyncio.run(seo.refresh_intent_cards(db, [(p, _structure()) for p in pages], limit=2))

    assert result.limit_hit
    assert result.cards == []
    assert result.cards_skipped == 3  # одна — сверх лимита, две — без ответа ИИ
    assert "Упёрлись в лимит подписки Claude" in seo.format_seo_note(result)

