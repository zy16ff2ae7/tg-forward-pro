#!/usr/bin/env bash
# Локальный запуск (long polling): ./run.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if [[ ! -f .env ]]; then
  echo "Нет файла .env — создаю из .env.example. Заполните его!"
  cp .env.example .env
  python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" > .secret.tmp
  SECRET="$(cat .secret.tmp)"; rm -f .secret.tmp
  sed -i '' "s|^SECRET_KEY=.*|SECRET_KEY=$SECRET|" .env
  echo "SECRET_KEY сгенерирован и записан в .env"
  exit 1
fi

if [[ ! -d venv ]]; then
  python3 -m venv venv
  ./venv/bin/pip install -q --upgrade pip
  ./venv/bin/pip install -q -r requirements.txt
fi

mkdir -p data logs
exec ./venv/bin/python -m app.main
