import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class SessionStatus(str, enum.Enum):
    ACTIVE = "active"
    NEEDS_SESSION = "needs_session"
    PAUSED = "paused"


class ChangeType(str, enum.Enum):
    NEW = "new"
    CHANGED = "changed"
    REMOVED = "removed"


class NotificationStatus(str, enum.Enum):
    SENT = "sent"
    FAILED = "failed"


class Competitor(Base):
    __tablename__ = "competitors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    base_url: Mapped[str] = mapped_column(Text)
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus, name="session_status", values_callable=lambda e: [m.value for m in e]),
        default=SessionStatus.NEEDS_SESSION,
    )
    storage_state_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_paused: Mapped[bool] = mapped_column(default=False)
    google_drive_folder_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    google_drive_folder_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_crawl_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_crawl_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_crawl_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    pages: Mapped[list["Page"]] = relationship(back_populates="competitor", cascade="all, delete-orphan")

    @property
    def is_crawling(self) -> bool:
        """Обход уже запущен и ещё не завершился (последний started позже последнего finished)."""
        if not self.last_crawl_started_at:
            return False
        return self.last_crawl_finished_at is None or self.last_crawl_finished_at < self.last_crawl_started_at


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("competitor_id", "url", name="uq_pages_competitor_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    is_removed: Mapped[bool] = mapped_column(default=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    competitor: Mapped["Competitor"] = relationship(back_populates="pages")
    snapshots: Mapped[list["PageSnapshot"]] = relationship(
        back_populates="page", cascade="all, delete-orphan", order_by="PageSnapshot.fetched_at"
    )
    changes: Mapped[list["PageChange"]] = relationship(back_populates="page", cascade="all, delete-orphan")


class PageSnapshot(Base):
    __tablename__ = "page_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"))
    content_hash: Mapped[str] = mapped_column(Text)
    text_content: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    page: Mapped["Page"] = relationship(back_populates="snapshots")


class PageChange(Base):
    __tablename__ = "page_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"))
    change_type: Mapped[ChangeType] = mapped_column(
        Enum(ChangeType, name="change_type", values_callable=lambda e: [m.value for m in e])
    )
    old_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("page_snapshots.id", ondelete="SET NULL"), nullable=True
    )
    new_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("page_snapshots.id", ondelete="SET NULL"), nullable=True
    )
    diff_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    page: Mapped["Page"] = relationship(back_populates="changes")
    old_snapshot: Mapped["PageSnapshot | None"] = relationship(foreign_keys=[old_snapshot_id])
    new_snapshot: Mapped["PageSnapshot | None"] = relationship(foreign_keys=[new_snapshot_id])
    ai_analysis: Mapped["AiAnalysis | None"] = relationship(
        back_populates="page_change", cascade="all, delete-orphan", uselist=False
    )
    notifications: Mapped[list["NotificationLog"]] = relationship(
        back_populates="page_change", cascade="all, delete-orphan"
    )


class AiAnalysis(Base):
    __tablename__ = "ai_analysis"

    id: Mapped[int] = mapped_column(primary_key=True)
    page_change_id: Mapped[int] = mapped_column(
        ForeignKey("page_changes.id", ondelete="CASCADE"), unique=True
    )
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    usp: Mapped[str | None] = mapped_column(Text, nullable=True)
    cta: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    page_change: Mapped["PageChange"] = relationship(back_populates="ai_analysis")


class ScheduleConfig(Base):
    """Единственная строка (id=1) — общий интервал обхода для всех конкурентов."""

    __tablename__ = "schedule_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    interval_days: Mapped[int] = mapped_column(default=7)
    interval_hours: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class NotificationLog(Base):
    __tablename__ = "notifications_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    page_change_id: Mapped[int | None] = mapped_column(
        ForeignKey("page_changes.id", ondelete="SET NULL"), nullable=True
    )
    competitor_id: Mapped[int | None] = mapped_column(
        ForeignKey("competitors.id", ondelete="SET NULL"), nullable=True
    )
    channel: Mapped[str] = mapped_column(Text, default="telegram")
    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus, name="notification_status", values_callable=lambda e: [m.value for m in e])
    )
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    page_change: Mapped["PageChange | None"] = relationship(back_populates="notifications")
