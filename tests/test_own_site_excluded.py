"""Наш сайт не должен вести себя как конкурент.

Самая частая ошибка в такой задаче — забыть фильтр в одном из мест, где
перечисляются конкуренты. Поэтому проверяем разом ленту изменений, счётчики
дашборда, список конкурентов и оба API. База — временная SQLite в памяти,
реальные данные владельца не трогаются.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.crawl_manager import start_all
from app.db import Base, get_db
from app.main import app
from app.models import (
    ChangeType,
    ComparisonVerdict,
    Competitor,
    OwnSiteComparison,
    Page,
    PageChange,
    SessionStatus,
)
from app.routers.ui import _paginate_changes

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    rival = Competitor(
        name="Конкурент", base_url="https://rival.ru", is_own=False, status=SessionStatus.ACTIVE
    )
    own = Competitor(
        name="Наш сайт", base_url="https://og1.ru", is_own=True, status=SessionStatus.ACTIVE
    )
    session.add_all([rival, own])
    session.flush()

    for competitor, url in ((rival, "https://rival.ru/ege"), (own, "https://og1.ru/ege")):
        page = Page(competitor_id=competitor.id, url=url, first_seen_at=NOW, last_seen_at=NOW)
        session.add(page)
        session.flush()
        session.add(
            PageChange(page_id=page.id, change_type=ChangeType.NEW, detected_at=NOW - timedelta(days=1))
        )
    session.commit()

    yield session
    session.close()


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    # Без контекстного менеджера: он запустил бы lifespan приложения, а тот лезет
    # в настоящую базу и заводит задачи планировщика — тестам это не нужно.
    test_client = TestClient(app)
    test_client.auth = (settings.basic_auth_username, settings.basic_auth_password)
    yield test_client
    app.dependency_overrides.clear()


def test_changes_feed_shows_only_competitor_pages(db):
    result = _paginate_changes(db, [], 1)

    assert result["total"] == 1
    assert result["items"][0].page.url == "https://rival.ru/ege"


def test_dashboard_counters_ignore_own_site(client):
    response = client.get("/")

    assert response.status_code == 200
    # Конкурент в базе один: если бы наш сайт считался вторым, счётчик показал бы 2.
    assert '<div class="value">1</div>' in response.text
    assert '<div class="value">2</div>' not in response.text
    assert "og1.ru" not in response.text


def test_competitors_page_does_not_list_own_site_as_a_competitor(client):
    response = client.get("/competitors")

    assert response.status_code == 200
    assert "rival.ru" in response.text
    # Наш сайт на странице есть, но отдельной карточкой с пометкой «наш сайт».
    assert "наш сайт" in response.text
    assert response.text.count("https://og1.ru") == 1


def test_changes_api_excludes_own_site(client):
    response = client.get("/api/changes")

    assert response.status_code == 200
    assert [item["page_url"] for item in response.json()] == ["https://rival.ru/ege"]


def test_competitors_api_excludes_own_site(client):
    response = client.get("/api/competitors")

    assert response.status_code == 200
    assert [item["base_url"] for item in response.json()] == ["https://rival.ru"]


def test_own_site_api_returns_the_own_site(client):
    response = client.get("/api/own-site")

    assert response.status_code == 200
    assert response.json()["base_url"] == "https://og1.ru"
    assert response.json()["is_own"] is True


def test_own_site_can_be_saved_from_the_interface(client, db):
    response = client.put("/api/own-site", json={"base_url": "https://og1.ru/new"})

    assert response.status_code == 200
    assert db.query(Competitor).filter(Competitor.is_own.is_(True)).count() == 1
    assert response.json()["base_url"] == "https://og1.ru/new"


def test_own_site_address_must_look_like_a_link(client):
    response = client.put("/api/own-site", json={"base_url": "og1.ru"})

    assert response.status_code == 422


def test_settings_page_shows_own_site(client):
    response = client.get("/settings")

    assert response.status_code == 200
    assert "https://og1.ru" in response.text
    assert "Наш сайт" in response.text


def test_crawl_all_competitors_does_not_touch_own_site(db, monkeypatch):
    spawned = []
    monkeypatch.setattr("app.crawl_manager._spawn", lambda competitor_id: spawned.append(competitor_id))

    result = start_all(db)

    own_id = db.query(Competitor).filter(Competitor.is_own.is_(True)).one().id
    assert result.started == 1
    assert own_id not in spawned


def test_own_site_crawl_button_goes_through_the_crawl_manager(client, db, monkeypatch):
    spawned = []
    monkeypatch.setattr("app.crawl_manager._spawn", lambda competitor_id: spawned.append(competitor_id))

    response = client.post("/api/own-site/crawl")

    own_id = db.query(Competitor).filter(Competitor.is_own.is_(True)).one().id
    assert response.status_code == 202
    assert spawned == [own_id]


def test_second_click_on_own_site_crawl_is_rejected(client, monkeypatch):
    monkeypatch.setattr("app.crawl_manager._spawn", lambda competitor_id: None)

    assert client.post("/api/own-site/crawl").status_code == 202
    assert client.post("/api/own-site/crawl").status_code == 409


def test_change_detail_shows_comparison_with_our_site(client, db):
    change = db.query(PageChange).join(Page).filter(Page.url == "https://rival.ru/ege").one()
    db.add(
        OwnSiteComparison(
            page_change_id=change.id,
            verdict=ComparisonVerdict.SIMILAR,
            our_page_url="https://og1.ru/ege",
            differences="У конкурента указана цена от 2900 ₽, у нас цены нет.",
            missing="Добавить блок с отзывами учеников.",
        )
    )
    db.commit()

    response = client.get(f"/changes/{change.id}")

    assert response.status_code == 200
    assert "У нас похожее есть" in response.text
    assert "https://og1.ru/ege" in response.text
    assert "2900" in response.text
    assert "Добавить блок с отзывами учеников." in response.text
