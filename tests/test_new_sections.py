"""Новый отслеживаемый раздел — точка отсчёта, а не тысяча «новых страниц».

Владелец убирает «!/articles» у skysmart.ru или добавляет вики foxford.ru в
include_paths: следующий обход не должен звать ИИ на каждую статью и раздувать отчёт.
Но статья, которую конкурент опубликовал в уже отслеживаемом разделе, — находка, сколько
бы их ни вышло разом. Плюс сохранение структуры страниц (page_structures).

Весь run_crawl_for_competitor на временной SQLite, обход/ИИ/отчёт/Telegram подменены.
"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import pipeline, seo
from app.ai.analyze import AiAnalysisResult
from app.ai.intent import IntentCard
from app.crawler.crawl import CrawlResult
from app.crawler.structure import PageStructure
from app.db import Base
from app.models import (
    ArticleIntent,
    ChangeType,
    Competitor,
    Page,
    PageChange,
    PageStructureRecord,
    SeoFinding,
    SessionStatus,
)

SITE = "https://skysmart.ru"


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    state = {"pages": {}, "structures": {}, "ai_calls": 0, "sent": [], "reports": [], "crawl_kwargs": []}

    async def fake_crawl(_db, competitor, _previous, _force_full, **kwargs):
        state["crawl_kwargs"].append(kwargs)
        return CrawlResult(
            base_url=competitor.base_url,
            pages=dict(state["pages"]),
            structures=dict(state["structures"]),
            urls_total=len(state["pages"]),
        )

    async def fake_analyze(page_diff):
        state["ai_calls"] += 1
        return AiAnalysisResult(
            category=None, usp=None, cta=None, summary=f"Суть {page_diff.url}", raw_response={}, importance="medium"
        )

    async def fake_report(_db, _competitor, _run_at, rows, notes, *, summary=None):
        state["reports"].append((rows, notes))
        return "https://docs/report"

    async def fake_notify(_notifier, _db, _competitor, text, *, page_change_id, buttons=None):
        state["sent"].append(text)

    async def no_recheck(diffs, previous_by_url):
        return {}

    monkeypatch.setattr(pipeline, "_crawl_competitor", fake_crawl)
    monkeypatch.setattr(pipeline, "analyze_page_change", fake_analyze)
    monkeypatch.setattr(pipeline, "_write_run_report", fake_report)
    monkeypatch.setattr(pipeline, "_notify", fake_notify)
    monkeypatch.setattr(pipeline, "_recheck_missing_pages", no_recheck)
    monkeypatch.setattr(pipeline, "_backfill_missing_analyses", lambda *_: asyncio.sleep(0, result=(0, 0)))
    monkeypatch.setattr(pipeline, "load_own_site", lambda _db: None)

    yield db, state
    db.close()


def _run(db, competitor):
    assert asyncio.run(pipeline.run_crawl_for_competitor(db, competitor))


def _articles(count, prefix="a"):
    return {f"{SITE}/articles/{prefix}{i}": f"статья {prefix}{i}" for i in range(count)}


def _rival(db, include_paths):
    rival = Competitor(name="Skysmart", base_url=SITE, status=SessionStatus.ACTIVE, include_paths=include_paths)
    db.add(rival)
    db.commit()
    return rival


def test_opened_section_is_a_baseline_but_real_new_pages_elsewhere_are_findings(env):
    db, state = env
    courses = {f"{SITE}/courses/ege": "ЕГЭ 4900 ₽", f"{SITE}/courses/oge": "ОГЭ"}
    # Статьи когда-то обходили (до исключения): в базе остались их старые тексты,
    # а /articles/gone с тех пор с сайта пропала.
    rival = _rival(db, None)
    state["pages"] = {**courses, **_articles(3), f"{SITE}/articles/gone": "старая статья"}
    _run(db, rival)

    rival.include_paths = "!/articles"
    state["pages"] = dict(courses)
    _run(db, rival)
    assert rival.tracked_include_paths == "!/articles"
    assert db.query(PageChange).count() == 0

    # Владелец снова открыл статьи. На сайте их уже 300 (старые три — с новым текстом),
    # а в курсах конкурент правда выпустил новую страницу.
    rival.include_paths = None
    articles = {**_articles(300), f"{SITE}/articles/a0": "статья a0, переписана"}
    state["pages"] = {**courses, f"{SITE}/courses/new": "новый курс", **articles}
    state["sent"].clear()
    _run(db, rival)

    changes = db.query(PageChange).all()
    assert [(c.page.url, c.change_type) for c in changes] == [(f"{SITE}/courses/new", ChangeType.NEW)]
    assert state["ai_calls"] == 1
    assert state["crawl_kwargs"][-1]["sections_added"] is True  # лимит страниц поднят на этот обход
    rows, notes = state["reports"][-1]
    assert len(rows) == 1
    assert any("Начали следить за разделом /articles: 300 стр." in note for note in notes)
    assert "Начали следить за разделом /articles: 300 стр." in state["sent"][-1]
    assert "Изменений найдено: 1" in state["sent"][-1]
    # пропавшая когда-то статья тихо снята с учёта — «удалённой» в следующем обходе не всплывёт
    assert db.query(Page).filter(Page.url == f"{SITE}/articles/gone").one().is_removed
    assert rival.tracked_include_paths == ""

    # Следующий обход: раздел уже отслеживается — новая статья в нём — настоящая находка,
    # правка старой — изменение; пропавшую давно статью «удалённой» не объявляем.
    state["pages"] = {
        **courses,
        f"{SITE}/courses/new": "новый курс",
        **articles,
        f"{SITE}/articles/a1": "статья a1 с новым разделом",
        f"{SITE}/articles/fresh": "свежая статья",
    }
    _run(db, rival)

    later = sorted((c.page.url, c.change_type.value) for c in db.query(PageChange).all()[1:])
    assert later == [(f"{SITE}/articles/a1", "changed"), (f"{SITE}/articles/fresh", "new")]
    assert state["crawl_kwargs"][-1]["sections_added"] is False


def test_many_new_pages_without_filter_change_are_real_findings(env):
    # Конкурент разом выпустил 40 статей в уже отслеживаемом разделе — это новость.
    db, state = env
    rival = _rival(db, "/articles")
    state["pages"] = _articles(5)
    _run(db, rival)

    state["pages"] = {**_articles(5), **_articles(40, prefix="new")}
    _run(db, rival)

    assert db.query(PageChange).count() == 40
    assert not any("Начали следить" in text for text in state["sent"])


def test_unknown_previous_filter_does_not_hide_new_pages(env):
    # Сайт обходили до появления tracked_include_paths и миграция его не заполнила:
    # сравнивать фильтры не с чем — все новые страницы считаем новыми, как раньше.
    db, state = env
    rival = _rival(db, "/courses")
    state["pages"] = {f"{SITE}/courses/ege": "ЕГЭ"}
    _run(db, rival)
    rival.tracked_include_paths = None
    rival.include_paths = None
    state["pages"] = {f"{SITE}/courses/ege": "ЕГЭ", **_articles(3)}

    _run(db, rival)

    assert db.query(PageChange).count() == 3


def test_structure_is_saved_and_kept_for_pages_not_fetched_again(env):
    db, state = env
    rival = _rival(db, None)
    article = f"{SITE}/articles/a0"
    course = f"{SITE}/courses/ege"
    state["pages"] = {article: "статья", course: "курс"}
    state["structures"] = {
        article: PageStructure("Title", "Desc", "H1 статьи", [(2, "Раздел"), (3, "Подраздел")], "текст статьи"),
    }
    _run(db, rival)

    record = db.query(PageStructureRecord).one()
    assert record.page.url == article
    assert (record.h1, record.meta_description, record.headings) == ("H1 статьи", "Desc", [[2, "Раздел"], [3, "Подраздел"]])
    assert record.main_text_length == len("текст статьи")

    # Обход, где страницу перенесли из прошлого снимка без загрузки: структура остаётся.
    state["structures"] = {}
    _run(db, rival)
    assert db.query(PageStructureRecord).one().h1 == "H1 статьи"
    # Статья со структурой не грузится заново ради неё; статья без структуры — грузится.
    assert state["crawl_kwargs"][-1]["refetch_urls"] == set()

    state["pages"][f"{SITE}/articles/a1"] = "ещё статья"
    _run(db, rival)
    _run(db, rival)
    assert state["crawl_kwargs"][-1]["refetch_urls"] == {f"{SITE}/articles/a1"}

    # Новая загрузка — структура перезаписывается.
    state["structures"] = {article: PageStructure(h1="Новый H1")}
    _run(db, rival)
    assert db.query(PageStructureRecord).one().h1 == "Новый H1"


def test_note_lists_several_opened_sections():
    urls = [f"https://foxford.ru/wiki/p{i}" for i in range(3)] + ["https://foxford.ru/blog/x"]

    note = pipeline.format_new_sections_note(urls)

    assert note.startswith("Начали следить за разделами: /wiki — 3 стр., /blog — 1 стр.")


def test_crawl_gives_cards_to_new_and_changed_articles_only(env, monkeypatch):
    db, state = env
    calls: list = []

    async def fake_cards(articles):
        calls.append([a.url for a in articles])
        return [IntentCard(topic="Тема", main_intent="узнать") for _ in articles]

    monkeypatch.setattr(seo, "make_intent_cards", fake_cards)
    rival = Competitor(name="Skysmart", base_url="https://skysmart.ru")
    db.add(rival)
    db.commit()
    article, course = "https://skysmart.ru/articles/a", "https://skysmart.ru/courses/c"
    state["pages"] = {article: "статья", course: "курс"}
    state["structures"] = {article: PageStructure(h1="H", main_text="т"), course: PageStructure(h1="H", main_text="т")}
    _run(db, rival)
    assert calls == []  # первый обход — точка отсчёта, разбирать нечего

    new_article = "https://skysmart.ru/articles/new"
    state["pages"].update({new_article: "новая статья", course: "курс со скидкой"})
    state["structures"] = {new_article: PageStructure(h1="Новая", main_text="Новая статья"), course: PageStructure(h1="H", main_text="т")}
    _run(db, rival)

    assert calls == [[new_article]]  # коммерческая страница карточку не получает
    assert db.query(ArticleIntent).one().page.url == new_article
    assert any("SEO: разобрано инфостатей" in note for note in state["reports"][-1][1])


def test_crawl_compares_new_competitor_article_with_our_articles(env, monkeypatch):
    db, state = env
    own = Competitor(name="Наш сайт", base_url="https://og1.ru", is_own=True, status=SessionStatus.ACTIVE)
    db.add(own)
    db.flush()
    our_page = Page(competitor_id=own.id, url="https://og1.ru/polza/okonchanie")
    db.add(our_page)
    db.flush()
    db.add(PageStructureRecord(page_id=our_page.id, h1="Окончание слова", headings=[], main_text="окончание слова"))
    rival = _rival(db, None)
    state["pages"] = {f"{SITE}/courses/ege": "ЕГЭ"}
    _run(db, rival)

    async def fake_cards(articles):
        return [IntentCard(topic="Окончание слова", main_intent="узнать про окончание") for _ in articles]

    asked: list = []

    async def fake_match(cases):
        asked.extend(article.url for article, _candidates in cases)
        return [
            seo.MatchResult("update", candidates[0].url, "partial", ["нулевое окончание"], "нет раздела", [], {})
            for _article, candidates in cases
        ]

    monkeypatch.setattr(seo, "make_intent_cards", fake_cards)
    monkeypatch.setattr(seo, "match_articles", fake_match)
    monkeypatch.setattr(pipeline, "export_week", lambda _db, _when: "https://sheets/week")
    article = f"{SITE}/articles/okonchanie"
    state["pages"][article] = "статья про окончание"
    state["structures"] = {article: PageStructure(h1="Окончание", main_text="окончание слова")}
    _run(db, rival)

    assert asked == [article]
    finding = db.query(SeoFinding).one()
    assert (finding.kind, finding.decision.our_url) == ("new", "https://og1.ru/polza/okonchanie")
    assert finding.page_change_id is not None
    assert any("Решения: обновить нашу — 1" in note for note in state["reports"][-1][1])
    assert "📊 Инфостатьи конкурента — в SEO-таблице: https://sheets/week" in state["sent"][-1]
    # Этап 7: в отчёте для продакта статьи нет — только строка-пояснение со ссылкой.
    rows, notes = state["reports"][-1]
    assert rows == []
    assert (
        "Находки по инфостатьям (1) в этот отчёт не входят — их разбор для SEO в SEO-таблице: https://sheets/week"
        in notes
    )


def test_commercial_report_keeps_commerce_and_drops_articles(env, monkeypatch):
    db, state = env
    rival = _rival(db, None)
    state["pages"] = {f"{SITE}/courses/ege": "ЕГЭ 4900"}
    _run(db, rival)
    compared: list = []

    async def fake_compare(page_diff, _own_site):
        compared.append(page_diff.url)

    monkeypatch.setattr(pipeline, "load_own_site", lambda _db: object())
    monkeypatch.setattr(pipeline, "compare_with_own_site", fake_compare)
    state["pages"].update({f"{SITE}/courses/ege": "ЕГЭ 3900", f"{SITE}/articles/new": "статья"})
    _run(db, rival)

    rows, _notes = state["reports"][-1]
    assert [row[2] for row in rows] == [f"{SITE}/courses/ege"]
    assert compared == [f"{SITE}/courses/ege"]  # статью «есть ли у нас» здесь не сравниваем
    assert db.query(PageChange).count() == 2  # в базе и ленте бота статья осталась
