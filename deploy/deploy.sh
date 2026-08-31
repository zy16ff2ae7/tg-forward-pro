#!/usr/bin/env bash
# Деплой сервиса на VDS: ./deploy/deploy.sh user@server
set -euo pipefail

TARGET_HOST="${1:-}"
APP_DIR="/opt/tg-forward"
SERVICE_NAME="tg-forward"

if [[ -z "$TARGET_HOST" ]]; then
  echo "Использование: ./deploy/deploy.sh user@server"
  exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Синхронизирую файлы в $TARGET_HOST:$APP_DIR"
ssh "$TARGET_HOST" "mkdir -p $APP_DIR/{data,logs}"

rsync -av --delete \
  --exclude '.git' \
  --exclude 'venv' \
  --exclude '__pycache__' \
  --exclude 'data/*.db' \
  --exclude 'logs/*' \
  --exclude '.env' \
  "$PROJECT_DIR"/ "$TARGET_HOST:$APP_DIR"/

echo "==> Ставлю зависимости"
ssh "$TARGET_HOST" "cd $APP_DIR \
  && python3 -m venv venv \
  && ./venv/bin/pip install -q --upgrade pip \
  && ./venv/bin/pip install -q -r requirements.txt"

echo "==> Проверяю .env на сервере"
# rsync выше намеренно не трогает .env: на сервере свои ключи и SECRET_KEY.
# Но systemd-юнит читает его через EnvironmentFile — без файла сервис не стартует,
# поэтому на первом деплое создаём заготовку и останавливаемся.
ENV_CREATED="$(ssh "$TARGET_HOST" "if [[ -f $APP_DIR/.env ]]; then echo no; \
  else cp $APP_DIR/.env.example $APP_DIR/.env && echo yes; fi")"

if [[ "$ENV_CREATED" == "yes" ]]; then
  echo "!! На сервере создан $APP_DIR/.env из примера."
  echo "!! Заполните его (BOT_TOKEN, API_ID, API_HASH, SECRET_KEY, WEBHOOK_URL) и"
  echo "!! запустите деплой снова. Сервис пока не запущен."
  exit 1
fi

echo "==> Устанавливаю systemd-юнит"
ssh "$TARGET_HOST" "cp $APP_DIR/deploy/$SERVICE_NAME.service /etc/systemd/system/ \
  && systemctl daemon-reload \
  && systemctl enable $SERVICE_NAME \
  && systemctl restart $SERVICE_NAME"

echo "==> Готово. Статус:"
# systemctl status возвращает 3, если сервис не активен — с pipefail это
# уронило бы весь скрипт на успешном деплое.
ssh "$TARGET_HOST" "systemctl status $SERVICE_NAME --no-pager | head -20" || true
