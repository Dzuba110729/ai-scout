#!/bin/bash
# Запуск AI-Скаута на всегда включённом Маке (см. README, раздел «Прод на отдельном Маке»).
# Вызывается службой launchd (deploy/ru.ai-scout.app.plist), но можно запустить и руками.
#
# После входа в систему Docker Desktop с базой поднимается не мгновенно, поэтому
# сначала ждём базу, потом накатываем миграции, потом стартуем сервер. Если сервер
# упадёт, launchd перезапустит этот скрипт заново (KeepAlive в plist).

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# launchd запускает службы с почти пустым PATH — без этого не найдётся ни docker, ни claude.
export PATH="$PROJECT_DIR/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH"
# Без USER claude не находит вход в подписку в Связке ключей и отвечает «Not logged in».
export USER="${USER:-$(id -un)}"
export LOGNAME="${LOGNAME:-$USER}"

DB_CONTAINER="${AI_SCOUT_DB_CONTAINER:-ai-scout-db}"
DB_WAIT_SECONDS="${AI_SCOUT_DB_WAIT_SECONDS:-300}"

echo "[run_prod] $(date '+%F %T') старт, проект: $PROJECT_DIR"

if ! docker info >/dev/null 2>&1; then
  echo "[run_prod] Docker ещё не запущен — открываю Docker Desktop"
  open -a Docker || true
fi

waited=0
until docker exec "$DB_CONTAINER" pg_isready -q >/dev/null 2>&1; do
  if [ "$waited" -ge "$DB_WAIT_SECONDS" ]; then
    echo "[run_prod] база $DB_CONTAINER не поднялась за $DB_WAIT_SECONDS с — выходим, launchd перезапустит"
    exit 1
  fi
  if docker info >/dev/null 2>&1 && ! docker ps --format '{{.Names}}' | grep -qx "$DB_CONTAINER"; then
    echo "[run_prod] контейнер $DB_CONTAINER не запущен — запускаю"
    docker start "$DB_CONTAINER" >/dev/null 2>&1 || true
  fi
  sleep 5
  waited=$((waited + 5))
done
echo "[run_prod] база готова (ждали ${waited} с)"

alembic upgrade head

# По умолчанию веб-интерфейс виден только с самого Мака: управление идёт через Telegram.
# Чтобы открыть его другим устройствам в локальной сети, добавьте в .env строку
# AI_SCOUT_BIND_HOST=0.0.0.0 (приложение .env читает само, а этому скрипту нужно достать её вручную).
BIND_HOST="$(grep -E '^AI_SCOUT_BIND_HOST=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
exec uvicorn app.main:app --host "${BIND_HOST:-127.0.0.1}" --port 8888
