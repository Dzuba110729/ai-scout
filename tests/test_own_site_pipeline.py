"""Как обход решает, что и сколько раз сравнивать с нашим сайтом.

Проверяем именно решения, а не сеть: вызовы claude -p подменены заглушками.
"""

import asyncio

import pytest

from app.ai import compare as compare_module
from app.ai.analyze import ClaudeCliError
from app.ai.compare import ComparisonResult, OwnPage, OwnSite, compare_with_own_site
from app.crawler.diff import ChangeType, PageDiff
from app.models import Competitor, Page, PageChange, PageSnapshot
from app.own_site import get_own_site, load_own_site
from app.pipeline import (
    _compare_with_own_site_and_store,
    _load_own_site_for_comparison,
    select_diffs_for_comparison,
)

OWN_PAGES = [
    OwnPage(
        url="https://og1.ru/ege",
        title="Подготовка к ЕГЭ",
        text="Курсы подготовки к ЕГЭ по математике и русскому языку.",
    )
]
OWN_SITE = OwnSite(base_url="https://og1.ru", pages=OWN_PAGES)


def _diff(url: str, change_type: ChangeType) -> PageDiff:
    return PageDiff(
        url=url,
        change_type=change_type,
        old_text="старый текст" if change_type is not ChangeType.NEW else None,
        new_text="Курсы подготовки к ЕГЭ" if change_type is not ChangeType.REMOVED else None,
    )


# --- какие изменения идут на сравнение ---


def test_removed_pages_are_never_compared():
    diffs = [_diff("https://r.ru/gone", ChangeType.REMOVED), _diff("https://r.ru/new", ChangeType.NEW)]

    selected = select_diffs_for_comparison(diffs, limit=10)

    assert [d.url for d in selected] == ["https://r.ru/new"]


def test_new_pages_are_compared_before_changed_ones():
    diffs = [
        _diff("https://r.ru/changed-1", ChangeType.CHANGED),
        _diff("https://r.ru/new-1", ChangeType.NEW),
        _diff("https://r.ru/changed-2", ChangeType.CHANGED),
        _diff("https://r.ru/new-2", ChangeType.NEW),
    ]

    selected = select_diffs_for_comparison(diffs, limit=3)

    assert [d.url for d in selected] == [
        "https://r.ru/new-1",
        "https://r.ru/new-2",
        "https://r.ru/changed-1",
    ]


def test_limit_caps_number_of_ai_calls():
    # Главная защита от разорения: 200 новых страниц не должны дать 200 вызовов ИИ.
    diffs = [_diff(f"https://r.ru/p{i}", ChangeType.NEW) for i in range(200)]

    assert len(select_diffs_for_comparison(diffs, limit=10)) == 10


def test_zero_limit_disables_comparison_completely():
    diffs = [_diff("https://r.ru/new", ChangeType.NEW)]

    assert select_diffs_for_comparison(diffs, limit=0) == []


# --- когда сравнение молча пропускается ---


class _FakeSession:
    """Минимальная замена сессии SQLAlchemy: хранит добавленные объекты."""

    def __init__(self, competitors=(), pages=()):
        self.added = []
        self._competitors = list(competitors)
        self._pages = list(pages)

    def add(self, obj):
        self.added.append(obj)

    def query(self, model):
        rows = self._competitors if model is Competitor else self._pages
        return _FakeQuery(rows)


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args):
        return self

    def order_by(self, *args):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


def test_comparison_is_skipped_when_own_site_is_not_configured():
    db = _FakeSession(competitors=[])
    competitor = Competitor(id=1, name="Конкурент", base_url="https://r.ru", is_own=False)

    own_site = asyncio.run(_load_own_site_for_comparison(db, competitor))

    assert own_site is None


def test_comparison_is_skipped_when_own_site_was_never_crawled():
    own = Competitor(id=2, name="Наш сайт", base_url="https://og1.ru", is_own=True)
    db = _FakeSession(competitors=[own], pages=[])

    assert load_own_site(db) is None


def test_own_site_is_not_compared_with_itself():
    own = Competitor(id=2, name="Наш сайт", base_url="https://og1.ru", is_own=True)
    db = _FakeSession(competitors=[own])

    assert asyncio.run(_load_own_site_for_comparison(db, own)) is None


def test_load_own_site_takes_latest_snapshot_of_each_page():
    own = Competitor(id=2, name="Наш сайт", base_url="https://og1.ru", is_own=True)
    page = Page(id=10, competitor_id=2, url="https://og1.ru/ege", title="ЕГЭ", is_removed=False)
    page.snapshots = [
        PageSnapshot(page_id=10, content_hash="a", text_content="старый текст"),
        PageSnapshot(page_id=10, content_hash="b", text_content="новый текст"),
    ]
    db = _FakeSession(competitors=[own], pages=[page])

    own_site = load_own_site(db)

    assert own_site is not None
    assert own_site.base_url == "https://og1.ru"
    assert [p.text for p in own_site.pages] == ["новый текст"]


def test_get_own_site_returns_none_when_not_configured():
    assert get_own_site(_FakeSession(competitors=[])) is None


# --- вызов ИИ ---


def test_comparison_without_candidates_does_not_call_ai(monkeypatch):
    called = False

    async def _fail(_prompt, **_kwargs):
        nonlocal called
        called = True
        return "{}"

    monkeypatch.setattr(compare_module, "run_claude_cli", _fail)
    unrelated = PageDiff(
        url="https://r.ru/trucks",
        change_type=ChangeType.NEW,
        new_text="Ремонт грузовых двигателей и токарные работы",
    )

    result = asyncio.run(compare_with_own_site(unrelated, OWN_SITE))

    assert result is None
    assert called is False


def test_comparison_calls_ai_once_and_returns_result(monkeypatch):
    calls = []

    async def _fake(prompt, **_kwargs):
        calls.append(prompt)
        return '{"verdict": "similar", "our_url": "https://og1.ru/ege", "differences": "цена", "missing": "отзывы"}'

    monkeypatch.setattr(compare_module, "run_claude_cli", _fake)

    result = asyncio.run(compare_with_own_site(_diff("https://r.ru/ege", ChangeType.NEW), OWN_SITE))

    assert len(calls) == 1
    assert result.verdict == "similar"
    assert result.our_url == "https://og1.ru/ege"


def test_crawl_continues_when_ai_is_unavailable(monkeypatch):
    async def _broken(_page_diff, _own_site):
        raise ClaudeCliError("CLI недоступен")

    monkeypatch.setattr("app.pipeline.compare_with_own_site", _broken)
    db = _FakeSession()
    page_change = PageChange(id=5, page_id=1, change_type=ChangeType.NEW)

    result = asyncio.run(
        _compare_with_own_site_and_store(db, page_change, _diff("https://r.ru/x", ChangeType.NEW), OWN_SITE)
    )

    assert result is None
    assert db.added == []


def test_successful_comparison_is_stored(monkeypatch):
    async def _ok(_page_diff, _own_site):
        return ComparisonResult(
            verdict="similar",
            our_url="https://og1.ru/ege",
            differences="У конкурента указана цена",
            missing="Добавить отзывы",
            raw_response={"verdict": "similar"},
        )

    monkeypatch.setattr("app.pipeline.compare_with_own_site", _ok)
    db = _FakeSession()
    page_change = PageChange(id=5, page_id=1, change_type=ChangeType.NEW)

    result = asyncio.run(
        _compare_with_own_site_and_store(db, page_change, _diff("https://r.ru/x", ChangeType.NEW), OWN_SITE)
    )

    assert result is not None
    assert len(db.added) == 1
    stored = db.added[0]
    assert stored.page_change_id == 5
    assert stored.verdict.value == "similar"
    assert stored.our_page_url == "https://og1.ru/ege"
    assert stored.missing == "Добавить отзывы"


@pytest.mark.parametrize("is_own", [True, False])
def test_progress_notifications_are_sent_only_for_competitors(is_own, monkeypatch):
    from app import pipeline

    sent = []

    async def _fake_notify(_notifier, _db, competitor, text, *, page_change_id):
        sent.append(text)

    monkeypatch.setattr(pipeline, "_notify", _fake_notify)
    competitor = Competitor(id=1, name="Сайт", base_url="https://x.ru", is_own=is_own)

    asyncio.run(pipeline._notify_progress(None, None, competitor, "обход начат"))

    assert sent == ([] if is_own else ["обход начат"])
