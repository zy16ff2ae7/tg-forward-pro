#!/usr/bin/env bash
# Деплой сервиса на VDS: ./deploy/deploy.sh user@server
set -euo pipefail

TARGET_HOST="${1:-}"
APP_DIR="/opt/tg-forward"
SERVICE_NAME="tg-forward"
# Бэкапы лежат ВНЕ каталога приложения: rsync --delete в этом скрипте
# зеркалит $APP_DIR по исходнику, и всё, чего нет в проекте, стирается.
# Один деплой уже уничтожил накопленные архивы — больше не повторится.
BACKUP_DIR="/var/backups/tg-forward"

if [[ -z "$TARGET_HOST" ]]; then
  echo "Использование: ./deploy/deploy.sh user@server"
  exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Пользователь сервиса и каталоги на $TARGET_HOST"
# Сервис работает НЕ от root (см. User= в юните): отдельный пользователь
# без шелла, код читает, пишет только в data/ и logs/.
ssh "$TARGET_HOST" "id -u tgforward >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -d $APP_DIR tgforward"
ssh "$TARGET_HOST" "mkdir -p $APP_DIR/data $APP_DIR/logs $BACKUP_DIR"

echo "==> Синхронизирую файлы в $TARGET_HOST:$APP_DIR"

# data и logs исключены целиком, а не по маске *.db. SQLite работает в режиме WAL,
# рядом с базой лежат app.db-wal и app.db-shm: маска их не покрывала, и деплой
# затирал живой журнал локальным (у разработчика он почти всегда пустой) —
# вместе с транзакциями, которые не успели попасть в основной файл.
#
# Копии вида .env.bak-* (их оставляет ручная подмена ключей) на сервере не нужны и
# опасны: один такой файл уже уехал в /opt вместе с локальным токеном и ключами.
# Поэтому под запретом всё .env.*, а .env.example разрешён явно и раньше запрета —
# в rsync побеждает первое подошедшее правило, а без примера первый деплой не соберёт .env.

# Имперсонация и dev-инструменты на сервер не едут:
# - tests/ на проде не нужны никогда;
# - scripts/gen_initdata.py подписывает валидный initData на любой user_id —
#   готовый ключ от чужого кабинета при доступе к серверу. Остальные скрипты
#   (import/export_session, gen_secret) на сервере используются, их оставляем.
rsync -av --delete \
  --exclude '.git' \
  --exclude 'venv' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '.DS_Store' \
  --exclude 'tests/' \
  --exclude 'scripts/gen_initdata.py' \
  --exclude 'data/*' \
  --exclude 'logs/*' \
  --exclude '.env' \
  --include '.env.example' \
  --exclude '.env.*' \
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
  else (umask 077 && cp $APP_DIR/.env.example $APP_DIR/.env) && echo yes; fi")"

if [[ "$ENV_CREATED" == "yes" ]]; then
  echo "!! На сервере создан $APP_DIR/.env из примера."
  echo "!! Заполните его (BOT_TOKEN, API_ID, API_HASH, SECRET_KEY, WEBHOOK_URL) и"
  echo "!! запустите деплой снова. Сервис пока не запущен."
  exit 1
fi

echo "==> Права: код — root, данные и секреты — tgforward"
# rsync копирует права с локальной машины, где .env мог остаться 644.
# .env — токен бота и ключ Fernet, data — БД с зашифрованными сессиями,
# logs — отладочные записи с номерами телефонов.
# .env читает systemd-юнит от имени tgforward: 600 + владелец. БД (data/*.db
# с WAL) и логи должны быть записываемы сервисом — отдаём каталоги целиком.
ssh "$TARGET_HOST" "mkdir -p $APP_DIR/data $APP_DIR/logs $BACKUP_DIR \
  && chown -R tgforward:tgforward $APP_DIR/data $APP_DIR/logs $BACKUP_DIR \
  && chown tgforward:tgforward $APP_DIR/.env && chmod 600 $APP_DIR/.env \
  && chmod 700 $APP_DIR/data $APP_DIR/logs $BACKUP_DIR \
  && find $APP_DIR/data $APP_DIR/logs $BACKUP_DIR -type f -exec chmod 600 {} + 2>/dev/null || true"

echo "==> Устанавливаю systemd-юнит"
ssh "$TARGET_HOST" "cp $APP_DIR/deploy/$SERVICE_NAME.service /etc/systemd/system/ \
  && cp $APP_DIR/deploy/$SERVICE_NAME-backup.service $APP_DIR/deploy/$SERVICE_NAME-backup.timer /etc/systemd/system/ \
  && systemctl daemon-reload \
  && systemctl enable $SERVICE_NAME \
  && systemctl enable --now $SERVICE_NAME-backup.timer \
  && systemctl restart $SERVICE_NAME"

echo "==> Готово. Статус:"
# systemctl status возвращает 3, если сервис не активен — с pipefail это
# уронило бы весь скрипт на успешном деплое.
ssh "$TARGET_HOST" "systemctl status $SERVICE_NAME --no-pager | head -20" || true
