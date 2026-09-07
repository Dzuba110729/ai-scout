from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.auth import require_basic_auth
from app.config import BASE_DIR
from app.db import get_db
from app.models import ChangeType, Competitor, Page, PageChange, SessionStatus
from app.scheduler import get_schedule_config

router = APIRouter(dependencies=[Depends(require_basic_auth)])
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def _changes_query():
    return (
        select(PageChange)
        .join(Page)
        .options(
            joinedload(PageChange.page).joinedload(Page.competitor),
            joinedload(PageChange.ai_analysis),
        )
    )


@router.get("/", response_class=HTMLResponse)
def dashboard_page(request: Request, db: Session = Depends(get_db)):
    competitors = db.query(Competitor).all()
    stats = {
        "total": len(competitors),
        "active": sum(1 for c in competitors if c.status == SessionStatus.ACTIVE),
        "needs_session": sum(1 for c in competitors if c.status == SessionStatus.NEEDS_SESSION),
        "changes_last_7d": db.scalar(
            select(func.count(PageChange.id)).where(
                PageChange.detected_at >= datetime.now(UTC) - timedelta(days=7)
            )
        )
        or 0,
    }
    attention_competitors = [c for c in competitors if c.status == SessionStatus.NEEDS_SESSION]

    stmt = _changes_query().order_by(PageChange.detected_at.desc()).limit(8)
    recent_changes = db.execute(stmt).unique().scalars().all()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "active_nav": "dashboard",
            "stats": stats,
            "attention_competitors": attention_competitors,
            "recent_changes": recent_changes,
        },
    )


@router.get("/competitors", response_class=HTMLResponse)
def competitors_page(request: Request, db: Session = Depends(get_db)):
    competitors = db.query(Competitor).order_by(Competitor.created_at.desc()).all()
    return templates.TemplateResponse(
        request, "competitors.html", {"active_nav": "competitors", "competitors": competitors}
    )


@router.get("/competitors/{competitor_id}", response_class=HTMLResponse)
def competitor_detail_page(competitor_id: int, request: Request, db: Session = Depends(get_db)):
    competitor = db.get(Competitor, competitor_id)
    if not competitor:
        return templates.TemplateResponse(
            request, "competitor_detail.html", {"active_nav": "competitors", "competitor": None}
        )

    pages = (
        db.query(Page)
        .filter(Page.competitor_id == competitor_id)
        .order_by(Page.last_seen_at.desc())
        .all()
    )

    stmt = (
        _changes_query()
        .where(Page.competitor_id == competitor_id)
        .order_by(PageChange.detected_at.desc())
        .limit(200)
    )
    changes = db.execute(stmt).unique().scalars().all()

    return templates.TemplateResponse(
        request,
        "competitor_detail.html",
        {
            "active_nav": "competitors",
            "competitor": competitor,
            "pages": pages,
            "changes": changes,
        },
    )


@router.get("/changes", response_class=HTMLResponse)
def changes_page(
    request: Request,
    competitor_id: str | None = None,
    change_type: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    competitor_id_int = int(competitor_id) if competitor_id else None
    stmt = _changes_query()

    if competitor_id_int:
        stmt = stmt.where(Page.competitor_id == competitor_id_int)
    if change_type in {c.value for c in ChangeType}:
        stmt = stmt.where(PageChange.change_type == ChangeType(change_type))
    if q:
        stmt = stmt.where(Page.url.ilike(f"%{q}%"))

    stmt = stmt.order_by(PageChange.detected_at.desc()).limit(300)
    changes = db.execute(stmt).unique().scalars().all()

    all_competitors = db.query(Competitor).order_by(Competitor.name).all()

    return templates.TemplateResponse(
        request,
        "changes.html",
        {
            "active_nav": "changes",
            "changes": changes,
            "all_competitors": all_competitors,
            "filters": {"competitor_id": competitor_id_int, "change_type": change_type, "q": q},
        },
    )


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
    return templates.TemplateResponse(
        request, "change_detail.html", {"active_nav": "changes", "change": change}
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    schedule = get_schedule_config(db)
    return templates.TemplateResponse(
        request, "settings.html", {"active_nav": "settings", "schedule": schedule}
    )
