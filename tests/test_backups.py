"""Бэкапы: снимок живой базы, шифрование, чистка, восстановление.

Проверяем на временном SQLite: делаем базу с данными, снимаем бэкап,
разворачиваем рядом и сверяем содержимое. Сеть и продакшен не трогаем.
"""
from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from app.backup import (
    BackupError,
    list_backups,
    make_backup,
    prune_backups,
    restore_backup,
    sqlite_path_from_url,
)


def _make_db(path) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT)")
        connection.executemany(
            "INSERT INTO notes (text) VALUES (?)", [("раз",), ("два",)]
        )
        connection.commit()
    finally:
        connection.close()


def _read_db(path) -> list[str]:
    connection = sqlite3.connect(str(path))
    try:
        return [row[0] for row in connection.execute("SELECT text FROM notes ORDER BY id")]
    finally:
        connection.close()


def test_encrypted_roundtrip_keeps_data(tmp_path):
    """Шифрованный бэкап разворачивается в базу с теми же данными."""
    db = tmp_path / "app.db"
    _make_db(db)

    info = make_backup(
        "sqlite+aiosqlite:///./app.db", tmp_path / "backups",
        base_dir=tmp_path, passphrase="секрет-со-вне-сервера",
    )

    assert info.encrypted is True
    assert info.path.suffix == ".enc"
    restored = tmp_path / "restored.db"
    restore_backup(info.path, restored, passphrase="секрет-со-вне-сервера")
    assert _read_db(restored) == ["раз", "два"]


def test_plain_backup_when_no_passphrase(tmp_path):
    """Без пароля архив открытый — честно, без вида шифрования."""
    db = tmp_path / "app.db"
    _make_db(db)

    info = make_backup(
        "sqlite+aiosqlite:///./app.db", tmp_path / "backups", base_dir=tmp_path
    )

    assert info.encrypted is False
    assert info.path.suffix == ".gz"
    restored = tmp_path / "restored.db"
    restore_backup(info.path, restored)
    assert _read_db(restored) == ["раз", "два"]


def test_backup_file_is_owner_only(tmp_path):
    """Архив с доступами — 0600, даже если бэкапится открытым."""
    db = tmp_path / "app.db"
    _make_db(db)

    info = make_backup(
        "sqlite+aiosqlite:///./app.db", tmp_path / "backups", base_dir=tmp_path
    )

    assert stat.S_IMODE(os.stat(info.path).st_mode) == 0o600


def test_wrong_passphrase_does_not_restore(tmp_path):
    """Чужой пароль — понятная ошибка, а не битая база."""
    db = tmp_path / "app.db"
    _make_db(db)
    info = make_backup(
        "sqlite+aiosqlite:///./app.db", tmp_path / "backups",
        base_dir=tmp_path, passphrase="правильный",
    )

    with pytest.raises(BackupError, match="BACKUP_PASSPHRASE"):
        restore_backup(info.path, tmp_path / "restored.db", passphrase="неправильный")


def test_restore_refuses_to_clobber_live_db(tmp_path):
    """Поверх живой базы — только с force: случайный рестор страшнее простоя."""
    db = tmp_path / "app.db"
    _make_db(db)
    info = make_backup(
        "sqlite+aiosqlite:///./app.db", tmp_path / "backups", base_dir=tmp_path
    )

    with pytest.raises(BackupError, match="--force"):
        restore_backup(info.path, db)
    assert _read_db(db) == ["раз", "два"]

    restore_backup(info.path, db, force=True)
    assert _read_db(db) == ["раз", "два"]


def test_postgres_is_refused_with_a_hint(tmp_path):
    """Postgres молча не бэкапим — отправляем к pg_dump."""
    with pytest.raises(BackupError, match="pg_dump"):
        make_backup(
            "postgresql+asyncpg://u:p@localhost/db", tmp_path / "backups",
            base_dir=tmp_path,
        )


def test_memory_db_has_nothing_to_back_up(tmp_path):
    """:memory: — не файл: пустой архив вместо ошибки был бы враньём."""
    with pytest.raises(BackupError, match=":memory:"):
        make_backup("sqlite:///:memory:", tmp_path / "backups", base_dir=tmp_path)


def test_missing_db_file_is_an_error(tmp_path):
    """Нет файла — нет бэкапа, с путём в сообщении."""
    with pytest.raises(BackupError, match="no-such.db"):
        make_backup(
            "sqlite+aiosqlite:///./no-such.db", tmp_path / "backups",
            base_dir=tmp_path,
        )


def test_prune_keeps_newest(tmp_path):
    """Глубина: свежие keep остаются, старые удаляются."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for stamp in ("20260101-040000", "20260102-040000", "20260103-040000"):
        (backup_dir / f"tgf-backup-{stamp}.tar.gz").write_bytes(b"x")

    removed = prune_backups(backup_dir, keep=2)

    assert [path.name for path in removed] == ["tgf-backup-20260101-040000.tar.gz"]
    assert [info.path.name for info in list_backups(backup_dir)] == [
        "tgf-backup-20260103-040000.tar.gz",
        "tgf-backup-20260102-040000.tar.gz",
    ]


def test_sqlite_path_forms(tmp_path):
    """Разбираем все формы sqlite-URL, чужие — в None."""
    base = tmp_path
    assert sqlite_path_from_url("sqlite+aiosqlite:///./data/app.db", base) == (
        base / "data" / "app.db"
    )
    assert sqlite_path_from_url("sqlite:////var/x.db", base) == Path("/var/x.db")
    assert sqlite_path_from_url("sqlite:///rel.db", base) == base / "rel.db"
    assert sqlite_path_from_url("sqlite:///:memory:", base) is None
    assert sqlite_path_from_url("postgresql+asyncpg://u:p@h/db", base) is None


def test_list_empty_dir_is_empty(tmp_path):
    assert list_backups(tmp_path / "nope") == []
