"""Полный цикл обхода одного конкурента: краулер -> diff -> сохранение -> ИИ -> Telegram.

Используется и планировщиком (раз в неделю), и ручным триггером из UI/API.
"""

import logging
from datetime import UTC, datetime

from playwright.async_api import async_playwright
from sqlalchemy.orm import Session

from app.ai.analyze import AiAnalysisResult, ClaudeCliError, analyze_page_change
from app.config import STORAGE_STATE_DIR, settings
from app.crawler.apify_crawl import ApifyCrawlError, crawl_competitor_via_apify
from app.crawler.browser import launch_browser, new_stealth_context, storage_state_path_for
from app.crawler.crawl import CompetitorBlockedError, CrawlResult, crawl_competitor
from app.crawler.diff import ChangeType, PageDiff, content_hash, diff_crawl
from app.integrations.google_docs import GoogleDocsClient, GoogleDocsError, build_row
from app.models import (
    AiAnalysis,
    ChangeType as DbChangeType,
    Competitor,
    NotificationLog,
    NotificationStatus,
    Page,
    PageChange,
    PageSnapshot,
    SessionStatus,
)
from app.notifications.telegram import (
    TelegramNotifier,
    format_blocked_message,
    format_error_message,
    format_finished_message,
    format_started_message,
)

logger = logging.getLogger(__name__)

_CHANGE_TYPE_TO_DB = {
    ChangeType.NEW: DbChangeType.NEW,
    ChangeType.CHANGED: DbChangeType.CHANGED,
    ChangeType.REMOVED: DbChangeType.REMOVED,
}


def _latest_snapshots_by_url(db: Session, competitor_id: int) -> dict[str, tuple[Page, str]]:
    """Последний снимок текста по каждой активной странице конкурента."""
    pages = (
        db.query(Page)
        .filter(Page.competitor_id == competitor_id, Page.is_removed.is_(False))
        .all()
    )
    result: dict[str, tuple[Page, str]] = {}
    for page in pages:
        if page.snapshots:
            latest = page.snapshots[-1]
            result[page.url] = (page, latest.text_content)
    return result


async def _notify(notifier: TelegramNotifier, db: Session, competitor: Competitor, text: str, *, page_change_id: int | None) -> None:
    status = NotificationStatus.SENT
    error = None
    try:
        await notifier.send(text)
    except Exception as exc:
        status = NotificationStatus.FAILED
        error = str(exc)
        logger.exception("Не удалось отправить уведомление в Telegram")

    db.add(
        NotificationLog(
            page_change_id=page_change_id,
            competitor_id=competitor.id,
            channel="telegram",
            status=status,
            message=text,
            error=error,
        )
    )


async def _crawl_competitor(competitor: Competitor) -> CrawlResult:
    """Сначала локальный Playwright (с сохранённой сессией, если она есть, иначе с чистым
    контекстом — этого достаточно для слабо защищённых сайтов), и только если он упёрся
    в блокировку — Apify Cloud как платный фолбэк (см. CLAUDE.md)."""
    storage_state_path = storage_state_path_for(competitor.id, STORAGE_STATE_DIR)
    has_session = storage_state_path.exists()

    try:
        async with (
            async_playwright() as playwright,
            launch_browser(playwright, headless=True) as browser,
            new_stealth_context(
                browser, storage_state_path=storage_state_path if has_session else None
            ) as context,
        ):
            return await crawl_competitor(context, competitor.base_url, max_pages=settings.crawl_max_pages)
    except CompetitorBlockedError:
        if not settings.apify_api_token:
            raise
        logger.info(
            "Локальный Playwright заблокирован у конкурента %s — пробуем Apify Cloud", competitor.name
        )
        return await crawl_competitor_via_apify(competitor.base_url)


async def run_crawl_for_competitor(db: Session, competitor: Competitor) -> None:
    notifier = TelegramNotifier()

    if competitor.is_paused:
        logger.info("Конкурент %s на паузе — пропускаем обход", competitor.name)
        return

    # Проверку "уже идёт обход" делает вызывающий код (router/scheduler) синхронно, до
    # постановки фоновой задачи — здесь её повторять нельзя: раз мы уже внутри функции,
    # значит вызывающий код только что сам выставил started_at, и is_crawling будет True.
    competitor.last_crawl_started_at = datetime.now(UTC)
    competitor.last_crawl_finished_at = None
    db.add(competitor)
    db.commit()
    await _notify(notifier, db, competitor, format_started_message(competitor.name), page_change_id=None)
    db.commit()

    # Всё, что может пойти не так, оборачиваем одним try — иначе необработанное
    # исключение где-то в diff/ИИ/отчёте оставит конкурента навсегда в статусе
    # "обход идёт" и заблокирует все следующие запуски (см. is_crawling).
    try:
        crawl_result = await _crawl_competitor(competitor)

        previous_by_url = _latest_snapshots_by_url(db, competitor.id)
        previous_texts = {url: text for url, (_page, text) in previous_by_url.items()}

        diffs = diff_crawl(previous_texts, crawl_result.pages)
        run_at = datetime.now(UTC)

        report_rows: list[list[str]] = []
        for page_diff in diffs:
            page = await _apply_page_diff(db, competitor, page_diff, previous_by_url)
            page_change = _record_change(db, page, page_diff)
            db.flush()  # получаем page_change.id до вызова ИИ

            analysis = await _analyze_and_store(db, page_change, page_diff)
            report_rows.append(build_row(page_diff, analysis, run_at))

        now = datetime.now(UTC)
        for url in crawl_result.pages:
            if url in previous_by_url:
                previous_by_url[url][0].last_seen_at = now
        for url, (page, _text) in previous_by_url.items():
            if url not in crawl_result.pages:
                page.is_removed = True
                page.removed_at = now

        if competitor.status != SessionStatus.ACTIVE:
            competitor.status = SessionStatus.ACTIVE
            db.add(competitor)

        report_url = None
        if report_rows:
            report_url = _write_run_report(competitor, run_at, report_rows)

        competitor.last_crawl_finished_at = now
        db.add(competitor)
        db.commit()

        finished_message = format_finished_message(competitor.name, len(diffs), report_url)
        await _notify(notifier, db, competitor, finished_message, page_change_id=None)
        db.commit()

        logger.info("Обход конкурента %s завершён: %s изменений", competitor.name, len(diffs))

    except (ApifyCrawlError, CompetitorBlockedError) as exc:
        db.rollback()
        competitor.status = SessionStatus.NEEDS_SESSION
        competitor.last_crawl_finished_at = datetime.now(UTC)
        db.add(competitor)
        reason = exc.reason if isinstance(exc, CompetitorBlockedError) else str(exc)
        url = exc.url if isinstance(exc, CompetitorBlockedError) else competitor.base_url
        message = format_blocked_message(competitor.name, url, reason)
        await _notify(notifier, db, competitor, message, page_change_id=None)
        db.commit()
        logger.warning("Обход конкурента %s не удался: %s", competitor.name, exc)

    except Exception as exc:  # noqa: BLE001 — гарантируем закрытие "обход идёт" при любой ошибке
        db.rollback()
        competitor.last_crawl_finished_at = datetime.now(UTC)
        db.add(competitor)
        message = format_error_message(competitor.name, str(exc))
        await _notify(notifier, db, competitor, message, page_change_id=None)
        db.commit()
        logger.exception("Непредвиденная ошибка при обходе конкурента %s", competitor.name)


def _write_run_report(competitor: Competitor, run_at: datetime, rows: list[list[str]]) -> str | None:
    """Создаёт документ прогона в папке конкурента на Google Drive. None при любом сбое."""
    client = GoogleDocsClient()
    if not client.is_configured:
        return None

    try:
        if not competitor.google_drive_folder_id:
            folder_id, folder_url = client.get_or_create_competitor_folder(competitor.name)
            competitor.google_drive_folder_id = folder_id
            competitor.google_drive_folder_url = folder_url

        return client.create_run_document(competitor.google_drive_folder_id, run_at, rows)
    except GoogleDocsError:
        logger.exception("Не удалось сохранить отчёт обхода конкурента %s в Google Docs", competitor.name)
        return None


async def _apply_page_diff(
    db: Session,
    competitor: Competitor,
    page_diff: PageDiff,
    previous_by_url: dict[str, tuple[Page, str]],
) -> Page:
    if page_diff.url in previous_by_url:
        return previous_by_url[page_diff.url][0]

    page = Page(competitor_id=competitor.id, url=page_diff.url)
    db.add(page)
    db.flush()
    previous_by_url[page_diff.url] = (page, "")
    return page


def _record_change(db: Session, page: Page, page_diff: PageDiff) -> PageChange:
    old_snapshot = None
    new_snapshot = None

    if page_diff.old_text is not None:
        old_snapshot = PageSnapshot(
            page_id=page.id, content_hash=content_hash(page_diff.old_text), text_content=page_diff.old_text
        )
        db.add(old_snapshot)
        db.flush()

    if page_diff.new_text is not None:
        new_snapshot = PageSnapshot(
            page_id=page.id, content_hash=content_hash(page_diff.new_text), text_content=page_diff.new_text
        )
        db.add(new_snapshot)
        db.flush()

    page_change = PageChange(
        page_id=page.id,
        change_type=_CHANGE_TYPE_TO_DB[page_diff.change_type],
        old_snapshot_id=old_snapshot.id if old_snapshot else None,
        new_snapshot_id=new_snapshot.id if new_snapshot else None,
        diff_text=page_diff.diff_text,
    )
    db.add(page_change)
    return page_change


async def _analyze_and_store(
    db: Session, page_change: PageChange, page_diff: PageDiff
) -> AiAnalysisResult | None:
    try:
        analysis = await analyze_page_change(page_diff)
    except (ClaudeCliError, ValueError) as exc:
        logger.warning("ИИ-анализ не удался для %s: %s", page_diff.url, exc)
        return None

    db.add(
        AiAnalysis(
            page_change_id=page_change.id,
            category=analysis.category,
            usp=analysis.usp,
            cta=analysis.cta,
            summary=analysis.summary,
            raw_response=analysis.raw_response,
        )
    )
    return analysis
