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

import json
import sys

from app.config import STORAGE_STATE_DIR
from app.crawler.browser import storage_state_path_for

_SAME_SITE_MAP = {
    "no_restriction": "None",
    "unspecified": "Lax",
    "lax": "Lax",
    "strict": "Strict",
}


def convert_cookie(cookie: dict) -> dict:
    same_site_raw = str(cookie.get("sameSite", "unspecified")).lower()
    expires = cookie.get("expirationDate")

    return {
        "name": cookie["name"],
        "value": cookie["value"],
        "domain": cookie["domain"],
        "path": cookie.get("path", "/"),
        "expires": float(expires) if expires is not None and not cookie.get("session") else -1,
        "httpOnly": bool(cookie.get("httpOnly", False)),
        "secure": bool(cookie.get("secure", False)),
        "sameSite": _SAME_SITE_MAP.get(same_site_raw, "Lax"),
    }


def main(competitor_id: int, export_path: str) -> None:
    with open(export_path, encoding="utf-8") as f:
        cookies_raw = json.load(f)

    if not isinstance(cookies_raw, list):
        raise ValueError("Ожидался JSON-массив кук (экспорт Cookie-Editor)")

    storage_state = {
        "cookies": [convert_cookie(c) for c in cookies_raw],
        "origins": [],
    }

    target_path = storage_state_path_for(competitor_id, STORAGE_STATE_DIR)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(storage_state, f, ensure_ascii=False, indent=2)

    print(f"Сохранено {len(storage_state['cookies'])} кук в {target_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Использование: python scripts/cookies_to_storage_state.py <competitor_id> <export.json>")
        sys.exit(1)

    main(int(sys.argv[1]), sys.argv[2])
