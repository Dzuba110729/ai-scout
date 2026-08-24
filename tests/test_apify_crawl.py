from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.crawler.apify_crawl import ApifyCrawlError, crawl_competitor_via_apify


def _dataset_items(items: list[dict]):
    async def _iterate():
        for item in items:
            yield item

    return _iterate()


def _mock_run(status: str, dataset_id: str = "ds1", run_id: str = "run1", message: str = ""):
    run = MagicMock()
    run.status = status
    run.status_message = message
    run.default_dataset_id = dataset_id
    run.id = run_id
    return run


@pytest.mark.asyncio
async def test_crawl_raises_without_token():
    with patch("app.crawler.apify_crawl.settings") as mock_settings:
        mock_settings.apify_api_token = ""
        with pytest.raises(ApifyCrawlError, match="APIFY_API_TOKEN"):
            await crawl_competitor_via_apify("https://x.ru")


@pytest.mark.asyncio
async def test_crawl_parses_successful_run_into_pages():
    items = [
        {"url": "https://x.ru/", "text": "Главная  \n\n\nстраница", "metadata": {"title": "Главная"}},
        {"url": "https://x.ru/promo", "text": "Акция -30%", "title": "Промо"},
    ]

    mock_dataset_client = MagicMock()
    mock_dataset_client.iterate_items = MagicMock(return_value=_dataset_items(items))

    mock_actor_client = MagicMock()
    mock_actor_client.call = AsyncMock(return_value=_mock_run("SUCCEEDED"))

    mock_client = MagicMock()
    mock_client.actor = MagicMock(return_value=mock_actor_client)
    mock_client.dataset = MagicMock(return_value=mock_dataset_client)

    with patch("app.crawler.apify_crawl.settings") as mock_settings, patch(
        "app.crawler.apify_crawl.ApifyClientAsync", return_value=mock_client
    ):
        mock_settings.apify_api_token = "test-token"
        mock_settings.apify_actor_id = "apify/website-content-crawler"
        mock_settings.apify_run_timeout_seconds = 600

        result = await crawl_competitor_via_apify("https://x.ru")

    assert result.pages["https://x.ru/"] == "Главная\nстраница"
    assert result.pages["https://x.ru/promo"] == "Акция -30%"
    assert result.page_titles["https://x.ru/"] == "Главная"
    assert result.page_titles["https://x.ru/promo"] == "Промо"


@pytest.mark.asyncio
async def test_crawl_raises_on_failed_run():
    mock_actor_client = MagicMock()
    mock_actor_client.call = AsyncMock(return_value=_mock_run("FAILED", message="сайт заблокировал обход"))

    mock_client = MagicMock()
    mock_client.actor = MagicMock(return_value=mock_actor_client)

    with patch("app.crawler.apify_crawl.settings") as mock_settings, patch(
        "app.crawler.apify_crawl.ApifyClientAsync", return_value=mock_client
    ):
        mock_settings.apify_api_token = "test-token"
        mock_settings.apify_actor_id = "apify/website-content-crawler"
        mock_settings.apify_run_timeout_seconds = 600

        with pytest.raises(ApifyCrawlError, match="FAILED"):
            await crawl_competitor_via_apify("https://x.ru")


@pytest.mark.asyncio
async def test_crawl_raises_when_zero_pages_returned():
    mock_dataset_client = MagicMock()
    mock_dataset_client.iterate_items = MagicMock(return_value=_dataset_items([]))

    mock_actor_client = MagicMock()
    mock_actor_client.call = AsyncMock(return_value=_mock_run("SUCCEEDED"))

    mock_client = MagicMock()
    mock_client.actor = MagicMock(return_value=mock_actor_client)
    mock_client.dataset = MagicMock(return_value=mock_dataset_client)

    with patch("app.crawler.apify_crawl.settings") as mock_settings, patch(
        "app.crawler.apify_crawl.ApifyClientAsync", return_value=mock_client
    ):
        mock_settings.apify_api_token = "test-token"
        mock_settings.apify_actor_id = "apify/website-content-crawler"
        mock_settings.apify_run_timeout_seconds = 600

        with pytest.raises(ApifyCrawlError, match="0 страниц|ни одной"):
            await crawl_competitor_via_apify("https://x.ru")


@pytest.mark.asyncio
async def test_crawl_raises_when_run_is_none():
    mock_actor_client = MagicMock()
    mock_actor_client.call = AsyncMock(return_value=None)

    mock_client = MagicMock()
    mock_client.actor = MagicMock(return_value=mock_actor_client)

    with patch("app.crawler.apify_crawl.settings") as mock_settings, patch(
        "app.crawler.apify_crawl.ApifyClientAsync", return_value=mock_client
    ):
        mock_settings.apify_api_token = "test-token"
        mock_settings.apify_actor_id = "apify/website-content-crawler"
        mock_settings.apify_run_timeout_seconds = 600

        with pytest.raises(ApifyCrawlError):
            await crawl_competitor_via_apify("https://x.ru")
