"""Действия над конкурентом, общие для веб-роутера и Telegram-бота.

Не знает про HTTP (никаких HTTPException/статус-кодов) — каждый вызывающий сам решает,
как сообщить об ошибке (веб — статус-кодом, бот — текстом в чат). Раньше эта логика
жила только в app/routers/competitors.py; вынесена сюда, когда появился Telegram-бот,
чтобы не дублировать создание/паузу/удаление конкурента в двух местах.
"""

import logging

from sqlalchemy.orm import Session

from app.integrations.google_docs import GoogleDocsClient, GoogleDocsError
from app.models import Competitor, SessionStatus
from app.scheduler import get_schedule_config, schedule_competitor_and_save_next_run, unschedule_competitor

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

    config = get_schedule_config(db)
    schedule_competitor_and_save_next_run(db, competitor, config)
    db.refresh(competitor)
    return competitor


def pause_competitor(db: Session, competitor: Competitor) -> None:
    competitor.is_paused = True
    db.commit()
    db.refresh(competitor)
    unschedule_competitor(competitor.id)


def resume_competitor(db: Session, competitor: Competitor) -> None:
    competitor.is_paused = False
    db.commit()
    db.refresh(competitor)
    config = get_schedule_config(db)
    schedule_competitor_and_save_next_run(db, competitor, config)
    db.refresh(competitor)


def delete_competitor(db: Session, competitor: Competitor) -> None:
    unschedule_competitor(competitor.id)
    db.delete(competitor)
    db.commit()
