"""Сквозные изменения (баннер/акция на многих страницах) — одна находка, а не десятки.

Обратная связь SEO-специалиста 2026-10-19: «курс Roblox» и рекламные баннеры
повторялись в отчёте построчно на каждой странице курса — отчёт на 73 страницы.
"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import pipeline
from app.ai.analyze import AiAnalysisResult, build_prompt, build_sitewide_prompt
from app.content_kind import COMMERCE, INFO, content_kind
from app.crawler.crawl import CrawlResult
from app.crawler.diff import ChangeType, PageDiff
from app.crawler.sitewide import SitewideGroup, find_sitewide_changes
from app.db import Base
from app.integrations.google_docs import _HEADER, build_sitewide_row
from app.models import AiAnalysis, Competitor, PageChange, SessionStatus

BANNER = "Скидка до 50%\nна все курсы\nПромокод СМОГУ\nЗапишите ребёнка сейчас"


def _course(subject: str, extra: str = "") -> str:
    return f"Курс {subject}\nПрограмма курса по предмету {subject}\nЦена\n{extra}Записаться"


def _changed(url: str, old: str, new: str) -> PageDiff:
    return PageDiff(url=url, change_type=ChangeType.CHANGED, old_text=old, new_text=new)


SUBJECTS = ["математика", "физика", "химия", "биология", "история"]


def _banner_diffs() -> list[PageDiff]:
    return [
        _changed(f"https://x.ru/courses/{s}", _course(s), _course(s, BANNER + "\n")) for s in SUBJECTS
    ]


def test_same_block_on_many_pages_is_one_group():
    split = find_sitewide_changes(_banner_diffs(), min_pages=3)

    assert len(split.groups) == 1
    group = split.groups[0]
    assert len(group.urls) == 5
    assert "+Промокод СМОГУ" in group.lines
    assert all(split.is_fully_sitewide(f"https://x.ru/courses/{s}") for s in SUBJECTS)


def test_page_keeps_its_own_change_besides_the_block():
    diffs = _banner_diffs()
    diffs[0] = _changed(
        diffs[0].url,
        _course("математика"),
        _course("математика", BANNER + "\nНовый модуль: геометрия для ОГЭ\n"),
    )

    split = find_sitewide_changes(diffs, min_pages=3)

    assert not split.is_fully_sitewide(diffs[0].url)
    own = split.own_changes[diffs[0].url]
    assert "Новый модуль: геометрия для ОГЭ" in own
    assert "СМОГУ" not in own


def test_block_on_too_few_pages_is_not_sitewide():
    split = find_sitewide_changes(_banner_diffs()[:2], min_pages=3)

    assert split.groups == []
    assert split.own_changes == {}


def test_different_prices_in_the_same_promo_are_one_group():
    diffs = [
        _changed(
            f"https://x.ru/catalog/{i}",
            f"Курс {i}\nСкидка 10% при оплате полностью\n{30 + i} 000\n₽\nОписание {i}",
            f"Курс {i}\nСкидка 30% при оплате полностью\n{10 + i} 400\n₽\nОписание {i}",
        )
        for i in range(4)
    ]

    split = find_sitewide_changes(diffs, min_pages=3)

    assert len(split.groups) == 1
    assert "+Скидка 30% при оплате полностью" in split.groups[0].lines


def test_disabled_when_min_pages_is_zero():
    assert find_sitewide_changes(_banner_diffs(), min_pages=0).groups == []


def test_prompt_for_page_with_block_shows_only_own_changes():
    page_diff = PageDiff(
        url="https://x.ru/a",
        change_type=ChangeType.CHANGED,
        old_text="a",
        new_text="b",
        own_diff_text="+Новый модуль",
    )

    prompt = build_prompt(page_diff)

    assert "+Новый модуль" in prompt
    assert "разобраны отдельно" in prompt


def test_sitewide_prompt_lists_examples_and_new_pages():
    group = SitewideGroup(urls=["https://x.ru/a", "https://x.ru/b", "https://x.ru/c"], lines=["+Курс Roblox"])

    prompt = build_sitewide_prompt(group, ["https://x.ru/roblox"])

    assert "сразу на 3 страницах" in prompt
    assert "https://x.ru/roblox" in prompt
    assert "related_url" in prompt


def test_sitewide_row_has_one_link_and_examples():
    group = SitewideGroup(urls=[f"https://x.ru/courses/{i}" for i in range(10)], lines=["+Курс Roblox"])
    analysis = AiAnalysisResult(
        category="курс",
        usp="новый курс Roblox",
        cta=None,
        summary="На всех страницах курсов появился блок курса Roblox.",
        raw_response={"related_url": "https://x.ru/roblox"},
        importance="high",
        importance_reason="Новое направление",
    )

    row = build_sitewide_row(group, analysis, pipeline.datetime(2026, 10, 19, tzinfo=pipeline.UTC))

    assert len(row) == len(_HEADER)
    assert row[1].startswith("Сквозное изменение\nна 10 стр.")
    assert row[2].startswith("Ведёт на: https://x.ru/roblox\nПримеры:\nhttps://x.ru/courses/0")
    assert row[2].endswith("и ещё 7")
    assert row[4] == "Коммерция (продукт)"


@pytest.mark.parametrize(
    ("url", "category", "kind"),
    [
        ("https://skysmart.ru/articles/english/kak-vyuchit", None, INFO),
        ("https://og1.ru/polza/10-sposobov", "другое", INFO),
        ("https://onlineschool-1.ru/blog/ege", None, INFO),
        ("https://x.ru/some-page", "статья", INFO),
        ("https://x.ru/some-page", "чек-лист", INFO),
        ("https://foxford.ru/courses/1234", "курс", COMMERCE),
        ("https://foxford.ru/catalog/10-klass", None, COMMERCE),
        ("https://x.ru/blogger-course", "лендинг", COMMERCE),
    ],
)
def test_content_kind(url, category, kind):
    assert content_kind(url, category) == kind


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    state = {"pages": {}, "analyzed": [], "sitewide": [], "sent": [], "reports": []}

    async def fake_crawl(_db, competitor, _previous, _force_full):
        return CrawlResult(base_url=competitor.base_url, pages=dict(state["pages"]), urls_total=len(state["pages"]))

    async def fake_analyze(page_diff):
        state["analyzed"].append(page_diff)
        return AiAnalysisResult(
            category="курс", usp=None, cta=None, summary=f"Суть {page_diff.url}", raw_response={}, importance="medium"
        )

    async def fake_sitewide(group, new_page_urls):
        state["sitewide"].append((group, new_page_urls))
        return AiAnalysisResult(
            category="оффер",
            usp="скидка до 50%",
            cta=None,
            summary="Акция на всех курсах.",
            raw_response={"related_url": None},
            importance="high",
            importance_reason="Скидка до 50%",
        )

    async def fake_report(_db, _competitor, _run_at, rows, notes, *, summary=None):
        state["reports"].append((rows, notes, summary))
        return "https://docs/report"

    async def fake_notify(_notifier, _db, competitor, text, *, page_change_id, buttons=None):
        state["sent"].append(text)

    async def no_recheck(diffs, previous_by_url):
        return {}

    async def no_overview(*_args, **_kwargs):
        return None

    monkeypatch.setattr(pipeline, "_crawl_competitor", fake_crawl)
    monkeypatch.setattr(pipeline, "analyze_page_change", fake_analyze)
    monkeypatch.setattr(pipeline, "analyze_sitewide_group", fake_sitewide)
    monkeypatch.setattr(pipeline, "_write_run_report", fake_report)
    monkeypatch.setattr(pipeline, "_notify", fake_notify)
    monkeypatch.setattr(pipeline, "_recheck_missing_pages", no_recheck)
    monkeypatch.setattr(pipeline, "write_run_overview", no_overview)
    monkeypatch.setattr(pipeline, "_backfill_missing_analyses", lambda *_: asyncio.sleep(0, result=(0, 0)))

    yield db, state
    db.close()


def test_crawl_reports_sitewide_block_once(env):
    db, state = env
    rival = Competitor(name="Скайсмарт", base_url="https://x.ru", status=SessionStatus.ACTIVE)
    db.add(rival)
    db.commit()
    state["pages"] = {f"https://x.ru/courses/{s}": _course(s) for s in SUBJECTS}
    asyncio.run(pipeline.run_crawl_for_competitor(db, rival))

    state["pages"] = {f"https://x.ru/courses/{s}": _course(s, BANNER + "\n") for s in SUBJECTS}
    state["pages"]["https://x.ru/courses/математика"] = _course("математика", BANNER + "\nНовый модуль: геометрия\n")
    state["pages"]["https://x.ru/roblox"] = "Курс Roblox для детей"
    asyncio.run(pipeline.run_crawl_for_competitor(db, rival))

    # ИИ: новая страница, своя правка страницы математики и один раз — блок.
    assert sorted(d.url for d in state["analyzed"]) == ["https://x.ru/courses/математика", "https://x.ru/roblox"]
    math_diff = next(d for d in state["analyzed"] if d.url.endswith("математика"))
    assert "геометрия" in math_diff.own_diff_text and "СМОГУ" not in math_diff.own_diff_text
    assert len(state["sitewide"]) == 1
    assert state["sitewide"][0][1] == ["https://x.ru/roblox"]

    # В базе — все 6 находок, и у каждой есть разбор: дозаправке нечего добирать.
    assert db.query(PageChange).count() == 6
    assert db.query(AiAnalysis).count() == 6
    member = (
        db.query(AiAnalysis)
        .join(PageChange)
        .filter(AiAnalysis.summary.like("Сквозное изменение на 5 стр.%"))
        .count()
    )
    assert member == 4

    rows, _notes, summary = state["reports"][-1]
    assert len(rows) == 3  # блок + математика + новая страница
    assert rows[0][1].startswith("Сквозное изменение\nна 5 стр.")  # важное — первым
    assert "сквозных изменений (одно на много страниц) — 1" in summary.counts
    assert "🔥 Важное" in state["sent"][-1]


def test_only_prices_left_besides_the_block_is_part_of_it():
    diffs = _banner_diffs()
    diffs[0] = _changed(diffs[0].url, _course("математика", "10 490\n"), _course("математика", BANNER + "\n7 280\n"))

    split = find_sitewide_changes(diffs, min_pages=3)

    assert split.is_fully_sitewide(diffs[0].url)
