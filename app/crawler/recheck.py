"""Перепроверка страниц, пропавших из результатов обхода.

Пропажа URL из обхода сама по себе НЕ означает удаление. Список страниц
обрезается по CRAWL_MAX_PAGES, страницу могли убрать из sitemap или перенести
перенаправлением — раньше все такие страницы слепо помечались удалёнными, и у
сайта на 500 страниц каждый прогон давал сотни ложных "удалений".

Поэтому перед вердиктом ходим по адресу страницы обычным HTTP-запросом (без
браузера — это дёшево) и разбираем три исхода: удалена, переехала, жива.
"""

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.crawler.diff import extract_text

logger = logging.getLogger(__name__)

_GONE_STATUSES = {404, 410}
_MAX_TARGET_TEXT = 4000

# Перепроверка идёт без браузера, поэтому представляемся обычным браузером —
# иначе часть сайтов отдаёт 403 на голый httpx и мы получим "не знаю" вместо ответа.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}


class DisappearReason(str, Enum):
    DELETED = "deleted"  # 404/410 — страницы больше нет
    MOVED = "moved"  # перенаправление на другой адрес
    STILL_ALIVE = "still_alive"  # 200 по тому же адресу — просто не попала в обход
    UNKNOWN = "unknown"  # проверить не удалось, судить об удалении нельзя


@dataclass(frozen=True)
class MissingPageCheck:
    url: str
    reason: DisappearReason
    status_code: int | None = None
    final_url: str | None = None
    final_text: str | None = None

    @property
    def is_really_gone(self) -> bool:
        """Только эти два исхода — повод показать владельцу изменение по странице."""
        return self.reason in (DisappearReason.DELETED, DisappearReason.MOVED)


def _page_identity(url: str) -> tuple[str, str, str]:
    parsed = urlparse(url)
    netloc = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    return netloc, path, parsed.query


def same_page(left: str, right: str) -> bool:
    """Переход http -> https, добавленный www или слэш на конце — не переезд,
    а тот же самый адрес. Иначе половина сайта числилась бы "переехавшей"."""
    return _page_identity(left) == _page_identity(right)


def classify_response(url: str, status_code: int, final_url: str, body: str) -> MissingPageCheck:
    if status_code in _GONE_STATUSES:
        return MissingPageCheck(
            url=url, reason=DisappearReason.DELETED, status_code=status_code, final_url=final_url
        )

    if status_code >= 400:
        # 403, 500 и прочее: сайт не отвечает по существу — считать страницу
        # удалённой на таком ответе нельзя, это была бы ложная тревога.
        return MissingPageCheck(url=url, reason=DisappearReason.UNKNOWN, status_code=status_code)

    if not same_page(url, final_url):
        return MissingPageCheck(
            url=url,
            reason=DisappearReason.MOVED,
            status_code=status_code,
            final_url=final_url,
            final_text=extract_text(body)[:_MAX_TARGET_TEXT],
        )

    return MissingPageCheck(
        url=url, reason=DisappearReason.STILL_ALIVE, status_code=status_code, final_url=final_url
    )


async def check_missing_page(client: httpx.AsyncClient, url: str) -> MissingPageCheck:
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        logger.warning("Не удалось перепроверить пропавшую страницу %s: %s", url, exc)
        return MissingPageCheck(url=url, reason=DisappearReason.UNKNOWN)

    return classify_response(url, response.status_code, str(response.url), response.text)


async def check_missing_pages(
    urls: list[str],
    *,
    max_pages: int | None = None,
    concurrency: int | None = None,
    timeout: float | None = None,
) -> dict[str, MissingPageCheck]:
    """Перепроверяет пропавшие страницы. Возвращает вердикт по каждому переданному URL.

    Проверяем не больше max_pages штук за прогон: у большого сайта пропавших может
    быть несколько сотен, и упереться в них на час никто не хочет. Непроверенные
    получают вердикт UNKNOWN — то есть остаются живыми до следующего раза.
    """
    max_pages = settings.crawl_recheck_max_pages if max_pages is None else max_pages
    concurrency = settings.crawl_recheck_concurrency if concurrency is None else concurrency
    timeout = settings.crawl_recheck_timeout_seconds if timeout is None else timeout

    to_check = urls[:max_pages]
    postponed = urls[max_pages:]

    results = {url: MissingPageCheck(url=url, reason=DisappearReason.UNKNOWN) for url in postponed}
    if postponed:
        logger.info(
            "Перепроверено %s пропавших страниц из %s, остальные отложены до следующего обхода",
            len(to_check),
            len(urls),
        )

    if not to_check:
        return results

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=_HEADERS) as client:

        async def _one(url: str) -> MissingPageCheck:
            async with semaphore:
                return await check_missing_page(client, url)

        checks = await asyncio.gather(*(_one(url) for url in to_check))

    for check in checks:
        results[check.url] = check
    return results
