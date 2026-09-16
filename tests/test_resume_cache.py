"""Черновик уже загруженных страниц: пропуск сети при повторном запуске и живой прогресс.

Если обход оборвался (упал сервер, легла база), следующий запуск не должен заново
идти на сайт конкурента за страницами, которые уже недавно получил — см. CLAUDE.md
и app.models.PageFetchCache.
"""

import pytest

from app.crawler import crawl as crawl_module
from app.crawler.crawl import SitemapEntry, crawl_competitor


def _patch_discovery(monkeypatch, urls: list[str]) -> None:
    async def fake_discover(base_url):
        return [SitemapEntry(url=u, lastmod=None) for u in urls]

    monkeypatch.setattr(crawl_module, "discover_sitemap_entries", fake_discover)


def _no_delay(monkeypatch) -> None:
    monkeypatch.setattr(crawl_module.settings, "crawl_request_delay_seconds", 0)


@pytest.mark.asyncio
async def test_fresh_cache_hit_skips_network_fetch(monkeypatch):
    _patch_discovery(monkeypatch, ["https://x.ru/a"])

    fetch_calls = []

    async def fake_fetch(context, url):
        fetch_calls.append(url)
        return "с сайта", "свежий текст"

    monkeypatch.setattr(crawl_module, "fetch_page_text", fake_fetch)

    async def cache_lookup(url):
        return "из черновика", "текст из черновика"

    result = await crawl_competitor(context=None, base_url="https://x.ru", cache_lookup=cache_lookup)

    assert fetch_calls == []  # к сайту конкурента вообще не ходили
    assert result.pages["https://x.ru/a"] == "текст из черновика"
    assert result.page_titles["https://x.ru/a"] == "из черновика"


@pytest.mark.asyncio
async def test_missing_cache_entry_fetches_and_stores(monkeypatch):
    _patch_discovery(monkeypatch, ["https://x.ru/a"])
    _no_delay(monkeypatch)

    async def fake_fetch(context, url):
        return "заголовок", "свежий текст"

    monkeypatch.setattr(crawl_module, "fetch_page_text", fake_fetch)

    stored = []

    async def cache_lookup(url):
        return None  # черновика нет или он устарел

    async def cache_store(url, title, text):
        stored.append((url, title, text))

    result = await crawl_competitor(
        context=None,
        base_url="https://x.ru",
        cache_lookup=cache_lookup,
        cache_store=cache_store,
    )

    assert stored == [("https://x.ru/a", "заголовок", "свежий текст")]
    assert result.pages["https://x.ru/a"] == "свежий текст"


@pytest.mark.asyncio
async def test_urls_discovered_reported_before_any_page_is_fetched(monkeypatch):
    _patch_discovery(monkeypatch, ["https://x.ru/a", "https://x.ru/b"])
    _no_delay(monkeypatch)

    fetch_order = []

    async def fake_fetch(context, url):
        fetch_order.append(("fetch", url))
        return "т", "т"

    monkeypatch.setattr(crawl_module, "fetch_page_text", fake_fetch)

    async def on_discovered(total):
        fetch_order.append(("discovered", total))

    await crawl_competitor(context=None, base_url="https://x.ru", on_urls_discovered=on_discovered)

    assert fetch_order[0] == ("discovered", 2)
    assert fetch_order.count(("fetch", "https://x.ru/a")) == 1
    assert fetch_order.count(("fetch", "https://x.ru/b")) == 1


@pytest.mark.asyncio
async def test_without_cache_hooks_behaves_as_before(monkeypatch):
    """Без хуков (обычный вызов из pipeline при первом запуске) поведение не меняется."""
    _patch_discovery(monkeypatch, ["https://x.ru/a"])
    _no_delay(monkeypatch)

    async def fake_fetch(context, url):
        return "заголовок", "текст"

    monkeypatch.setattr(crawl_module, "fetch_page_text", fake_fetch)

    result = await crawl_competitor(context=None, base_url="https://x.ru")

    assert result.pages == {"https://x.ru/a": "текст"}
    assert result.page_titles == {"https://x.ru/a": "заголовок"}
