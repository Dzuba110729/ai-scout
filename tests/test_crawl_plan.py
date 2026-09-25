"""plan_crawl: что грузить с сайта заново, а что перенести из прошлого снимка —
по дате обновления страницы из карты сайта (см. CLAUDE.md про инкрементальный обход)."""

from datetime import UTC, datetime

from app.crawler.crawl import PreviousPageInfo, SitemapEntry, plan_crawl

OLD = datetime(2026, 1, 1, tzinfo=UTC)
NEW = datetime(2026, 6, 1, tzinfo=UTC)


def test_first_crawl_fetches_everything_regardless_of_dates():
    entries = [SitemapEntry(url="https://x.ru/a", lastmod=OLD)]

    plan = plan_crawl(entries, previous_pages={}, force_full=True, max_fetch=10)

    assert plan.to_fetch == ["https://x.ru/a"]
    assert plan.carry_over == {}


def test_unchanged_lastmod_is_skipped():
    entries = [SitemapEntry(url="https://x.ru/a", lastmod=OLD)]
    previous = {"https://x.ru/a": PreviousPageInfo(text="старый текст", lastmod=OLD)}

    plan = plan_crawl(entries, previous_pages=previous, force_full=False, max_fetch=10)

    assert plan.to_fetch == []
    assert plan.carry_over == {"https://x.ru/a": "старый текст"}


def test_newer_lastmod_is_fetched_again():
    entries = [SitemapEntry(url="https://x.ru/a", lastmod=NEW)]
    previous = {"https://x.ru/a": PreviousPageInfo(text="старый текст", lastmod=OLD)}

    plan = plan_crawl(entries, previous_pages=previous, force_full=False, max_fetch=10)

    assert plan.to_fetch == ["https://x.ru/a"]
    assert plan.carry_over == {}


def test_brand_new_url_is_always_fetched():
    entries = [SitemapEntry(url="https://x.ru/new", lastmod=OLD)]

    plan = plan_crawl(entries, previous_pages={}, force_full=False, max_fetch=10)

    assert plan.to_fetch == ["https://x.ru/new"]


def test_missing_lastmod_on_site_forces_recheck_every_time():
    # Сайт вообще не сообщает дату обновления — сравнивать не с чем, поэтому
    # честно перепроверяем страницу каждый раз, как раньше.
    entries = [SitemapEntry(url="https://x.ru/a", lastmod=None)]
    previous = {"https://x.ru/a": PreviousPageInfo(text="текст", lastmod=None)}

    plan = plan_crawl(entries, previous_pages=previous, force_full=False, max_fetch=10)

    assert plan.to_fetch == ["https://x.ru/a"]


def test_page_never_dated_before_is_rechecked_once():
    # У страницы раньше не было сохранённой даты (например, обход был до появления
    # этого поля) — сравнивать не с чем, перепроверяем один раз, дальше пойдёт по датам.
    entries = [SitemapEntry(url="https://x.ru/a", lastmod=OLD)]
    previous = {"https://x.ru/a": PreviousPageInfo(text="текст", lastmod=None)}

    plan = plan_crawl(entries, previous_pages=previous, force_full=False, max_fetch=10)

    assert plan.to_fetch == ["https://x.ru/a"]


def test_removed_page_is_simply_absent_from_live_urls():
    entries = [SitemapEntry(url="https://x.ru/a", lastmod=OLD)]
    previous = {
        "https://x.ru/a": PreviousPageInfo(text="текст", lastmod=OLD),
        "https://x.ru/gone": PreviousPageInfo(text="было", lastmod=OLD),
    }

    plan = plan_crawl(entries, previous_pages=previous, force_full=False, max_fetch=10)

    assert plan.live_urls == {"https://x.ru/a"}
    assert "https://x.ru/gone" not in plan.to_fetch
    assert "https://x.ru/gone" not in plan.carry_over


def test_fetch_limit_keeps_a_stable_slice_and_carries_over_the_rest():
    # Тот же принцип, что раньше проверялся для лимита прямо в чтении sitemap:
    # порядок должен быть стабильным, иначе лимит каждый раз резал бы другой хвост
    # изменившихся страниц, и разница между прогонами выглядела бы как случайная.
    entries = [SitemapEntry(url=f"https://x.ru/p{i:02d}", lastmod=NEW) for i in range(5)]
    previous = {e.url: PreviousPageInfo(text=f"было {e.url}", lastmod=OLD) for e in entries}

    plan = plan_crawl(entries, previous_pages=previous, force_full=False, max_fetch=2)

    assert plan.to_fetch == ["https://x.ru/p00", "https://x.ru/p01"]
    assert set(plan.carry_over) == {"https://x.ru/p02", "https://x.ru/p03", "https://x.ru/p04"}


def test_brand_new_urls_over_the_limit_are_just_not_fetched_yet():
    # Совсем новых адресов сверх лимита переносить нечего (текста для них ещё нет) —
    # они просто появятся, когда до них дойдёт очередь в одном из следующих обходов.
    entries = [SitemapEntry(url=f"https://x.ru/new{i}", lastmod=OLD) for i in range(3)]

    plan = plan_crawl(entries, previous_pages={}, force_full=False, max_fetch=1)

    assert plan.to_fetch == ["https://x.ru/new0"]
    assert plan.carry_over == {}


def test_urls_total_counts_the_whole_sitemap():
    entries = [SitemapEntry(url=f"https://x.ru/p{i}", lastmod=None) for i in range(3)]

    plan = plan_crawl(entries, previous_pages={}, force_full=True, max_fetch=100)

    assert plan.urls_total == 3


def test_new_pages_go_first_then_redated_then_undated_under_the_limit():
    # Как у skysmart.ru: тысячи недатированных статей не должны вытеснять из лимита
    # совсем новые страницы и страницы с обновлённой датой.
    undated = [SitemapEntry(url=f"https://x.ru/a{i}", lastmod=None) for i in range(5)]
    redated = SitemapEntry(url="https://x.ru/z-redated", lastmod=NEW)
    brand_new = SitemapEntry(url="https://x.ru/zz-new", lastmod=None)
    previous = {e.url: PreviousPageInfo(text="было", lastmod=None) for e in undated}
    previous[redated.url] = PreviousPageInfo(text="было", lastmod=OLD)

    plan = plan_crawl(
        [*undated, redated, brand_new], previous_pages=previous, force_full=False, max_fetch=3
    )

    assert plan.to_fetch == ["https://x.ru/zz-new", "https://x.ru/z-redated", "https://x.ru/a0"]
    assert set(plan.carry_over) == {f"https://x.ru/a{i}" for i in range(1, 5)}
