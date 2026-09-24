"""Оценка важности находок и еженедельный дайджест.

ИИ не вызывается: проверяем разбор его ответа, порядок строк в отчёте, фильтр
«🔥 Важные» в боте и сборку дайджеста из данных временной SQLite.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import bot_views, digest
from app.ai.analyze import (
    AiAnalysisResult,
    importance_rank,
    normalize_importance,
    parse_ai_response,
)
from app.crawler.diff import ChangeType as DiffChangeType
from app.crawler.diff import PageDiff
from app.db import Base
from app.integrations.google_docs import build_row
from app.models import (
    AiAnalysis,
    ChangeType,
    ComparisonVerdict,
    Competitor,
    OwnSiteComparison,
    Page,
    PageChange,
    SessionStatus,
)
from app.notifications.telegram import format_important_summary

NOW = datetime.now(UTC)


def test_importance_is_parsed_and_normalized():
    result = parse_ai_response(
        '{"summary": "Снизили цену", "importance": "HIGH", "importance_reason": "Цена курса ниже нашей"}'
    )

    assert result.importance == "high"
    assert result.importance_reason == "Цена курса ниже нашей"


@pytest.mark.parametrize(
    ("raw", "expected"), [("low", "low"), ("Мелочь", "low"), ("средняя", "medium"), ("??", None), (3, None)]
)
def test_normalize_importance(raw, expected):
    assert normalize_importance(raw) == expected


def test_unknown_importance_ranks_as_medium():
    assert importance_rank("high") < importance_rank(None) == importance_rank("medium") < importance_rank("low")


def _analysis(importance, reason=None):
    return AiAnalysisResult(
        category=None, usp=None, cta=None, summary="Суть", raw_response={},
        importance=importance, importance_reason=reason,
    )


def test_report_row_marks_important_and_explains_why():
    diff = PageDiff(url="https://x.ru/sale", change_type=DiffChangeType.NEW, old_text=None, new_text="t")

    row = build_row(diff, _analysis("high", "Новая акция"), NOW)

    assert row[1] == "Новая страница\n🔥 Важно"
    assert "Почему важно: Новая акция" in row[6]


def test_report_row_without_importance_is_unchanged():
    diff = PageDiff(url="https://x.ru/a", change_type=DiffChangeType.NEW, old_text=None, new_text="t")

    assert build_row(diff, _analysis(None), NOW)[1] == "Новая страница"


def test_important_summary_limits_list():
    items = [(f"https://x.ru/{i}", _analysis("high", f"причина {i}")) for i in range(7)]

    text = format_important_summary(items, limit=5)

    assert text.startswith("🔥 Важное:")
    assert "причина 4" in text and "причина 5" not in text
    assert "Ещё важных находок: 2" in text


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    fox = Competitor(
        name="Фоксфорд", base_url="https://foxford.ru", status=SessionStatus.ACTIVE,
        last_crawl_finished_at=NOW - timedelta(days=1), last_report_url="https://docs/fox",
    )
    stale = Competitor(
        name="Умскул", base_url="https://umschool.net", status=SessionStatus.ACTIVE,
        last_crawl_finished_at=NOW - timedelta(days=20),
    )
    session.add_all([fox, stale])
    session.flush()

    def add(competitor, slug, importance, days_ago, verdict=None):
        page = Page(competitor_id=competitor.id, url=f"{competitor.base_url}/{slug}", title=slug)
        session.add(page)
        session.flush()
        change = PageChange(page_id=page.id, change_type=ChangeType.NEW, detected_at=NOW - timedelta(days=days_ago))
        session.add(change)
        session.flush()
        session.add(AiAnalysis(page_change_id=change.id, summary=f"про {slug}", importance=importance))
        if verdict:
            session.add(OwnSiteComparison(page_change_id=change.id, verdict=verdict, missing="Нет такого курса"))

    add(fox, "sale", "high", 1, ComparisonVerdict.NONE)
    add(fox, "blog", "low", 2)
    add(fox, "reviews", "medium", 3)
    add(fox, "old-sale", "high", 30)  # за пределами недели
    session.commit()
    yield session
    session.close()


def test_important_filter_in_bot_feed(db):
    result = bot_views.load_changes(db, kind="important", page_size=50)

    assert {c.page.url for c in result.items} == {"https://foxford.ru/sale", "https://foxford.ru/old-sale"}
    assert bot_views.change_line(result.items[0]).startswith("🔥🆕")


def test_digest_puts_important_first_and_skips_trivia(db):
    text = digest.build_digest(db, days=7, overview="Фоксфорд запустил распродажу.", now=NOW).text

    top = text.split("Самое важное:")[1]
    assert top.index("про sale") < top.index("про reviews")
    assert "про blog" not in text  # мелочь в дайджест не идёт
    assert "old-sale" not in text  # старше недели
    assert "Главное:\nФоксфорд запустил распродажу." in text
    assert "Нет такого курса" in text
    assert "Умскул: не обходился" in text
    assert "Фоксфорд: 🆕 3, из них 🔥 1" in text


def test_digest_buttons_link_reports_and_important_feed(db):
    buttons = digest.build_digest(db, days=7, now=NOW).buttons

    flat = [b for row in buttons for b in row]
    assert {"text": "📄 Фоксфорд", "url": "https://docs/fox"} in flat
    assert any(b.get("callback_data") == "chg:all:important:1:n" for b in flat)


def test_digest_without_competitors(db):
    db.query(Competitor).delete()
    db.commit()

    assert "Конкурентов пока нет" in digest.build_digest(db, now=NOW).text


def test_overview_prompt_orders_by_importance(db):
    changes = digest._period_changes(db, NOW - timedelta(days=7))

    prompt = digest.build_overview_prompt(changes, 7)

    assert prompt.index("про sale") < prompt.index("про reviews") < prompt.index("про blog")
