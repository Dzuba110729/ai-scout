"""Запуск Playwright-браузера в стелс-режиме и работа с storage_state конкурента.

storage_state (куки + localStorage) проходится вручную один раз в headed-режиме
при добавлении конкурента или истечении сессии (см. CLAUDE.md), а затем
переиспользуется фоновым краулером до тех пор, пока сайт его принимает.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
VIEWPORT = {"width": 1366, "height": 768}
LOCALE = "ru-RU"
TIMEZONE_ID = "Europe/Moscow"

# Маскирует типичные признаки headless/автоматизации, которые проверяют антибот-скрипты.
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
);
"""


def storage_state_path_for(competitor_id: int, storage_state_dir: Path) -> Path:
    return storage_state_dir / f"competitor_{competitor_id}.json"


@asynccontextmanager
async def launch_browser(playwright: Playwright, *, headless: bool = True, channel: str | None = None):
    """channel="chrome" запускает установленный на машине настоящий Google Chrome
    вместо бандлового Chrome for Testing — некоторые антибот-системы (например,
    Qrator) распознают Chrome for Testing по CDP-фингерпринту даже в headed-режиме.
    """
    browser: Browser = await playwright.chromium.launch(
        headless=headless,
        channel=channel,
        args=["--disable-blink-features=AutomationControlled"],
    )
    try:
        yield browser
    finally:
        await browser.close()


@asynccontextmanager
async def new_stealth_context(
    browser: Browser,
    *,
    storage_state_path: Path | None = None,
):
    """Контекст браузера с реалистичным UA/viewport и (опционально) сохранённой сессией."""
    storage_state = str(storage_state_path) if storage_state_path and storage_state_path.exists() else None

    context: BrowserContext = await browser.new_context(
        user_agent=USER_AGENT,
        viewport=VIEWPORT,
        locale=LOCALE,
        timezone_id=TIMEZONE_ID,
        storage_state=storage_state,
    )
    await context.add_init_script(STEALTH_INIT_SCRIPT)
    try:
        yield context
    finally:
        await context.close()


async def save_storage_state(context: BrowserContext, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=str(path))


async def capture_manual_session(url: str, storage_state_path: Path) -> None:
    """Headed-прогон для ручного решения капчи/логина.

    Открывает браузер с UI, ждёт, пока пользователь сам пройдёт защиту/логин
    и нажмёт Enter в терминале, затем сохраняет storage_state на диск.
    """
    async with async_playwright() as playwright:
        async with launch_browser(playwright, headless=False, channel="chrome") as browser:
            async with new_stealth_context(browser) as context:
                page = await context.new_page()
                await page.goto(url)
                input(
                    "Пройдите капчу/логин в открывшемся окне браузера, "
                    "затем нажмите Enter здесь, чтобы сохранить сессию..."
                )
                await save_storage_state(context, storage_state_path)
