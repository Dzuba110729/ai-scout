from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.crawler.browser import new_stealth_context, save_storage_state, storage_state_path_for


def test_storage_state_path_is_per_competitor(tmp_path: Path):
    path_a = storage_state_path_for(1, tmp_path)
    path_b = storage_state_path_for(2, tmp_path)

    assert path_a != path_b
    assert path_a.name == "competitor_1.json"
    assert path_a.parent == tmp_path


@pytest.mark.asyncio
async def test_save_storage_state_writes_to_given_path(tmp_path: Path):
    context = MagicMock()
    context.storage_state = AsyncMock()
    target = tmp_path / "nested" / "competitor_1.json"

    await save_storage_state(context, target)

    context.storage_state.assert_awaited_once_with(path=str(target))
    assert target.parent.exists()  # директория должна быть создана


@pytest.mark.asyncio
async def test_new_context_passes_none_when_storage_state_missing(tmp_path: Path):
    browser = MagicMock()
    context = MagicMock()
    context.add_init_script = AsyncMock()
    context.close = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)

    missing_path = tmp_path / "competitor_1.json"

    async with new_stealth_context(browser, storage_state_path=missing_path):
        pass

    _, kwargs = browser.new_context.call_args
    assert kwargs["storage_state"] is None


@pytest.mark.asyncio
async def test_new_context_reuses_existing_storage_state(tmp_path: Path):
    browser = MagicMock()
    context = MagicMock()
    context.add_init_script = AsyncMock()
    context.close = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)

    existing_path = tmp_path / "competitor_1.json"
    existing_path.write_text("{}")

    async with new_stealth_context(browser, storage_state_path=existing_path):
        pass

    _, kwargs = browser.new_context.call_args
    assert kwargs["storage_state"] == str(existing_path)
