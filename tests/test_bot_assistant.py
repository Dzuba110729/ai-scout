"""Telegram-бот: разбор ответа ИИ-помощника, экраны и выполнение действий.

ИИ здесь не вызывается — проверяем то, что вокруг него: что из ответа модели
выполняется, а что отбрасывается, и что экраны бота собираются из данных БД
(временная SQLite в памяти) без нашего сайта в списках и лентах.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import bot_views, telegram_bot
from app.ai import assistant
from app.ai.assistant import AssistantAction, build_prompt, parse_reply
from app.competitor_ops import normalize_url
from app.db import Base
from app.models import AiAnalysis, ChangeType, Competitor, Page, PageChange, SessionStatus
from app.notifications.telegram import report_buttons

NOW = datetime.now(UTC)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    rival = Competitor(
        name="Фоксфорд",
        base_url="https://foxford.ru",
        status=SessionStatus.ACTIVE,
        last_report_url="https://docs.google.com/document/d/abc/edit",
        last_report_at=NOW - timedelta(days=1),
    )
    own = Competitor(name="Наш сайт", base_url="https://og1.ru", is_own=True, status=SessionStatus.ACTIVE)
    session.add_all([rival, own])
    session.flush()

    kinds = [ChangeType.NEW, ChangeType.CHANGED, ChangeType.REMOVED] * 4  # 12 находок у конкурента
    for i, kind in enumerate(kinds):
        page = Page(competitor_id=rival.id, url=f"https://foxford.ru/p{i}", title=f"Страница {i}")
        session.add(page)
        session.flush()
        change = PageChange(page_id=page.id, change_type=kind, detected_at=NOW - timedelta(hours=i))
        session.add(change)
        session.flush()
        session.add(AiAnalysis(page_change_id=change.id, summary=f"Суть находки {i}"))

    own_page = Page(competitor_id=own.id, url="https://og1.ru/ege")
    session.add(own_page)
    session.flush()
    session.add(PageChange(page_id=own_page.id, change_type=ChangeType.NEW, detected_at=NOW))
    session.commit()

    yield session
    session.close()


# ---------------------------------------------------------------- ответ модели


def test_reply_with_known_action_is_accepted():
    reply = parse_reply('Запускаю. {"reply": "Запускаю обход", "action": {"type": "crawl", "competitor_id": 3}}')

    assert reply.text == "Запускаю обход"
    assert reply.action == AssistantAction("crawl", {"competitor_id": 3})


def test_unknown_action_is_dropped_not_executed():
    reply = parse_reply('{"reply": "Готово", "action": {"type": "drop_database"}}')

    assert reply.action is None
    assert reply.text == "Готово"


def test_action_without_required_args_is_dropped():
    reply = parse_reply('{"reply": "Добавляю", "action": {"type": "add_competitor", "name": "Умскул"}}')

    assert reply.action is None


def test_empty_reply_is_an_error():
    with pytest.raises(ValueError):
        parse_reply('{"reply": "", "action": null}')


def test_prompt_marks_site_data_and_keeps_history():
    prompt = build_prompt("Конкуренты: ...", [("что нового?", "Две новые страницы")], "а у фоксфорда?")

    assert "<<<" in prompt and ">>>" in prompt
    assert "Пользователь: что нового?" in prompt
    assert prompt.rstrip().endswith("а у фоксфорда?")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("foxford.ru", "https://foxford.ru"),
        (" https://foxford.ru/ ", "https://foxford.ru/"),
        ("http://x.ru", "http://x.ru"),
    ],
)
def test_normalize_url_accepts_bare_domains(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "фоксфорд", "ftp://x.ru", "foxford ru"])
def test_normalize_url_rejects_non_urls(raw):
    with pytest.raises(ValueError):
        normalize_url(raw)


def test_report_buttons_link_document_folder_and_feed():
    rows = report_buttons(7, "https://docs/doc", "https://drive/folder")

    assert rows[0] == [{"text": "📄 Открыть отчёт", "url": "https://docs/doc"}]
    assert rows[1] == [{"text": "📁 Все отчёты", "url": "https://drive/folder"}]
    assert rows[2][0]["callback_data"] == "chg:c7:all:1:n"


def test_report_buttons_without_report_still_offer_feed():
    rows = report_buttons(7, None, None)

    assert len(rows) == 1
    assert rows[0][0]["callback_data"].startswith("chg:c7")


# ---------------------------------------------------------------- экраны


def test_changes_feed_skips_own_site_and_paginates(db):
    first = bot_views.load_changes(db, page=1, page_size=5)

    assert first.total == 12  # находка нашего сайта в ленту не попала
    assert first.total_pages == 3
    assert all(not c.page.competitor.is_own for c in first.items)
    assert first.items[0].page.url == "https://foxford.ru/p0"  # свежие сверху


def test_changes_feed_filters_by_kind(db):
    result = bot_views.load_changes(db, kind="removed", page_size=50)

    assert result.total == 4
    assert {c.change_type for c in result.items} == {ChangeType.REMOVED}


def test_page_beyond_last_shows_last_page(db):
    result = bot_views.load_changes(db, page=99, page_size=5)

    assert result.page == 3


def test_summary_and_card_render(db):
    summary = bot_views.summary_text(db)
    rival = db.query(Competitor).filter_by(is_own=False).one()
    card = bot_views.competitor_card_text(db, rival)

    assert "Конкурентов: 1" in summary
    assert "https://og1.ru" in summary
    assert "Фоксфорд" in card
    assert "Последний отчёт" in card


def test_assistant_context_has_ids_and_report_links(db):
    context = assistant.build_context(db)
    rival = db.query(Competitor).filter_by(is_own=False).one()

    assert f"id={rival.id}; Фоксфорд" in context
    assert "https://docs.google.com/document/d/abc/edit" in context
    assert "Суть находки 0" in context
    assert "og1.ru/ege" not in context.split("Находки за 30 дней")[1]


def test_all_callback_data_fits_telegram_limit(db):
    rival = db.query(Competitor).filter_by(is_own=False).one()
    screens = [
        telegram_bot._changes_screen(db, f"c{rival.id}", "changed", 1),
        telegram_bot._changes_screen(db, "all", "all", 2),
        telegram_bot._competitor_screen(db, rival),
        telegram_bot._settings_screen(db),
    ]
    for _text, markup in screens:
        for row in markup.inline_keyboard:
            for button in row:
                if button.callback_data:
                    assert len(button.callback_data.encode()) <= 64


# ---------------------------------------------------------------- действия


def test_crawl_action_starts_crawl(db, monkeypatch):
    started = []
    monkeypatch.setattr(telegram_bot.crawl_manager, "start", lambda _db, c: started.append(c.name) or True)
    rival = db.query(Competitor).filter_by(is_own=False).one()

    text, _ = asyncio.run(telegram_bot._execute(db, AssistantAction("crawl", {"competitor_id": rival.id})))

    assert started == ["Фоксфорд"]
    assert "ссылку на отчёт" in text


def test_action_on_own_site_or_missing_competitor_is_refused(db, monkeypatch):
    monkeypatch.setattr(telegram_bot.crawl_manager, "start", lambda *_: pytest.fail("не должен запускаться"))
    own = db.query(Competitor).filter_by(is_own=True).one()

    for competitor_id in (own.id, 999, "abc"):
        text, _ = asyncio.run(telegram_bot._execute(db, AssistantAction("crawl", {"competitor_id": competitor_id})))
        assert "Не нашёл" in text


def test_delete_action_only_asks_for_confirmation(db):
    rival = db.query(Competitor).filter_by(is_own=False).one()

    text, markup = asyncio.run(
        telegram_bot._execute(db, AssistantAction("delete_competitor", {"competitor_id": rival.id}))
    )

    assert db.get(Competitor, rival.id) is not None
    assert "Удалить" in text
    assert markup.inline_keyboard[0][0].callback_data == f"comp:{rival.id}:delete_yes"


@pytest.mark.parametrize(("days", "hours"), [(0, 0), (365, 0), (-1, 5)])
def test_bad_schedule_is_not_applied(db, days, hours):
    text, _ = asyncio.run(telegram_bot._execute(db, AssistantAction("set_schedule", {"days": days, "hours": hours})))

    assert text.startswith("Не поменял")
    config = telegram_bot.bot_views.get_schedule_config(db)
    assert (config.interval_days, config.interval_hours) == (7, 0)
