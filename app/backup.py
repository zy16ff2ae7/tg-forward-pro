"""Бэкапы базы: зашифрованные копии по таймеру и восстановление из них.

В базе лежат зашифрованные сессии чужих Telegram-аккаунтов и платежи, а сам
ключ шифрования — только в ``.env`` на сервере. Поэтому бэкап без шифрования
хуже, чем его отсутствие: это готовая копия доступов. Архив шифруется
паролем из ``BACKUP_PASSPHRASE`` (ключ — PBKDF2, соль — в заголовке файла),
пароль хранится ВНЕ сервера — иначе сгорел сервер, сгорел и бэкап.

Что и как:
* SQLite копируется через ``sqlite3.Connection.backup`` — снимок консистентен
  даже под WAL-нагрузкой, останавливать сервис не нужно;
* Postgres этим скриптом не накрывается: для него нужен ``pg_dump`` —
  честно отказываемся, а не делаем вид;
* восстановление поверх живой базы — только с ``force`` и только после
  остановки сервиса: иначе WAL разойдётся с файлом.
"""
from __future__ import annotations

import base64
import io
import json
import os
import sqlite3
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# Магическое начало шифрованного файла + 16 байт соли, дальше токен Fernet.
_MAGIC = b"TGF1"
_SALT_LEN = 16
_PBKDF2_ITERS = 200_000


class BackupError(Exception):
    """Бэкап не сделан / не прочитан — с причиной человеческими словами."""


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    size: int
    created_at: datetime
    encrypted: bool


def sqlite_path_from_url(database_url: str, base_dir: Path) -> Path | None:
    """Путь к файлу SQLite из DATABASE_URL. Не SQLite — None.

    ``:memory:`` тоже None: бэкапить нечего, и это надо заметить, а не молча
    сделать пустой архив.
    """
    scheme = database_url.split(":", 1)[0].split("+")[0]
    if scheme != "sqlite":
        return None
    rest = database_url.split("://", 1)[1] if "://" in database_url else ""
    rest = rest.split("?", 1)[0]
    if rest in (":memory:", "/:memory:"):
        return None
    # Четыре слеша — абсолютный путь (sqlite:////var/x.db), три — относительный.
    if rest.startswith("//"):
        return Path("/" + rest.lstrip("/"))
    if rest.startswith("/"):
        rest = rest[1:]
    path = Path(rest)
    return path if path.is_absolute() else base_dir / path


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_PBKDF2_ITERS
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _encrypt(payload: bytes, passphrase: str) -> bytes:
    salt = os.urandom(_SALT_LEN)
    token = Fernet(_derive_key(passphrase, salt)).encrypt(payload)
    return _MAGIC + salt + token


def _decrypt(blob: bytes, passphrase: str) -> bytes:
    if not blob.startswith(_MAGIC) or len(blob) <= len(_MAGIC) + _SALT_LEN:
        raise BackupError("Файл не похож на наш шифрованный бэкап")
    salt = blob[len(_MAGIC) : len(_MAGIC) + _SALT_LEN]
    try:
        return Fernet(_derive_key(passphrase, salt)).decrypt(
            blob[len(_MAGIC) + _SALT_LEN :]
        )
    except InvalidToken:
        raise BackupError("Неверный BACKUP_PASSPHRASE — бэкап не расшифровался") from None


def _pack(db_path: Path) -> bytes:
    """База + метка — в tar.gz в памяти. База маленькая, файл не нужен."""
    meta = {
        "app": "tg-forward-pro",
        "db_name": db_path.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = db_path.read_bytes()
        info = tarfile.TarInfo(name="app.db")
        info.size = len(data)
        info.mtime = int(datetime.now(timezone.utc).timestamp())
        tar.addfile(info, io.BytesIO(data))
        meta_bytes = json.dumps(meta, ensure_ascii=False).encode("utf-8")
        minfo = tarfile.TarInfo(name="meta.json")
        minfo.size = len(meta_bytes)
        minfo.mtime = info.mtime
        tar.addfile(minfo, io.BytesIO(meta_bytes))
    return buf.getvalue()


def _unpack(payload: bytes, dest: Path) -> None:
    """tar.gz → файл базы. Атомарно: пишем рядом, потом переименовываем."""
    tmp = dest.with_name(dest.name + ".restoring")
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        member = tar.getmember("app.db")
        extracted = tar.extractfile(member)
        if extracted is None:
            raise BackupError("В архиве нет app.db — бэкап битый")
        tmp.write_bytes(extracted.read())
    os.replace(tmp, dest)


def list_backups(backup_dir: Path) -> list[BackupInfo]:
    """Все бэкапы каталога — свежие вперёд."""
    found: list[BackupInfo] = []
    if not backup_dir.is_dir():
        return found
    for path in sorted(backup_dir.glob("tgf-backup-*.tar.gz*")):
        stat = path.stat()
        found.append(
            BackupInfo(
                path=path,
                size=stat.st_size,
                created_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
                encrypted=path.suffix == ".enc",
            )
        )
    found.sort(key=lambda b: b.path.name, reverse=True)
    return found


def prune_backups(backup_dir: Path, keep: int) -> list[Path]:
    """Оставляет свежие ``keep`` архивов, остальные удаляет. Возвращает удалённые."""
    doomed = list_backups(backup_dir)[max(1, keep) :]
    for info in doomed:
        info.path.unlink(missing_ok=True)
    return [info.path for info in doomed]


def make_backup(
    database_url: str,
    backup_dir: Path,
    *,
    base_dir: Path,
    keep: int = 7,
    passphrase: str | None = None,
) -> BackupInfo:
    """Снимает бэкап, чистит старые, возвращает описание свежего."""
    db_path = sqlite_path_from_url(database_url, base_dir)
    if db_path is None:
        if urlparse(database_url).scheme.split("+")[0] == "sqlite":
            raise BackupError("База в памяти (:memory:) — бэкапить нечего")
        raise BackupError(
            "Не SQLite-база: этот скрипт накрывает только SQLite, "
            "для Postgres используйте pg_dump"
        )
    if not db_path.is_file():
        raise BackupError(f"Файла базы нет: {db_path}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    encrypted = bool(passphrase)
    name = f"tgf-backup-{stamp}.tar.gz" + (".enc" if encrypted else "")
    dest = backup_dir / name

    # Онлайн-снимок: читаем живую базу штатным API, WAL нам не мешает.
    snapshot = dest.with_name(dest.name + ".snapshot.db")
    try:
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(str(snapshot))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        payload = _pack(snapshot)
        if encrypted:
            assert passphrase is not None
            payload = _encrypt(payload, passphrase)
        dest.write_bytes(payload)
        os.chmod(dest, 0o600)
    finally:
        snapshot.unlink(missing_ok=True)

    prune_backups(backup_dir, keep)
    stat = dest.stat()
    return BackupInfo(
        path=dest,
        size=stat.st_size,
        created_at=datetime.now(timezone.utc),
        encrypted=encrypted,
    )


def restore_backup(
    archive: Path,
    db_path: Path,
    *,
    passphrase: str | None = None,
    force: bool = False,
) -> Path:
    """Разворачивает архив в файл базы. Поверх живого — только с force."""
    if not archive.is_file():
        raise BackupError(f"Архива нет: {archive}")
    if db_path.exists() and not force:
        raise BackupError(
            f"{db_path} уже существует — восстановление затёрло бы живую базу. "
            "Остановите сервис и повторите с --force"
        )
    blob = archive.read_bytes()
    if archive.suffix == ".enc":
        if not passphrase:
            raise BackupError("Бэкап шифрованный — нужен BACKUP_PASSPHRASE")
        payload = _decrypt(blob, passphrase)
    else:
        payload = blob
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _unpack(payload, db_path)
    return db_path
