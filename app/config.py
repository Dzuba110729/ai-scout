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

    # Кому разрешено пользоваться интерактивным меню бота (список конкурентов,
    # запуск обхода и т.д.) — через запятую, в дополнение к telegram_chat_id.
    # Пусто — доступ только у telegram_chat_id.
    telegram_bot_allowed_chat_ids: str = ""

    claude_cli_path: str = "claude"
    claude_cli_timeout_seconds: int = 120

    # Сколько вызовов `claude -p` можно держать в работе одновременно за один
    # обход. Раньше находки анализировались строго по одной — на десятках находок
    # за прогон это заметно удлиняло обход. Без потолка длинный список находок
    # запустил бы вызовы все разом.
    claude_cli_concurrency: int = 3

    # В конце каждого обхода дозаправляем ИИ-разбор для находок, оставшихся без
    # него с прошлых прогонов (см. app/ai/backfill.py). Потолок за один обход:
    # если CLI лежит совсем, не нужно долбить его сотнями попыток подряд.
    # 0 — автодозаправка выключена.
    ai_backfill_max_per_run: int = 50

    apify_api_token: str = ""
    apify_actor_id: str = "apify/website-content-crawler"
    apify_run_timeout_seconds: int = 600

    # Без initialConcurrency актор по умолчанию "разгоняется" с 1 запроса в несколько
    # минут до максимума — на сайте в пару сотен страниц это и давало 15+ минут обхода.
    # Сразу выставляем полную параллельность.
    apify_max_concurrency: int = 30

    # Способ 1 (личный Google-аккаунт, OAuth) — см. scripts/google_oauth_login.py.
    google_oauth_client_secret_path: str = ""
    google_oauth_token_path: str = str(BASE_DIR / "google_oauth_token.json")

    # Способ 2 (сервисный аккаунт) — если задан, имеет приоритет над OAuth выше.
    google_service_account_json_path: str = ""

    crawl_interval_days: int = 7
    crawl_request_delay_seconds: float = 2.0
    crawl_max_pages: int = 200

    # Первый обход конкурента (и периодическая полная сверка, см. crawl_full_recheck_days)
    # тянет вообще все страницы карты сайта, а не только изменившиеся по дате — у него
    # свой, намного больший потолок, чем у обычного еженедельного обхода.
    crawl_full_crawl_max_pages: int = 3000

    # Раз в столько дней делаем полный обход заново, даже если по датам в карте сайта
    # ничего не поменялось — не все сайты честно обновляют дату, это подстраховка.
    crawl_full_recheck_days: int = 30

    # Сколько страниц ОДНОГО сайта грузим одновременно (вкладки одного браузера).
    # Раньше строго по одной — первый обход сайта на 3000 страниц шёл больше суток.
    # Пауза crawl_request_delay_seconds соблюдается в каждой вкладке, так что сайт
    # видит примерно crawl_page_concurrency запросов за паузу. Если конкурент
    # начинает блокировать — уменьшить до 1–2.
    crawl_page_concurrency: int = 4

    # Не грузить при обходе картинки, шрифты, видео, счётчики и виджеты чатов —
    # на текст страницы не влияют, а загрузку тормозят в разы (app/crawler/browser.py).
    crawl_block_heavy_resources: bool = True

    # Сколько конкурентов обходим одновременно. Каждый обход поднимает свой
    # браузер, поэтому без потолка десять конкурентов съедят всю память сервера.
    crawl_concurrency: int = 3

    # Перепроверка страниц, пропавших из результатов обхода (удалена / переехала /
    # просто не попала в этот обход). Обычные HTTP-запросы, без браузера.
    crawl_recheck_max_pages: int = 100
    crawl_recheck_concurrency: int = 5
    crawl_recheck_timeout_seconds: float = 15.0

    # Если обход оборвался (упал сервер, легла база) и страница уже была загружена
    # не раньше этого срока назад — при повторном запуске берём её из черновика,
    # а не грузим с сайта конкурента заново. Слишком большой срок рискует показать
    # устаревшие данные при ручном перезапуске вскоре после обычного обхода.
    crawl_resume_max_age_hours: float = 12.0

    # Наш сайт — фиксированный: с ним сравниваются находки у конкурентов, и его же
    # обходят по кнопке «🔄 Обойти наш сайт», чтобы увидеть, что на нём поменялось.
    # Если записи о нашем сайте в БД нет — она создаётся при старте сервиса.
    own_site_url: str = "https://og1.ru"

    # Сравнение находок конкурента с нашим сайтом («есть ли такое у нас»).
    # Каждое сравнение — отдельный вызов ИИ, поэтому за один обход их число
    # ограничено: без потолка обход с 200 новыми страницами дал бы 200 вызовов.
    own_site_compare_max_per_run: int = 10
    own_site_compare_candidates: int = 3
    own_site_compare_text_limit: int = 3000

    # Еженедельный дайджест в Telegram (app/digest.py). День — mon..sun, час — по
    # часовому поясу Мака, на котором крутится сервис. digest_days — за сколько дней
    # собирать находки.
    digest_enabled: bool = True
    digest_day_of_week: str = "mon"
    digest_hour: int = 10
    digest_days: int = 7

    log_level: str = "INFO"

    def telegram_allowed_chat_ids(self) -> set[str]:
        ids = {cid.strip() for cid in self.telegram_bot_allowed_chat_ids.split(",") if cid.strip()}
        if self.telegram_chat_id:
            ids.add(str(self.telegram_chat_id).strip())
        return ids


settings = Settings()
STORAGE_STATE_DIR.mkdir(exist_ok=True)
