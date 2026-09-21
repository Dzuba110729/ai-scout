# AI-Скаут

Внутренний монитор конкурентов. Архитектура и ограничения — см. [CLAUDE.md](CLAUDE.md).

## Локальный запуск (без Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m patchright install chromium

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

## Прод на отдельном Маке (всегда включён, управление через Telegram)

Рабочая схема: приложение запускается прямо на Маке (не в Docker) под присмотром
launchd — штатной службы macOS, которая поднимает его после входа в систему и
перезапускает при падении. В Docker живёт только Postgres. Проверено 2026-09-21.

Почему не Docker для приложения: на Маке Claude Code хранит вход в подписку в
Связке ключей (Keychain), а не в `~/.claude`. Linux-контейнер до Связки ключей не
достаёт, поэтому `claude -p` внутри него отвечает «Not logged in», и ИИ-анализ не
работает. `docker-compose.yml` оставлен для Linux-серверов.

### Первичная установка

```bash
# 1. Код и зависимости
git clone https://github.com/Dzuba110729/ai-scout.git ~/ai-scout
cd ~/ai-scout
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m patchright install chromium

# 2. База (Docker Desktop должен быть установлен и запущен)
docker run -d --name ai-scout-db --restart unless-stopped \
  -e POSTGRES_USER=scout -e POSTGRES_PASSWORD=scout -e POSTGRES_DB=ai_scout \
  -p 5432:5432 postgres:16-alpine

# 3. Секреты — переносятся руками с рабочего компьютера (в git их нет):
#    .env, google_oauth_token.json, client_secret_*.json, папка storage_states/

# 4. Claude Code CLI: установить и один раз войти в подписку
claude          # внутри выполнить /login

# 5. Служба launchd
bash scripts/install_launchd.sh
tail -f logs/app.log     # дождаться «Uvicorn running»
```

### Настройки самого Мака (обязательно)

- **Docker Desktop → Settings → General → «Start Docker Desktop when you sign in»** —
  включить. `scripts/run_prod.sh` сам откроет Docker, если он не запущен, но
  автозапуск надёжнее.
- **Системные настройки → Экономия энергии** (или «Батарея» на ноутбуке): запретить
  автоматический сон, включить «Перезапуск после сбоя питания».
- **Пользователи и группы → Автоматический вход** — включить для вашего пользователя.
  Служба живёт в сеансе пользователя (ей нужны Связка ключей и Docker Desktop), без
  входа в систему после перезагрузки она не поднимется.
- **Google Cloud → Google Auth Platform → Audience → Publish app.** Пока OAuth-приложение
  в статусе Testing, Google отзывает токен каждые 7 дней, и отчёты перестают
  создаваться. Кнопка неактивна, пока на странице Branding не заполнены App name и
  User support email. После публикации перевыпустить токен:
  `python scripts/google_oauth_login.py`.

### Обслуживание

```bash
tail -f logs/app.log                                    # логи
launchctl kickstart -k gui/$(id -u)/ru.ai-scout.app     # перезапустить
launchctl bootout gui/$(id -u)/ru.ai-scout.app          # остановить и снять службу

# Обновить код
cd ~/ai-scout && git pull && source .venv/bin/activate && pip install -e ".[dev]" \
  && launchctl kickstart -k gui/$(id -u)/ru.ai-scout.app
```

Веб-интерфейс по умолчанию доступен только с самого Мака (http://127.0.0.1:8888).
Чтобы открыть его другим устройствам в локальной сети, добавьте в `.env` строку
`AI_SCOUT_BIND_HOST=0.0.0.0` и перезапустите службу — доступ по-прежнему под
Basic Auth.

## Docker Compose (Linux-сервер)

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
или как требует ваша подписка) до `docker compose up`. **На Маке это не работает**
(вход хранится в Связке ключей, см. раздел выше) — на Linux-хосте не проверялось.

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

## Сравнение с нашим собственным сайтом

По каждой находке у конкурента инструмент отвечает: **есть ли такое у нас, а если
похожее — чем отличается и чего нам не хватает**. Это видно в карточке изменения,
в ленте, в отчёте Google Docs и в итоговом сообщении Telegram.

Как включить: на странице `/settings` в карточке «Наш сайт» указать адрес своего
сайта и один раз запустить его обход («Обойти наш сайт сейчас»). Дальше он
обходится по общему расписанию. Пока сайт не указан или ни разу не обойден,
сравнение молча пропускается — обход конкурентов работает как раньше.

Наш сайт хранится в той же таблице `competitors` с признаком `is_own` (это
переиспользует готовый обход), но конкурентом не считается: не попадает в ленту
изменений, счётчики дашборда, отчёты и уведомления.

Как это устроено (`app/ai/compare.py`): по тексту страницы конкурента подбираются
три самые похожие наши страницы обычным сравнением слов (TF-IDF + грубое отсечение
русских окончаний, без эмбеддингов и внешних сервисов), и они вместе уходят в
**один** запрос к `claude -p`. Число сравнений за прогон ограничено
`OWN_SITE_COMPARE_MAX_PER_RUN` (сначала новые страницы, потом изменившиеся;
удалённые не сравниваются) — иначе обход с сотнями находок сделал бы сотни вызовов ИИ.

## Основные API-эндпоинты (Basic Auth)

- `GET/POST /api/competitors` — список / добавление конкурента
- `POST /api/competitors/{id}/crawl` — ручной запуск обхода
- `POST /api/competitors/{id}/pause` / `/resume` — пауза обхода
- `GET /api/changes` — лента изменений (JSON)
- `GET/PUT /api/own-site` — адрес нашего сайта; `POST /api/own-site/crawl` — его обход

UI: `/` — дашборд, `/competitors` — список конкурентов, `/competitors/{id}` — карточка
конкурента (вкладки «Обзор» / «Страницы» / «История изменений»), `/changes` — лента
изменений с фильтрами, `/changes/{id}` — карточка «было/стало», `/settings` — расписание обхода.

## Структура

- `app/models.py`, `migrations/` — схема БД (Postgres)
- `app/crawler/apify_crawl.py` — обход через Apify Cloud (основной путь)
- `app/crawler/` — diff-движок; Playwright/детект блокировок — резервный путь
- `app/ai/analyze.py` — ИИ-анализ через Claude Code CLI headless
- `app/ai/compare.py`, `app/own_site.py` — сравнение находок с нашим собственным сайтом
- `app/notifications/telegram.py` — уведомления в Telegram
- `app/pipeline.py` — связывает обход → diff → сохранение → ИИ → уведомление
- `app/scheduler.py` — APScheduler, обход раз в `CRAWL_INTERVAL_DAYS` дней
- `app/routers/`, `app/templates/` — FastAPI + Jinja2 UI
