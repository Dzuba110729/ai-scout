"""Этап 5 — Google Таблица SEO-находок (app/integrations/seo_sheet.py): строки по темам,
склейка конкурентов, недели по Москве, запись листа (Sheets API подменён)."""

import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.integrations import seo_sheet
from app.integrations.seo_sheet import HEADER, SheetFinding, build_sheet_rows, week_bounds
from app.models import Competitor, Page, SeoFinding, TopicDecision

MSK = ZoneInfo("Europe/Moscow")


def _item(**overrides):
    data = {
        "decision_id": 1,
        "topic": "Окончание слова",
        "main_intent": "узнать, что такое окончание",
        "material_type": "статья",
        "decision": "update",
        "our_url": "https://og1.ru/polza/okonchanie",
        "add_subintents": ["нулевое окончание", "двойные окончания"],
        "argument": "У нас нет примеров.",
        "cannibal_urls": [],
        "subintents": ["виды окончаний", "нулевое окончание", "как найти", "примеры", "упражнения"],
        "competitor": "skysmart.ru",
        "url": "https://skysmart.ru/articles/okonchanie",
        "kind": "new",
        "detected_at": datetime(2026, 10, 19, 10, tzinfo=UTC),
    }
    data.update(overrides)
    return SheetFinding(**data)


def test_one_row_per_topic_with_all_competitors_in_one_cell():
    rows = build_sheet_rows(
        [
            _item(),
            _item(competitor="foxford.ru", url="https://foxford.ru/wiki/okonchanie", detected_at=datetime(2026, 10, 20, tzinfo=UTC)),
        ],
        tz=MSK,
    )

    assert len(rows) == 1
    row = dict(zip(HEADER, rows[0], strict=True))
    assert row["Дата"] == "19.10.2026"
    assert row["Действие"] == "создание"
    assert row["Конкурент"] == "skysmart.ru\nfoxford.ru"
    assert row["URL"] == "https://skysmart.ru/articles/okonchanie\nhttps://foxford.ru/wiki/okonchanie"
    assert row["Краткое описание"] == "Окончание слова. Интент: узнать, что такое окончание"
    assert row["УТП (подинтент)"] == "Подинтенты: виды окончаний; нулевое окончание; как найти; примеры (и ещё 1)"
    assert row["Наличие у нас"] == "Частично\nhttps://og1.ru/polza/okonchanie"
    assert row["Что добавить"] == "Обновить нашу статью: добавить нулевое окончание; двойные окончания. У нас нет примеров."


def test_changed_article_shows_added_subintent_and_new_topic_and_cannibalization():
    rows = build_sheet_rows(
        [
            _item(kind="changed", added_subintents=["советы по сдаче"]),
            _item(
                decision_id=2,
                decision="new",
                our_url=None,
                argument=None,
                topic="ВПР 4 класс",
                cannibal_urls=["https://og1.ru/polza/a", "https://og1.ru/polza/b"],
            ),
        ],
        tz=MSK,
    )

    assert rows[0][1] == "изменение"
    assert rows[0][6] == "Добавлен подинтент: советы по сдаче"
    assert rows[1][7] == "Нет\n⚠️ Риск каннибализации:\nhttps://og1.ru/polza/a\nhttps://og1.ru/polza/b"
    assert rows[1][8] == "Написать новую статью."


def test_week_is_monday_to_sunday_in_moscow():
    # Воскресенье 22:30 UTC — по Москве это уже понедельник следующей недели.
    start, end, title = week_bounds(datetime(2026, 10, 25, 22, 30, tzinfo=UTC), tz=MSK)

    assert start == datetime(2026, 10, 26, tzinfo=MSK)
    assert title == "Неделя 26.10–01.11.2026"
    assert (end - start).days == 7


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


def _finding(db, kind, when):
    site = Competitor(name="Skysmart", base_url="https://www.skysmart.ru/")
    db.add(site)
    db.flush()
    page = Page(competitor_id=site.id, url=f"https://skysmart.ru/articles/{kind}")
    decision = TopicDecision(topic="Т", main_intent="И", decision="new")
    db.add_all([page, decision])
    db.flush()
    db.add(SeoFinding(decision_id=decision.id, page_id=page.id, competitor_id=site.id, kind=kind, detected_at=when))
    db.flush()


def test_week_sheet_takes_only_new_and_changed_of_that_week_and_audit_its_own(db):
    _finding(db, "new", datetime(2026, 10, 20, tzinfo=UTC))
    _finding(db, "changed", datetime(2026, 10, 12, tzinfo=UTC))  # прошлая неделя
    _finding(db, "audit", datetime(2026, 10, 20, tzinfo=UTC))
    start, end, _title = week_bounds(datetime(2026, 10, 21, tzinfo=UTC))

    week = seo_sheet.load_sheet_findings(db, start=start, end=end)
    audit = seo_sheet.load_sheet_findings(db, audit=True)

    assert [(f.kind, f.competitor) for f in week] == [("new", "skysmart.ru")]
    assert [f.kind for f in audit] == ["audit"]


class _Call:
    def __init__(self, log, name, kwargs, result=None):
        self.log, self.name, self.kwargs, self.result = log, name, kwargs, result

    def execute(self):
        self.log.append((self.name, self.kwargs))
        return self.result


class _FakeSheets:
    """Минимальная подмена googleapiclient для spreadsheets()/values()."""

    def __init__(self, existing_titles):
        self.log: list = []
        self.titles = dict(existing_titles)

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, **kwargs):
        sheets = [{"properties": {"title": t, "sheetId": i}} for t, i in self.titles.items()]
        return _Call(self.log, "get", kwargs, {"sheets": sheets})

    def batchUpdate(self, **kwargs):  # имя метода задано googleapiclient
        requests = kwargs["body"]["requests"]
        if "addSheet" in requests[0]:
            return _Call(self.log, "addSheet", kwargs, {"replies": [{"addSheet": {"properties": {"sheetId": 77}}}]})
        return _Call(self.log, "format", kwargs, {})

    def clear(self, **kwargs):
        return _Call(self.log, "clear", kwargs)

    def update(self, **kwargs):
        return _Call(self.log, "update", kwargs)


def test_write_sheet_adds_missing_sheet_rewrites_values_and_formats(monkeypatch):
    fake = _FakeSheets({"Неделя 12.10–18.10.2026": 5})
    client = seo_sheet.SeoSheetClient()
    monkeypatch.setattr(client, "_sheets", lambda: fake)

    sheet_id = client.write_sheet("SID", "Неделя 19.10–25.10.2026", [["a"] * 9])

    assert sheet_id == 77
    names = [name for name, _kwargs in fake.log]
    assert names == ["get", "addSheet", "clear", "update", "format"]
    update = dict(fake.log)["update"]
    assert update["range"] == "'Неделя 19.10–25.10.2026'!A1"
    assert update["body"]["values"][0] == HEADER
    add = dict(fake.log)["addSheet"]["body"]["requests"][0]["addSheet"]["properties"]
    assert add["index"] == 0  # свежая неделя — первым листом


def test_existing_sheet_is_rewritten_not_duplicated(monkeypatch):
    fake = _FakeSheets({"Стартовый аудит": 3})
    client = seo_sheet.SeoSheetClient()
    monkeypatch.setattr(client, "_sheets", lambda: fake)

    assert client.write_sheet("SID", "Стартовый аудит", [], index=None) == 3
    assert [name for name, _kwargs in fake.log] == ["get", "clear", "update", "format"]


def test_crawl_message_links_the_sheet(monkeypatch):
    from app.notifications.telegram import report_buttons

    buttons = report_buttons(1, "https://docs/r", None, seo_sheet_url="https://sheets/x")

    assert {"text": "📊 SEO-таблица: инфостатьи", "url": "https://sheets/x"} in [b for row in buttons for b in row]
    assert asyncio.iscoroutinefunction(seo_sheet.export_week) is False  # зовётся через to_thread
