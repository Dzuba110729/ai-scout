"""Действия над конкурентом, общие для веб-роутера и Telegram-бота.

Не знает про HTTP (никаких HTTPException/статус-кодов) — каждый вызывающий сам решает,
как сообщить об ошибке (веб — статус-кодом, бот — текстом в чат). Раньше эта логика
жила только в app/routers/competitors.py; вынесена сюда, когда появился Telegram-бот,
чтобы не дублировать создание/паузу/удаление конкурента в двух местах.
"""

import logging

from sqlalchemy.orm import Session

from app.config import settings
from app.integrations.google_docs import GoogleDocsClient, GoogleDocsError
from app.models import Competitor, SessionStatus
from app.own_site import OWN_SITE_NAME, get_own_site
from app.scheduler import sync_next_run

logger = logging.getLogger(__name__)


def create_competitor(db: Session, name: str, base_url: str) -> Competitor:
    competitor = Competitor(name=name, base_url=base_url, status=SessionStatus.NEEDS_SESSION)

    docs = GoogleDocsClient()
    if docs.is_configured:
        try:
            folder_id, folder_url = docs.get_or_create_competitor_folder(name)
            competitor.google_drive_folder_id = folder_id
            competitor.google_drive_folder_url = folder_url
        except GoogleDocsError:
            logger.exception("Не удалось создать папку Google Drive для конкурента %s", name)

    db.add(competitor)
    db.commit()
    db.refresh(competitor)

    sync_next_run(db, competitor)
    db.refresh(competitor)
    return competitor


def pause_competitor(db: Session, competitor: Competitor) -> None:
    # Отдельного таймера у сайта нет: общий плановый цикл просто пропускает тех,
    # кто на паузе (см. app/scheduler.py).
    competitor.is_paused = True
    db.commit()
    db.refresh(competitor)


def resume_competitor(db: Session, competitor: Competitor) -> None:
    competitor.is_paused = False
    db.commit()
    db.refresh(competitor)
    sync_next_run(db, competitor)
    db.refresh(competitor)


def delete_competitor(db: Session, competitor: Competitor) -> None:
    db.delete(competitor)
    db.commit()


def normalize_url(raw: str) -> str:
    """Адрес сайта из формы или из чата. ValueError — это не адрес сайта.

    В чате адрес часто пишут без схемы («foxford.ru») — дописываем https://.
    """
    url = raw.strip()
    if not url:
        raise ValueError("Адрес сайта пустой")
    if not url.startswith(("http://", "https://")):
        if "://" in url or "." not in url or " " in url:
            raise ValueError("Адрес должен начинаться с http:// или https://")
        url = f"https://{url}"
    return url


def ensure_own_site(db: Session) -> Competitor | None:
    """Заводит наш сайт по settings.own_site_url, если его ещё нет. Существующий
    не трогает: адрес мог быть задан в веб-интерфейсе."""
    own = get_own_site(db)
    if own is not None or not settings.own_site_url:
        return own
    logger.info("Заводим наш сайт %s", settings.own_site_url)
    return save_own_site(db, settings.own_site_url)


def save_own_site(db: Session, base_url: str, name: str | None = None) -> Competitor:
    """Задаёт (или меняет) адрес нашего сайта. Запись всегда одна.

    ValueError — адрес не похож на адрес сайта.
    """
    base_url = normalize_url(base_url)

    own = get_own_site(db)
    if own is None:
        own = Competitor(name=name or OWN_SITE_NAME, base_url=base_url, is_own=True)
        # Папка отчётов в Google Drive нашему сайту не нужна: отчёты делаются
        # по конкурентам, наш сайт обходится только ради свежих страниц.
        db.add(own)
    else:
        own.base_url = base_url
        if name:
            own.name = name

    db.commit()
    db.refresh(own)

    sync_next_run(db, own)
    db.refresh(own)
    return own
