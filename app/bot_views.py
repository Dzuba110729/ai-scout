"""Данные и тексты экранов Telegram-бота: сводка, карточка конкурента, лента находок,
отчёты, настройки.

Отдельно от app/telegram_bot.py намеренно: здесь нет ничего про aiogram, только
запросы к БД и сборка текста — это легко проверить тестами на SQLite, а бот лишь
раскладывает готовый текст по сообщениям и кнопкам. Тот же текст (сводка, лента)
уходит ИИ-помощнику как контекст — см. app/ai/assistant.py.
"""

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.models import (
    AiAnalysis,
    ChangeType,
    Competitor,
    Page,
    PageChange,
    PageFetchCache,
    SessionStatus,
)
from app.own_site import get_own_site
from app.scheduler import get_schedule_config

CHANGES_PAGE_SIZE = 8

STATUS_LABELS = {
    SessionStatus.ACTIVE: "работает",
    SessionStatus.NEEDS_SESSION: "нужна ручная сессия",
    SessionStatus.PAUSED: "на паузе",
}

CHANGE_EMOJI = {
    ChangeType.NEW: "🆕",
    ChangeType.CHANGED: "✏️",
    ChangeType.REMOVED: "🗑",
}

# Фильтр ленты в callback_data -> тип изменения (None — все; «important» — не тип,
# а оценка важности от ИИ, обрабатывается в load_changes отдельно).
CHANGE_KINDS: dict[str, ChangeType | None] = {
    "all": None,
    "important": None,
    "new": ChangeType.NEW,
    "changed": ChangeType.CHANGED,
    "removed": ChangeType.REMOVED,
}

CHANGE_KIND_LABELS = {
    "all": "Все",
    "important": "🔥 Важные",
    "new": "🆕 Новые",
    "changed": "✏️ Изменения",
    "removed": "🗑 Удалённые",
}

IMPORTANT_MARK = "🔥"


def fmt_dt(value: datetime | None, empty: str = "—") -> str:
    """Дата в часовом поясе Мака, на котором крутится сервис."""
    if value is None:
        return empty
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone().strftime("%d.%m.%Y %H:%M")


def fmt_interval(days: int, hours: int) -> str:
    parts = []
    if days:
        parts.append(f"{days} дн.")
    if hours:
        parts.append(f"{hours} ч")
    return "каждые " + " ".join(parts) if parts else "не задано"


def _shorten(text: str | None, limit: int) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def list_competitors(db: Session) -> list[Competitor]:
    return (
        db.query(Competitor)
        .filter(Competitor.is_own.is_(False))
        .order_by(Competitor.created_at.desc(), Competitor.id.desc())
        .all()
    )


def crawl_progress(db: Session, competitor: Competitor) -> str | None:
    """«обошли 120 из 340 страниц» для идущего обхода, иначе None."""
    if not competitor.is_crawling:
        return None
    done = db.scalar(
        select(func.count(PageFetchCache.id)).where(PageFetchCache.competitor_id == competitor.id)
    ) or 0
    if competitor.crawl_pages_total:
        return f"обошли {done} из {competitor.crawl_pages_total} страниц"
    return "ищем страницы в карте сайта" if not done else f"обошли {done} страниц"


def status_line(competitor: Competitor) -> str:
    parts = [STATUS_LABELS[competitor.status]]
    if competitor.is_paused and competitor.status is not SessionStatus.PAUSED:
        parts.append("пауза")
    if competitor.is_crawling:
        parts.append("⏳ обход идёт")
    return ", ".join(parts)


def _change_counts(db: Session, since: datetime, competitor_id: int | None = None) -> dict[ChangeType, int]:
    stmt = (
        select(PageChange.change_type, func.count(PageChange.id))
        .join(Page)
        .join(Competitor)
        .where(Competitor.is_own.is_(False), PageChange.detected_at >= since)
        .group_by(PageChange.change_type)
    )
    if competitor_id is not None:
        stmt = stmt.where(Page.competitor_id == competitor_id)
    return dict(db.execute(stmt).all())


def _counts_line(counts: dict[ChangeType, int]) -> str:
    if not counts:
        return "находок нет"
    return ", ".join(
        f"{CHANGE_EMOJI[kind]} {counts[kind]}" for kind in (ChangeType.NEW, ChangeType.CHANGED, ChangeType.REMOVED)
        if counts.get(kind)
    )


def summary_text(db: Session) -> str:
    competitors = list_competitors(db)
    config = get_schedule_config(db)
    now = datetime.now(UTC)

    lines = ["📊 Сводка", ""]
    if not competitors:
        lines.append("Конкурентов пока нет — добавьте первого в разделе «🏢 Конкуренты».")
    else:
        active = sum(1 for c in competitors if not c.is_paused)
        lines.append(f"Конкурентов: {len(competitors)} (следим за {active}, на паузе {len(competitors) - active})")
        need_session = [c.name for c in competitors if c.status is SessionStatus.NEEDS_SESSION]
        if need_session:
            lines.append(f"⚠️ Нужна ручная сессия: {', '.join(need_session)}")

        crawling = [c for c in competitors if c.is_crawling]
        if crawling:
            lines.append("")
            lines.append("⏳ Сейчас обходим:")
            for c in crawling:
                lines.append(f"— {c.name}: {crawl_progress(db, c)}")

        lines.append("")
        lines.append(f"Находки за 7 дней: {_counts_line(_change_counts(db, now - timedelta(days=7)))}")
        important = load_changes(db, kind="important", since=now - timedelta(days=7), page_size=1).total
        if important:
            lines.append(f"{IMPORTANT_MARK} Из них важных: {important}")
        lines.append(f"Находки за 30 дней: {_counts_line(_change_counts(db, now - timedelta(days=30)))}")

        upcoming = [c.next_crawl_at for c in competitors if c.next_crawl_at and not c.is_paused]
        lines.append("")
        lines.append(f"Расписание: {fmt_interval(config.interval_days, config.interval_hours)}")
        lines.append(f"Ближайший плановый обход: {fmt_dt(min(upcoming)) if upcoming else '—'}")

        finished = [c for c in competitors if c.last_crawl_finished_at]
        if finished:
            last = max(finished, key=lambda c: c.last_crawl_finished_at)
            lines.append(f"Последний обход: {last.name}, {fmt_dt(last.last_crawl_finished_at)}")

    own = get_own_site(db)
    lines.append("")
    if own is None:
        lines.append("🌐 Наш сайт не заведён — без него нет сравнения «есть ли у нас такое».")
    else:
        state = f"⏳ идёт обход, {crawl_progress(db, own)}" if own.is_crawling else (
            f"обойдён {fmt_dt(own.last_crawl_finished_at, 'ещё не был')}"
        )
        lines.append(f"🌐 Наш сайт: {own.base_url}, {state}")

    return "\n".join(lines)


def _next_crawl_label(competitor: Competitor) -> str:
    if competitor.is_paused:
        return "на паузе"
    if competitor.cookies_only:
        return "по свежим кукам — пришлите выгрузку из Cookie-Editor"
    return fmt_dt(competitor.next_crawl_at)


def competitor_card_text(db: Session, competitor: Competitor) -> str:
    now = datetime.now(UTC)
    pages_count = db.scalar(
        select(func.count(Page.id)).where(Page.competitor_id == competitor.id, Page.is_removed.is_(False))
    ) or 0

    lines = [
        f"🏢 {competitor.name}",
        competitor.base_url,
        "",
        f"Статус: {status_line(competitor)}",
    ]
    progress = crawl_progress(db, competitor)
    if progress:
        lines.append(f"Прогресс: {progress}")
    lines += [
        f"Страниц под наблюдением: {pages_count}",
        f"Последний обход: {fmt_dt(competitor.last_crawl_finished_at, 'ещё не было')}",
        f"Следующий по расписанию: {_next_crawl_label(competitor)}",
        f"Находки за 30 дней: {_counts_line(_change_counts(db, now - timedelta(days=30), competitor.id))}",
    ]
    if competitor.last_report_at:
        lines.append(f"Последний отчёт: {fmt_dt(competitor.last_report_at)}")
    if competitor.cookies_only:
        lines += [
            "",
            (
                "🍪 Сайт пускает бота только со свежими куками из вашего Chrome (живут ~час). "
                "Откройте сайт в Chrome → Cookie-Editor → Export → JSON → пришлите файл сюда: "
                "бот сохранит сессию и сразу начнёт обход."
            ),
        ]
    elif competitor.status is SessionStatus.NEEDS_SESSION:
        lines += [
            "",
            (
                "⚠️ Сайт мог заблокировать обход. Если обход не проходит — нужна ручная сессия "
                "(см. README, раздел про антибот-защиту)."
            ),
        ]
    return "\n".join(lines)


@dataclass
class ChangesPage:
    items: list[PageChange]
    page: int
    total_pages: int
    total: int


def load_changes(
    db: Session,
    *,
    competitor_id: int | None = None,
    kind: str = "all",
    page: int = 1,
    page_size: int = CHANGES_PAGE_SIZE,
    since: datetime | None = None,
) -> ChangesPage:
    # Общая лента — только конкуренты. Лента одного сайта может быть и нашей:
    # так смотрят, что изменилось на нашем сайте после его обхода.
    if competitor_id is not None:
        conditions = [Page.competitor_id == competitor_id]
    else:
        conditions = [Competitor.is_own.is_(False)]
    change_type = CHANGE_KINDS.get(kind)
    if change_type is not None:
        conditions.append(PageChange.change_type == change_type)
    if kind == "important":
        conditions.append(AiAnalysis.importance == "high")
    if since is not None:
        conditions.append(PageChange.detected_at >= since)

    def _base(stmt):
        return stmt.join(Page).join(Competitor).outerjoin(AiAnalysis).where(*conditions)

    total = db.scalar(_base(select(func.count(PageChange.id)).select_from(PageChange))) or 0
    total_pages = max((total + page_size - 1) // page_size, 1)
    page = min(max(page, 1), total_pages)

    items = (
        db.execute(
            _base(select(PageChange))
            .options(
                joinedload(PageChange.page).joinedload(Page.competitor),
                joinedload(PageChange.ai_analysis),
            )
            .order_by(PageChange.detected_at.desc(), PageChange.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .unique()
        .scalars()
        .all()
    )
    return ChangesPage(items=list(items), page=page, total_pages=total_pages, total=total)


def is_important(change: PageChange) -> bool:
    return bool(change.ai_analysis and change.ai_analysis.importance == "high")


def change_line(change: PageChange, *, with_competitor: bool = True, summary_limit: int = 220) -> str:
    page = change.page
    head = CHANGE_EMOJI[change.change_type]
    if is_important(change):
        head = f"{IMPORTANT_MARK}{head}"
    if with_competitor:
        head += f" {page.competitor.name}"
    head += f" · {fmt_dt(change.detected_at)}"

    lines = [head]
    if page.title:
        lines.append(_shorten(page.title, 120))
    summary = change.ai_analysis.summary if change.ai_analysis else None
    if summary:
        lines.append(_shorten(summary, summary_limit))
    if change.change_type is ChangeType.REMOVED and page.redirect_to_url:
        lines.append(f"↪️ теперь ведёт на {page.redirect_to_url}")
    lines.append(page.url)
    return "\n".join(lines)


def changes_text(db: Session, result: ChangesPage, *, competitor: Competitor | None, kind: str) -> str:
    if competitor is not None and competitor.is_own:
        header = f"🌐 Изменения на нашем сайте {competitor.base_url} — {CHANGE_KIND_LABELS[kind].lower()}"
    else:
        scope = f"по «{competitor.name}»" if competitor else "по всем конкурентам"
        header = f"🆕 Находки {scope} — {CHANGE_KIND_LABELS[kind].lower()}"
    if not result.items:
        return f"{header}\n\nПока ничего нет."

    blocks = [f"{header}\nСтраница {result.page} из {result.total_pages}, всего {result.total}"]
    blocks += [change_line(c, with_competitor=competitor is None) for c in result.items]
    return "\n\n".join(blocks)


def reports_text(db: Session) -> str:
    competitors = list_competitors(db)
    if not competitors:
        return "📄 Отчётов пока нет — нет ни одного конкурента."

    lines = ["📄 Отчёты об обходах", ""]
    for c in competitors:
        if c.last_report_url:
            lines.append(f"— {c.name}: последний от {fmt_dt(c.last_report_at)}")
        else:
            lines.append(f"— {c.name}: отчётов ещё не было")
    lines += ["", "Кнопки ниже открывают последний отчёт и папку со всеми отчётами."]
    return "\n".join(lines)


def integrations_status() -> list[str]:
    """Что из внешнего подключено — чтобы не гадать, почему нет отчёта или ИИ-разбора."""
    from app.integrations.google_docs import GoogleDocsClient

    claude_ok = bool(shutil.which(settings.claude_cli_path)) or Path(settings.claude_cli_path).exists()
    return [
        f"{'✅' if claude_ok else '❌'} ИИ-разбор (Claude Code CLI)",
        f"{'✅' if GoogleDocsClient().is_configured else '❌'} Отчёты в Google Docs",
        f"{'✅' if settings.apify_api_token else '➖'} Apify (запасной обход защищённых сайтов)",
        f"{'✅' if settings.telegram_chat_id else '❌'} Уведомления в Telegram",
    ]


def settings_text(db: Session) -> str:
    config = get_schedule_config(db)
    lines = [
        "⚙️ Настройки",
        "",
        f"Расписание обходов: {fmt_interval(config.interval_days, config.interval_hours)}",
        "Выберите новый интервал кнопкой ниже или напишите, например: «обходи раз в 3 дня».",
        "",
        "Подключения:",
        *integrations_status(),
    ]
    return "\n".join(lines)
