"""Структура страницы для SEO-разбора (app/crawler/structure.py): мета-теги, H1,
оглавление, основной текст без меню и сайдбаров — и что она не трогает текст для
сравнения обходов. Плюс таблица page_structures в миграции 0013."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from app.content_kind import is_info_url
from app.crawler import crawl as crawl_module
from app.crawler.crawl import PreviousPageInfo, SitemapEntry, crawl_competitor, plan_crawl
from app.crawler.diff import extract_text
from app.crawler.structure import PageStructure, extract_structure, structure_from_apify_item
from app.models import PageStructureRecord

_HEAD = """
<head>
  <title> Окончание слов — что это такое? </title>
  <meta name="description" content="Что такое окончание и какие окончания бывают.">
  <meta property="og:description" content="другое описание">
</head>
"""

_MENU = """
<header><nav><a href="/">Главная</a><a href="/courses">Курсы</a></nav></header>
"""

_FOOTER = """
<footer><p>© Школа, 2026. Все права защищены. Политика конфиденциальности.</p></footer>
"""

_PARAGRAPH = "Окончание — изменяемая часть слова, которая образует форму слова. " * 6


def _article_page(body: str) -> str:
    return f"<html>{_HEAD}<body>{_MENU}<main>{body}</main>{_FOOTER}</body></html>"


def test_meta_h1_and_outline_in_order():
    html = _article_page(
        f"""
        <article>
          <h1>Окончание слов</h1>
          <p>{_PARAGRAPH}</p>
          <h2>Виды окончаний</h2><p>{_PARAGRAPH}</p>
          <h3>Нулевое окончание</h3><p>{_PARAGRAPH}</p>
          <h2>Как найти окончание</h2><p>{_PARAGRAPH}</p>
        </article>
        """
    )

    structure = extract_structure(html)

    assert structure.meta_title == "Окончание слов — что это такое?"
    assert structure.meta_description == "Что такое окончание и какие окончания бывают."
    assert structure.h1 == "Окончание слов"
    assert structure.headings == [
        (2, "Виды окончаний"),
        (3, "Нулевое окончание"),
        (2, "Как найти окончание"),
    ]


def test_main_text_has_no_menu_footer_sidebar_or_related_cards():
    html = _article_page(
        f"""
        <div class="breadcrumbs"><a href="/">Главная</a> / <a href="/articles">Статьи</a></div>
        <article class="post">
          <h1>Окончание слов</h1>
          <p>Вступление: <a href="/x">ссылка внутри</a> абзаца не рвёт предложение.</p>
          <p>{_PARAGRAPH}</p>
          <div class="share">Поделиться ВКонтакте Телеграм</div>
          <form><input placeholder="Телефон"><button>Записаться на курс</button></form>
        </article>
        <aside><h2>Популярное</h2><p>Боковая колонка с рекламой курса.</p></aside>
        <section>
          <h2>Похожие статьи</h2>
          <article class="card"><a href="/a1">Суффиксы в русском языке</a></article>
          <article class="card"><a href="/a2">Приставки</a></article>
        </section>
        """
    )

    structure = extract_structure(html)

    assert "Вступление: ссылка внутри абзаца не рвёт предложение." in structure.main_text
    assert "изменяемая часть слова" in structure.main_text
    for noise in ("Главная", "Курсы", "Поделиться", "Записаться", "Боковая колонка", "Суффиксы", "права защищены"):
        assert noise not in structure.main_text
    assert structure.headings == []  # «Популярное» и «Похожие статьи» — не оглавление статьи


def test_short_article_with_h1_wins_over_main_full_of_reviews():
    # Термин глоссария (onlineschool-1.ru): пара строк в <article> с H1, а в <main>
    # вокруг — длинная карусель отзывов и форма заявки.
    reviews = "".join(f"<p>Отзыв {i}: очень понравилось заниматься, спасибо учителям!</p>" for i in range(30))
    html = _article_page(
        f"""
        <article class="glossary-term-page">
          <h1>Калориметр</h1>
          <p>Прибор для измерения количества теплоты, выделяемой или поглощаемой телом.</p>
        </article>
        <div class="reviews">{reviews}</div>
        """
    )

    structure = extract_structure(html)

    assert structure.main_text.startswith("Калориметр\nПрибор для измерения")
    assert "Отзыв" not in structure.main_text


def test_article_body_microdata_is_preferred():
    html = _article_page(
        f"""
        <div class="page">
          <h1>Тепловой эффект</h1>
          <div class="banner-top">+20 баллов на ЕГЭ</div>
          <div itemprop="articleBody"><p>{_PARAGRAPH}</p><h2>Практика</h2><p>{_PARAGRAPH}</p></div>
        </div>
        """
    )

    structure = extract_structure(html)

    assert structure.h1 == "Тепловой эффект"  # H1 — со всей страницы, даже вне основного блока
    assert structure.headings == [(2, "Практика")]
    assert "+20 баллов" not in structure.main_text


def test_page_without_semantic_tags_uses_densest_paragraph_block():
    html = (
        "<html><head><title>T</title></head><body>"
        '<div class="menu"><a href="/1">Раз</a><a href="/2">Два</a></div>'
        f'<div class="content"><h1>Заголовок</h1><p>{_PARAGRAPH}</p><p>{_PARAGRAPH}</p></div>'
        '<div class="footer"><p>Телефон 8 800</p></div>'
        "</body></html>"
    )

    structure = extract_structure(html)

    assert "изменяемая часть" in structure.main_text
    assert "Телефон" not in structure.main_text
    assert "Раз" not in structure.main_text


def test_missing_meta_falls_back_to_open_graph():
    html = (
        '<html><head><meta property="og:title" content="OG заголовок">'
        '<meta property="og:description" content="OG описание"></head><body><p>Текст</p></body></html>'
    )

    structure = extract_structure(html)

    assert structure.meta_title == "OG заголовок"
    assert structure.meta_description == "OG описание"
    assert structure.h1 is None


def test_structure_does_not_change_text_for_comparison():
    # Текст для диффа (extract_text) — тот же, что и до появления структуры: иначе
    # следующий обход объявил бы изменёнными все страницы.
    html = _article_page(f"<article><h1>Окончание слов</h1><p>{_PARAGRAPH}</p></article>")
    before = extract_text(html)

    extract_structure(html)

    assert extract_text(html) == before
    assert before.startswith("Окончание слов — что это такое?")  # <title> в тексте для сравнения, как раньше


def test_structure_round_trips_through_dict():
    structure = PageStructure("T", "D", "H", [(2, "a"), (3, "b")], "текст")

    assert PageStructure.from_dict(structure.to_dict()) == structure


def test_apify_item_gives_meta_h1_and_outline_from_markdown():
    item = {
        "url": "https://x.ru/articles/a",
        "text": "Окончание слов\nТекст статьи\n\nВиды окончаний\nЕщё текст",
        "markdown": "# Окончание слов\n\nТекст статьи\n\n## Виды окончаний\n\n### **Нулевое**\n\n## Похожие статьи",
        "metadata": {"title": "Окончание — Skysmart", "description": "Описание статьи"},
    }

    structure = structure_from_apify_item(item)

    assert structure.meta_title == "Окончание — Skysmart"
    assert structure.meta_description == "Описание статьи"
    assert structure.h1 == "Окончание слов"
    assert structure.headings == [(2, "Виды окончаний"), (3, "Нулевое")]
    assert structure.main_text == "Окончание слов\nТекст статьи\nВиды окончаний\nЕщё текст"


def test_apify_item_without_markdown_has_meta_only():
    structure = structure_from_apify_item({"text": "Текст", "metadata": {"title": "T"}})

    assert structure.meta_title == "T"
    assert structure.h1 is None
    assert structure.headings == []


def test_info_sections_of_all_tracked_sites():
    for url in (
        "https://skysmart.ru/articles/russian/okonchanie-slov",
        "https://foxford.ru/wiki/fizika/kalorimetr",
        "https://onlineschool-1.ru/blog/education/9-luchshih-filmov-pro-shkolu",
        "https://onlineschool-1.ru/questions/gdye-raspolozhyena-gora-elbrus",
        "https://onlineschool-1.ru/glossary/fizika/kalorimetr",
        "https://onlineschool-1.ru/news/x",
        "https://og1.ru/polza/test-na-gotovnost",
        "https://og1.ru/novosti/x",
        "https://og1.ru/novosti-obrazovaniya/x",
        "https://og1.ru/videouroki/khimiya/11-klass-hi/dispersionnye-sistemy",
    ):
        assert is_info_url(url), url
    for url in ("https://skysmart.ru/courses/ege", "https://foxford.ru/catalog/x", "https://og1.ru/kursy/ege"):
        assert not is_info_url(url), url


# --- обход: структура в результате, догрузка статей без структуры ---


def test_plan_refetches_pages_without_structure_even_with_same_date():
    from datetime import UTC, datetime

    day = datetime(2026, 1, 1, tzinfo=UTC)
    entries = [SitemapEntry(url="https://x.ru/articles/a", lastmod=day), SitemapEntry(url="https://x.ru/b", lastmod=day)]
    previous = {entry.url: PreviousPageInfo(text="старый", lastmod=day) for entry in entries}

    plan = plan_crawl(
        entries,
        previous_pages=previous,
        force_full=False,
        max_fetch=0,
        max_undated=100,
        refetch={"https://x.ru/articles/a"},
    )

    assert plan.to_fetch == ["https://x.ru/articles/a"]
    assert plan.carry_over == {"https://x.ru/b": "старый"}


@pytest.mark.asyncio
async def test_crawl_result_keeps_structure_of_fetched_pages_only(monkeypatch):
    monkeypatch.setattr(crawl_module.settings, "crawl_request_delay_seconds", 0)
    urls = ["https://x.ru/articles/a", "https://x.ru/articles/b"]

    async def fake_discover(base_url, sitemap_url=None):
        return [SitemapEntry(url=u, lastmod=None) for u in urls]

    async def fake_fetch(context, url):
        return f"title {url}", f"text {url}", PageStructure(h1=f"H1 {url}")

    async def cache_lookup(url):
        return ("из черновика", "text из черновика") if url.endswith("/b") else None

    monkeypatch.setattr(crawl_module, "discover_sitemap_entries", fake_discover)
    monkeypatch.setattr(crawl_module, "fetch_page_text", fake_fetch)

    result = await crawl_competitor(context=None, base_url="https://x.ru", cache_lookup=cache_lookup)

    assert set(result.pages) == set(urls)
    # у страницы из черновика структуры нет — в базе останется прежняя
    assert {url: s.h1 for url, s in result.structures.items()} == {urls[0]: f"H1 {urls[0]}"}


# --- миграция 0013 ---


def _load_migration():
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0013_page_structure_and_tracked_sections.py"
    spec = importlib.util.spec_from_file_location("migration_0013", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_creates_table_matching_model_and_remembers_current_filter():
    migration = _load_migration()
    assert migration.down_revision == "0012"
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE pages (id INTEGER PRIMARY KEY)"))
        conn.execute(
            text("CREATE TABLE competitors (id INTEGER PRIMARY KEY, include_paths TEXT, last_crawl_finished_at TEXT)")
        )
        conn.execute(
            text(
                "INSERT INTO competitors VALUES (1, '!/articles', '2026-10-19'), (2, NULL, '2026-10-19'), (3, NULL, NULL)"
            )
        )
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()

        tracked = dict(conn.execute(text("SELECT id, tracked_include_paths FROM competitors")).all())
        # уже обойдённые сайты: фильтр записан как есть («весь сайт» — пустая строка);
        # ни разу не обойдённый — null, его первый обход и так точка отсчёта
        assert tracked == {1: "!/articles", 2: "", 3: None}

    columns = {column["name"] for column in inspect(engine).get_columns("page_structures")}
    assert columns == {column.name for column in PageStructureRecord.__table__.columns}

    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        migration.downgrade()
    assert "page_structures" not in inspect(engine).get_table_names()
