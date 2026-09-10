from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import ChangeType, ComparisonVerdict, DisappearanceReason, SessionStatus


class CompetitorCreate(BaseModel):
    name: str
    base_url: str


class OwnSiteUpdate(BaseModel):
    """Адрес нашего собственного сайта — с ним сравниваются находки у конкурентов."""

    base_url: str
    name: str | None = None


class CompetitorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    base_url: str
    is_own: bool
    status: SessionStatus
    is_paused: bool
    session_expires_at: datetime | None
    google_drive_folder_url: str | None
    last_crawl_started_at: datetime | None
    last_crawl_finished_at: datetime | None
    is_crawling: bool
    next_crawl_at: datetime | None
    created_at: datetime


class ScheduleConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    interval_days: int
    interval_hours: int


class ScheduleConfigUpdate(BaseModel):
    interval_days: int
    interval_hours: int


class AiAnalysisOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    category: str | None
    usp: str | None
    cta: str | None
    summary: str | None


class OwnSiteComparisonOut(BaseModel):
    """Есть ли такая же страница у нас на сайте и чем она отличается."""

    model_config = ConfigDict(from_attributes=True)

    verdict: ComparisonVerdict
    our_page_url: str | None
    differences: str | None
    missing: str | None


class PageChangeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    change_type: ChangeType
    diff_text: str | None
    detected_at: datetime
    page_url: str
    competitor_name: str
    ai_analysis: AiAnalysisOut | None
    own_comparison: OwnSiteComparisonOut | None = None

    # Заполняются, когда страница пропала из обхода: удалена, переехала или просто
    # не попала в этот обход (см. DisappearanceReason).
    disappearance_reason: DisappearanceReason | None = None
    redirect_to_url: str | None = None
    redirect_target_summary: str | None = None


class CrawlAllOut(BaseModel):
    status: str
    started: int
    skipped: int
    paused: int
