"""Запуск Playwright-браузера в стелс-режиме и работа с storage_state конкурента.

storage_state (куки + localStorage) проходится вручную один раз в headed-режиме
при добавлении конкурента или истечении сессии (см. CLAUDE.md), а затем
переиспользуется фоновым краулером до тех пор, пока сайт его принимает.

Используется patchright, а не обычный playwright: patchright исполняет служебные
CDP-команды в изолированном контексте, а не в основном JS-контексте страницы,
поэтому антибот-скрипты (Qrator и подобные) не видят тот же CDP-фингерпринт
автоматизации, на котором раньше палился обычный Playwright (см. CLAUDE.md).
API идентичен playwright.async_api — это drop-in замена.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from patchright.async_api import Browser, BrowserContext, Playwright, async_playwright

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


# Что не нужно, чтобы достать текст страницы. Картинки/шрифты/видео — просто лишний
# трафик. Счётчики и виджеты хуже: они держат сеть «шумной», и ожидание networkidle
# в fetch_page_text упиралось в свой таймаут почти на каждой странице (замер на
# og1.ru: ~16 с загрузки на страницу). Сайт конкурента и его собственные скрипты
# (в том числе антибот-проверки) не трогаем — режем только чужие домены из списка.
_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
_BLOCKED_URL_PARTS = (
    "mc.yandex.",
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "googleadservices.com",
    "top-fwz1.mail.ru",
    "vk.com/rtrg",
    "connect.facebook.net",
    "facebook.com/tr",
    "jivosite.com",
    "jivo.ru",
    "carrotquest.",
    "roistat.com",
    "calltouch.ru",
    "callibri.ru",
    "comagic.ru",
    "uiscom.ru",
    "hotjar.com",
    "clarity.ms",
)


def is_blocked_request(resource_type: str, url: str) -> bool:
    if resource_type in _BLOCKED_RESOURCE_TYPES:
        return True
    return any(part in url for part in _BLOCKED_URL_PARTS)


async def _block_heavy_requests(route) -> None:
    request = route.request
    if is_blocked_request(request.resource_type, request.url):
        await route.abort()
    else:
        await route.continue_()


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
    block_heavy_resources: bool = False,
):
    """Контекст браузера с реалистичным UA/viewport и (опционально) сохранённой сессией.

    block_heavy_resources — для фонового обхода: не грузить картинки, шрифты,
    счётчики и виджеты (см. _BLOCKED_URL_PARTS). В ручном headed-прогоне не нужен:
    там человек смотрит на страницу.
    """
    storage_state = str(storage_state_path) if storage_state_path and storage_state_path.exists() else None

    context: BrowserContext = await browser.new_context(
        user_agent=USER_AGENT,
        viewport=VIEWPORT,
        locale=LOCALE,
        timezone_id=TIMEZONE_ID,
        storage_state=storage_state,
    )
    await context.add_init_script(STEALTH_INIT_SCRIPT)
    if block_heavy_resources:
        await context.route("**/*", _block_heavy_requests)
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
