"""Антиспам-защита: как распознаём ограничения Telegram и что делаем в ответ.

``FloodWait`` (пауза в секундах) код соблюдал всегда, а вот остальные сигналы
ограничений раньше падали в общую кучу «ошибка отправки» — и задача продолжала
долбить с паузой 30 секунд. Временное ограничение от такого превращается в
полноценный спамблок, а спамблок — в заморозку. Поэтому сигналы делятся на три
сорта с тремя разными реакциями:

* ``PeerFloodError`` — аккаунт уже помечен за спам. Реакция — «рубильник» на
  весь аккаунт (``manager.note_peer_flood``): все отправки встают на
  ``PEER_FLOOD_PAUSE_HOURS``, ретраев нет вообще.
* ``SlowModeWaitError`` — медленный режим чата. Реакция — как на ``FloodWait``:
  ждём ровно столько, сколько просят (у ошибки тоже есть ``seconds``).
* мёртвая сессия (ключ отозван, номер забанен или удалён) — ретраить нечего:
  аккаунт гасится с причиной «подключите заново», а не долбится вечно.

Здесь только распознавание и константы темпа. Рубильник и гашение живут в
менеджере: только он видит все задачи аккаунта разом.
"""
from __future__ import annotations

import hashlib
import secrets

from telethon.errors import (
    AuthKeyDuplicatedError,
    PeerFloodError,
    PhoneNumberBannedError,
    SessionRevokedError,
    SlowModeWaitError,
    UserDeactivatedError,
)

# На сколько встают все отправки аккаунта после PeerFlood. Ограничение за спам
# держится часами, дёргать раньше — продлевать его.
PEER_FLOOD_PAUSE_HOURS = 12

# Пауза веера между чатами. Раньше веер слал в сотни чатов со скоростью сети —
# быстрее и опаснее любой рассылки.
BROADCAST_CHAT_GAP = 5.0

# Вступления: пол, умолчания и потолок. Залп вступлений с паузой в пару секунд —
# ботнет-паттерн, за который аккаунты мёрзнут пачками.
JOIN_MIN_GAP = 30  # быстрее вступать нельзя, даже если очень хочется
JOIN_DEFAULT_GAP = 60  # пауза между вступлениями у новой задачи
JOIN_DEFAULT_LIMIT = 10  # вступлений за один запуск у новой задачи
JOIN_DEFAULT_DAILY = 10  # вступлений в сутки на задачу у новой задачи
# Потолок на аккаунт поверх задач: три задачи с лимитом 10 — это всё равно
# не 30 вступлений в сутки, а 20.
ACCOUNT_DAILY_JOIN_CAP = 20

# Ключ мёртв: сессию отозвали, вошли тем же ключом с другой машины.
DEAD_SESSION_REVOKED: tuple[type, ...] = (
    AuthKeyDuplicatedError,
    SessionRevokedError,
)
# Номера больше нет: забанен или удалён вместе с аккаунтом.
DEAD_SESSION_BANNED: tuple[type, ...] = (
    UserDeactivatedError,
    PhoneNumberBannedError,
)
DEAD_SESSION_ERRORS: tuple[type, ...] = (
    *DEAD_SESSION_REVOKED,
    *DEAD_SESSION_BANNED,
)


def is_peer_flood(exc: BaseException) -> bool:
    """Аккаунт помечен за спам: слать и вступать сейчас нельзя."""
    return isinstance(exc, PeerFloodError)


def slowmode_seconds(exc: BaseException) -> int | None:
    """Сколько просит подождать медленный режим чата. Не он — None."""
    if isinstance(exc, SlowModeWaitError):
        try:
            return max(0, int(getattr(exc, "seconds", 0) or 0))
        except (TypeError, ValueError):
            return 0
    return None


def dead_session_kind(exc: BaseException) -> str | None:
    """Мёртвая сессия: «revoked» (ключ отозван) или «banned» (номера нет).

    Всё остальное — живая сессия с временной бедой: сеть, права, лимиты.
    """
    if isinstance(exc, DEAD_SESSION_BANNED):
        return "banned"
    if isinstance(exc, DEAD_SESSION_REVOKED):
        return "revoked"
    return None
# Отпечатки устройства: у каждого аккаунта свой. Раньше все клиенты сервиса
# представлялись одинаково (MacBook Pro/macOS/1.0) — одинаковый api_id + IP +
# отпечаток у десятков номеров, и Telegram кластеризует их как одну ферму:
# спам одного пользователя бросает тень на всех. Поэтому рабочий клиент
# аккаунта берёт отпечаток из пула по его номеру телефона: у разных номеров —
# разные, у одного номера — один и тот же при каждом перезапуске. Только
# десктопы: сервис и так ходит с серверных IP, мобильные отпечатки оттуда
# выглядели бы ещё подозрительнее.
DEVICE_FINGERPRINTS: tuple[dict[str, str], ...] = (
    {"device_model": "PC 64bit", "system_version": "Windows 11", "app_version": "5.12.3"},
    {"device_model": "PC 64bit", "system_version": "Windows 10", "app_version": "5.10.7"},
    {"device_model": "Desktop", "system_version": "Ubuntu 24.04", "app_version": "5.12.3"},
    {"device_model": "Desktop", "system_version": "Ubuntu 22.04", "app_version": "5.9.1"},
    {"device_model": "Desktop", "system_version": "Debian 12", "app_version": "5.11.2"},
    {"device_model": "MacBook Pro", "system_version": "macOS 15", "app_version": "5.12.3"},
    {"device_model": "MacBook Air", "system_version": "macOS 14", "app_version": "5.10.0"},
    {"device_model": "PC 64bit", "system_version": "Fedora 41", "app_version": "5.11.0"},
    {"device_model": "Desktop", "system_version": "Arch Linux", "app_version": "5.12.1"},
    {"device_model": "PC 64bit", "system_version": "Windows 11", "app_version": "4.16.8"},
)


def device_fingerprint(seed: str) -> dict[str, str]:
    """Отпечаток аккаунта по строке-сиду (обычно номер телефона).

    Выбор детерминированный: один сид — один отпечаток навсегда, хранить в
    базе нечего. Возвращаем копию: словарь уходит в конструктор клиента, и
    чужое изменение пула нам не нужно.
    """
    digest = hashlib.sha256(str(seed or "").encode()).digest()
    pick = int.from_bytes(digest[:4], "big") % len(DEVICE_FINGERPRINTS)
    return dict(DEVICE_FINGERPRINTS[pick])


def random_fingerprint() -> dict[str, str]:
    """Случайный отпечаток — когда сида ещё нет (QR-вход до сканирования)."""
    return dict(secrets.choice(DEVICE_FINGERPRINTS))
