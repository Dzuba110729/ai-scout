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

    # Если Claude ответил, что лимит подписки исчерпан, столько минут не вызываем его
    # вовсе: иначе обход на сотни находок добивал лимит сотнями заведомо пустых
    # вызовов. Пропущенное потом доберёт дозаправка (app/ai/backfill.py).
    claude_cli_limit_pause_minutes: int = 60

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
    # Сколько ждать, пока JS-проверка «вы не робот» пройдёт сама (см. _goto_through_challenge).
    crawl_challenge_wait_seconds: float = 15.0
    crawl_max_pages: int = 200

    # Первый обход конкурента (и периодическая полная сверка, см. crawl_full_recheck_days)
    # тянет вообще все страницы карты сайта, а не только изменившиеся по дате — у него
    # свой, намного больший потолок, чем у обычного еженедельного обхода.
    # Должен покрывать карту сайта целиком: страницы, не попавшие в первый обход,
    # следующий обход иначе принял бы за «новые».
    crawl_full_crawl_max_pages: int = 10000

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
    # Наш сайт обходится ещё и каждый день (только новые и изменившиеся страницы),
    # чтобы сравнение находок конкурента всегда шло с актуальной версией нашего сайта,
    # когда бы ни запустили обход конкурента. Час — по часовому поясу ниже, а не Мака
    # (решение владельца 2026-09-29).
    own_site_daily_enabled: bool = True
    own_site_daily_hour: int = 5
    own_site_daily_timezone: str = "Europe/Moscow"
    # Наш сайт не открылся (нет интернета, сайт лёг, защита не пустила) — обход
    # повторяется через own_site_retry_minutes, всего до own_site_retry_attempts
    # повторов. В плановом цикле конкуренты ждут удачного обхода нашего сайта; если
    # все повторы не помогли — обходятся со старым снимком нашего сайта (2026-10-09).
    own_site_retry_minutes: int = 45
    own_site_retry_attempts: int = 3

    # Блок, поменявшийся сразу на стольких страницах, — одно «сквозное изменение»
    # (app/crawler/sitewide.py). 0 — не склеивать.
    sitewide_min_pages: int = 3

    # Сравнение находок конкурента с нашим сайтом («есть ли такое у нас»).
    # Каждое сравнение — отдельный вызов ИИ, поэтому за один обход их число
    # ограничено: без потолка обход с 200 новыми страницами дал бы 200 вызовов.
    own_site_compare_max_per_run: int = 10
    own_site_compare_candidates: int = 3
    own_site_compare_text_limit: int = 3000
    # Первый обход конкурента: изменений ещё нет, но сравнить его ключевые страницы
    # (курсы, цены, разделы — не статьи) с нашим сайтом уже можно. Потолок — чтобы не
    # упереться в лимиты подписки Claude (решение владельца 2026-09-25: до 50).
    own_site_baseline_compare_max: int = 50

    # SEO-разбор инфостатей (обратная связь SEO-специалиста 2026-10-19, этапы 3–5).
    # Карточка интента статьи (app/ai/intent.py) — один вызов `claude -p` на пачку из
    # seo_intent_batch_size статей; за обход — не больше seo_intent_max_per_run статей
    # (новые и изменённые инфостатьи конкурента и нашего сайта). Остальное доберёт
    # стартовый аудит (scripts/seo_audit.py).
    seo_enabled: bool = True
    seo_intent_max_per_run: int = 30
    seo_intent_batch_size: int = 4
    seo_intent_text_limit: int = 2500
    # Сравнение со своими статьями (app/ai/topic_match.py): кандидаты без ИИ (TF-IDF),
    # потом один вызов на пачку из seo_compare_batch_size статей конкурента.
    seo_compare_max_per_run: int = 30
    seo_compare_batch_size: int = 3
    seo_compare_candidates: int = 3
    seo_compare_text_limit: int = 1200
    # Насколько похожа новая тема на уже разобранную (0..1), чтобы склеить их в одну
    # запись без вызова ИИ: одна тема у нескольких конкурентов = одна строка таблицы.
    seo_topic_merge_threshold: float = 0.55
    # Сколько ждать ответа CLI на пачку статей — дольше, чем на одну находку.
    seo_cli_timeout_seconds: int = 300

    # Еженедельный дайджест в Telegram (app/digest.py). День — mon..sun, час — по
    # digest_timezone, а не по поясу Мака (Мак стоит на тихоокеанском времени, и
    # «10 утра» превращалось в 20:00 по Москве). digest_days — за сколько дней
    # собирать находки.
    digest_enabled: bool = True
    digest_day_of_week: str = "mon"
    digest_hour: int = 10
    digest_timezone: str = "Europe/Moscow"
    digest_days: int = 7

    log_level: str = "INFO"

    def telegram_allowed_chat_ids(self) -> set[str]:
        ids = {cid.strip() for cid in self.telegram_bot_allowed_chat_ids.split(",") if cid.strip()}
        if self.telegram_chat_id:
            ids.add(str(self.telegram_chat_id).strip())
        return ids


settings = Settings()
STORAGE_STATE_DIR.mkdir(exist_ok=True)
