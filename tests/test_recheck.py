"""Перепроверка страниц, пропавших из обхода: удалена / переехала / жива."""

import asyncio

import httpx
import pytest

from app.crawler import recheck
from app.crawler.recheck import (
    DisappearReason,
    check_missing_page,
    check_missing_pages,
    classify_response,
    same_page,
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_classify_404_as_deleted():
    check = classify_response("https://x.ru/old", 404, "https://x.ru/old", "<html>404</html>")

    assert check.reason is DisappearReason.DELETED
    assert check.is_really_gone is True


def test_classify_410_as_deleted():
    check = classify_response("https://x.ru/old", 410, "https://x.ru/old", "")

    assert check.reason is DisappearReason.DELETED


def test_classify_redirect_as_moved_and_keeps_target_text():
    check = classify_response(
        "https://x.ru/old-course",
        200,
        "https://x.ru/new-course",
        "<html><body><h1>Новый курс</h1><p>Скидка 30%</p></body></html>",
    )

    assert check.reason is DisappearReason.MOVED
    assert check.final_url == "https://x.ru/new-course"
    assert "Новый курс" in check.final_text
    assert check.is_really_gone is True


def test_classify_200_same_url_as_still_alive():
    check = classify_response("https://x.ru/page", 200, "https://x.ru/page", "<html>жива</html>")

    assert check.reason is DisappearReason.STILL_ALIVE
    assert check.is_really_gone is False


def test_classify_403_as_unknown_not_deleted():
    # Сайт огрызнулся — судить об удалении по такому ответу нельзя,
    # иначе получим ложную тревогу в отчёте.
    check = classify_response("https://x.ru/page", 403, "https://x.ru/page", "")

    assert check.reason is DisappearReason.UNKNOWN
    assert check.is_really_gone is False


def test_http_to_https_is_not_a_move():
    assert same_page("http://x.ru/page", "https://x.ru/page") is True


def test_trailing_slash_and_www_are_not_a_move():
    assert same_page("https://x.ru/page/", "https://www.x.ru/page") is True


def test_different_path_is_a_move():
    assert same_page("https://x.ru/a", "https://x.ru/b") is False


def test_query_string_difference_is_a_move():
    assert same_page("https://x.ru/a?v=1", "https://x.ru/a?v=2") is False


@pytest.mark.asyncio
async def test_check_missing_page_follows_redirect_chain():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "https://x.ru/new"})
        return httpx.Response(200, html="<html><body>Теперь тут вебинары</body></html>")

    async with _client(handler) as client:
        check = await check_missing_page(client, "https://x.ru/old")

    assert check.reason is DisappearReason.MOVED
    assert check.final_url == "https://x.ru/new"
    assert "вебинары" in check.final_text


@pytest.mark.asyncio
async def test_check_missing_page_network_error_is_unknown():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("нет связи")

    async with _client(handler) as client:
        check = await check_missing_page(client, "https://x.ru/old")

    assert check.reason is DisappearReason.UNKNOWN


@pytest.mark.asyncio
async def test_check_missing_pages_classifies_all_three_outcomes(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/deleted":
            return httpx.Response(404)
        if path == "/moved":
            return httpx.Response(302, headers={"Location": "https://x.ru/target"})
        return httpx.Response(200, html="<html><body>жива</body></html>")

    _patch_client(monkeypatch, handler)

    checks = await check_missing_pages(
        ["https://x.ru/deleted", "https://x.ru/moved", "https://x.ru/alive"]
    )

    assert checks["https://x.ru/deleted"].reason is DisappearReason.DELETED
    assert checks["https://x.ru/moved"].reason is DisappearReason.MOVED
    assert checks["https://x.ru/moved"].final_url == "https://x.ru/target"
    assert checks["https://x.ru/alive"].reason is DisappearReason.STILL_ALIVE


@pytest.mark.asyncio
async def test_check_missing_pages_respects_limit(monkeypatch):
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(404)

    _patch_client(monkeypatch, handler)

    urls = [f"https://x.ru/p{i}" for i in range(10)]
    checks = await check_missing_pages(urls, max_pages=3)

    assert len(requested) == 3
    assert len(checks) == 10
    # Непроверенные остаются без вердикта — значит удалёнными не считаются
    assert checks["https://x.ru/p9"].reason is DisappearReason.UNKNOWN
    assert checks["https://x.ru/p0"].reason is DisappearReason.DELETED


@pytest.mark.asyncio
async def test_check_missing_pages_limits_parallel_requests(monkeypatch):
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)  # даём остальным запросам шанс стартовать
        in_flight -= 1
        return httpx.Response(404)

    _patch_client(monkeypatch, handler)

    urls = [f"https://x.ru/p{i}" for i in range(20)]
    await check_missing_pages(urls, concurrency=2)

    assert peak == 2


@pytest.mark.asyncio
async def test_check_missing_pages_on_empty_list_does_nothing(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover — не должен вызваться
        raise AssertionError("сети быть не должно")

    _patch_client(monkeypatch, handler)

    assert await check_missing_pages([]) == {}


def _patch_client(monkeypatch, handler) -> None:
    """Подсовывает check_missing_pages клиент с фиктивным транспортом — тесты без сети."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient  # запоминаем до подмены, иначе получится рекурсия
    monkeypatch.setattr(
        recheck.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(**{**kwargs, "transport": transport}),
    )
