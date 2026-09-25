"""Полный цикл обхода одного конкурента: краулер -> diff -> сохранение -> ИИ -> Telegram.

Запускается только через app/crawl_manager.py — он же отвечает за отметку
«обход идёт» и за лимит одновременных обходов.

Всё, что ходит в сеть синхронно (Google Docs/Drive), выносится в отдельный поток
через asyncio.to_thread: иначе на время создания отчёта встаёт весь event loop —
другие обходы не двигаются, а веб-интерфейс не отвечает.
"""

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from patchright.async_api import async_playwright
from sqlalchemy.orm import Session

from app.ai.analyze import (
    AiAnalysisResult,
    ClaudeCliError,
    analyze_page_change,
    importance_rank,
    parse_ai_response,
    run_claude_cli,
)
from app.ai.backfill import backfill_missing_analyses
from app.ai.compare import ComparisonResult, OwnSite, compare_with_own_site
from app.config import STORAGE_STATE_DIR, settings
from app.crawler.apify_crawl import ApifyCrawlError, crawl_competitor_via_apify
from app.crawler.browser import launch_browser, new_stealth_context, storage_state_path_for
from app.crawler.crawl import (
    CacheLookup,
    CacheStore,
    CompetitorBlockedError,
    CrawlResult,
    OnUrlsDiscovered,
    PreviousPageInfo,
    crawl_competitor,
)
from app.crawler.diff import ChangeType, PageDiff, content_hash, diff_crawl
from app.crawler.recheck import DisappearReason, MissingPageCheck, check_missing_pages
from app.integrations.google_docs import GoogleDocsClient, GoogleDocsError, build_row
from app.models import (
    AiAnalysis,
    ComparisonVerdict,
    Competitor,
    DisappearanceReason,
    NotificationLog,
    NotificationStatus,
    OwnSiteComparison,
    Page,
    PageChange,
    PageFetchCache,
    PageSnapshot,
    SessionStatus,
)
from app.models import (
    ChangeType as DbChangeType,
)
from app.notifications.telegram import (
    TelegramNotifier,
    format_baseline_finished,
    format_baseline_note,
    format_blocked_message,
    format_comparison_summary,
    format_error_message,
    format_finished_message,
    format_important_summary,
    format_own_site_finished,
    format_started_message,
    own_site_buttons,
    report_buttons,
)
from app.own_site import load_own_site

logger = logging.getLogger(__name__)

_NO_CHANGES_NOTE = "Изменений с прошлого обхода не найдено."

_CHANGE_TYPE_TO_DB = {
    ChangeType.NEW: DbChangeType.NEW,
    ChangeType.CHANGED: DbChangeType.CHANGED,
    ChangeType.REMOVED: DbChangeType.REMOVED,
}

_DISAPPEARANCE_TO_DB = {
    DisappearReason.DELETED: DisappearanceReason.DELETED,
    DisappearReason.MOVED: DisappearanceReason.MOVED,
    DisappearReason.STILL_ALIVE: DisappearanceReason.MISSING_FROM_CRAWL,
    DisappearReason.UNKNOWN: DisappearanceReason.MISSING_FROM_CRAWL,
}

_REDIRECT_PROMPT = (
    "Ты аналитик, который следит за сайтом конкурента для отдела маркетинга.\n"
    "Страница {old_url} больше не открывается по прежнему адресу — сайт "
    "перенаправляет на {new_url}.\n"
    "Ниже текст страницы, на которую ведёт перенаправление.\n\n"
    "Текст:\n"
    "```\n"
    "{text}\n"
    "```\n\n"
    "Верни СТРОГО один JSON-объект (без пояснений вне JSON):\n"
    "{{\n"
    '  "category": "тип страницы: лендинг/оффер/цены/статья/другое",\n'
    '  "usp": "ключевое УТП или выгода, если есть, иначе null",\n'
    '  "cta": "текст призыва к действию, если есть, иначе null",\n'
    '  "summary": "1-2 предложения простым русским: что теперь на этой странице"\n'
    "}}\n"
)


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


async def _notify(
    notifier: TelegramNotifier,
    db: Session,
    competitor: Competitor,
    text: str,
    *,
    page_change_id: int | None,
    buttons: list[list[dict]] | None = None,
) -> None:
    status = NotificationStatus.SENT
    error = None
    try:
        await notifier.send(text, buttons=buttons)
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


async def _notify_progress(
    notifier: TelegramNotifier,
    db: Session,
    competitor: Competitor,
    text: str,
    *,
    buttons: list[list[dict]] | None = None,
) -> None:
    """Рутинные сообщения «обход начат / завершён» — только по конкурентам.

    Итог обхода нашего сайта уходит отдельно, своим сообщением «что изменилось на
    нашем сайте» (см. run_crawl_for_competitor). Про сломавшийся обход сообщаем по
    любому сайту — это идёт мимо этой функции.
    """
    if competitor.is_own:
        return
    await _notify(notifier, db, competitor, text, page_change_id=None, buttons=buttons)


def _fetch_cache_lookup(db: Session, competitor_id: int) -> CacheLookup:
    """Черновик страницы, загруженной не позже CRAWL_RESUME_MAX_AGE_HOURS назад.

    Нужен на случай обрыва обхода (упал сервер, легла база): следующий запуск не
    должен заново идти на сайт конкурента за страницами, которые уже недавно получил.
    """
    max_age = timedelta(hours=settings.crawl_resume_max_age_hours)

    async def lookup(url: str) -> tuple[str, str] | None:
        def _run() -> tuple[str, str] | None:
            row = (
                db.query(PageFetchCache)
                .filter(PageFetchCache.competitor_id == competitor_id, PageFetchCache.url == url)
                .one_or_none()
            )
            if row is None:
                return None
            fetched_at = row.fetched_at if row.fetched_at.tzinfo else row.fetched_at.replace(tzinfo=UTC)
            if datetime.now(UTC) - fetched_at >= max_age:
                return None
            return row.title or "", row.text_content

        return await asyncio.to_thread(_run)

    return lookup


def _fetch_cache_store(db: Session, competitor_id: int) -> CacheStore:
    async def store(url: str, title: str, text: str) -> None:
        def _run() -> None:
            existing = (
                db.query(PageFetchCache)
                .filter(PageFetchCache.competitor_id == competitor_id, PageFetchCache.url == url)
                .one_or_none()
            )
            if existing is not None:
                existing.title = title
                existing.text_content = text
                existing.fetched_at = datetime.now(UTC)
            else:
                db.add(
                    PageFetchCache(
                        competitor_id=competitor_id, url=url, title=title, text_content=text
                    )
                )
            db.commit()

        try:
            await asyncio.to_thread(_run)
        except Exception:
            db.rollback()
            logger.exception("Не удалось сохранить черновик страницы %s", url)

    return store


def _on_urls_discovered(db: Session, competitor_id: int) -> OnUrlsDiscovered:
    async def on_discovered(total: int) -> None:
        def _run() -> None:
            competitor = db.get(Competitor, competitor_id)
            if competitor is not None:
                competitor.crawl_pages_total = total
                db.add(competitor)
                db.commit()

        try:
            await asyncio.to_thread(_run)
        except Exception:
            db.rollback()
            logger.exception("Не удалось сохранить общее число страниц конкурента %s", competitor_id)

    return on_discovered


def _clear_fetch_cache(db: Session, competitor_id: int) -> None:
    """Черновик страниц нужен только для незавершённого обхода. После успеха
    он не нужен — а если оставить, следующий недельный обход мог бы принять
    старый черновик за свежие данные и пропустить настоящие изменения."""
    db.query(PageFetchCache).filter(PageFetchCache.competitor_id == competitor_id).delete()
    db.query(Competitor).filter(Competitor.id == competitor_id).update({"crawl_pages_total": None})


def _needs_full_crawl(competitor: Competitor) -> bool:
    """Первый обход конкурента (нет даты последнего полного обхода) или пора сделать
    очередную периодическую полную сверку — на случай сайтов, которые не обновляют
    дату в карте сайта честно, и инкрементальный обход по ней тихо пропускал бы
    настоящие изменения. См. CLAUDE.md про инкрементальный обход по sitemap lastmod."""
    if competitor.last_full_crawl_at is None:
        return True
    last = competitor.last_full_crawl_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return datetime.now(UTC) - last >= timedelta(days=settings.crawl_full_recheck_days)


async def _crawl_competitor(
    db: Session,
    competitor: Competitor,
    previous_pages: dict[str, PreviousPageInfo],
    force_full: bool,
) -> CrawlResult:
    """Сначала локальный Playwright (с сохранённой сессией, если она есть, иначе с чистым
    контекстом — этого достаточно для слабо защищённых сайтов), и только если он упёрся
    в блокировку — Apify Cloud как платный фолбэк (см. CLAUDE.md).

    Черновик уже загруженных страниц (PageFetchCache) работает только для локального
    Playwright — у Apify весь сайт приходит одним ответом, догружать по одной странице
    там нечего. У Apify также нет инкрементального обхода: актор всегда обходит сайт
    заново целиком — известное ограничение, обход через него просто не даёт экономии.
    """
    storage_state_path = storage_state_path_for(competitor.id, STORAGE_STATE_DIR)
    has_session = storage_state_path.exists()
    max_pages = settings.crawl_full_crawl_max_pages if force_full else settings.crawl_max_pages

    try:
        async with (
            async_playwright() as playwright,
            launch_browser(playwright, headless=True) as browser,
            new_stealth_context(
                browser,
                storage_state_path=storage_state_path if has_session else None,
                block_heavy_resources=settings.crawl_block_heavy_resources,
            ) as context,
        ):
            return await crawl_competitor(
                context,
                competitor.base_url,
                max_pages=max_pages,
                previous_pages=previous_pages,
                force_full=force_full,
                cache_lookup=_fetch_cache_lookup(db, competitor.id),
                cache_store=_fetch_cache_store(db, competitor.id),
                on_urls_discovered=_on_urls_discovered(db, competitor.id),
                sitemap_url=competitor.sitemap_url,
            )
    except CompetitorBlockedError:
        if not settings.apify_api_token:
            raise
        logger.info(
            "Локальный Playwright заблокирован у конкурента %s — пробуем Apify Cloud", competitor.name
        )
        return await crawl_competitor_via_apify(competitor.base_url, max_pages=max_pages)


async def run_crawl_for_competitor(db: Session, competitor: Competitor) -> None:
    notifier = TelegramNotifier()

    if competitor.is_paused:
        logger.info("Конкурент %s на паузе — пропускаем обход", competitor.name)
        return

    # Отметку «обход идёт» уже поставил crawl_manager — синхронно, до постановки
    # фоновой задачи. Повторять здесь проверку is_crawling нельзя: раз мы внутри
    # функции, значит отметка только что выставлена и проверка всегда сработает.
    await _notify_progress(notifier, db, competitor, format_started_message(competitor.name))
    db.commit()

    # Всё, что может пойти не так, оборачиваем одним try — иначе необработанное
    # исключение где-то в diff/ИИ/отчёте оставит конкурента навсегда в статусе
    # "обход идёт" и заблокирует все следующие запуски (см. is_crawling).
    try:
        # Тяжёлая выборка: тянет тексты всех страниц конкурента (мегабайты) —
        # синхронный SQLAlchemy на такой запросе держит event loop заметно долго.
        # Нужна ДО обхода: по сохранённой дате из sitemap решаем, что реально грузить.
        previous_by_url = await asyncio.to_thread(_latest_snapshots_by_url, db, competitor.id)
        previous_texts = {url: text for url, (_page, text) in previous_by_url.items()}
        previous_pages = {
            url: PreviousPageInfo(text=text, lastmod=page.sitemap_lastmod)
            for url, (page, text) in previous_by_url.items()
        }
        force_full = _needs_full_crawl(competitor)

        crawl_result = await _crawl_competitor(db, competitor, previous_pages, force_full)

        diffs = diff_crawl(previous_texts, crawl_result.pages)
        run_at = datetime.now(UTC)

        checks = await _recheck_missing_pages(diffs, previous_by_url)
        diffs = [d for d in diffs if _is_real_change(d, checks)]

        now = datetime.now(UTC)
        for url in crawl_result.pages:
            if url in previous_by_url:
                _mark_page_alive(previous_by_url[url][0], now)
        for url, check in checks.items():
            _apply_missing_check(previous_by_url[url][0], check, now)

        own_site = await _load_own_site_for_comparison(db, competitor)
        compare_urls: set[str] = set()
        if own_site:
            selected = select_diffs_for_comparison(diffs, settings.own_site_compare_max_per_run)
            compare_urls = {d.url for d in selected}

        # Подготовка (быстрая работа с БД) — строго последовательно: page_change.id
        # нужен ДО вызова ИИ, а один SQLAlchemy Session нельзя дёргать из по-настоящему
        # параллельных операций разом.
        # Первый обход сайта: сравнивать не с чем, и diff_crawl объявил бы «новой» каждую
        # страницу — на сайте в 2000 страниц это 2000 вызовов ИИ, отчёт на 2000 строк и
        # лента, забитая мусором. Поэтому первый обход — только точка отсчёта: страницы
        # и их текст сохраняем, находками не считаем.
        is_baseline = not previous_by_url
        diffs_by_url = {d.url: d for d in diffs}
        prepared: list[tuple[PageDiff, Page, PageChange, str | None]] = []
        for page_diff in diffs:
            page = _apply_page_diff(db, competitor, page_diff, previous_by_url, crawl_result.sitemap_lastmod)
            if is_baseline:
                _save_baseline_snapshot(db, page, page_diff)
                continue
            check = checks.get(page_diff.url)
            moved = check.final_url if check and check.reason is DisappearReason.MOVED else None
            if moved:
                page.redirect_target_summary = await _describe_redirect_target(check)

            page_change = _record_change(db, page, page_diff)
            db.flush()  # получаем page_change.id до вызова ИИ
            prepared.append((page_diff, page, page_change, moved))

        # Сами вызовы ИИ (claude -p, секунды каждый) не трогают БД, пока не получат
        # ответ — поэтому безопасно гонять их параллельно (до claude_cli_concurrency
        # разом), а не строго по одному, как раньше. db.add() внутри _analyze_and_store/
        # _compare_with_own_site_and_store происходит уже после await, синхронно —
        # это не настоящая параллельная запись в Session.
        ai_semaphore = asyncio.Semaphore(max(1, settings.claude_cli_concurrency))

        async def _limited(coro):
            async with ai_semaphore:
                return await coro

        analyses = await asyncio.gather(
            *(
                _limited(_analyze_and_store(db, page_change, page_diff))
                for page_diff, _page, page_change, _moved in prepared
            )
        )
        comparisons_by_item = await asyncio.gather(
            *(
                _limited(_compare_with_own_site_and_store(db, page_change, page_diff, own_site))
                if page_diff.url in compare_urls
                else _no_comparison()
                for page_diff, _page, page_change, _moved in prepared
            )
        )

        ranked_rows: list[tuple[int, list[str]]] = []
        comparisons: list[tuple[str, ComparisonResult]] = []
        important: list[tuple[str, AiAnalysisResult]] = []
        for (page_diff, page, _page_change, moved), analysis, comparison in zip(
            prepared, analyses, comparisons_by_item, strict=True
        ):
            if comparison:
                comparisons.append((page_diff.url, comparison))
            if analysis and analysis.importance == "high":
                important.append((page_diff.url, analysis))

            ranked_rows.append(
                (
                    importance_rank(analysis.importance if analysis else None),
                    build_row(
                        page_diff,
                        analysis,
                        run_at,
                        redirect_to=moved,
                        redirect_summary=page.redirect_target_summary if moved else None,
                        comparison=comparison,
                    ),
                )
            )
        # Важное — в начало отчёта; внутри одной важности порядок прежний (sort стабилен).
        report_rows = [row for _rank, row in sorted(ranked_rows, key=lambda item: item[0])]

        if is_baseline and own_site:
            # Изменений в первом обходе нет, но «есть ли у нас такое» спросить уже можно.
            baseline_comparisons = await _compare_baseline_with_own_site(diffs, own_site, _limited)
            comparisons.extend(baseline_comparisons)
            report_rows.extend(
                build_row(diff, None, run_at, comparison=comparison, type_label=_BASELINE_ROW_LABEL)
                for diff, comparison in (
                    (diffs_by_url[url], comparison) for url, comparison in baseline_comparisons
                )
            )

        if competitor.status != SessionStatus.ACTIVE:
            competitor.status = SessionStatus.ACTIVE
            db.add(competitor)

        notes = build_run_notes(crawl_result, checks)

        # Находки этого и прошлых прогонов, оставшиеся без ИИ-разбора (CLI не ответил).
        # Инкрементальный обход сам их не пересмотрит — страницы-то не менялись.
        db.flush()
        backfilled, attempted = await _backfill_missing_analyses(db, competitor)
        if attempted:
            notes.append(_format_backfill_note(backfilled, attempted))

        report_url = None
        # Отчёт в Google Docs — только по конкурентам и после каждого их обхода, даже
        # при нуле находок: «изменений нет» — тоже результат. По нашему сайту отчёт
        # не нужен (решение владельца): что на нём изменилось, бот присылает в чат.
        if not competitor.is_own:
            if is_baseline:
                report_notes = [*notes, format_baseline_note(len(diffs))]
            else:
                report_notes = notes if report_rows else [*notes, _NO_CHANGES_NOTE]
            report_url = await _write_run_report(db, competitor, run_at, report_rows, report_notes)
            if report_url:
                competitor.last_report_url = report_url
                competitor.last_report_at = run_at

        competitor.last_crawl_finished_at = now
        if force_full:
            competitor.last_full_crawl_at = now
        db.add(competitor)
        _clear_fetch_cache(db, competitor.id)
        await asyncio.to_thread(db.commit)

        if competitor.is_own:
            # Наш сайт обходят по кнопке «обойти наш сайт», чтобы проверить, что на нём
            # поменялось, — итог нужен так же, как по конкуренту, но в своём виде.
            own_items = [
                (page_diff, analysis)
                for (page_diff, _page, _change, _moved), analysis in zip(prepared, analyses, strict=True)
            ]
            await _notify(
                notifier,
                db,
                competitor,
                format_own_site_finished(
                    competitor.base_url, own_items, baseline_pages=len(diffs) if is_baseline else None
                ),
                page_change_id=None,
                buttons=own_site_buttons(competitor.id) if own_items else None,
            )
            db.commit()
            logger.info("Обход нашего сайта завершён: %s изменений", len(own_items))
            return

        if is_baseline:
            message_parts = [format_baseline_finished(competitor.name, len(diffs), report_url)]
        else:
            message_parts = [format_finished_message(competitor.name, len(diffs), report_url)]
        if important:
            message_parts.append(format_important_summary(important))
        if notes:
            message_parts.append("\n".join(notes))
        if comparisons:
            message_parts.append(format_comparison_summary(comparisons))
        await _notify_progress(
            notifier,
            db,
            competitor,
            "\n\n".join(message_parts),
            buttons=report_buttons(competitor.id, report_url, competitor.google_drive_folder_url),
        )
        db.commit()

        logger.info("Обход конкурента %s завершён: %s изменений", competitor.name, len(prepared))

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

    except Exception as exc:
        db.rollback()
        competitor.last_crawl_finished_at = datetime.now(UTC)
        db.add(competitor)
        message = format_error_message(competitor.name, str(exc))
        await _notify(notifier, db, competitor, message, page_change_id=None)
        db.commit()
        logger.exception("Непредвиденная ошибка при обходе конкурента %s", competitor.name)


async def _recheck_missing_pages(
    diffs: list[PageDiff], previous_by_url: dict[str, tuple[Page, str]]
) -> dict[str, MissingPageCheck]:
    """Ходит по адресам страниц, пропавших из обхода, и выясняет их судьбу.

    Раньше такая страница слепо помечалась удалённой. При лимите в 200 страниц у
    сайта на 500 это давало 300 ложных «удалений» каждый прогон.
    """
    missing_pages = [
        previous_by_url[d.url][0]
        for d in diffs
        if d.change_type is ChangeType.REMOVED and d.url in previous_by_url
    ]
    if not missing_pages:
        return {}

    # За прогон проверяем ограниченное число страниц, поэтому начинаем с тех, кого
    # давно не проверяли — иначе один и тот же хвост списка никогда не дойдёт до проверки.
    missing_pages.sort(key=lambda page: (page.last_checked_at is not None, page.last_checked_at))
    return await check_missing_pages([page.url for page in missing_pages])


def _is_real_change(page_diff: PageDiff, checks: dict[str, MissingPageCheck]) -> bool:
    """Пропажу из обхода показываем владельцу, только если страница правда удалена
    или переехала. Живая страница, не попавшая в обход, — это не новость."""
    if page_diff.change_type is not ChangeType.REMOVED:
        return True
    check = checks.get(page_diff.url)
    return check is not None and check.is_really_gone


def _mark_page_alive(page: Page, now: datetime) -> None:
    page.last_seen_at = now
    page.last_checked_at = now
    # Страница вернулась в обход — снимаем прежний вердикт, иначе в интерфейсе
    # навсегда останется отметка "удалена/переехала" по живой странице.
    page.is_removed = False
    page.removed_at = None
    page.disappearance_reason = None
    page.redirect_to_url = None
    page.redirect_target_summary = None


def _apply_missing_check(page: Page, check: MissingPageCheck, now: datetime) -> None:
    page.last_checked_at = now
    page.disappearance_reason = _DISAPPEARANCE_TO_DB[check.reason]

    if not check.is_really_gone:
        return  # страница жива (или вердикта нет) — удалённой не помечаем

    page.is_removed = True
    page.removed_at = now
    if check.reason is DisappearReason.MOVED:
        page.redirect_to_url = check.final_url


def build_run_notes(crawl_result: CrawlResult, checks: dict[str, MissingPageCheck]) -> list[str]:
    """Пояснения к прогону простым языком — уходят в отчёт и в Telegram."""
    notes: list[str] = []

    if crawl_result.is_truncated:
        notes.append(
            f"Показан не весь сайт: у конкурента найдено страниц — {crawl_result.urls_total}, "
            f"а за один обход мы смотрим только {len(crawl_result.pages)}. "
            "Чтобы видеть больше, увеличьте лимит страниц в настройках."
        )

    if crawl_result.failed_urls:
        notes.append(
            f"Не удалось загрузить страниц (таймаут или обрыв сети): {len(crawl_result.failed_urls)}. "
            "Попробуем ещё раз в следующем обходе."
        )

    moved = sum(1 for c in checks.values() if c.reason is DisappearReason.MOVED)
    if moved:
        notes.append(f"Страниц переехало на новый адрес: {moved}.")

    survived = sum(1 for c in checks.values() if not c.is_really_gone)
    if survived:
        notes.append(
            f"Страниц не попало в этот обход, но они по-прежнему открываются: {survived}. "
            "Удалёнными мы их не считаем."
        )

    return notes


def build_redirect_prompt(old_url: str, new_url: str, target_text: str) -> str:
    return _REDIRECT_PROMPT.format(old_url=old_url, new_url=new_url, text=target_text[:4000])


async def _describe_redirect_target(check: MissingPageCheck) -> str | None:
    """Коротко спрашивает у ИИ, что теперь на странице, куда ведёт перенаправление."""
    if not check.final_url or not check.final_text:
        return None

    try:
        raw_text = await run_claude_cli(
            build_redirect_prompt(check.url, check.final_url, check.final_text)
        )
        return parse_ai_response(raw_text).summary
    except (ClaudeCliError, ValueError) as exc:
        logger.warning("Не удалось выяснить, что теперь на странице %s: %s", check.final_url, exc)
        return None


def _create_run_report(
    competitor_name: str,
    folder_id: str | None,
    run_at: datetime,
    rows: list[list[str]],
    notes: list[str],
) -> tuple[str | None, str | None, str | None]:
    """Создаёт документ прогона на Google Drive. Возвращает (url отчёта, id папки, url папки).

    Блокирующая: google-api-python-client ходит в сеть синхронно. Вызывать только
    через asyncio.to_thread — иначе на всё время создания отчёта встаёт event loop.
    """
    client = GoogleDocsClient()
    if not client.is_configured:
        return None, folder_id, None

    try:
        folder_url = None
        if not folder_id:
            folder_id, folder_url = client.get_or_create_competitor_folder(competitor_name)
        return client.create_run_document(folder_id, run_at, rows, notes=notes), folder_id, folder_url
    except GoogleDocsError:
        logger.exception("Не удалось сохранить отчёт обхода конкурента %s в Google Docs", competitor_name)
        return None, folder_id, None


async def _write_run_report(
    db: Session,
    competitor: Competitor,
    run_at: datetime,
    rows: list[list[str]],
    notes: list[str],
) -> str | None:
    report_url, folder_id, folder_url = await asyncio.to_thread(
        _create_run_report, competitor.name, competitor.google_drive_folder_id, run_at, rows, notes
    )

    # ORM-объект правим на основном потоке, а не внутри to_thread: Session не
    # рассчитан на работу из двух потоков одновременно.
    if folder_id and folder_id != competitor.google_drive_folder_id:
        competitor.google_drive_folder_id = folder_id
        competitor.google_drive_folder_url = folder_url
        db.add(competitor)

    return report_url


def _apply_page_diff(
    db: Session,
    competitor: Competitor,
    page_diff: PageDiff,
    previous_by_url: dict[str, tuple[Page, str]],
    sitemap_lastmod: dict[str, datetime | None],
) -> Page:
    if page_diff.url in previous_by_url:
        page = previous_by_url[page_diff.url][0]
        if page_diff.change_type is ChangeType.CHANGED:
            # Страницу правда перезагрузили (иначе бы не было diff) — обновляем
            # сохранённую дату, чтобы следующий обход снова мог её пропустить.
            page.sitemap_lastmod = sitemap_lastmod.get(page_diff.url)
        return page

    # Страница может уже быть в БД, но помеченной удалённой (например, ложно —
    # старой логикой). Создать вторую запись с тем же url нельзя: на паре
    # (конкурент, url) стоит уникальный индекс, и обход упал бы на вставке.
    page = (
        db.query(Page)
        .filter(Page.competitor_id == competitor.id, Page.url == page_diff.url)
        .one_or_none()
    )
    if page is not None:
        _mark_page_alive(page, datetime.now(UTC))
    else:
        page = Page(competitor_id=competitor.id, url=page_diff.url)
        db.add(page)
    page.sitemap_lastmod = sitemap_lastmod.get(page_diff.url)
    db.flush()

    previous_by_url[page_diff.url] = (page, "")
    return page


def _save_baseline_snapshot(db: Session, page: Page, page_diff: PageDiff) -> None:
    """Текст страницы с первого обхода — без записи «изменения»: следующий обход
    будет сравнивать с ним (см. _latest_snapshots_by_url)."""
    text = page_diff.new_text or ""
    db.add(PageSnapshot(page_id=page.id, content_hash=content_hash(text), text_content=text))


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


def select_diffs_for_comparison(diffs: list[PageDiff], limit: int) -> list[PageDiff]:
    """Какие находки сравнивать с нашим сайтом.

    Удалённые страницы отсекаем: спрашивать «есть ли у нас такое» про то, чего уже
    нет у конкурента, бессмысленно. Лимит нужен, потому что каждое сравнение — это
    отдельный вызов ИИ: обход, нашедший 200 новых страниц, иначе сделал бы 200
    вызовов. Новые страницы идут первыми — это самые важные находки.
    """
    if limit <= 0:
        return []

    new_pages = [d for d in diffs if d.change_type is ChangeType.NEW]
    changed_pages = [d for d in diffs if d.change_type is ChangeType.CHANGED]
    return (new_pages + changed_pages)[:limit]


_BASELINE_ROW_LABEL = "Первый обход:\nсравнение с нашим сайтом"

# Адреса статей, новостей, документов — не то, с чем сравнивать наше предложение.
_BASELINE_SKIP_PATH_RE = re.compile(
    r"/(articles?|blog|news|novosti|media|questions?|voprosy|polza|stat[iy]a?|wiki|tags?|"
    r"category|author|page|search|faq|oferta[^/]*|polic[yi][^/]*|politika[^/]*|soglasie[^/]*|"
    r"privacy[^/]*|documents?|dokumenty|svedeniya[^/]*|vakansii|jobs?|login|lk|account)(/|$)",
    re.IGNORECASE,
)
# Учебные материалы в адресе (варианты ВПР с ответами, решебники) — тоже не предложение школы.
_BASELINE_SKIP_SLUG_RE = re.compile(r"variant|zadani|otvet|reshebnik|gdz|konspekt|urok-\d", re.IGNORECASE)
# Коммерческие страницы — то, ради чего сравниваем: курсы, цены, классы, программы.
_BASELINE_KEY_PATH_RE = re.compile(
    r"kurs|course|price|cen[ay]|stoimost|tarif|shkol|school|klass|class|program|ege|oge|"
    r"repetitor|tutor|podgotovk|obuchen|napravlen|predmet|homeschool|semejn|eksternat",
    re.IGNORECASE,
)


def select_key_pages_for_baseline(diffs: list[PageDiff], limit: int) -> list[PageDiff]:
    """Ключевые страницы конкурента для сравнения с нашим сайтом в первом обходе.

    Статьи/новости/документы отбрасываем; сначала коммерческие адреса (курсы, цены,
    классы), внутри — короткие (разделы раньше глубоких подстраниц). Порядок
    детерминированный — повторный первый обход выберет те же страницы.
    """
    if limit <= 0:
        return []

    def _key(diff: PageDiff) -> tuple[int, int, int, str]:
        path = urlparse(diff.url).path.rstrip("/")
        depth = path.count("/")
        commercial = 0 if _BASELINE_KEY_PATH_RE.search(path) else 1
        return (commercial, depth, len(path), diff.url)

    candidates = [
        diff
        for diff in diffs
        if diff.change_type is ChangeType.NEW
        and diff.new_text
        and not _BASELINE_SKIP_PATH_RE.search(urlparse(diff.url).path)
        and not _BASELINE_SKIP_SLUG_RE.search(urlparse(diff.url).path)
    ]
    return sorted(candidates, key=_key)[:limit]


_VERDICT_ORDER = {"none": 0, "similar": 1, "exact": 2}


async def _compare_baseline_with_own_site(
    diffs: list[PageDiff], own_site: OwnSite, limited
) -> list[tuple[str, ComparisonResult]]:
    """Сравнение ключевых страниц конкурента с нашим сайтом в первом обходе.

    В базу не пишется (OwnSiteComparison привязан к находке, а находок в первом обходе
    нет) — результат идёт в отчёт и итоговое сообщение. «У нас такого нет» — первым.
    """
    selected = select_key_pages_for_baseline(diffs, settings.own_site_baseline_compare_max)

    async def _one(diff: PageDiff) -> ComparisonResult | None:
        try:
            return await compare_with_own_site(diff, own_site)
        except (ClaudeCliError, ValueError) as exc:
            logger.warning("Не удалось сравнить с нашим сайтом %s: %s", diff.url, exc)
            return None

    results = await asyncio.gather(*(limited(_one(diff)) for diff in selected))
    pairs = [(diff.url, result) for diff, result in zip(selected, results, strict=True) if result]
    return sorted(pairs, key=lambda pair: _VERDICT_ORDER.get(pair[1].verdict, 3))


async def _load_own_site_for_comparison(db: Session, competitor: Competitor) -> OwnSite | None:
    """Страницы нашего сайта для сравнения — или None, если сравнивать не с чем.

    Сравнение — не обязательная часть обхода: пока владелец не завёл свой сайт (или
    тот ни разу не обойден), обход конкурентов должен идти как раньше.
    """
    if competitor.is_own:
        return None  # сам с собой сайт не сравниваем
    return await asyncio.to_thread(load_own_site, db)


async def _no_comparison() -> ComparisonResult | None:
    """Заглушка-корутина для asyncio.gather там, где сравнение с нашим сайтом не нужно."""
    return None


async def _compare_with_own_site_and_store(
    db: Session, page_change: PageChange, page_diff: PageDiff, own_site: OwnSite
) -> ComparisonResult | None:
    """Один вызов ИИ на находку. Ошибка ИИ не должна останавливать обход."""
    try:
        comparison = await compare_with_own_site(page_diff, own_site)
    except (ClaudeCliError, ValueError) as exc:
        logger.warning("Не удалось сравнить с нашим сайтом %s: %s", page_diff.url, exc)
        return None

    if comparison is None:
        return None

    db.add(
        OwnSiteComparison(
            page_change_id=page_change.id,
            verdict=ComparisonVerdict(comparison.verdict),
            our_page_url=comparison.our_url,
            differences=comparison.differences,
            missing=comparison.missing,
            raw_response=comparison.raw_response,
        )
    )
    return comparison


async def _backfill_missing_analyses(db: Session, competitor: Competitor) -> tuple[int, int]:
    limit = settings.ai_backfill_max_per_run
    if limit <= 0:
        return 0, 0
    try:
        return await backfill_missing_analyses(db, competitor.id, limit=limit)
    except Exception:  # дозаправка — довесок; её сбой не должен валить сам обход
        logger.exception("Дозаправка ИИ-анализа для конкурента %s упала", competitor.name)
        db.rollback()
        return 0, 0


def _format_backfill_note(done: int, attempted: int) -> str:
    if done == attempted:
        return f"Дозаправлен ИИ-разбор для находок, оставшихся без него раньше: {done}."
    return (
        f"Дозаправлен ИИ-разбор для находок без него: {done} из {attempted}. "
        f"Остальные {attempted - done} попробуем в следующем обходе."
    )


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
            importance=analysis.importance,
            importance_reason=analysis.importance_reason,
            raw_response=analysis.raw_response,
        )
    )
    return analysis
