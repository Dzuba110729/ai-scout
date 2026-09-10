from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.auth import require_basic_auth
from app.config import BASE_DIR
from app.db import get_db
from app.diff_view import build_side_by_side
from app.models import ChangeType, Competitor, Page, PageChange, SessionStatus
from app.scheduler import get_schedule_config

router = APIRouter(dependencies=[Depends(require_basic_auth)])
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

PAGE_SIZE = 25


def _changes_query():
    return (
        select(PageChange)
        .join(Page)
        .options(
            joinedload(PageChange.page).joinedload(Page.competitor),
            joinedload(PageChange.ai_analysis),
        )
    )


def _parse_page(raw: str | None) -> int:
    """Номер страницы приходит строкой, а не int.

    Сброс фильтра через <select> отправляет в адрес пустое значение
    (competitor_id=&page=), и на типизированном int-параметре FastAPI
    отвечал бы ошибкой 422 вместо того, чтобы просто показать первую страницу.
    """
    try:
        value = int(raw) if raw else 1
    except ValueError:
        value = 1
    return max(value, 1)


def _paginate_changes(db: Session, conditions: list, page: int) -> dict:
    """Отдаёт одну страницу изменений.

    Общее количество считается отдельным count-запросом (без joinedload и сортировки),
    чтобы не тянуть все строки в память ради подсчёта.
    """
    count_stmt = select(func.count(PageChange.id)).join(Page)
    stmt = _changes_query()
    for condition in conditions:
        count_stmt = count_stmt.where(condition)
        stmt = stmt.where(condition)
    total = db.scalar(count_stmt) or 0

    total_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    # Номер страницы больше последней (например, после смены фильтра) —
    # показываем последнюю существующую, а не пустой экран.
    page = min(page, total_pages)
    offset = (page - 1) * PAGE_SIZE

    stmt = stmt.order_by(PageChange.detected_at.desc()).offset(offset).limit(PAGE_SIZE)
    items = db.execute(stmt).unique().scalars().all()

    return {
        "items": items,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "page_size": PAGE_SIZE,
        "first_index": offset + 1 if items else 0,
        "last_index": offset + len(items),
        "has_prev": page > 1,
        "has_next": page < total_pages,
    }


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
def competitor_detail_page(
    competitor_id: int,
    request: Request,
    page: str | None = None,
    db: Session = Depends(get_db),
):
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

    pagination = _paginate_changes(db, [Page.competitor_id == competitor_id], _parse_page(page))

    return templates.TemplateResponse(
        request,
        "competitor_detail.html",
        {
            "active_nav": "competitors",
            "competitor": competitor,
            "pages": pages,
            "changes": pagination["items"],
            "pagination": pagination,
            # Пагинация истории живёт на вкладке — после перехода надо снова её открыть.
            "page_url_base": f"/competitors/{competitor_id}?",
        },
    )


@router.get("/changes", response_class=HTMLResponse)
def changes_page(
    request: Request,
    competitor_id: str | None = None,
    change_type: str | None = None,
    q: str | None = None,
    page: str | None = None,
    db: Session = Depends(get_db),
):
    competitor_id_int = int(competitor_id) if competitor_id else None

    conditions = []
    if competitor_id_int:
        conditions.append(Page.competitor_id == competitor_id_int)
    if change_type in {c.value for c in ChangeType}:
        conditions.append(PageChange.change_type == ChangeType(change_type))
    if q:
        conditions.append(Page.url.ilike(f"%{q}%"))

    pagination = _paginate_changes(db, conditions, _parse_page(page))

    all_competitors = db.query(Competitor).order_by(Competitor.name).all()

    # Ссылки «Назад»/«Вперёд» должны сохранять выбранные фильтры.
    kept_filters = {"competitor_id": competitor_id, "change_type": change_type, "q": q}
    query_prefix = urlencode({k: v for k, v in kept_filters.items() if v})

    return templates.TemplateResponse(
        request,
        "changes.html",
        {
            "active_nav": "changes",
            "changes": pagination["items"],
            "pagination": pagination,
            "page_url_base": f"/changes?{query_prefix + '&' if query_prefix else ''}",
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

    side_by_side = None
    if change and change.old_snapshot and change.new_snapshot:
        side_by_side = build_side_by_side(
            change.old_snapshot.text_content, change.new_snapshot.text_content
        )

    return templates.TemplateResponse(
        request,
        "change_detail.html",
        {"active_nav": "changes", "change": change, "side_by_side": side_by_side},
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    schedule = get_schedule_config(db)
    return templates.TemplateResponse(
        request, "settings.html", {"active_nav": "settings", "schedule": schedule}
    )
