#!/usr/bin/env bash
# Локальный запуск (long polling): ./run.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if [[ ! -f .env ]]; then
  echo "Нет файла .env — создаю из .env.example. Заполните его!"
  # umask до создания файла: иначе .env на секунду появится с правами 644,
  # а в нём уже будет сгенерированный SECRET_KEY.
  (umask 077 && cp .env.example .env)
  # Fernet-ключ = 32 случайных байта в urlsafe-base64: генерируется
  # стандартной библиотекой, системный cryptography для этого не нужен
  # (на чистой машине его ещё нет — venv создаётся позже).
  SECRET="$(python3 -c "import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())")"
  # sed -i различается: на macOS нужен пустой суффикс -i '', на Linux — нет.
  if [[ "$(uname)" == "Darwin" ]]; then
    sed -i '' "s|^SECRET_KEY=.*|SECRET_KEY=$SECRET|" .env
  else
    sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$SECRET|" .env
  fi
  chmod 600 .env
  echo "SECRET_KEY сгенерирован и записан в .env (права 600)"
  exit 1
fi

chmod 600 .env

if [[ ! -d venv ]]; then
  python3 -m venv venv
  ./venv/bin/pip install -q --upgrade pip
  ./venv/bin/pip install -q -r requirements.txt
fi

mkdir -p data logs
# В data лежит БД с зашифрованными сессиями, в logs — отладочные записи с
# номерами телефонов. Чужому пользователю на машине там делать нечего.
chmod 700 data logs
exec ./venv/bin/python -m app.main
