#!/usr/bin/env python3
"""Бэкапы базы: `backup` (по таймеру), `restore` (вручную), `list`.

Настройки — из .env: BACKUP_DIR, BACKUP_KEEP, BACKUP_PASSPHRASE.
Пароль шифрования храните ВНЕ сервера (менеджер паролей, бумага в сейфе):
без него шифрованный бэкап — мусор, а лежит пароль обычно рядом с базой.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backup import (  # noqa: E402
    BackupError,
    list_backups,
    make_backup,
    restore_backup,
    sqlite_path_from_url,
)
from app.config import BASE_DIR, settings  # noqa: E402


def _backup_dir() -> Path:
    configured = Path(settings.backup_dir or "backups")
    return configured if configured.is_absolute() else BASE_DIR / configured


def cmd_backup() -> int:
    try:
        info = make_backup(
            settings.database_url,
            _backup_dir(),
            base_dir=BASE_DIR,
            keep=settings.backup_keep,
            passphrase=settings.backup_passphrase or None,
        )
    except BackupError as exc:
        print(f"Бэкап не сделан: {exc}")
        return 1
    lock = "🔒 шифрованный" if info.encrypted else "⚠️ ОТКРЫТЫЙ (задайте BACKUP_PASSPHRASE)"
    print(f"✓ {info.path.name} ({info.size // 1024} КБ, {lock})")
    return 0


def cmd_list() -> int:
    found = list_backups(_backup_dir())
    if not found:
        print("Бэкапов нет.")
        return 0
    for info in found:
        lock = "🔒" if info.encrypted else "⚠️"
        print(f"{lock} {info.path.name} — {info.size // 1024} КБ, {info.created_at:%d.%m.%Y %H:%M}")
    return 0


def cmd_restore(args) -> int:
    archive = Path(args.archive)
    if args.db:
        db_path = Path(args.db)
    else:
        resolved = sqlite_path_from_url(settings.database_url, BASE_DIR)
        if resolved is None:
            print("DATABASE_URL не указывает на файл SQLite — дайте путь через --db")
            return 1
        db_path = resolved
    try:
        result = restore_backup(
            archive, db_path, passphrase=settings.backup_passphrase or None,
            force=args.force,
        )
    except BackupError as exc:
        print(f"Не восстановлено: {exc}")
        return 1
    print(f"✓ База восстановлена: {result}")
    print("Перезапустите сервис: systemctl restart tg-forward")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Бэкапы базы tg-forward-pro")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backup", help="снять бэкап (для таймера)")
    sub.add_parser("list", help="показать архивы")
    restore = sub.add_parser("restore", help="развернуть архив в базу")
    restore.add_argument("archive", help="файл tgf-backup-*.tar.gz[.enc]")
    restore.add_argument("--db", default="", help="куда развернуть (по умолчанию — из DATABASE_URL)")
    restore.add_argument("--force", action="store_true",
                         help="затереть существующую базу (сервис должен стоять)")
    args = parser.parse_args(argv)
    if args.command == "backup":
        return cmd_backup()
    if args.command == "list":
        return cmd_list()
    return cmd_restore(args)


if __name__ == "__main__":
    sys.exit(main())
