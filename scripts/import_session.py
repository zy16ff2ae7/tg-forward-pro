#!/usr/bin/env python3
"""Импорт готовой MTProto-сессии (auth_key + dc_id) в сервис.

Зачем: иногда аккаунт уже авторизован где-то ещё, и от него осталась только
«сырая» сессия в виде

    <auth_key в hex, 512 символов>:<номер DC>

Например: `bd64...e9131:1`. Получать код из Telegram повторно не нужно —
этого ключа достаточно, чтобы работать от имени аккаунта. Но ключ сам по себе
не является сессией Telethon, поэтому его нужно упаковать в StringSession:

    struct.pack('>B4sH256s', dc_id, ip, port, auth_key)  ->  base64url

Именно этот формат читает `TelegramClient(StringSession(...))`.

ВАЖНО: api_id / api_hash всё равно обязательны. Это идентификаторы приложения
(my.telegram.org), без них ни один MTProto-клиент не установит соединение —
ключ шифрования канала есть, а «паспорта» у клиента нет.

Запуск из корня проекта:

    PYTHONPATH=. python scripts/import_session.py --session "hex:1"
    PYTHONPATH=. python scripts/import_session.py --session "hex:1" --user-id 123456789 --save

Без --save скрипт только проверяет ключ (подключается и печатает get_me),
ничего в базу не пишет.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import ipaddress
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon import TelegramClient  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import repo  # noqa: E402
from app.db.database import init_db, session_scope  # noqa: E402
from app.security import encrypt_session  # noqa: E402
from app.telegram_client.manager import _proxy_dict  # noqa: E402

# Адреса production-DC, которые используют MTProto-клиенты.
# ip и port нужны не «для галочки» — они попадают внутрь строки сессии,
# и Telethon подключается именно по ним.
DC_SERVERS: dict[int, tuple[str, int]] = {
    1: ("149.154.175.53", 443),
    2: ("149.154.167.51", 443),
    3: ("149.154.175.100", 443),
    4: ("149.154.167.91", 443),
    5: ("91.108.56.130", 443),
}

STRUCT = ">B4sH256s"
AUTH_KEY_BYTES = 256


class ParseError(RuntimeError):
    """Не удалось разобрать строку сессии."""


def build_string_session(raw: str) -> str:
    """Превращает «сырую» сессию в Telethon StringSession.

    Понимает три формата:
      1) `hex:dc`        — сырой auth_key + номер DC (то, что выдаёт генератор);
      2) `hex`           — только ключ, DC по умолчанию 2;
      3) `1<BCHOMEC...>`  — уже готовая строка Telethon — возвращаем как есть.
    """
    raw = raw.strip().strip('"').strip("'")
    if not raw:
        raise ParseError("Пустая строка сессии.")

    # Формат 3: готовая строка Telethon. Она начинается с версии '1'
    # и имеет длину 353 символа (1 + base64 от 263 байт).
    if raw[0] == "1" and len(raw) == 353:
        StringSession(raw)  # проверка, что строка валидна
        return raw

    dc_id = 2
    if ":" in raw:
        hex_part, _, dc_part = raw.rpartition(":")
        if dc_part.isdigit():
            dc_id = int(dc_part)
            raw = hex_part
    elif raw[-1].isdigit() and len(raw) == 513:
        # на всякий случай: `hex` с приклеенной цифрой без двоеточия
        dc_id = int(raw[-1])
        raw = raw[:-1]

    raw = raw.replace(" ", "").replace("\n", "")
    if len(raw) != AUTH_KEY_BYTES * 2:
        raise ParseError(
            f"Ключ должен быть {AUTH_KEY_BYTES * 2} hex-символов (256 байт), "
            f"а получено {len(raw)}."
        )
    try:
        auth_key = bytes.fromhex(raw)
    except ValueError as exc:
        raise ParseError(f"Строка не похожа на hex: {exc}") from exc

    if dc_id not in DC_SERVERS:
        raise ParseError(
            f"DC {dc_id} неизвестен. Допустимые: {', '.join(map(str, sorted(DC_SERVERS)))}."
        )

    ip, port = DC_SERVERS[dc_id]
    packed = struct.pack(
        STRUCT, dc_id, ipaddress.ip_address(ip).packed, port, auth_key
    )
    return "1" + base64.urlsafe_b64encode(packed).decode("ascii")


async def check_session(session_string: str):
    """Подключается и возвращает get_me() — так видно, что ключ живой."""
    client = TelegramClient(
        StringSession(session_string),
        settings.api_id,
        settings.api_hash,
        proxy=_proxy_dict(settings.proxy),
        device_model="MacBook Pro",
        system_version="macOS",
        app_version="1.0",
        connection_retries=3,
        request_retries=3,
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Ключ принят, но аккаунт не авторизован (is_user_authorized=False). "
                "Скорее всего сессия отозвана — завершите её в Настройках → Устройства."
            )
        return await client.get_me()
    finally:
        await client.disconnect()


async def save_session(session_string: str, user_id: int, phone: str) -> int:
    """Кладёт сессию в БД в зашифрованном виде (Fernet, ключ из SECRET_KEY)."""
    await init_db()
    async with session_scope() as session:
        await repo.get_or_create_user(session, user_id)
        account = await repo.add_account(
            session,
            user_id=user_id,
            phone=phone,
            session_encrypted=encrypt_session(session_string),
        )
        return int(account.id)


async def main_async(args: argparse.Namespace) -> int:
    if not settings.mtproto_ready:
        print(
            "Не заданы API_ID / API_HASH в .env — без них подключение невозможно.\n"
            "Получите их на my.telegram.org (API development tools) или запустите:\n"
            "  PYTHONPATH=. python scripts/get_api_credentials.py",
            file=sys.stderr,
        )
        return 2
    if not settings.secret_key:
        print("Не задан SECRET_KEY в .env — сессию нечем шифровать.", file=sys.stderr)
        return 2

    raw = args.session or getpass.getpass("Сырая сессия (hex:dc): ")
    try:
        session_string = build_string_session(raw)
    except ParseError as exc:
        print(f"Не удалось разобрать сессию: {exc}", file=sys.stderr)
        return 2

    print(f"DC: {StringSession(session_string).dc_id}")
    print("Подключаюсь к Telegram…")
    try:
        me = await check_session(session_string)
    except Exception as exc:  # noqa: BLE001 — показываем причину как есть
        print(f"Сессия не работает: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    name = " ".join(p for p in (me.first_name, me.last_name) if p) or "(без имени)"
    phone = me.phone or args.phone or ""
    print(
        f"\n✓ Аккаунт живой: {name}"
        f" | @{me.username or '—'}"
        f" | id={me.id}"
        f" | {phone or 'номер скрыт'}"
        f" | premium={getattr(me, 'premium', False)}"
    )

    if args.show:
        print(f"\nTelethon StringSession:\n{session_string}")

    if not args.save:
        print(
            "\nПроверка пройдена. Чтобы записать сессию в базу, добавьте "
            "--save --user-id <ваш telegram id>."
        )
        return 0

    if not args.user_id:
        print("Для --save нужен --user-id (ваш Telegram user_id).", file=sys.stderr)
        return 2

    await init_db()
    account_id = await save_session(session_string, int(args.user_id), phone)
    print(f"\n✓ Сессия зашифрована и сохранена. Аккаунт id={account_id}, user_id={args.user_id}.")
    print("Перезапустите бота, чтобы он поднял клиент этого аккаунта.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Импорт готовой MTProto-сессии (auth_key + dc) в tg-forward-pro."
    )
    parser.add_argument("--session", help="строка вида <hex>:<dc> (или готовая StringSession)")
    parser.add_argument("--user-id", help="Telegram user_id владельца кабинета")
    parser.add_argument("--phone", default="", help="номер телефона (по умолчанию из get_me)")
    parser.add_argument("--save", action="store_true", help="записать сессию в БД")
    parser.add_argument("--show", action="store_true", help="показать готовую StringSession")
    # getpass не дружит с интерактивом в пайпах — сессию можно передать через env
    import os

    if not parser.parse_known_args()[0].session and os.getenv("RAW_SESSION"):
        parser.set_defaults(session=os.environ["RAW_SESSION"])
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nПрервано.")
        sys.exit(130)
