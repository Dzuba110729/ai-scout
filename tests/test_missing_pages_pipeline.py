"""Что пайплайн делает с пропавшими страницами: удалена / переехала / просто выпала."""

from datetime import UTC, datetime

from app.crawler.crawl import CrawlResult
from app.crawler.diff import ChangeType, PageDiff
from app.crawler.recheck import DisappearReason, MissingPageCheck
from app.models import DisappearanceReason, Page
from app.pipeline import (
    _apply_missing_check,
    _is_real_change,
    _mark_page_alive,
    build_run_notes,
)

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _page(url: str = "https://x.ru/a") -> Page:
    return Page(competitor_id=1, url=url, is_removed=False)


def _removed_diff(url: str = "https://x.ru/a") -> PageDiff:
    return PageDiff(url=url, change_type=ChangeType.REMOVED, old_text="старый текст")


def test_deleted_page_is_marked_removed():
    page = _page()
    check = MissingPageCheck(url=page.url, reason=DisappearReason.DELETED, status_code=404)

    _apply_missing_check(page, check, NOW)

    assert page.is_removed is True
    assert page.removed_at == NOW
    assert page.disappearance_reason is DisappearanceReason.DELETED


def test_moved_page_stores_where_it_went():
    page = _page()
    check = MissingPageCheck(
        url=page.url,
        reason=DisappearReason.MOVED,
        status_code=200,
        final_url="https://x.ru/new",
        final_text="Новый курс",
    )

    _apply_missing_check(page, check, NOW)

    assert page.is_removed is True
    assert page.disappearance_reason is DisappearanceReason.MOVED
    assert page.redirect_to_url == "https://x.ru/new"


def test_alive_page_missed_by_crawl_is_not_marked_removed():
    # Главная починка: страница просто не попала в обход (лимит страниц / убрали из
    # карты сайта) — раньше её слепо помечали удалённой.
    page = _page()
    check = MissingPageCheck(url=page.url, reason=DisappearReason.STILL_ALIVE, status_code=200)

    _apply_missing_check(page, check, NOW)

    assert page.is_removed is False
    assert page.removed_at is None
    assert page.disappearance_reason is DisappearanceReason.MISSING_FROM_CRAWL
    assert page.last_checked_at == NOW


def test_unverifiable_page_is_not_marked_removed():
    page = _page()
    check = MissingPageCheck(url=page.url, reason=DisappearReason.UNKNOWN)

    _apply_missing_check(page, check, NOW)

    assert page.is_removed is False
    assert page.disappearance_reason is DisappearanceReason.MISSING_FROM_CRAWL


def test_page_seen_again_loses_previous_verdict():
    page = _page()
    page.is_removed = True
    page.removed_at = NOW
    page.disappearance_reason = DisappearanceReason.MOVED
    page.redirect_to_url = "https://x.ru/new"
    page.redirect_target_summary = "Теперь тут вебинары"

    _mark_page_alive(page, NOW)

    assert page.is_removed is False
    assert page.removed_at is None
    assert page.disappearance_reason is None
    assert page.redirect_to_url is None
    assert page.redirect_target_summary is None


def test_removed_change_is_reported_only_when_page_is_really_gone():
    checks = {
        "https://x.ru/deleted": MissingPageCheck(
            url="https://x.ru/deleted", reason=DisappearReason.DELETED
        ),
        "https://x.ru/moved": MissingPageCheck(
            url="https://x.ru/moved", reason=DisappearReason.MOVED, final_url="https://x.ru/new"
        ),
        "https://x.ru/alive": MissingPageCheck(
            url="https://x.ru/alive", reason=DisappearReason.STILL_ALIVE
        ),
    }

    assert _is_real_change(_removed_diff("https://x.ru/deleted"), checks) is True
    assert _is_real_change(_removed_diff("https://x.ru/moved"), checks) is True
    assert _is_real_change(_removed_diff("https://x.ru/alive"), checks) is False


def test_removed_change_without_verdict_is_not_reported():
    assert _is_real_change(_removed_diff(), {}) is False


def test_new_and_changed_pages_are_always_reported():
    new_diff = PageDiff(url="https://x.ru/n", change_type=ChangeType.NEW, new_text="текст")
    changed_diff = PageDiff(
        url="https://x.ru/c", change_type=ChangeType.CHANGED, old_text="было", new_text="стало"
    )

    assert _is_real_change(new_diff, {}) is True
    assert _is_real_change(changed_diff, {}) is True


def test_notes_warn_when_site_is_bigger_than_the_page_limit():
    crawl_result = CrawlResult(base_url="https://x.ru", pages={"https://x.ru/a": "т"}, urls_total=500)

    notes = build_run_notes(crawl_result, {})

    assert any("500" in note and "не весь сайт" in note for note in notes)


def test_notes_warn_about_pages_that_failed_to_load():
    crawl_result = CrawlResult(
        base_url="https://x.ru",
        pages={"https://x.ru/a": "т"},
        urls_total=2,
        failed_urls=["https://x.ru/broken"],
    )

    notes = build_run_notes(crawl_result, {})

    assert any("Не удалось загрузить страниц" in note and "1" in note for note in notes)


def test_notes_silent_when_whole_site_fits():
    crawl_result = CrawlResult(
        base_url="https://x.ru", pages={"https://x.ru/a": "т", "https://x.ru/b": "т"}, urls_total=2
    )

    assert build_run_notes(crawl_result, {}) == []


def test_notes_count_moved_and_surviving_pages():
    crawl_result = CrawlResult(base_url="https://x.ru", pages={"https://x.ru/a": "т"}, urls_total=1)
    checks = {
        "https://x.ru/moved": MissingPageCheck(url="https://x.ru/moved", reason=DisappearReason.MOVED),
        "https://x.ru/alive": MissingPageCheck(
            url="https://x.ru/alive", reason=DisappearReason.STILL_ALIVE
        ),
        "https://x.ru/unknown": MissingPageCheck(
            url="https://x.ru/unknown", reason=DisappearReason.UNKNOWN
        ),
    }

    notes = build_run_notes(crawl_result, checks)

    assert any("переехало на новый адрес: 1" in note for note in notes)
    assert any("по-прежнему открываются: 2" in note for note in notes)
