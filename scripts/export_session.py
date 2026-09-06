#!/usr/bin/env python3
"""Экспорт уже подключённого аккаунта из БД в строку Telethon StringSession.

Обратная сторона `import_session.py`: там мы кладём готовую сессию в базу,
здесь — достаём её обратно. Нужно, чтобы перенести аккаунт на другую машину
(ноутбук → VDS) без повторного входа по номеру: на новом месте достаточно
`import_session.py --session <строка> --save`.

Сессия в базе лежит зашифрованной Fernet-ключом из `SECRET_KEY`, поэтому
расшифровать её можно только там, где этот ключ совпадает. На новом месте
ключ может быть другим — не страшно: импорт зашифрует сессию заново уже
местным ключом.

Запуск из корня проекта:

    # показать все подключённые аккаунты
    PYTHONPATH=. python scripts/export_session.py --list

    # вывести сессию в консоль
    PYTHONPATH=. python scripts/export_session.py --user-id 7686196719

    # или в файл (права 600) — так безопаснее, строка не осядет в истории шелла
    PYTHONPATH=. python scripts/export_session.py --user-id 7686196719 --out /tmp/acc.session

Строка даёт полный доступ к аккаунту — не пересылайте её в чаты и не коммитьте.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import repo  # noqa: E402
from app.db.database import init_db, session_scope  # noqa: E402
from app.security import CryptoError, decrypt_session  # noqa: E402


async def list_accounts() -> None:
    async with session_scope() as session:
        accounts = await repo.all_active_accounts(session)
    if not accounts:
        print("Подключённых аккаунтов нет.")
        return
    print("Подключённые аккаунты:")
    for acc in accounts:
        print(
            f"  id={acc.id} user_id={acc.user_id} "
            f"{acc.phone or 'номер скрыт'} "
            f"ошибка={acc.last_error or '—'}"
        )
    print(
        "\nЭкспорт: --user-id <id> или --account-id <id> "
        "(добавьте --out FILE, чтобы не светить строку в истории)."
    )


async def find_account(user_id: int | None, account_id: int | None):
    """Находит аккаунт по user_id, по id аккаунта или по обоим сразу.

    `repo.get_account` требует и то и другое, поэтому по одному id проще
    перебрать все аккаунты — их всё равно единицы.
    """
    async with session_scope() as session:
        if user_id is not None:
            accounts = await repo.list_accounts(session, user_id)
            if account_id is None:
                return accounts[0] if accounts else None
            return next((a for a in accounts if a.id == account_id), None)
        candidates = await repo.all_active_accounts(session)
        return next((a for a in candidates if a.id == account_id), None)


async def main_async(args: argparse.Namespace) -> int:
    await init_db()

    if args.list or (args.user_id is None and args.account_id is None):
        await list_accounts()
        return 0

    account = await find_account(args.user_id, args.account_id)
    if account is None:
        print("Аккаунт с такими --user-id / --account-id не найден.", file=sys.stderr)
        return 2

    try:
        session_string = decrypt_session(account.session_encrypted)
    except CryptoError as exc:
        print(f"Не удалось расшифровать: {exc}", file=sys.stderr)
        print("Скорее всего, SECRET_KEY отличается от того, которым сессия", file=sys.stderr)
        print("зашифровывалась.", file=sys.stderr)
        return 1

    if args.out:
        path = Path(args.out).expanduser()
        path.write_text(session_string + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        print(f"Сессия аккаунта id={account.id} ({account.phone or 'номер скрыт'})")
        print(f"записана в {path} (права 600, {len(session_string)} символов).")
    else:
        print(session_string)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Экспорт сессии аккаунта из БД в Telethon StringSession."
    )
    parser.add_argument("--user-id", type=int, help="Telegram user_id владельца")
    parser.add_argument("--account-id", type=int, help="id аккаунта в таблице")
    parser.add_argument("--list", action="store_true", help="показать аккаунты и выйти")
    parser.add_argument("--out", help="путь к файлу (создаётся с правами 600)")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nПрервано.")
        sys.exit(130)
