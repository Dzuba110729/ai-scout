"""Конвертирует экспорт кук из расширения Cookie-Editor в storage_state для Playwright.

Использование:
    python scripts/cookies_to_storage_state.py <competitor_id> <путь_к_export.json>

Как получить export.json:
    1. Установите расширение "Cookie-Editor" в Chrome (Chrome Web Store).
    2. Откройте сайт конкурента в обычном (не автоматизированном) Chrome —
       убедитесь, что он загружается нормально, без блокировки.
    3. Нажмите иконку Cookie-Editor → Export → Export as JSON.
    4. Сохраните содержимое буфера обмена в файл и передайте путь сюда.

После конвертации craulер будет переиспользовать эту сессию (см. app/crawler/browser.py,
резервный локальный путь для конкурентов, которых не проходит даже Apify Cloud).
"""

import sys
from pathlib import Path

from app.config import STORAGE_STATE_DIR
from app.crawler.browser import storage_state_path_for
from app.crawler.cookies import (  # noqa: F401 — convert_cookie нужен тестам
    convert_cookie,
    parse_cookie_export,
    save_storage_state,
)


def main(competitor_id: int, export_path: str) -> None:
    cookies = parse_cookie_export(Path(export_path).read_bytes())
    target_path = storage_state_path_for(competitor_id, STORAGE_STATE_DIR)
    saved = save_storage_state(cookies, target_path)
    print(f"Сохранено {saved} кук в {target_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Использование: python scripts/cookies_to_storage_state.py <competitor_id> <export.json>")
        sys.exit(1)

    main(int(sys.argv[1]), sys.argv[2])
