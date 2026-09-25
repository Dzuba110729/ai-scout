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


class DisappearanceReason(str, enum.Enum):
    """Почему страница пропала из результатов обхода.

    MISSING_FROM_CRAWL — страница по-прежнему открывается, просто не попала в этот
    обход (упёрлись в лимит страниц или её убрали из карты сайта). Удалённой такая
    страница НЕ считается: раньше именно она давала ложные тревоги в отчётах.
    """

    DELETED = "deleted"
    MOVED = "moved"
    MISSING_FROM_CRAWL = "missing_from_crawl"


class ComparisonVerdict(str, enum.Enum):
    """Есть ли у нас на сайте то же, что появилось у конкурента."""

    EXACT = "exact"
    SIMILAR = "similar"
    NONE = "none"


class Competitor(Base):
    __tablename__ = "competitors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    base_url: Mapped[str] = mapped_column(Text)
    # Наш собственный сайт заведён такой же строкой, чтобы переиспользовать краулер
    # и таблицы страниц. Конкурентом он при этом не считается: не попадает ни в ленту
    # изменений, ни в счётчики, ни в отчёты, ни в уведомления.
    is_own: Mapped[bool] = mapped_column(default=False)
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

    # Сколько страниц нашли всего в текущем обходе (известно сразу после того, как
    # прочитана карта сайта, ещё до начала загрузки самих страниц). null — обход
    # не идёт или страницы ещё ищутся. Число уже обойдённых страниц отдельно не
    # хранится — это просто количество строк в PageFetchCache для конкурента.
    crawl_pages_total: Mapped[int | None] = mapped_column(nullable=True)

    # Когда в последний раз обошли ВСЕ страницы сайта, а не только изменившиеся по
    # дате из карты сайта. null — обхода ещё не было, следующий обход обязан быть
    # полным. См. app.pipeline._needs_full_crawl и CLAUDE.md про инкрементальный обход.
    last_full_crawl_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Свой адрес карты сайта, если стандартный /sitemap.xml не подходит. Может быть
    # XML-картой или обычной HTML-страницей со ссылками (у skysmart.ru это /sitemap:
    # ~1500 нужных страниц вместо 28 000 в XML-картах, большая часть которых —
    # однотипные SEO-страницы «курсы в городе N»). null — ищем карту сами.
    sitemap_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Документ последнего обхода в Google Docs — чтобы бот мог прислать его в любой
    # момент, а не только в сообщении об окончании обхода.
    last_report_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_report_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
    disappearance_reason: Mapped[DisappearanceReason | None] = mapped_column(
        Enum(
            DisappearanceReason,
            name="disappearance_reason",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=True,
    )
    redirect_to_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    redirect_target_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Дата обновления этой страницы по данным sitemap на момент последней реальной
    # загрузки. null — сайт не сообщает дату, или страницу ещё ни разу не грузили
    # после появления этого поля. См. app.crawler.crawl.plan_crawl.
    sitemap_lastmod: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
    own_comparison: Mapped["OwnSiteComparison | None"] = relationship(
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
    # Насколько находка важна для маркетинга: high / medium / low (см. app.ai.analyze).
    # null — разбор сделан до появления оценки; считается «средне».
    importance: Mapped[str | None] = mapped_column(Text, nullable=True)
    importance_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    page_change: Mapped["PageChange"] = relationship(back_populates="ai_analysis")


class OwnSiteComparison(Base):
    """Ответ на вопрос «есть ли такое у нас» по конкретной находке у конкурента."""

    __tablename__ = "own_site_comparisons"

    id: Mapped[int] = mapped_column(primary_key=True)
    page_change_id: Mapped[int] = mapped_column(
        ForeignKey("page_changes.id", ondelete="CASCADE"), unique=True
    )
    verdict: Mapped[ComparisonVerdict] = mapped_column(
        Enum(
            ComparisonVerdict,
            name="comparison_verdict",
            values_callable=lambda e: [m.value for m in e],
        )
    )
    our_page_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    differences: Mapped[str | None] = mapped_column(Text, nullable=True)
    missing: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    page_change: Mapped["PageChange"] = relationship(back_populates="own_comparison")


class ScheduleConfig(Base):
    """Единственная строка (id=1) — общий интервал обхода для всех конкурентов."""

    __tablename__ = "schedule_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    interval_days: Mapped[int] = mapped_column(default=7)
    interval_hours: Mapped[int] = mapped_column(default=0)
    # Когда запустится следующий плановый цикл обходов (см. app/scheduler.py) —
    # в БД, чтобы перезапуск сервиса не отодвигал его на полный интервал.
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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


class PageFetchCache(Base):
    """Черновик уже загруженных за текущий обход страниц — не часть истории изменений.

    Пишется сразу при загрузке каждой страницы (пока PageSnapshot появляется только
    в самом конце, после diff по всему сайту разом). Даёт две вещи: видно, сколько
    страниц уже обойдено (это просто количество строк здесь), и при обрыве обхода
    (упал сервер, легла база) следующий запуск не грузит с сайта заново страницы,
    которые уже недавно получили — см. app.crawler.crawl.crawl_competitor(cache_lookup=...).

    После УСПЕШНОГО завершения обхода строки для этого конкурента удаляются —
    иначе следующий недельный обход мог бы пропустить настоящие новые изменения,
    приняв старый черновик за свежие данные.
    """

    __tablename__ = "page_fetch_cache"
    __table_args__ = (UniqueConstraint("competitor_id", "url", name="uq_page_fetch_cache_competitor_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_content: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
