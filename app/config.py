from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_STATE_DIR = BASE_DIR / "storage_states"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str = "postgresql+psycopg://scout:scout@localhost:5432/ai_scout"

    basic_auth_username: str = "admin"
    basic_auth_password: str = "change-me"

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    claude_cli_path: str = "claude"
    claude_cli_timeout_seconds: int = 120

    apify_api_token: str = ""
    apify_actor_id: str = "apify/website-content-crawler"
    apify_run_timeout_seconds: int = 600

    # Способ 1 (личный Google-аккаунт, OAuth) — см. scripts/google_oauth_login.py.
    google_oauth_client_secret_path: str = ""
    google_oauth_token_path: str = str(BASE_DIR / "google_oauth_token.json")

    # Способ 2 (сервисный аккаунт) — если задан, имеет приоритет над OAuth выше.
    google_service_account_json_path: str = ""

    crawl_interval_days: int = 7
    crawl_request_delay_seconds: float = 2.0
    crawl_max_pages: int = 200

    log_level: str = "INFO"


settings = Settings()
STORAGE_STATE_DIR.mkdir(exist_ok=True)
