#!/usr/bin/env bash
# Публикует локальный кабинет (мини-апп) в интернет по HTTPS и прописывает
# полученный адрес в .env как WEBAPP_URL.
#
# Зачем: Telegram открывает мини-апп ТОЛЬКО по HTTPS-адресу. На 127.0.0.1
# кнопка в боте просто не появится — бот не знает, куда её вести.
#
# Запуск (в обычном Терминале, не из этой песочницы):
#   ./scripts/expose.sh
#
# Что делается:
#   1. Находит ngrok или cloudflared.
#   2. Поднимает туннель до порта бота (по умолчанию 8080).
#   3. Ждёт публичный HTTPS-адрес.
#   4. Обновляет WEBAPP_URL в .env (старую строку заменяет, а не дублирует).
#   5. Печатает адрес и напоминание перезапустить бота.
#
# Остановить туннель: ./scripts/expose.sh --stop

set -euo pipefail

PORT="${PORT:-8080}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

if [[ "${1:-}" == "--stop" ]]; then
  pkill -f "ngrok http $PORT" 2>/dev/null || true
  pkill -f "cloudflared tunnel --url http://127.0.0.1:$PORT" 2>/dev/null || true
  echo "Туннель остановлен."
  exit 0
fi

# ── Выбор туннеля ────────────────────────────────────────────────────────
TOOL=""
if command -v ngrok >/dev/null 2>&1; then
  TOOL="ngrok"
elif command -v cloudflared >/dev/null 2>&1; then
  TOOL="cloudflared"
else
  echo "Не найден ни ngrok, ни cloudflared."
  echo "Установите одно из двух:"
  echo "  brew install ngrok        # бесплатного тарифа достаточно"
  echo "  brew install cloudflared  # быстрый туннель без регистрации"
  exit 1
fi
echo "Туннель: $TOOL → http://127.0.0.1:$PORT"

# ── Запуск ───────────────────────────────────────────────────────────────
if [[ "$TOOL" == "ngrok" ]]; then
  ngrok http "$PORT" --log=stdout >"$LOG_DIR/ngrok.log" 2>&1 &
  echo $! >"$LOG_DIR/ngrok.pid"
else
  cloudflared tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate \
    --logfile "$LOG_DIR/cloudflared.log" >/dev/null 2>&1 &
  echo $! >"$LOG_DIR/cloudflared.pid"
fi

# ── Ожидание адреса ──────────────────────────────────────────────────────
url=""
for _ in $(seq 1 40); do
  if [[ "$TOOL" == "ngrok" ]]; then
    # Локальный API ngrok отдаёт список туннелей в JSON.
    url="$(curl -s --max-time 2 http://127.0.0.1:4040/api/tunnels \
      | tr ',' '\n' | grep -oE 'https://[a-zA-Z0-9.-]+\.ngrok[^"]*' | head -1 || true)"
  else
    url="$(grep -oE 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' \
      "$LOG_DIR/cloudflared.log" 2>/dev/null | head -1 || true)"
  fi
  [[ -n "$url" ]] && break
  sleep 1
done

if [[ -z "$url" ]]; then
  echo "Не удалось получить публичный адрес за 40 секунд."
  echo "Лог: $LOG_DIR/${TOOL}.log"
  exit 1
fi
echo "Публичный адрес: $url"

# ── Запись в .env ────────────────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
  cp "$ROOT/.env.example" "$ENV_FILE"
  echo "Создан .env из .env.example — заполните токены."
fi

if grep -qE '^WEBAPP_URL=' "$ENV_FILE"; then
  # Меняем существующую строку, а не добавляем вторую.
  sed -i.bak "s|^WEBAPP_URL=.*|WEBAPP_URL=$url|" "$ENV_FILE"
  rm -f "$ENV_FILE.bak"
else
  printf '\nWEBAPP_URL=%s\n' "$url" >>"$ENV_FILE"
fi
echo "В .env записано: WEBAPP_URL=$url"

cat <<EOF

────────────────────────────────────────────────────────
Осталось перезапустить бота, чтобы он повесил кнопку:

  cd "$ROOT" && ./run.sh

После этого в чате с ботом появится кнопка «Открыть» слева от поля ввода.
Туннель жив, пока работает этот процесс и открыт Терминал.
Остановить: $0 --stop
EOF
