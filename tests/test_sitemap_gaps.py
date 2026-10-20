"""Карта сайта потеряла целые разделы, а страницы живы (onlineschool-1.ru, 19.10.2026:
/sitemap.xml пустой, в карте только энциклопедия). Такие страницы не «удалены» —
берём их из прошлого снимка, а владельцу пишем пояснение."""

from datetime import UTC, datetime

import pytest

from app.crawler import crawl as crawl_module
from app.crawler.crawl import (
    WHOLE_MAP_GAP,
    CrawlResult,
    PreviousPageInfo,
    SitemapEntry,
    crawl_competitor,
    find_sitemap_gaps,
)
from app.crawler.recheck import DisappearReason, MissingPageCheck
from app.pipeline import build_run_notes

OLD = datetime(2026, 1, 1, tzinfo=UTC)


def _previous(urls: list[str]) -> dict[str, PreviousPageInfo]:
    return {url: PreviousPageInfo(text=f"текст {url}", lastmod=OLD) for url in urls}


def _section(name: str, count: int) -> list[str]:
    return [f"https://x.ru/{name}/p{index:03d}" for index in range(count)]


def test_whole_section_missing_from_map_is_a_gap():
    blog = _section("blog", 12)
    courses = _section("courses", 30)
    entries = [SitemapEntry(url=url, lastmod=OLD) for url in courses]

    gaps = find_sitemap_gaps(entries, _previous(blog + courses))

    assert [(gap.section, gap.urls) for gap in gaps] == [("/blog", blog)]


def test_few_pages_gone_from_live_section_is_not_a_gap():
    """Пара удалённых статей в разделе, который в карте есть, — обычная жизнь сайта."""
    blog = _section("blog", 40)
    entries = [SitemapEntry(url=url, lastmod=OLD) for url in blog[2:]]

    assert find_sitemap_gaps(entries, _previous(blog)) == []


def test_small_vanished_section_is_left_to_recheck():
    promo = _section("promo", 3)
    courses = _section("courses", 30)
    entries = [SitemapEntry(url=url, lastmod=OLD) for url in courses]

    assert find_sitemap_gaps(entries, _previous(promo + courses)) == []


def test_map_that_lost_most_pages_is_one_whole_map_gap():
    """Как у onlineschool-1.ru: пропало всё, кроме раздела, который мы и так не смотрим."""
    previous = _previous(_section("blog", 30) + _section("questions", 40) + ["https://x.ru/attestation"])

    gaps = find_sitemap_gaps([], previous)

    assert len(gaps) == 1
    assert gaps[0].section == WHOLE_MAP_GAP
    assert gaps[0].urls == sorted(previous)


def _patch(monkeypatch, entries, alive: bool):
    async def fake_discover(base_url, sitemap_url=None):
        return entries

    checked: list[list[str]] = []

    async def fake_check(urls, *, max_pages=None, **_kwargs):
        checked.append(list(urls))
        reason = DisappearReason.STILL_ALIVE if alive else DisappearReason.DELETED
        return {url: MissingPageCheck(url=url, reason=reason) for url in urls}

    fetched: list[str] = []

    async def fake_fetch(context, url):
        fetched.append(url)
        return "заголовок", f"свежий {url}"

    monkeypatch.setattr(crawl_module, "discover_sitemap_entries", fake_discover)
    monkeypatch.setattr(crawl_module, "check_missing_pages", fake_check)
    monkeypatch.setattr(crawl_module, "fetch_page_text", fake_fetch)
    monkeypatch.setattr(crawl_module.settings, "crawl_request_delay_seconds", 0)
    return checked, fetched


@pytest.mark.asyncio
async def test_live_vanished_section_is_carried_over_not_removed(monkeypatch):
    blog = _section("blog", 12)
    courses = _section("courses", 30)
    checked, fetched = _patch(monkeypatch, [SitemapEntry(url=url, lastmod=OLD) for url in courses], alive=True)
    previous = _previous(blog + courses)

    result = await crawl_competitor(None, "https://x.ru", previous_pages=previous, force_full=False)

    assert set(result.pages) == set(blog + courses)
    assert result.pages[blog[0]] == previous[blog[0]].text
    assert fetched == []  # даты прежние — на сайт за ними не ходим
    assert len(checked) == 1 and len(checked[0]) == crawl_module.SITEMAP_GAP_SAMPLE_SIZE
    assert set(checked[0]) <= set(blog)
    assert result.restored_from_previous == {"/blog": 12}
    assert not result.is_truncated


@pytest.mark.asyncio
async def test_section_really_deleted_from_site_stays_removed(monkeypatch):
    blog = _section("blog", 12)
    courses = _section("courses", 30)
    _patch(monkeypatch, [SitemapEntry(url=url, lastmod=OLD) for url in courses], alive=False)

    result = await crawl_competitor(None, "https://x.ru", previous_pages=_previous(blog + courses), force_full=False)

    assert set(result.pages) == set(courses)
    assert result.restored_from_previous == {}


@pytest.mark.asyncio
async def test_map_with_only_excluded_section_restores_tracked_pages(monkeypatch):
    """Карта отдаёт лишь исключённую энциклопедию: после фильтра пусто, но обход не
    уходит в «по ссылкам» и не теряет 70 страниц."""
    tracked = _section("blog", 30) + _section("questions", 40)
    encyclopedia = [SitemapEntry(url=url, lastmod=OLD) for url in _section("encyclopedia", 100)]
    _checked, fetched = _patch(monkeypatch, encyclopedia, alive=True)

    async def no_link_crawl(*_args, **_kwargs):
        raise AssertionError("обход по ссылкам не нужен")

    monkeypatch.setattr(crawl_module, "discover_urls_by_crawling", no_link_crawl)
    monkeypatch.setattr(crawl_module, "_sitemap_page_via_browser", no_link_crawl)

    result = await crawl_competitor(
        None,
        "https://x.ru",
        previous_pages=_previous(tracked),
        force_full=False,
        include_paths=["!/encyclopedia"],
    )

    assert set(result.pages) == set(tracked)
    assert fetched == []
    assert result.restored_from_previous == {"/blog": 30, "/questions": 40}


@pytest.mark.asyncio
async def test_full_recheck_loads_restored_pages(monkeypatch):
    """Раз в месяц (полная сверка) страницы пропавших разделов всё-таки грузятся —
    изменения в них не теряются насовсем."""
    blog = _section("blog", 12)
    courses = _section("courses", 30)
    _checked, fetched = _patch(monkeypatch, [SitemapEntry(url=url, lastmod=OLD) for url in courses], alive=True)

    await crawl_competitor(
        None, "https://x.ru", previous_pages=_previous(blog + courses), force_full=True, max_pages=1000
    )

    assert set(blog) <= set(fetched)


def test_run_notes_explain_restored_sections():
    result = CrawlResult(base_url="https://x.ru", restored_from_previous={"/blog": 592, "/questions": 1189})

    notes = build_run_notes(result, {})

    assert len(notes) == 1
    assert "1781" in notes[0]
    assert "/questions (1189), /blog (592)" in notes[0]
