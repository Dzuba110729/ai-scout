"""Список страниц из sitemap.xml: стабильный порядок и честный подсчёт размера сайта."""

import httpx
import pytest

from app.crawler import crawl as crawl_module
from app.crawler.crawl import CrawlResult, discover_urls_from_sitemap

_SITEMAP_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>'
)


def _sitemap(urls: list[str]) -> str:
    return _SITEMAP_TEMPLATE.format(entries="".join(f"<url><loc>{u}</loc></url>" for u in urls))


def _patch_client(monkeypatch, body: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        crawl_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(**{**kwargs, "transport": transport}),
    )


@pytest.mark.asyncio
async def test_page_limit_cuts_a_stable_slice(monkeypatch):
    # Раньше лимит резал неупорядоченное множество, поэтому каждый прогон брал
    # другие страницы — и разница выглядела как сотни новых и удалённых страниц.
    urls = [f"https://x.ru/p{i:03d}" for i in range(50)]
    _patch_client(monkeypatch, _sitemap(urls))

    first, _ = await discover_urls_from_sitemap("https://x.ru", max_pages=10)
    second, _ = await discover_urls_from_sitemap("https://x.ru", max_pages=10)

    assert first == second
    assert len(first) == 10


@pytest.mark.asyncio
async def test_reports_how_many_pages_the_site_really_has(monkeypatch):
    urls = [f"https://x.ru/p{i:03d}" for i in range(50)]
    _patch_client(monkeypatch, _sitemap(urls))

    pages, total = await discover_urls_from_sitemap("https://x.ru", max_pages=10)

    assert len(pages) == 10
    assert total == 50


@pytest.mark.asyncio
async def test_no_sitemap_returns_empty_list_and_zero_total(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        crawl_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(**{**kwargs, "transport": transport}),
    )

    assert await discover_urls_from_sitemap("https://x.ru") == ([], 0)


def test_crawl_result_knows_it_saw_only_part_of_the_site():
    truncated = CrawlResult(base_url="https://x.ru", pages={"https://x.ru/a": "т"}, urls_total=500)
    complete = CrawlResult(base_url="https://x.ru", pages={"https://x.ru/a": "т"}, urls_total=1)

    assert truncated.is_truncated is True
    assert complete.is_truncated is False
