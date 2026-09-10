from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.auth import require_basic_auth
from app.db import get_db
from app.models import Competitor, Page, PageChange
from app.schemas import OwnSiteComparisonOut, PageChangeOut

router = APIRouter(prefix="/api/changes", tags=["changes"], dependencies=[Depends(require_basic_auth)])


@router.get("", response_model=list[PageChangeOut])
def list_changes(limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    stmt = (
        select(PageChange)
        .join(Page)
        .join(Competitor)
        # Наш собственный сайт лежит в той же таблице, но лентой изменений считаются
        # только находки у конкурентов (см. app/own_site.py).
        .where(Competitor.is_own.is_(False))
        .options(
            joinedload(PageChange.page).joinedload(Page.competitor),
            joinedload(PageChange.ai_analysis),
            joinedload(PageChange.own_comparison),
        )
        .order_by(PageChange.detected_at.desc())
        .limit(limit)
        .offset(offset)
    )
    changes = db.execute(stmt).unique().scalars().all()

    return [
        PageChangeOut(
            id=change.id,
            change_type=change.change_type,
            diff_text=change.diff_text,
            detected_at=change.detected_at,
            page_url=change.page.url,
            competitor_name=change.page.competitor.name,
            ai_analysis=change.ai_analysis,
            own_comparison=(
                OwnSiteComparisonOut.model_validate(change.own_comparison)
                if change.own_comparison
                else None
            ),
            disappearance_reason=change.page.disappearance_reason,
            redirect_to_url=change.page.redirect_to_url,
            redirect_target_summary=change.page.redirect_target_summary,
        )
        for change in changes
    ]
