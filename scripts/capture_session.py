"""Ручной headed-прогон: пройти капчу/логин локально и сохранить storage_state конкурента.

Запускать локально (не в Docker) при добавлении конкурента или истечении сессии:

    python scripts/capture_session.py <competitor_id> <url>

После сохранения storage_state нужно скопировать файл из storage_states/
на сервер (или в volume storage_states контейнера), если обход идёт в Docker.
"""

import asyncio
import sys

from app.config import STORAGE_STATE_DIR
from app.crawler.browser import capture_manual_session, storage_state_path_for


async def main(competitor_id: int, url: str) -> None:
    path = storage_state_path_for(competitor_id, STORAGE_STATE_DIR)
    await capture_manual_session(url, path)
    print(f"Сессия сохранена: {path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Использование: python scripts/capture_session.py <competitor_id> <url>")
        sys.exit(1)

    asyncio.run(main(int(sys.argv[1]), sys.argv[2]))
