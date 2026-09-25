"""Куки из Cookie-Editor для защищённых сайтов и фильтр разделов сайта (foxford.ru)."""

import json
import os

import pytest

from app.crawler import crawl as crawl_module
from app.crawler.cookies import (
    CookieExportError,
    parse_cookie_export,
    save_storage_state,
    site_matches_cookies,
)
from app.crawler.crawl import SitemapEntry, crawl_competitor, parse_include_paths, path_included
from app.notifications.telegram import format_blocked_message

_COOKIE = {"domain": ".foxford.ru", "name": "qrator_jsid", "value": "секрет", "path": "/", "session": True}


def test_parses_cookie_editor_json_even_with_smart_quotes():
    raw = json.dumps([_COOKIE], ensure_ascii=False).replace('"', "“", 1).encode()

    cookies = parse_cookie_export(raw)

    assert cookies[0]["name"] == "qrator_jsid"


@pytest.mark.parametrize("raw", [b"hello", b"{}", b"[]", b'[{"name": "x"}]'])
def test_rejects_anything_that_is_not_a_cookie_export(raw):
    with pytest.raises(CookieExportError):
        parse_cookie_export(raw)


def test_cookies_are_matched_to_the_competitor_site():
    assert site_matches_cookies("https://foxford.ru/", [_COOKIE])
    assert site_matches_cookies("https://www.foxford.ru/", [_COOKIE])
    assert not site_matches_cookies("https://skysmart.ru/", [_COOKIE])
    assert not site_matches_cookies("https://notfoxford.ru/", [_COOKIE])


def test_saved_session_is_readable_only_by_owner(tmp_path):
    path = tmp_path / "competitor_4.json"

    assert save_storage_state([_COOKIE], path) == 1

    assert os.stat(path).st_mode & 0o777 == 0o600
    state = json.loads(path.read_text())
    assert state["cookies"][0]["expires"] == -1  # сессионная кука


def test_include_paths_parsing_and_matching():
    prefixes = parse_include_paths("/catalog, courses/\n/podgotovka-*")

    assert prefixes == ["/catalog", "/courses", "/podgotovka-*"]
    assert path_included("https://x.ru/", prefixes)  # главная — всегда
    assert path_included("https://x.ru/catalog/courses/fizika", prefixes)
    assert path_included("https://x.ru/courses", prefixes)
    assert not path_included("https://x.ru/courses-old", prefixes)
    assert path_included("https://x.ru/podgotovka-oge-biologiya", prefixes)
    assert not path_included("https://x.ru/entrance/msu", prefixes)
    assert path_included("https://x.ru/entrance/msu", [])  # без фильтра — весь сайт


@pytest.mark.asyncio
async def test_crawl_takes_only_selected_sections(monkeypatch):
    async def fake_discover(base_url, sitemap_url=None):
        return [
            SitemapEntry(url="https://x.ru/catalog/a", lastmod=None),
            SitemapEntry(url="https://x.ru/entrance/1", lastmod=None),
            SitemapEntry(url="https://x.ru/entrance/2", lastmod=None),
        ]

    async def fake_fetch(context, url):
        return "заголовок", f"текст {url}"

    monkeypatch.setattr(crawl_module, "discover_sitemap_entries", fake_discover)
    monkeypatch.setattr(crawl_module, "fetch_page_text", fake_fetch)
    monkeypatch.setattr(crawl_module.settings, "crawl_request_delay_seconds", 0)

    result = await crawl_competitor(
        context=None, base_url="https://x.ru", force_full=True, include_paths=["/catalog"]
    )

    assert list(result.pages) == ["https://x.ru/catalog/a"]
    assert result.urls_total == 1  # SEO-разделы не считаются «необойдённой частью сайта»


def test_blocked_message_for_cookie_site_asks_for_fresh_cookies():
    text = format_blocked_message("foxford.ru", "https://foxford.ru/", "HTTP 401", cookies_only=True)

    assert "Cookie-Editor" in text
    assert "APIFY" not in text


class _FakeMessage:
    def __init__(self):
        self.answers: list[str] = []
        self.deleted = False

    async def answer(self, text, **_kwargs):
        self.answers.append(text)

    async def delete(self):
        self.deleted = True


@pytest.fixture
def bot_db(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app import telegram_bot
    from app.db import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    make_session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    started: list[str] = []
    monkeypatch.setattr(telegram_bot, "SessionLocal", make_session)
    monkeypatch.setattr(telegram_bot, "STORAGE_STATE_DIR", tmp_path)
    monkeypatch.setattr(telegram_bot.crawl_manager, "start", lambda _db, c: started.append(c.name) or True)
    return make_session, started, tmp_path


@pytest.mark.asyncio
async def test_bot_saves_cookies_deletes_message_and_starts_crawl(bot_db):
    from app import telegram_bot
    from app.models import Competitor

    make_session, started, storage_dir = bot_db
    db = make_session()
    db.add(Competitor(name="Скайсмарт", base_url="https://skysmart.ru/"))
    db.add(Competitor(name="Фоксфорд", base_url="https://foxford.ru/", cookies_only=True))
    db.commit()
    foxford_id = db.query(Competitor).filter_by(name="Фоксфорд").one().id
    db.close()
    message = _FakeMessage()

    await telegram_bot._accept_cookies(message, json.dumps([_COOKIE]).encode())

    assert message.deleted
    assert started == ["Фоксфорд"]
    assert (storage_dir / f"competitor_{foxford_id}.json").exists()
    assert "секрет" not in message.answers[-1]


@pytest.mark.asyncio
async def test_bot_explains_when_cookies_are_from_unknown_site(bot_db):
    from app import telegram_bot

    _make_session, started, _dir = bot_db
    message = _FakeMessage()

    await telegram_bot._accept_cookies(message, json.dumps([_COOKIE]).encode())

    assert started == []
    assert "foxford.ru" in message.answers[-1]
