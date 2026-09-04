"""Права на файлы с секретами: .env, база, логи.

Служба читает эти файлы от своего пользователя, поэтому «группе» и «остальным»
здесь не нужно ничего. Оставленные по умолчанию 644 означают, что токен бота и
зашифрованные сессии может прочитать любой пользователь машины — на VDS это
и есть самый частый способ потерять бота.

Модуль сознательно не импортирует ничего из ``app``: им пользуются и
``app.config`` (в предупреждениях), и точка входа (чтобы починить права
на старте).
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

# Владелец читает и пишет, больше никто.
SECRET_FILE_MODE = 0o600
SECRET_DIR_MODE = 0o700

# Всё, что содержит секреты или пользовательские данные:
# .env — токен бота и ключ Fernet; data — БД с зашифрованными сессиями
# (включая WAL и SHM: в них лежат ещё не слитые в базу страницы);
# logs — переписка отладочного уровня и номера телефонов.
SENSITIVE_GLOBS: tuple[str, ...] = (
    ".env",
    "data/*.db",
    "data/*.db-wal",
    "data/*.db-shm",
    "logs/*.log",
    "logs/*.log.*",
)

SENSITIVE_DIRS: tuple[str, ...] = ("data", "logs")


def group_or_world_accessible(path: Path | str) -> bool:
    """Виден ли файл кому-то кроме владельца (любое из прав g/o)."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return False
    return bool(mode & (stat.S_IRWXG | stat.S_IRWXO))


def harden(path: Path | str, mode: int = SECRET_FILE_MODE) -> bool:
    """Снимает лишние права. True — если что-то реально поменяли.

    Ошибку не поднимаем: файл может лежать на файловой системе без прав POSIX
    (или принадлежать другому пользователю) — тогда об этом скажет warnings(),
    но сервис всё равно должен подняться.
    """
    try:
        current = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return False
    if current == mode:
        return False
    try:
        os.chmod(path, mode)
    except OSError:
        return False
    return True


def harden_runtime_files(base_dir: Path) -> list[str]:
    """Приводит .env, базу и логи к 600, а их папки — к 700.

    Возвращает список путей (относительно base_dir), права которых изменили,
    — точку входа это позволяет один раз написать в лог.
    """
    fixed: list[str] = []

    for name in SENSITIVE_DIRS:
        directory = base_dir / name
        if directory.is_dir() and harden(directory, SECRET_DIR_MODE):
            fixed.append(name + "/")

    for pattern in SENSITIVE_GLOBS:
        for path in sorted(base_dir.glob(pattern)):
            if path.is_file() and harden(path):
                fixed.append(str(path.relative_to(base_dir)))

    return fixed


def private_opener(path: str, flags: int) -> int:
    """opener для open(): новый файл создаётся сразу с правами 600.

    Нужен для логов: loguru пересоздаёт файл при каждой ротации, и без этого
    свежий app.log снова получил бы права по umask (обычно 644).
    """
    return os.open(path, flags, SECRET_FILE_MODE)
