from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.auth import require_basic_auth
from app.config import BASE_DIR
from app.db import get_db
from app.models import Competitor, Page, PageChange
from app.scheduler import get_schedule_config

router = APIRouter(dependencies=[Depends(require_basic_auth)])
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


@router.get("/", response_class=HTMLResponse)
def competitors_page(request: Request, db: Session = Depends(get_db)):
    competitors = db.query(Competitor).order_by(Competitor.created_at.desc()).all()
    schedule = get_schedule_config(db)
    return templates.TemplateResponse(
        request, "competitors.html", {"competitors": competitors, "schedule": schedule}
    )


@router.get("/changes", response_class=HTMLResponse)
def changes_page(request: Request, db: Session = Depends(get_db)):
    stmt = (
        select(PageChange)
        .join(Page)
        .options(
            joinedload(PageChange.page).joinedload(Page.competitor),
            joinedload(PageChange.ai_analysis),
        )
        .order_by(PageChange.detected_at.desc())
        .limit(300)
    )
    changes = db.execute(stmt).unique().scalars().all()

    groups_by_competitor_id: dict[int, dict] = {}
    for change in changes:
        competitor = change.page.competitor
        group = groups_by_competitor_id.setdefault(
            competitor.id, {"competitor": competitor, "changes": []}
        )
        group["changes"].append(change)

    # Порядок групп — по свежести последнего изменения конкурента (changes уже отсортированы).
    groups = sorted(
        groups_by_competitor_id.values(),
        key=lambda group: group["changes"][0].detected_at,
        reverse=True,
    )

    return templates.TemplateResponse(request, "changes.html", {"groups": groups})


@router.get("/changes/{change_id}", response_class=HTMLResponse)
def change_detail_page(change_id: int, request: Request, db: Session = Depends(get_db)):
    stmt = (
        select(PageChange)
        .options(
            joinedload(PageChange.page).joinedload(Page.competitor),
            joinedload(PageChange.ai_analysis),
            joinedload(PageChange.old_snapshot),
            joinedload(PageChange.new_snapshot),
        )
        .where(PageChange.id == change_id)
    )
    change = db.execute(stmt).unique().scalar_one_or_none()
    return templates.TemplateResponse(request, "change_detail.html", {"change": change})
