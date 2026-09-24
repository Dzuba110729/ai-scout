"""Наш собственный сайт: адрес, статус обхода и ручной запуск обхода.

Хранится в таблице competitors с признаком is_own (см. app/own_site.py) — так
переиспользуется весь готовый обход. Запись всегда одна: повторный PUT меняет
адрес уже существующей.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import competitor_ops, crawl_manager
from app.auth import require_basic_auth
from app.db import get_db
from app.models import Competitor
from app.own_site import get_own_site
from app.schemas import CompetitorOut, OwnSiteUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/own-site", tags=["own-site"], dependencies=[Depends(require_basic_auth)])


@router.get("", response_model=CompetitorOut | None)
def read_own_site(db: Session = Depends(get_db)):
    return get_own_site(db)


@router.put("", response_model=CompetitorOut)
def save_own_site(payload: OwnSiteUpdate, db: Session = Depends(get_db)):
    # В вебе адрес вводят в форму — требуем схему, как и раньше; без схемы
    # (og1.ru) принимает только бот, где адрес пишут в чат как попало.
    if not payload.base_url.strip().startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="Адрес должен начинаться с http:// или https://")
    try:
        return competitor_ops.save_own_site(db, payload.base_url, payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/crawl", status_code=202)
async def trigger_own_site_crawl(db: Session = Depends(get_db)):
    own = _get_or_404(db)

    # Идём через crawl_manager, как и обход конкурентов: там защита от повторного
    # запуска и общий лимит одновременных обходов.
    if not crawl_manager.start(db, own):
        raise HTTPException(status_code=409, detail="Обход уже выполняется — дождитесь завершения")

    return {"status": "запущен обход нашего сайта", "competitor_id": own.id}


@router.post("/stop", status_code=202)
async def stop_own_site_crawl(db: Session = Depends(get_db)):
    own = _get_or_404(db)

    if not crawl_manager.stop(own.id):
        raise HTTPException(status_code=409, detail="Обход сейчас не выполняется")

    return {"status": "обход остановлен", "competitor_id": own.id}


def _get_or_404(db: Session) -> Competitor:
    own = get_own_site(db)
    if own is None:
        raise HTTPException(status_code=404, detail="Наш сайт ещё не указан")
    return own
