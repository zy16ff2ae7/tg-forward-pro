#!/usr/bin/env python3
"""Генератор подписанного initData для ручных проверок мини-аппа.

Зачем: мини-апп авторизуется подписью Telegram, а в браузере «просто так»
её не получить — только изнутри Telegram-клиента. Скрипт собирает такую же
подпись локально, чтобы дёргать API curl-ом без запуска клиента.

    python scripts/gen_initdata.py 555000111            # напечатать initData
    python scripts/gen_initdata.py 555000111 --curl     # готовый curl для /api/me
    python scripts/gen_initdata.py 555000111 --header   # только заголовок

Важно: подпись подлинная, поэтому API пустит запрос. Не используйте
user_id реального пользователя для проверок, которые меняют подписку
(например /api/subscription/bank) — только тестовые id.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402


def build_init_data(user_id: int, name: str = "Test", username: str = "tester") -> str:
    """Собирает initData с корректной подписью.

    data_check_string собирается из **декодированных** значений: сервер
    читает initData через parse_qsl и сравнивает подпись с декодированной
    строкой. Если склеить urlencode-вывод, подпись не сойдётся.
    """
    user = json.dumps(
        {
            "id": user_id,
            "first_name": name,
            "last_name": "User",
            "username": username,
            "language_code": "ru",
            "allows_write_to_pm": True,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    data = {
        "auth_date": str(int(time.time())),
        "query_id": "AAHtestquery",
        "user": user,
    }

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    # Значения кодируются, ключи — нет: так же их отдаёт Telegram.
    return "&".join(f"{key}={quote(value, safe='')}" for key, value in sorted(data.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description="Генерирует подписанный initData")
    parser.add_argument("user_id", type=int, help="Telegram user_id (берите тестовый)")
    parser.add_argument("--name", default="Test", help="Имя пользователя")
    parser.add_argument("--username", default="tester", help="Юзернейм")
    parser.add_argument("--curl", action="store_true", help="Напечатать готовый curl")
    parser.add_argument("--header", action="store_true", help="Напечатать только заголовок")
    parser.add_argument("--url", default=f"http://127.0.0.1:{settings.port}", help="Базовый URL")
    args = parser.parse_args()

    if not settings.bot_token:
        print("BOT_TOKEN не задан в .env — подпись собрать нельзя.", file=sys.stderr)
        return 1

    init_data = build_init_data(args.user_id, args.name, args.username)

    if args.header:
        print(f"X-Telegram-Init-Data: {init_data}")
    elif args.curl:
        print(f"curl -s --noproxy '*' -H 'X-Telegram-Init-Data: {init_data}' \\")
        print(f"  {args.url}/api/me")
    else:
        print(init_data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
