"""Модели БД сервиса автопересылки."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.timeutil import utcnow as _utcnow


class User(Base):
    """Пользователь сервиса (Telegram-аккаунт, который управляет пересылкой)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram user_id
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    accounts: Mapped[list["TelegramAccount"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    rules: Mapped[list["Rule"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    subscription: Mapped["Subscription | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )

    @property
    def mention(self) -> str:
        if self.username:
            return "@" + self.username
        return self.full_name or str(self.id)


class TelegramAccount(Base):
    """Подключённый личный Telegram-аккаунт (юзебот) для чтения источников."""

    __tablename__ = "telegram_accounts"
    __table_args__ = (Index("ix_accounts_user", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    # Telethon StringSession, зашифрован Fernet-ключом из SECRET_KEY
    session_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="accounts")
    rules: Mapped[list["Rule"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class Rule(Base):
    """Правило пересылки: из какого чата в какой и как именно."""

    __tablename__ = "rules"
    __table_args__ = (
        Index("ix_rules_user", "user_id"),
        Index("ix_rules_account_source", "account_id", "source_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("telegram_accounts.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_title: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_title: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # copy — публикуем как своё (без метки «Переслано от»), forward — обычный форвард
    mode: Mapped[str] = mapped_column(String(16), default="copy", nullable=False)
    # Чем занимается задача. forward — обычная пересылка (исторически первая),
    # остальные значения разбирает app.telegram_client.jobs
    kind: Mapped[str] = mapped_column(String(24), default="forward", nullable=False)
    # Архив: задача выполнена и убрана из рабочих списков, но не удалена
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    delay_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Настройки фильтров и преобразований (см. app.telegram_client.filters)
    filters: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    forwarded_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    user: Mapped["User"] = relationship(back_populates="rules")
    account: Mapped["TelegramAccount"] = relationship(back_populates="rules")


class Subscription(Base):
    """Абонемент пользователя: до какой даты активен доступ."""

    __tablename__ = "subscriptions"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    active_until: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Копилка: дни, снятые с активного периода и ждущие распределения.
    # В отличие от active_until они не «горят» — не привязаны к конкретной дате.
    banked_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="subscription")


class Payment(Base):
    """Платёж за абонемент (любой из провайдеров)."""

    __tablename__ = "payments"
    __table_args__ = (Index("ix_payments_user", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # stars | yookassa | usdt | manual
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)  # XTR | RUB | USDT
    months: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # pending | paid | failed | expired
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Для USDT: уникальная сумма-метка, по которой ищем перевод
    memo: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="payments")


class ForwardLog(Base):
    """Журнал пересылок: что, куда и чем закончилось."""

    __tablename__ = "forward_logs"
    __table_args__ = (Index("ix_logs_rule", "rule_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_msg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="ok", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class CollectedItem(Base):
    """То, что насобирали задачи-сборщики: парсер аудитории и ловец чеков.

    Храним одной таблицей на оба типа: структура результата кладётся в payload,
    поэтому новые поля не требуют миграций.
    """

    __tablename__ = "collected_items"
    __table_args__ = (
        Index("ix_collected_rule", "rule_id"),
        Index("ix_collected_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # parser | checks — по чему этот результат собран
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    # У парсера: {"user_id", "username", "name", "phone"}.
    # У ловца чеков: {"chat_id", "message_id", "link", "amount", "text"}.
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class PendingLogin(Base):
    """Незавершённый вход по номеру телефона (код/2FA)."""

    __tablename__ = "pending_logins"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    session_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    phone_code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    # waiting_code | waiting_password
    stage: Mapped[str] = mapped_column(String(32), default="waiting_code", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
