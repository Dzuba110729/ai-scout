from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import competitor_ops, crawl_manager
from app.auth import require_basic_auth
from app.db import get_db
from app.models import Competitor
from app.schemas import CompetitorCreate, CompetitorOut, CrawlAllOut

router = APIRouter(prefix="/api/competitors", tags=["competitors"], dependencies=[Depends(require_basic_auth)])


@router.get("", response_model=list[CompetitorOut])
def list_competitors(db: Session = Depends(get_db)):
    # Наш собственный сайт живёт в этой же таблице, но конкурентом не является —
    # он доступен через /api/own-site (см. app/own_site.py).
    return (
        db.query(Competitor)
        .filter(Competitor.is_own.is_(False))
        .order_by(Competitor.created_at.desc())
        .all()
    )


@router.post("", response_model=CompetitorOut, status_code=201)
def create_competitor(payload: CompetitorCreate, db: Session = Depends(get_db)):
    return competitor_ops.create_competitor(db, payload.name, payload.base_url)


@router.post("/crawl-all", status_code=202, response_model=CrawlAllOut)
async def trigger_crawl_all(db: Session = Depends(get_db)):
    """Запускает обход всех конкурентов, кроме поставленных на паузу.

    Объявлен выше маршрутов с {competitor_id}, чтобы "crawl-all" не был принят
    за идентификатор конкурента.
    """
    result = crawl_manager.start_all(db)
    return {
        "status": "запущен обход всех конкурентов",
        "started": result.started,
        "skipped": result.skipped_running,
        "paused": result.skipped_paused,
    }


@router.post("/{competitor_id}/pause", response_model=CompetitorOut)
def pause_competitor(competitor_id: int, db: Session = Depends(get_db)):
    competitor = _get_or_404(db, competitor_id)
    competitor_ops.pause_competitor(db, competitor)
    return competitor


@router.post("/{competitor_id}/resume", response_model=CompetitorOut)
def resume_competitor(competitor_id: int, db: Session = Depends(get_db)):
    competitor = _get_or_404(db, competitor_id)
    competitor_ops.resume_competitor(db, competitor)
    return competitor


@router.delete("/{competitor_id}", status_code=204)
def delete_competitor(competitor_id: int, db: Session = Depends(get_db)):
    competitor = _get_or_404(db, competitor_id)
    competitor_ops.delete_competitor(db, competitor)


@router.post("/{competitor_id}/crawl", status_code=202)
async def trigger_crawl(competitor_id: int, db: Session = Depends(get_db)):
    competitor = _get_or_404(db, competitor_id)

    # Отметку "обход начат" и постановку фоновой задачи делает crawl_manager —
    # одним синхронным куском, до ответа. Иначе быстрый повторный клик успевает
    # проскочить проверку, пока фоновая задача ещё не стартовала.
    if not crawl_manager.start(db, competitor):
        raise HTTPException(status_code=409, detail="Обход уже выполняется — дождитесь завершения")

    return {"status": "запущен обход", "competitor_id": competitor.id}


@router.post("/{competitor_id}/stop", status_code=202)
async def stop_crawl(competitor_id: int, db: Session = Depends(get_db)):
    _get_or_404(db, competitor_id)

    if not crawl_manager.stop(competitor_id):
        raise HTTPException(status_code=409, detail="Обход этого конкурента сейчас не выполняется")

    return {"status": "обход остановлен", "competitor_id": competitor_id}


def _get_or_404(db: Session, competitor_id: int) -> Competitor:
    competitor = db.get(Competitor, competitor_id)
    if not competitor:
        raise HTTPException(status_code=404, detail="Конкурент не найден")
    return competitor
