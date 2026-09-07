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
    # Когда человек забрал подарок за подписку на канал. Метка одна на аккаунт:
    # подарок разовый, и отписка-подписка второго раза не даёт.
    channel_bonus_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

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
    # Когда владельцу сказали, что аккаунт выпал. Беда одна, а фоновый цикл
    # ходит каждые пять минут: без метки человек получал бы одно и то же
    # сообщение до самого повторного входа. Удачный вход метку снимает — о
    # следующем таком случае надо сказать снова.
    error_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

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
    # Когда начался текущий непрерывный доступ. Нужен напоминанию «скоро конец»:
    # без него оно смотрело только на остаток и у трёхдневного пробного периода
    # срабатывало в первую же минуту — «продлевайте» приходило вместе с
    # «здравствуйте». Пусто у строк, созданных до этой колонки.
    period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Когда сказали, что срок вышел и задачи встали. Одна беда — одно письмо;
    # продление метку снимает, чтобы о следующем конце сказать снова.
    expired_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
    __table_args__ = (
        Index("ix_payments_user", "user_id"),
        # Один перевод в блокчейне — один зачёт. Уникальный индекс по хешу
        # транзакции не даст засчитать один и тот же перевод дважды даже при
        # гонке двух проверок. NULL в SQLite/Postgres не конфликтуют между собой,
        # поэтому платежи без tx_id (звёзды, карта) индекс не трогает.
        Index("ux_payments_tx", "tx_id", unique=True),
    )

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
    # Для USDT: хеш транзакции, которой закрыт платёж
    tx_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
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


class JoinLog(Base):
    """Вступления автоподписки — по строке на вступление.

    Отдельно от журнала: у разового запуска в журнале одна пара
    «сбой + успех», а не строка на каждый чат (иначе сотня ссылок
    вымывала бы из журнала остальные задачи). По этим строкам считается
    дневной лимит вступлений; вчерашние стираются при записи новых.
    """

    __tablename__ = "join_logs"
    __table_args__ = (Index("ix_joins_rule", "rule_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
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


class SavedMessage(Base):
    """Сохранённое сообщение из библиотеки: то, что рассылка отправляет в чаты.

    Два вида записи в одной таблице:

    * свой текст — ``text`` заполнен, ``chat_id``/``message_id`` нулевые;
    * ссылка на готовое сообщение — ``chat_id``/``message_id`` указывают на пост
      в чате пользователя. Такое сообщение рассылка перечитывает через Telethon
      и копирует целиком, поэтому медиа и вложенные пересылки сохраняются как
      есть. Копию медиа у себя не держим — это чужой контент и лишний вес.
    """

    __tablename__ = "saved_messages"
    __table_args__ = (Index("ix_saved_messages_user", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Откуда взять сообщение целиком (0 — сообщение задано текстом)
    chat_id: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class PendingDelivery(Base):
    """Отправка, поставленная в очередь, но ещё не доведённая до конца.

    Очередь доставки живёт в памяти процесса: при перезапуске (деплой, падение,
    ``systemctl restart``) всё, что стояло в ней и в отложенных задержках,
    исчезало без следа — сообщение просто не доезжало, и пользователь узнавал
    об этом сам. Здесь лежат ссылки на исходные сообщения; после старта очередь
    перечитывает их из источника и досылает.

    Текст и медиа сознательно не храним: это чужие переписки, и копия в нашей
    базе — лишний риск. Достаточно ``(source_chat_id, message_id)``.
    """

    __tablename__ = "pending_deliveries"
    __table_args__ = (
        Index("ix_pending_delivery_due", "due_at"),
        Index("ix_pending_delivery_account", "account_id"),
        # Один и тот же пост по одному и тому же правилу — одна отправка.
        # Иначе повторный запуск восстановления удвоил бы сообщения.
        Index("ux_pending_delivery_msg", "rule_id", "source_chat_id", "message_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Когда отправлять: сейчас или после задержки из правила
    due_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
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
    # Сколько раз код не подошёл. Опечатка в цифре — обычное дело, поэтому вход
    # из-за неё не сбрасывается; счётчик нужен, чтобы перебор кода не был
    # бесконечным. Лежит в БД, а не в памяти: шаг входа переживает перезапуск.
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class PhoneCodeSend(Base):
    """Когда номеру последний раз уходил код входа.

    Пауза между запросами кода жила в ``pending_logins.created_at``, а «Отмена»
    эту строку удаляет — и следующий запрос уходил в Telegram сразу. В боевом
    журнале так и вышло: один номер получил три кода за 43 секунды. Лимит висит
    на самом номере, а не на человеке, поэтому и помним по номеру: отмена,
    другой пользователь и перезапуск сервиса паузу не обнуляют.

    Таблица короткоживущая: метки старше часа удаляются при следующей записи.
    """

    __tablename__ = "phone_code_sends"

    phone: Mapped[str] = mapped_column(String(32), primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
