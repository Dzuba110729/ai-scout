# AI-Скаут

Внутренний монитор конкурентов. Архитектура и ограничения — см. [CLAUDE.md](CLAUDE.md).

## Локальный запуск (без Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium

cp .env.example .env
# отредактируйте .env: BASIC_AUTH_*, TELEGRAM_*, APIFY_API_TOKEN, DATABASE_URL

# нужен PostgreSQL, например через docker:
docker run -d --name ai-scout-db -e POSTGRES_USER=scout -e POSTGRES_PASSWORD=scout \
  -e POSTGRES_DB=ai_scout -p 5432:5432 postgres:16-alpine

alembic upgrade head
uvicorn app.main:app --reload --port 8888
```

Откройте http://localhost:8888 (Basic Auth — логин/пароль из `.env`).

## Тесты

```bash
pytest
```

## Docker Compose

```bash
cp .env.example .env
docker compose up --build
```

Compose поднимает Postgres и приложение, применяет миграции при старте.

### Claude Code CLI внутри контейнера

ИИ-анализ идёт через `claude -p` (headless-режим личной подписки, не Anthropic API —
см. CLAUDE.md). Внутри контейнера установлен `@anthropic-ai/claude-code`, но ему
нужна авторизация подписки. `docker-compose.yml` монтирует `~/.claude` с хоста в
`/root/.claude:ro` внутри контейнера — авторизуйтесь на хосте один раз (`claude login`
или как требует ваша подписка) до `docker compose up`.

## Обход конкурентов и антибот-защита

Два пути обхода, `app/pipeline.py` выбирает автоматически:

1. **Локальный Playwright с ручной сессией** — приоритетный путь, если для конкурента
   сохранён `storage_states/competitor_<id>.json`. Актуально для сайтов с очень
   сильной антибот-защитой (например Qrator, как у foxford.ru), которую не проходит
   даже Apify с прокси. Получить сессию:

   ```bash
   python scripts/cookies_to_storage_state.py <competitor_id> <export.json>
   ```

   `export.json` — экспорт кук из расширения **Cookie-Editor** в обычном (не
   автоматизированном!) Chrome, где сайт конкурента уже открывается нормально.
   Подробности — в шапке скрипта. Это не "обход капчи ботом" — вы проходите защиту
   как живой пользователь, скрипт лишь конвертирует уже полученную сессию в формат
   Playwright.

2. **Apify Cloud** (актор `apify/website-content-crawler`) — если сохранённой сессии
   нет. Решение подключить внешний сервис принято 2026-08-10 (см. CLAUDE.md): для
   слабо защищённых сайтов достаточно прокси Apify, без ручной возни с куками. Нужен
   `APIFY_API_TOKEN` в `.env`. Каждый прогон расходует платный лимit аккаунта Apify.

Резервный локальный Playwright без сохранённой сессии (`scripts/capture_session.py`,
headed-браузер) детектится некоторыми защитами (Qrator) на уровне CDP-протокола
автоматизации даже в headed-режиме через настоящий Chrome — поэтому он практически
бесполезен против сильной защиты и оставлен только для слабо защищённых сайтов.

## Основные API-эндпоинты (Basic Auth)

- `GET/POST /api/competitors` — список / добавление конкурента
- `POST /api/competitors/{id}/crawl` — ручной запуск обхода
- `POST /api/competitors/{id}/pause` / `/resume` — пауза обхода
- `GET /api/changes` — лента изменений (JSON)

UI: `/` — конкуренты, `/changes` — лента изменений, `/changes/{id}` — карточка «было/стало».

## Структура

- `app/models.py`, `migrations/` — схема БД (Postgres)
- `app/crawler/apify_crawl.py` — обход через Apify Cloud (основной путь)
- `app/crawler/` — diff-движок; Playwright/детект блокировок — резервный путь
- `app/ai/analyze.py` — ИИ-анализ через Claude Code CLI headless
- `app/notifications/telegram.py` — уведомления в Telegram
- `app/pipeline.py` — связывает обход → diff → сохранение → ИИ → уведомление
- `app/scheduler.py` — APScheduler, обход раз в `CRAWL_INTERVAL_DAYS` дней
- `app/routers/`, `app/templates/` — FastAPI + Jinja2 UI
