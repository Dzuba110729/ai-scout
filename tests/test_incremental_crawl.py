"""crawl_competitor в режиме обычного (не первого) обхода: на сайт идём только за
страницами, которые реально изменились по дате в карте сайта — остальное берём
из прошлого снимка, не открывая браузер. См. CLAUDE.md про инкрементальный обход."""

from datetime import UTC, datetime

import pytest

from app.crawler import crawl as crawl_module
from app.crawler.crawl import (
    CompetitorBlockedError,
    PreviousPageInfo,
    SitemapEntry,
    crawl_competitor,
)

OLD = datetime(2026, 1, 1, tzinfo=UTC)
NEW = datetime(2026, 6, 1, tzinfo=UTC)


def _patch_discovery(monkeypatch, entries: list[SitemapEntry]) -> None:
    async def fake_discover(base_url):
        return entries

    monkeypatch.setattr(crawl_module, "discover_sitemap_entries", fake_discover)


@pytest.mark.asyncio
async def test_unchanged_page_is_not_fetched_from_the_site(monkeypatch):
    _patch_discovery(monkeypatch, [SitemapEntry(url="https://x.ru/a", lastmod=OLD)])
    monkeypatch.setattr(crawl_module.settings, "crawl_request_delay_seconds", 0)

    fetch_calls = []

    async def fake_fetch(context, url):
        fetch_calls.append(url)
        return "заголовок", "новый текст"

    monkeypatch.setattr(crawl_module, "fetch_page_text", fake_fetch)

    previous = {"https://x.ru/a": PreviousPageInfo(text="старый текст", lastmod=OLD)}

    result = await crawl_competitor(
        context=None, base_url="https://x.ru", previous_pages=previous, force_full=False
    )

    assert fetch_calls == []
    assert result.pages == {"https://x.ru/a": "старый текст"}


@pytest.mark.asyncio
async def test_changed_page_is_fetched_and_removed_page_disappears(monkeypatch):
    _patch_discovery(
        monkeypatch,
        [
            SitemapEntry(url="https://x.ru/changed", lastmod=NEW),
            SitemapEntry(url="https://x.ru/same", lastmod=OLD),
        ],
    )
    monkeypatch.setattr(crawl_module.settings, "crawl_request_delay_seconds", 0)

    async def fake_fetch(context, url):
        return "заголовок", "свежий текст"

    monkeypatch.setattr(crawl_module, "fetch_page_text", fake_fetch)

    previous = {
        "https://x.ru/changed": PreviousPageInfo(text="старый текст", lastmod=OLD),
        "https://x.ru/same": PreviousPageInfo(text="без изменений", lastmod=OLD),
        "https://x.ru/gone": PreviousPageInfo(text="исчезнет", lastmod=OLD),
    }

    result = await crawl_competitor(
        context=None, base_url="https://x.ru", previous_pages=previous, force_full=False
    )

    assert result.pages == {
        "https://x.ru/changed": "свежий текст",
        "https://x.ru/same": "без изменений",
    }
    assert "https://x.ru/gone" not in result.pages
    assert result.sitemap_lastmod["https://x.ru/changed"] == NEW


@pytest.mark.asyncio
async def test_force_full_refetches_everything_even_if_lastmod_did_not_change(monkeypatch):
    _patch_discovery(monkeypatch, [SitemapEntry(url="https://x.ru/a", lastmod=OLD)])
    monkeypatch.setattr(crawl_module.settings, "crawl_request_delay_seconds", 0)

    fetch_calls = []

    async def fake_fetch(context, url):
        fetch_calls.append(url)
        return "заголовок", "свежий текст"

    monkeypatch.setattr(crawl_module, "fetch_page_text", fake_fetch)

    previous = {"https://x.ru/a": PreviousPageInfo(text="старый текст", lastmod=OLD)}

    result = await crawl_competitor(
        context=None, base_url="https://x.ru", previous_pages=previous, force_full=True
    )

    assert fetch_calls == ["https://x.ru/a"]
    assert result.pages["https://x.ru/a"] == "свежий текст"


@pytest.mark.asyncio
async def test_one_failed_page_does_not_break_the_whole_crawl(monkeypatch):
    # Реальный случай: og1.ru упал целиком из-за таймайта на одной странице
    # (Page.goto: Timeout 30000ms exceeded) — остальные страницы обходу не мешают.
    _patch_discovery(
        monkeypatch,
        [
            SitemapEntry(url="https://x.ru/broken", lastmod=NEW),
            SitemapEntry(url="https://x.ru/ok", lastmod=NEW),
        ],
    )
    monkeypatch.setattr(crawl_module.settings, "crawl_request_delay_seconds", 0)

    async def fake_fetch(context, url):
        if url == "https://x.ru/broken":
            raise TimeoutError("Page.goto: Timeout 30000ms exceeded.")
        return "заголовок", "свежий текст"

    monkeypatch.setattr(crawl_module, "fetch_page_text", fake_fetch)

    previous = {
        "https://x.ru/broken": PreviousPageInfo(text="старый текст", lastmod=OLD),
        "https://x.ru/ok": PreviousPageInfo(text="было", lastmod=OLD),
    }

    result = await crawl_competitor(
        context=None, base_url="https://x.ru", previous_pages=previous, force_full=False
    )

    assert result.failed_urls == ["https://x.ru/broken"]
    # Не удалось загрузить — считаем неизменной, а не пропавшей.
    assert result.pages["https://x.ru/broken"] == "старый текст"
    assert result.pages["https://x.ru/ok"] == "свежий текст"
    # Дату не обновляем — иначе следующий обход решит, что страница уже проверена
    # по новой дате, хотя реального текста мы так и не получили.
    assert "https://x.ru/broken" not in result.sitemap_lastmod


@pytest.mark.asyncio
async def test_brand_new_page_that_fails_to_load_is_simply_skipped(monkeypatch):
    # Нечего переносить (текста ещё нет) — страница появится, когда обход её
    # всё-таки сможет загрузить.
    _patch_discovery(monkeypatch, [SitemapEntry(url="https://x.ru/new", lastmod=OLD)])
    monkeypatch.setattr(crawl_module.settings, "crawl_request_delay_seconds", 0)

    async def fake_fetch(context, url):
        raise TimeoutError("Page.goto: Timeout 30000ms exceeded.")

    monkeypatch.setattr(crawl_module, "fetch_page_text", fake_fetch)

    result = await crawl_competitor(
        context=None, base_url="https://x.ru", previous_pages={}, force_full=False
    )

    assert result.failed_urls == ["https://x.ru/new"]
    assert "https://x.ru/new" not in result.pages


@pytest.mark.asyncio
async def test_blocked_error_still_stops_the_whole_crawl(monkeypatch):
    # Блокировка — не сбой одной страницы, а сигнал остановиться и уйти в Apify-фолбэк.
    _patch_discovery(monkeypatch, [SitemapEntry(url="https://x.ru/a", lastmod=OLD)])
    monkeypatch.setattr(crawl_module.settings, "crawl_request_delay_seconds", 0)

    async def fake_fetch(context, url):
        raise CompetitorBlockedError(url, "капча")

    monkeypatch.setattr(crawl_module, "fetch_page_text", fake_fetch)

    with pytest.raises(CompetitorBlockedError):
        await crawl_competitor(context=None, base_url="https://x.ru", previous_pages={}, force_full=True)
