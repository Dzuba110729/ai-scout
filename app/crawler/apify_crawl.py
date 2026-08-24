"""Обход конкурента через Apify Cloud вместо локального Playwright.

Решение принято 2026-08-10 (см. CLAUDE.md): собственный Playwright-браузер —
даже headed, даже через установленный Chrome — детектится антибот-защитой
Qrator на уровне CDP-протокола автоматизации. Apify Cloud использует свою
инфраструктуру (антидетект-браузеры, прокси) и не подвержена этой проблеме.
"""

import logging
import re
from datetime import timedelta

from apify_client import ApifyClientAsync

from app.config import settings
from app.crawler.crawl import CrawlResult

logger = logging.getLogger(__name__)

_FAILED_STATUSES = {"FAILED", "ABORTED", "TIMED-OUT", "TIMED_OUT"}


class ApifyCrawlError(Exception):
    """Прогон актора Apify завершился неудачно (или не настроен токен)."""


def _normalize_text(text: str) -> str:
    lines = (line.strip() for line in text.splitlines())
    normalized = "\n".join(line for line in lines if line)
    return re.sub(r"\n{3,}", "\n\n", normalized)


def _extract_url_and_text(item: dict) -> tuple[str | None, str | None]:
    url = item.get("url")
    text = item.get("text") or item.get("markdown")
    return url, text


def _extract_title(item: dict) -> str:
    metadata = item.get("metadata") or {}
    return metadata.get("title") or item.get("title") or ""


async def crawl_competitor_via_apify(base_url: str, *, max_pages: int = 200) -> CrawlResult:
    if not settings.apify_api_token:
        raise ApifyCrawlError("APIFY_API_TOKEN не задан в .env")

    client = ApifyClientAsync(settings.apify_api_token)
    actor_client = client.actor(settings.apify_actor_id)

    run_input = {
        "startUrls": [{"url": base_url}],
        "maxCrawlPages": max_pages,
        "crawlerType": "playwright:adaptive",
        "proxyConfiguration": {"useApifyProxy": True},
    }

    run = await actor_client.call(
        run_input=run_input, run_timeout=timedelta(seconds=settings.apify_run_timeout_seconds)
    )

    if run is None:
        raise ApifyCrawlError(f"Не удалось запустить актор {settings.apify_actor_id}")

    status = run.status
    if status != "SUCCEEDED":
        raise ApifyCrawlError(f"Прогон Apify завершился со статусом {status}: {run.status_message}")

    dataset_client = client.dataset(run.default_dataset_id)

    result = CrawlResult(base_url=base_url)
    async for item in dataset_client.iterate_items():
        url, text = _extract_url_and_text(item)
        if not url or not text:
            continue
        result.pages[url] = _normalize_text(text)
        result.page_titles[url] = _extract_title(item)

    if not result.pages:
        logger.warning("Apify вернул 0 страниц для %s (run %s)", base_url, run.id)
        raise ApifyCrawlError(
            "Apify отработал без ошибки, но не смог получить ни одной страницы "
            "(вероятно, сайт заблокировал даже Apify-прокси)"
        )

    return result
