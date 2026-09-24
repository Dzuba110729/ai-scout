"""Обход одного конкурента: находим страницы, рендерим их и отдаём url -> текст.

Классификация new/changed/removed делается отдельно в diff.py — этот модуль
только собирает "снимок" текущего состояния сайта конкурента.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urldefrag, urljoin, urlparse
from xml.etree import ElementTree

import httpx
from patchright.async_api import BrowserContext

from app.config import settings
from app.crawler.blocking import blocked_reason, is_blocked
from app.crawler.diff import extract_text

logger = logging.getLogger(__name__)

DEFAULT_MAX_PAGES = 200
_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# Потолок на число под-файлов sitemap index — просто защита от совсем патологической
# карты сайта. Сама карта сайта читается целиком, без обрезки по числу страниц: это
# обычный HTTP-запрос без браузера, дешёвый даже для тысяч адресов.
_MAX_SITEMAP_INDEX_FILES = 200

# Черновик уже загруженных страниц (см. app.models.PageFetchCache) — необязательные
# хуки, чтобы этот модуль по-прежнему не знал о базе данных напрямую (её открывает
# и закрывает вызывающий код в app/pipeline.py).
CacheLookup = Callable[[str], Awaitable[tuple[str, str] | None]]  # url -> (title, text) | None
CacheStore = Callable[[str, str, str], Awaitable[None]]  # url, title, text
OnUrlsDiscovered = Callable[[int], Awaitable[None]]  # сколько адресов будем обходить


class CompetitorBlockedError(Exception):
    """Обход остановлен: сайт конкурента блокирует нас (403/challenge/капча)."""

    def __init__(self, url: str, reason: str):
        self.url = url
        self.reason = reason
        super().__init__(f"Заблокировано на {url}: {reason}")


@dataclass
class CrawlResult:
    base_url: str
    pages: dict[str, str] = field(default_factory=dict)  # url -> нормализованный текст
    page_titles: dict[str, str] = field(default_factory=dict)
    urls_total: int = 0  # сколько страниц нашли всего, ДО обрезки по лимиту
    # Дата обновления страницы по карте сайта на момент ЭТОГО обхода — только для
    # адресов, которые реально попали в pages (свежепойманные и перенесённые как есть).
    # Следующий обход сравнит с этим, решая, что можно не перезагружать.
    sitemap_lastmod: dict[str, datetime | None] = field(default_factory=dict)
    # Страницы, которые не удалось загрузить в этом обходе (таймаут, обрыв сети и т.п.) —
    # не обвал всего обхода, а точечный пропуск: страница будет перепроверена в
    # следующий раз. См. цикл загрузки в crawl_competitor.
    failed_urls: list[str] = field(default_factory=list)

    @property
    def is_truncated(self) -> bool:
        """Сайт больше, чем мы успеваем обойти за прогон — владельцу это надо сказать
        явно, иначе непонятно, почему в отчёте виден не весь сайт."""
        return self.urls_total > len(self.pages)


def _same_domain(url: str, base_netloc: str) -> bool:
    return urlparse(url).netloc == base_netloc


def normalize_url(url: str) -> str:
    url, _fragment = urldefrag(url)
    return url.rstrip("/") or url


@dataclass(frozen=True)
class SitemapEntry:
    url: str
    lastmod: datetime | None  # дата обновления страницы по данным sitemap, если есть


def _parse_lastmod(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _url_entries(root: ElementTree.Element) -> list[tuple[str, str | None]]:
    entries: list[tuple[str, str | None]] = []
    for url_el in root.findall(".//sm:url", _SITEMAP_NS):
        loc = url_el.find("sm:loc", _SITEMAP_NS)
        if loc is None or not loc.text:
            continue
        lastmod_el = url_el.find("sm:lastmod", _SITEMAP_NS)
        entries.append((loc.text, lastmod_el.text if lastmod_el is not None else None))
    return entries


async def discover_sitemap_entries(base_url: str) -> list[SitemapEntry]:
    """Все адреса сайта из sitemap.xml, с датой обновления страницы (если сайт её даёт).

    Без обрезки лимитом страниц — карта сайта читается целиком (см. _MAX_SITEMAP_INDEX_FILES
    про единственный потолок, и то чисто защитный). Какие из этих адресов реально нужно
    загрузить с сайта, решает plan_crawl — отдельно и позже.
    """
    sitemap_url = urljoin(base_url, "/sitemap.xml")
    raw_entries: list[tuple[str, str | None]] = []

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        try:
            response = await client.get(sitemap_url)
        except httpx.HTTPError:
            return []

        if response.status_code != 200:
            return []

        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError:
            return []

        # sitemap index — верхнеуровневый файл со ссылками на другие sitemap'ы
        sitemap_locs = [loc.text for loc in root.findall(".//sm:sitemap/sm:loc", _SITEMAP_NS) if loc.text]
        if sitemap_locs:
            for loc in sitemap_locs[:_MAX_SITEMAP_INDEX_FILES]:
                try:
                    sub_response = await client.get(loc)
                    sub_root = ElementTree.fromstring(sub_response.content)
                except (httpx.HTTPError, ElementTree.ParseError):
                    continue
                raw_entries.extend(_url_entries(sub_root))
        else:
            raw_entries = _url_entries(root)

    base_netloc = urlparse(base_url).netloc
    deduped: dict[str, datetime | None] = {}
    for url, lastmod_text in raw_entries:
        normalized = normalize_url(url)
        if _same_domain(normalized, base_netloc):
            deduped[normalized] = _parse_lastmod(lastmod_text)

    # Сортировка обязательна: дальше по этому списку срезают лимит на то, сколько
    # страниц реально загрузить — на неупорядоченном множестве каждый прогон резал бы
    # другой хвост, и разница между прогонами выглядела бы как ложные new/removed.
    return [SitemapEntry(url=url, lastmod=deduped[url]) for url in sorted(deduped)]


@dataclass(frozen=True)
class PreviousPageInfo:
    text: str
    lastmod: datetime | None  # дата из sitemap на момент, когда текст был загружен


@dataclass(frozen=True)
class CrawlPlan:
    to_fetch: list[str]  # реально идём на сайт
    carry_over: dict[str, str]  # переносим текст из прошлого снимка без изменений
    live_urls: set[str]  # все адреса, которые сайт показывает сейчас
    urls_total: int


def plan_crawl(
    entries: list[SitemapEntry],
    *,
    previous_pages: dict[str, PreviousPageInfo],
    force_full: bool,
    max_fetch: int,
) -> CrawlPlan:
    """Решает, какие страницы грузить с сайта заново, а какие можно не трогать.

    force_full=True (первый обход конкурента или периодическая полная сверка,
    см. CLAUDE.md) — грузим всё, max_fetch тут просто большой защитный потолок.

    force_full=False (обычный еженедельный обход) — грузим только: страницы,
    которых раньше не было; страницы, у которых дата в карте сайта стала новее
    сохранённой; страницы без известной даты вообще (своей или чужой) — с ними
    сравнивать не с чем, поэтому перепроверяем как раньше, каждый раз. Остальное
    просто переносим из прошлого снимка, не открывая браузер.

    Страница, пропавшая из карты сайта, сюда вообще не попадает ни в to_fetch,
    ни в carry_over — этого достаточно, чтобы diff_crawl отдельно классифицировал
    её как removed (см. app.crawler.diff).
    """
    candidates: list[str] = []
    carry_over: dict[str, str] = {}

    for entry in entries:
        previous = previous_pages.get(entry.url)
        if force_full or previous is None:
            candidates.append(entry.url)
            continue
        if entry.lastmod is None or previous.lastmod is None or entry.lastmod > previous.lastmod:
            candidates.append(entry.url)
        else:
            carry_over[entry.url] = previous.text

    to_fetch = sorted(candidates)[: max(0, max_fetch)]

    # Кандидаты сверх лимита за этот прогон не потеряны и не "удалены" — просто
    # переносим прежний текст, а по-настоящему изменившееся содержимое подхватит
    # один из следующих обходов (дата у них в карте сайта всё ещё новее сохранённой).
    # Для совсем новых адресов (previous_pages о них не знает) переносить нечего —
    # они появятся, когда до них дойдёт очередь.
    for url in candidates:
        if url not in to_fetch and url in previous_pages:
            carry_over[url] = previous_pages[url].text

    return CrawlPlan(
        to_fetch=to_fetch,
        carry_over=carry_over,
        live_urls={entry.url for entry in entries},
        urls_total=len(entries),
    )


async def discover_urls_by_crawling(
    context: BrowserContext,
    base_url: str,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[str]:
    """BFS-обход ссылок с главной страницы, если sitemap.xml недоступен."""
    base_netloc = urlparse(base_url).netloc
    seen: set[str] = {normalize_url(base_url)}
    queue: list[str] = [base_url]
    discovered: list[str] = []

    page = await context.new_page()
    try:
        while queue and len(discovered) < max_pages:
            url = queue.pop(0)
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception:  # noqa: BLE001 — сетевые сбои одной страницы не должны рушить весь обход
                logger.warning("Не удалось открыть %s при обходе ссылок", url)
                continue

            if response is None:
                continue

            html = await page.content()
            if is_blocked(response.status, html):
                raise CompetitorBlockedError(url, blocked_reason(response.status, html) or "неизвестно")

            discovered.append(url)

            hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
            for href in hrefs:
                normalized = normalize_url(href)
                if normalized not in seen and _same_domain(normalized, base_netloc):
                    seen.add(normalized)
                    queue.append(normalized)

            await asyncio.sleep(settings.crawl_request_delay_seconds)
    finally:
        await page.close()

    return discovered


async def fetch_page_text(context: BrowserContext, url: str) -> tuple[str, str]:
    """Открывает страницу и возвращает (заголовок, нормализованный текст). Бросает CompetitorBlockedError."""
    page = await context.new_page()
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            # SPA (React/Vue) дорисовывают контент после domcontentloaded — ждём
            # затихания сети, чтобы не забрать пустой каркас страницы.
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:  # noqa: BLE001 — страницы с фоновым polling/аналитикой никогда не затихают, это не ошибка
            pass
        html = await page.content()
        status = response.status if response else 0

        if is_blocked(status, html):
            raise CompetitorBlockedError(url, blocked_reason(status, html) or "неизвестно")

        title = await page.title()
        return title, extract_text(html)
    finally:
        await page.close()


async def _fetch_all(
    context: BrowserContext,
    urls: list[str],
    *,
    cache_lookup: CacheLookup | None,
    cache_store: CacheStore | None,
) -> dict[str, tuple[str, str] | None]:
    """Грузит страницы в crawl_page_concurrency вкладок разом. url -> (title, text),
    None — страницу не удалось загрузить (таймаут, обрыв сети).

    Раньше страницы шли строго по одной, и первый обход сайта на тысячи страниц
    занимал сутки. Каждая вкладка после своей загрузки выдерживает паузу
    crawl_request_delay_seconds — вежливость к сайту сохраняется на вкладку.

    CompetitorBlockedError в любой вкладке останавливает все остальные и уходит
    наверх — это сигнал прекратить весь обход, а не сбой одной страницы.
    """
    results: dict[str, tuple[str, str] | None] = {}
    queue: asyncio.Queue[str] = asyncio.Queue()
    for url in urls:
        queue.put_nowait(url)

    # Хуки черновика ходят в одну Session БД через asyncio.to_thread — из нескольких
    # вкладок разом это были бы параллельные потоки на одной сессии. Сами операции
    # быстрые, поэтому просто выстраиваем их в очередь.
    cache_lock = asyncio.Lock()

    async def _one(url: str) -> None:
        if cache_lookup:
            async with cache_lock:
                cached = await cache_lookup(url)
            if cached is not None:
                results[url] = cached
                return
        try:
            title, text = await fetch_page_text(context, url)
        except CompetitorBlockedError:
            raise
        except Exception:
            # Таймаут/обрыв на одной странице не должен рушить весь обход (см.
            # CLAUDE.md про инкрементальный обход и историю с og1.ru: одна
            # зависшая страница валила весь прогон целиком).
            logger.warning("Не удалось загрузить страницу %s — пропускаем в этом обходе", url, exc_info=True)
            results[url] = None
            return
        results[url] = (title, text)
        if cache_store:
            async with cache_lock:
                await cache_store(url, title, text)
        await asyncio.sleep(settings.crawl_request_delay_seconds)

    async def _worker() -> None:
        while True:
            try:
                url = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await _one(url)

    workers = [
        asyncio.create_task(_worker()) for _ in range(max(1, min(settings.crawl_page_concurrency, len(urls))))
    ]
    try:
        await asyncio.gather(*workers)
    except BaseException:
        # gather не отменяет соседей сам: без этого остальные вкладки продолжили бы
        # ходить по сайту, который нас уже заблокировал (или обход уже остановили).
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise
    return results


async def crawl_competitor(
    context: BrowserContext,
    base_url: str,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    previous_pages: dict[str, PreviousPageInfo] | None = None,
    force_full: bool = True,
    cache_lookup: CacheLookup | None = None,
    cache_store: CacheStore | None = None,
    on_urls_discovered: OnUrlsDiscovered | None = None,
) -> CrawlResult:
    """Обход одного конкурента: находим URL и рендерим только те страницы, которые
    реально нужно (см. plan_crawl) — остальные переносим из прошлого снимка.

    Останавливается сразу при первой блокировке (CompetitorBlockedError) —
    без ретраев, как того требует архитектура (см. CLAUDE.md).

    cache_lookup/cache_store дают устойчивость к обрыву обхода (упал сервер, легла
    база): если страница уже загружалась недавно, берём готовый текст вместо того,
    чтобы снова идти на сайт конкурента. Без них (по умолчанию) поведение прежнее.
    """
    previous_pages = previous_pages or {}
    entries = await discover_sitemap_entries(base_url)

    sitemap_lastmod_by_url: dict[str, datetime | None] = {}
    if entries:
        plan = plan_crawl(entries, previous_pages=previous_pages, force_full=force_full, max_fetch=max_pages)
        to_fetch = plan.to_fetch
        carry_over = plan.carry_over
        urls_total = plan.urls_total
        sitemap_lastmod_by_url = {entry.url: entry.lastmod for entry in entries}
    else:
        # Sitemap недоступна/пуста — обходим по ссылкам, как раньше. Дат обновления
        # тут никто не даёт, поэтому пропускать нечего: грузим всё в пределах лимита.
        to_fetch = await discover_urls_by_crawling(context, base_url, max_pages=max_pages)
        carry_over = {}
        # При обходе по ссылкам мы не знаем, сколько страниц у сайта всего: очередь
        # обрывается на лимите. Упёрлись в лимит — значит страниц точно больше.
        urls_total = len(to_fetch) + 1 if len(to_fetch) >= max_pages else len(to_fetch)

    if on_urls_discovered:
        await on_urls_discovered(len(to_fetch))

    result = CrawlResult(base_url=base_url, urls_total=urls_total)
    result.pages.update(carry_over)

    fetched = await _fetch_all(
        context, to_fetch, cache_lookup=cache_lookup, cache_store=cache_store
    )

    # Складываем в порядке to_fetch, а не в порядке, в каком вкладки закончили:
    # результат обхода не должен зависеть от того, какая страница загрузилась быстрее.
    for url in to_fetch:
        page = fetched[url]
        if page is None:
            result.failed_urls.append(url)
            previous = previous_pages.get(url)
            if previous is not None:
                result.pages[url] = previous.text  # считаем неизменной, перепроверим в следующий раз
            continue
        title, text = page
        result.pages[url] = text
        result.page_titles[url] = title

    # Дату у страниц, которые не удалось загрузить, не обновляем — с их прежней
    # (более старой) датой они снова станут кандидатами на загрузку в следующий раз.
    result.sitemap_lastmod = {
        url: sitemap_lastmod_by_url[url]
        for url in result.pages
        if url in sitemap_lastmod_by_url and url not in result.failed_urls
    }

    return result
