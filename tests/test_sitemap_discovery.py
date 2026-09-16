"""Список всех страниц сайта из sitemap.xml, с датой обновления каждой страницы.

Карта сайта читается целиком, без обрезки лимитом — лимит на то, сколько страниц
реально ЗАГРУЖАТЬ, применяется отдельно (см. tests/test_crawl_plan.py)."""

from datetime import UTC, datetime

import httpx
import pytest

from app.crawler import crawl as crawl_module
from app.crawler.crawl import CrawlResult, discover_sitemap_entries


def _patch_client(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        crawl_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(**{**kwargs, "transport": transport}),
    )


def _urlset(entries: list[tuple[str, str | None]]) -> str:
    body = "".join(
        f"<url><loc>{url}</loc>{f'<lastmod>{lastmod}</lastmod>' if lastmod else ''}</url>"
        for url, lastmod in entries
    )
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'


def _sitemap_index(locs: list[str]) -> str:
    body = "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
    return (
        '<?xml version="1.0"?>'
        f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</sitemapindex>'
    )


@pytest.mark.asyncio
async def test_reads_lastmod_as_a_timezone_aware_date(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_urlset([("https://x.ru/a", "2026-05-01T10:00:00+03:00")]))

    _patch_client(monkeypatch, handler)

    entries = await discover_sitemap_entries("https://x.ru")

    assert len(entries) == 1
    assert entries[0].url == "https://x.ru/a"
    assert entries[0].lastmod == datetime(2026, 5, 1, 7, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_bare_date_without_time_is_treated_as_utc_midnight(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_urlset([("https://x.ru/a", "2026-05-01")]))

    _patch_client(monkeypatch, handler)

    entries = await discover_sitemap_entries("https://x.ru")

    assert entries[0].lastmod == datetime(2026, 5, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_missing_lastmod_is_none(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_urlset([("https://x.ru/a", None)]))

    _patch_client(monkeypatch, handler)

    entries = await discover_sitemap_entries("https://x.ru")

    assert entries[0].lastmod is None


@pytest.mark.asyncio
async def test_reads_more_than_five_sub_sitemaps(monkeypatch):
    # Раньше код брал только первые 5 файлов из sitemap index — на карте сайта
    # с восемью под-файлами три последних терялись целиком, даже из подсчёта размера.
    sub_urls = [f"https://x.ru/sitemap-{i}.xml" for i in range(8)]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://x.ru/sitemap.xml":
            return httpx.Response(200, text=_sitemap_index(sub_urls))
        index = sub_urls.index(url)
        return httpx.Response(200, text=_urlset([(f"https://x.ru/p{index}", None)]))

    _patch_client(monkeypatch, handler)

    entries = await discover_sitemap_entries("https://x.ru")

    assert {e.url for e in entries} == {f"https://x.ru/p{i}" for i in range(8)}


@pytest.mark.asyncio
async def test_no_sitemap_returns_empty_list(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    _patch_client(monkeypatch, handler)

    assert await discover_sitemap_entries("https://x.ru") == []


def test_crawl_result_knows_it_saw_only_part_of_the_site():
    truncated = CrawlResult(base_url="https://x.ru", pages={"https://x.ru/a": "т"}, urls_total=500)
    complete = CrawlResult(base_url="https://x.ru", pages={"https://x.ru/a": "т"}, urls_total=1)

    assert truncated.is_truncated is True
    assert complete.is_truncated is False
