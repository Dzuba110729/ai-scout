"""Одноразовая авторизация личного Google-аккаунта для отчётов об обходах.

Запуск (локально, один раз): python scripts/google_oauth_login.py

Откроется браузер — войдите под аккаунтом, куда должны попадать документы
и папки, дайте согласие на доступ к Drive/Docs. После этого access-токен
будет обновляться автоматически по refresh-токену, без повторного входа.

Перед запуском:
1. В Google Cloud Console создайте OAuth client ID (тип "Desktop app").
2. На экране согласия (OAuth consent screen) добавьте свой аккаунт в Test users,
   если приложение в статусе Testing — иначе Google откажет во входе.
3. Скачайте JSON клиента и укажите путь к нему в .env как
   GOOGLE_OAUTH_CLIENT_SECRET_PATH.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow

from app.config import settings
from app.integrations.google_docs import _SCOPES


def main() -> None:
    if not settings.google_oauth_client_secret_path:
        raise SystemExit("Заполните GOOGLE_OAUTH_CLIENT_SECRET_PATH в .env перед запуском")

    flow = InstalledAppFlow.from_client_secrets_file(settings.google_oauth_client_secret_path, _SCOPES)
    credentials = flow.run_local_server(port=0)

    token_path = Path(settings.google_oauth_token_path)
    token_path.write_text(credentials.to_json())
    print(f"Готово — токен сохранён в {token_path}")


if __name__ == "__main__":
    main()
