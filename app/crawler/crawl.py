"""Обход одного конкурента: находим страницы, рендерим их и отдаём url -> текст.

Классификация new/changed/removed делается отдельно в diff.py — этот модуль
только собирает "снимок" текущего состояния сайта конкурента.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse
from xml.etree import ElementTree

import httpx
from playwright.async_api import BrowserContext

from app.config import settings
from app.crawler.blocking import blocked_reason, is_blocked
from app.crawler.diff import extract_text

logger = logging.getLogger(__name__)

DEFAULT_MAX_PAGES = 200
_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


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


def _same_domain(url: str, base_netloc: str) -> bool:
    return urlparse(url).netloc == base_netloc


def _normalize_url(url: str) -> str:
    url, _fragment = urldefrag(url)
    return url.rstrip("/") or url


async def discover_urls_from_sitemap(base_url: str, *, max_pages: int = DEFAULT_MAX_PAGES) -> list[str]:
    """Пытается получить список URL из sitemap.xml (быстрее и надёжнее, чем обход ссылок)."""
    sitemap_url = urljoin(base_url, "/sitemap.xml")
    urls: list[str] = []

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
            for loc in sitemap_locs[:5]:  # не уходим в десятки под-sitemap'ов
                if len(urls) >= max_pages:
                    break
                try:
                    sub_response = await client.get(loc)
                    sub_root = ElementTree.fromstring(sub_response.content)
                except (httpx.HTTPError, ElementTree.ParseError):
                    continue
                urls.extend(
                    loc.text
                    for loc in sub_root.findall(".//sm:url/sm:loc", _SITEMAP_NS)
                    if loc.text
                )
        else:
            urls = [loc.text for loc in root.findall(".//sm:url/sm:loc", _SITEMAP_NS) if loc.text]

    base_netloc = urlparse(base_url).netloc
    deduped = {_normalize_url(u) for u in urls if _same_domain(u, base_netloc)}
    return list(deduped)[:max_pages]


async def discover_urls_by_crawling(
    context: BrowserContext,
    base_url: str,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[str]:
    """BFS-обход ссылок с главной страницы, если sitemap.xml недоступен."""
    base_netloc = urlparse(base_url).netloc
    seen: set[str] = {_normalize_url(base_url)}
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
                normalized = _normalize_url(href)
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


async def crawl_competitor(
    context: BrowserContext,
    base_url: str,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> CrawlResult:
    """Полный обход одного конкурента: находим URL и рендерим каждую страницу.

    Останавливается сразу при первой блокировке (CompetitorBlockedError) —
    без ретраев, как того требует архитектура (см. CLAUDE.md).
    """
    urls = await discover_urls_from_sitemap(base_url, max_pages=max_pages)
    if not urls:
        urls = await discover_urls_by_crawling(context, base_url, max_pages=max_pages)

    result = CrawlResult(base_url=base_url)
    for url in urls:
        title, text = await fetch_page_text(context, url)
        result.pages[url] = text
        result.page_titles[url] = title
        await asyncio.sleep(settings.crawl_request_delay_seconds)

    return result
