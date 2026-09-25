"""Список всех страниц сайта из sitemap.xml, с датой обновления каждой страницы.

Карта сайта читается целиком, без обрезки лимитом — лимит на то, сколько страниц
реально ЗАГРУЖАТЬ, применяется отдельно (см. tests/test_crawl_plan.py)."""

from datetime import UTC, datetime

import httpx
import pytest

from app.crawler import crawl as crawl_module
from app.crawler.crawl import CrawlResult, discover_sitemap_entries, find_sitemap_page_link


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



@pytest.mark.asyncio
async def test_falls_back_to_sitemaps_listed_in_robots_txt(monkeypatch):
    # Как у skysmart.ru: /sitemap.xml редиректит на главную (HTML, не XML).
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/sitemap.xml":
            return httpx.Response(200, text="<!DOCTYPE html><html><body>главная</body></html>")
        if path == "/robots.txt":
            return httpx.Response(
                200, text="User-agent: *\nSitemap: https://x.ru/a.xml\nsitemap:https://x.ru/b.xml\n"
            )
        if path == "/a.xml":
            return httpx.Response(200, text=_urlset([("https://x.ru/p1", None)]))
        if path == "/b.xml":
            return httpx.Response(200, text=_urlset([("https://x.ru/p2", "2026-05-01")]))
        return httpx.Response(404)

    _patch_client(monkeypatch, handler)

    entries = await discover_sitemap_entries("https://x.ru")

    assert [e.url for e in entries] == ["https://x.ru/p1", "https://x.ru/p2"]


@pytest.mark.asyncio
async def test_robots_txt_is_ignored_when_sitemap_xml_works(monkeypatch):
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text=_urlset([("https://x.ru/p1", None)]))
        return httpx.Response(200, text="Sitemap: https://x.ru/other.xml")

    _patch_client(monkeypatch, handler)

    entries = await discover_sitemap_entries("https://x.ru")

    assert [e.url for e in entries] == ["https://x.ru/p1"]
    assert "/robots.txt" not in requested


@pytest.mark.asyncio
async def test_files_like_pdf_are_not_treated_as_pages(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_urlset([("https://x.ru/page", None), ("https://x.ru/files/Вариант%201.PDF", None)]),
        )

    _patch_client(monkeypatch, handler)

    entries = await discover_sitemap_entries("https://x.ru")

    assert [e.url for e in entries] == ["https://x.ru/page"]


@pytest.mark.asyncio
async def test_custom_html_sitemap_page_gives_its_same_site_links(monkeypatch):
    # Как у skysmart.ru/sitemap: обычная страница со ссылками, а не XML.
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/sitemap":
            return httpx.Response(
                200,
                text=(
                    '<html><body><a href="/courses">Курсы</a> <a href="price#top">Цены</a>'
                    '<a href="https://other.ru/x">чужой</a><a href="https://x.ru/courses">дубль</a>'
                    "</body></html>"
                ),
            )
        return httpx.Response(404)

    _patch_client(monkeypatch, handler)

    entries = await discover_sitemap_entries("https://x.ru", sitemap_url="https://x.ru/sitemap")

    assert [e.url for e in entries] == ["https://x.ru/courses", "https://x.ru/price"]
    assert all(e.lastmod is None for e in entries)
    assert requested == ["/sitemap"]  # стандартные места не трогаем, раз адрес задан


@pytest.mark.asyncio
async def test_custom_sitemap_url_can_be_xml(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/maps/main.xml":
            return httpx.Response(200, text=_urlset([("https://x.ru/p1", "2026-05-01")]))
        return httpx.Response(404)

    _patch_client(monkeypatch, handler)

    entries = await discover_sitemap_entries("https://x.ru", sitemap_url="https://x.ru/maps/main.xml")

    assert [e.url for e in entries] == ["https://x.ru/p1"]
    assert entries[0].lastmod is not None

def test_crawl_result_knows_it_saw_only_part_of_the_site():
    truncated = CrawlResult(base_url="https://x.ru", pages={"https://x.ru/a": "т"}, urls_total=500)
    complete = CrawlResult(base_url="https://x.ru", pages={"https://x.ru/a": "т"}, urls_total=1)

    assert truncated.is_truncated is True
    assert complete.is_truncated is False


def test_finds_sitemap_link_in_footer_by_its_text():
    html = (
        '<header><a href="/courses">Курсы</a></header>'
        '<footer><a href="/o-nas">О нас</a> <a href="/map-page"><span>Карта</span>\n <b>сайта</b></a></footer>'
    )

    assert find_sitemap_page_link(html, "https://x.ru/") == "https://x.ru/map-page"


def test_link_text_wins_over_a_similar_looking_address():
    html = '<a href="/sitemap.xml">XML</a><a href="https://x.ru/karta">Карта сайта</a>'

    assert find_sitemap_page_link(html, "https://x.ru/") == "https://x.ru/karta"


def test_falls_back_to_link_address_when_text_is_an_icon():
    html = '<nav><a href="/about">О нас</a><a href="/karta-sajta/"><img alt=""></a></nav>'

    assert find_sitemap_page_link(html, "https://x.ru/") == "https://x.ru/karta-sajta/"


def test_sitemap_link_to_another_site_is_ignored():
    html = '<a href="https://other.ru/sitemap">Карта сайта</a>'

    assert find_sitemap_page_link(html, "https://x.ru/") is None


@pytest.mark.asyncio
async def test_without_xml_maps_takes_pages_from_sitemap_link_on_home_page(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/":
            return httpx.Response(200, text='<footer><a href="/karta">Карта сайта</a></footer>')
        if path == "/karta":
            return httpx.Response(200, text='<a href="/courses">Курсы</a><a href="/price">Цены</a>')
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        return httpx.Response(404)

    _patch_client(monkeypatch, handler)

    entries = await discover_sitemap_entries("https://x.ru/")

    assert [e.url for e in entries] == ["https://x.ru/courses", "https://x.ru/price"]


@pytest.mark.asyncio
async def test_html_sitemap_links_lose_advertising_tags(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                '<a href="/art/?_gl=1*abc*_ga*MTE3">Искусство</a>'
                '<a href="/art/?utm_source=menu">Искусство</a>'
                '<a href="/catalog?page=2&utm_medium=x">Каталог</a>'
            ),
        )

    _patch_client(monkeypatch, handler)

    entries = await discover_sitemap_entries("https://x.ru", sitemap_url="https://x.ru/karta")

    assert [e.url for e in entries] == ["https://x.ru/art", "https://x.ru/catalog?page=2"]
