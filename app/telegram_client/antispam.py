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
