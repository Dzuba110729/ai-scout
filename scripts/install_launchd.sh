#!/bin/bash
# Ставит (или переустанавливает) службу launchd, которая держит AI-Скаут запущенным
# и поднимает его после входа в систему. Запускать один раз на прод-Маке из корня проекта:
#
#   bash scripts/install_launchd.sh
#
# Снять службу:  launchctl bootout gui/$(id -u)/ru.ai-scout.app
# Перезапустить: launchctl kickstart -k gui/$(id -u)/ru.ai-scout.app
# Логи:          tail -f logs/app.log

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="ru.ai-scout.app"
PLIST_SRC="$PROJECT_DIR/deploy/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/logs"

sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$PLIST_SRC" > "$PLIST_DST"

# Если служба уже стояла — снимаем старую, иначе bootstrap откажется.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"

echo "Служба $LABEL установлена и запущена."
echo "Лог: $PROJECT_DIR/logs/app.log"
