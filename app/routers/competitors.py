import logging
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import require_basic_auth
from app.db import SessionLocal, get_db
from app.integrations.google_docs import GoogleDocsClient, GoogleDocsError
from app.models import Competitor, SessionStatus
from app.pipeline import run_crawl_for_competitor
from app.scheduler import get_schedule_config, schedule_competitor_and_save_next_run, unschedule_competitor
from app.schemas import CompetitorCreate, CompetitorOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/competitors", tags=["competitors"], dependencies=[Depends(require_basic_auth)])


@router.get("", response_model=list[CompetitorOut])
def list_competitors(db: Session = Depends(get_db)):
    return db.query(Competitor).order_by(Competitor.created_at.desc()).all()


@router.post("", response_model=CompetitorOut, status_code=201)
def create_competitor(payload: CompetitorCreate, db: Session = Depends(get_db)):
    competitor = Competitor(
        name=payload.name,
        base_url=payload.base_url,
        status=SessionStatus.NEEDS_SESSION,
    )

    docs = GoogleDocsClient()
    if docs.is_configured:
        try:
            folder_id, folder_url = docs.get_or_create_competitor_folder(payload.name)
            competitor.google_drive_folder_id = folder_id
            competitor.google_drive_folder_url = folder_url
        except GoogleDocsError:
            logger.exception("Не удалось создать папку Google Drive для конкурента %s", payload.name)

    db.add(competitor)
    db.commit()
    db.refresh(competitor)

    config = get_schedule_config(db)
    schedule_competitor_and_save_next_run(db, competitor, config)
    db.refresh(competitor)

    return competitor


@router.post("/{competitor_id}/pause", response_model=CompetitorOut)
def pause_competitor(competitor_id: int, db: Session = Depends(get_db)):
    competitor = _get_or_404(db, competitor_id)
    competitor.is_paused = True
    db.commit()
    db.refresh(competitor)
    unschedule_competitor(competitor.id)
    return competitor


@router.post("/{competitor_id}/resume", response_model=CompetitorOut)
def resume_competitor(competitor_id: int, db: Session = Depends(get_db)):
    competitor = _get_or_404(db, competitor_id)
    competitor.is_paused = False
    db.commit()
    db.refresh(competitor)
    config = get_schedule_config(db)
    schedule_competitor_and_save_next_run(db, competitor, config)
    db.refresh(competitor)
    return competitor


@router.delete("/{competitor_id}", status_code=204)
def delete_competitor(competitor_id: int, db: Session = Depends(get_db)):
    competitor = _get_or_404(db, competitor_id)
    unschedule_competitor(competitor.id)
    db.delete(competitor)
    db.commit()


@router.post("/{competitor_id}/crawl", status_code=202)
def trigger_crawl(competitor_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    competitor = _get_or_404(db, competitor_id)

    if competitor.is_crawling:
        raise HTTPException(status_code=409, detail="Обход уже выполняется — дождитесь завершения")

    # Фиксируем "обход начат" синхронно, до ответа — иначе при быстром повторном клике
    # вторая проверка is_crawling ещё не увидит первый запуск (фоновая задача стартует
    # только после отправки ответа) и запустит обход дважды.
    competitor.last_crawl_started_at = datetime.now(UTC)
    competitor.last_crawl_finished_at = None
    db.commit()

    async def _run():
        job_db = SessionLocal()
        try:
            job_competitor = job_db.get(Competitor, competitor_id)
            if job_competitor:
                await run_crawl_for_competitor(job_db, job_competitor)
        except Exception:  # noqa: BLE001 — фоновая задача, ошибка уже логируется внутри pipeline
            logger.exception("Ручной обход конкурента %s завершился ошибкой", competitor_id)
        finally:
            job_db.close()

    background_tasks.add_task(_run)
    return {"status": "запущен обход", "competitor_id": competitor.id}


def _get_or_404(db: Session, competitor_id: int) -> Competitor:
    competitor = db.get(Competitor, competitor_id)
    if not competitor:
        raise HTTPException(status_code=404, detail="Конкурент не найден")
    return competitor
