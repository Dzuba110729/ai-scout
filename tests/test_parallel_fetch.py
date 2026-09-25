"""Параллельная загрузка страниц одного сайта (app.crawler.crawl._fetch_all) и
фильтр тяжёлых запросов браузера. Сеть и браузер не нужны: fetch_page_text подменён."""

import asyncio

import pytest

from app.crawler import crawl as crawl_module
from app.crawler.browser import is_blocked_request
from app.crawler.crawl import CompetitorBlockedError, SitemapEntry, crawl_competitor


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(crawl_module.settings, "crawl_request_delay_seconds", 0)
    monkeypatch.setattr(crawl_module.settings, "crawl_page_concurrency", 3)


def _patch_discovery(monkeypatch, urls):
    async def fake_discover(base_url, sitemap_url=None):
        return [SitemapEntry(url=u, lastmod=None) for u in urls]

    monkeypatch.setattr(crawl_module, "discover_sitemap_entries", fake_discover)


@pytest.mark.asyncio
async def test_pages_load_in_parallel_but_result_keeps_sitemap_order(monkeypatch):
    urls = [f"https://x.ru/p{i}" for i in range(6)]
    _patch_discovery(monkeypatch, urls)
    in_flight = 0
    peak = 0

    async def fake_fetch(context, url):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        # первые страницы грузятся дольше — завершатся не по порядку
        await asyncio.sleep(0.03 if url.endswith(("p0", "p1")) else 0.01)
        in_flight -= 1
        return f"title {url}", f"text {url}"

    monkeypatch.setattr(crawl_module, "fetch_page_text", fake_fetch)

    result = await crawl_competitor(context=None, base_url="https://x.ru")

    assert peak == 3
    assert list(result.pages) == urls
    assert result.pages["https://x.ru/p0"] == "text https://x.ru/p0"


@pytest.mark.asyncio
async def test_block_in_one_tab_stops_the_others(monkeypatch):
    urls = [f"https://x.ru/p{i}" for i in range(20)]
    _patch_discovery(monkeypatch, urls)
    fetched = []

    async def fake_fetch(context, url):
        if url == "https://x.ru/p1":
            raise CompetitorBlockedError(url, "403")
        await asyncio.sleep(0.01)
        fetched.append(url)
        return "t", "x"

    monkeypatch.setattr(crawl_module, "fetch_page_text", fake_fetch)

    with pytest.raises(CompetitorBlockedError):
        await crawl_competitor(context=None, base_url="https://x.ru")

    await asyncio.sleep(0.05)  # отменённые вкладки не должны доделать очередь
    assert len(fetched) < len(urls) - 1


@pytest.mark.asyncio
async def test_failed_page_is_skipped_not_fatal(monkeypatch):
    urls = ["https://x.ru/a", "https://x.ru/b"]
    _patch_discovery(monkeypatch, urls)

    async def fake_fetch(context, url):
        if url.endswith("a"):
            raise TimeoutError("завис")
        return "t", "b text"

    monkeypatch.setattr(crawl_module, "fetch_page_text", fake_fetch)

    result = await crawl_competitor(context=None, base_url="https://x.ru")

    assert result.failed_urls == ["https://x.ru/a"]
    assert result.pages == {"https://x.ru/b": "b text"}


@pytest.mark.asyncio
async def test_cache_hooks_never_run_concurrently(monkeypatch):
    # Хуки черновика ходят в одну Session БД — параллельно их вызывать нельзя.
    urls = [f"https://x.ru/p{i}" for i in range(8)]
    _patch_discovery(monkeypatch, urls)
    active = 0
    overlaps = 0

    async def guarded():
        nonlocal active, overlaps
        active += 1
        overlaps += active > 1
        await asyncio.sleep(0.005)
        active -= 1

    async def lookup(url):
        await guarded()

    async def store(url, title, text):
        await guarded()

    async def fake_fetch(context, url):
        await asyncio.sleep(0.001)
        return "t", url

    monkeypatch.setattr(crawl_module, "fetch_page_text", fake_fetch)

    await crawl_competitor(context=None, base_url="https://x.ru", cache_lookup=lookup, cache_store=store)

    assert overlaps == 0


@pytest.mark.parametrize(
    ("resource_type", "url", "blocked"),
    [
        ("image", "https://x.ru/a.png", True),
        ("font", "https://x.ru/a.woff2", True),
        ("script", "https://mc.yandex.ru/metrika/tag.js", True),
        ("script", "https://mod.calltouch.ru/init.js", True),
        ("script", "https://x.ru/app.js", False),
        ("document", "https://x.ru/", False),
        ("xhr", "https://x.ru/api/prices", False),
    ],
)
def test_only_heavy_and_tracking_requests_are_blocked(resource_type, url, blocked):
    assert is_blocked_request(resource_type, url) is blocked
