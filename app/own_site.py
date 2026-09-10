"""Наш собственный сайт — та же таблица competitors, но с признаком is_own.

Отдельная сущность не заводилась намеренно: так обход, страницы и снимки текста
работают ровно тем же кодом, что и для конкурентов. Всё, что нужно дополнительно —
не показывать наш сайт там, где перечисляются конкуренты, и уметь достать его
страницы для сравнения.
"""

import logging

from sqlalchemy.orm import Session

from app.ai.compare import OwnPage, OwnSite
from app.models import Competitor, Page

logger = logging.getLogger(__name__)

OWN_SITE_NAME = "Наш сайт"


def get_own_site(db: Session) -> Competitor | None:
    return (
        db.query(Competitor)
        .filter(Competitor.is_own.is_(True))
        .order_by(Competitor.id)
        .first()
    )


def competitors_only(query):
    """Фильтр «только конкуренты» для запросов по таблице competitors."""
    return query.filter(Competitor.is_own.is_(False))


def load_own_site(db: Session) -> OwnSite | None:
    """Страницы нашего сайта для сравнения. None — сайт не заведён или ещё не обойдён.

    Блокирующая и тяжёлая (тянет тексты всех страниц) — из обхода вызывать только
    через asyncio.to_thread, иначе встанет весь event loop.
    """
    own = get_own_site(db)
    if own is None:
        return None

    pages = (
        db.query(Page)
        .filter(Page.competitor_id == own.id, Page.is_removed.is_(False))
        .all()
    )

    own_pages = [
        OwnPage(url=page.url, title=page.title, text=page.snapshots[-1].text_content)
        for page in pages
        if page.snapshots
    ]
    if not own_pages:
        logger.info("Наш сайт заведён, но ещё ни разу не обойден — сравнивать не с чем")
        return None

    return OwnSite(base_url=own.base_url, pages=own_pages)
