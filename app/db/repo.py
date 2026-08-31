"""Репозиторий: типовые запросы к БД."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import (
    CollectedItem,
    ForwardLog,
    Payment,
    PendingLogin,
    Rule,
    Subscription,
    TelegramAccount,
    User,
)
from app.timeutil import utcnow  # реэкспорт: repo.utcnow() остаётся рабочим


# ──────────────────────────────── Пользователи ────────────────────────────────


async def get_user(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def get_or_create_user(
    session: AsyncSession,
    user_id: int,
    username: str | None = None,
    full_name: str | None = None,
) -> tuple[User, bool]:
    """Возвращает (пользователь, создан_ли_впервые)."""
    user = await session.get(User, user_id)
    if user is not None:
        user.username = username or user.username
        user.full_name = full_name or user.full_name
        await session.flush()
        return user, False

    user = User(
        id=user_id,
        username=username,
        full_name=full_name,
        is_admin=user_id in settings.admin_ids,
    )
    session.add(user)
    await session.flush()
    return user, True


async def count_users(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(User))
    return int(result.scalar() or 0)


async def list_user_ids(session: AsyncSession) -> Sequence[int]:
    result = await session.execute(select(User.id).where(User.is_banned.is_(False)))
    return result.scalars().all()


# ───────────────────────────────── Подписки ───────────────────────────────────


async def get_subscription(session: AsyncSession, user_id: int) -> Subscription | None:
    return await session.get(Subscription, user_id)


def _is_active(sub: Subscription | None, now: datetime) -> bool:
    return sub is not None and sub.active_until > now


async def has_active_subscription(session: AsyncSession, user_id: int) -> bool:
    sub = await session.get(Subscription, user_id)
    return _is_active(sub, utcnow())


async def subscription_until(session: AsyncSession, user_id: int) -> datetime | None:
    sub = await session.get(Subscription, user_id)
    if _is_active(sub, utcnow()):
        return sub.active_until  # type: ignore[union-attr]
    return None


async def activate_subscription(
    session: AsyncSession, user_id: int, months: int = 1
) -> datetime:
    """Продлевает подписку. Возвращает новую дату окончания."""
    now = utcnow()
    sub = await session.get(Subscription, user_id)
    base = max(now, sub.active_until) if sub and sub.active_until > now else now
    new_until = base + timedelta(days=30 * months)

    if sub is None:
        sub = Subscription(user_id=user_id, active_until=new_until)
        session.add(sub)
    else:
        sub.active_until = new_until
        sub.reminded_at = None
    await session.flush()
    return new_until


async def grant_trial(session: AsyncSession, user_id: int) -> datetime | None:
    """Пробный период, если он включён в настройках и ещё не выдавался."""
    if settings.trial_days <= 0:
        return None
    if await session.get(Subscription, user_id) is not None:
        return None
    until = utcnow() + timedelta(days=settings.trial_days)
    session.add(Subscription(user_id=user_id, active_until=until))
    await session.flush()
    return until


async def count_active_subscriptions(session: AsyncSession) -> int:
    now = utcnow()
    result = await session.execute(
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.active_until > now)
    )
    return int(result.scalar() or 0)


async def expiring_soon(session: AsyncSession) -> Sequence[Subscription]:
    """Подписки, которые истекут в течение суток и по которым ещё не напоминали."""
    now = utcnow()
    threshold = now + timedelta(days=settings.renew_remind_days)
    result = await session.execute(
        select(Subscription).where(
            Subscription.active_until > now,
            Subscription.active_until <= threshold,
            Subscription.reminded_at.is_(None),
        )
    )
    return result.scalars().all()


async def expired_subscriptions(session: AsyncSession) -> Sequence[Subscription]:
    now = utcnow()
    result = await session.execute(
        select(Subscription).where(Subscription.active_until <= now)
    )
    return result.scalars().all()


async def mark_reminded(session: AsyncSession, user_id: int) -> None:
    sub = await session.get(Subscription, user_id)
    if sub is not None:
        sub.reminded_at = utcnow()
        await session.flush()


async def bank_days(session: AsyncSession, user_id: int, days: int) -> int:
    """Замораживает дни: снимает с активного периода и кладёт в копилку.

    Всегда оставляет хотя бы сутки активного периода, иначе абонемент бы
    «выключился» в момент заморозки. Возвращает, сколько дней реально ушло
    в копилку.
    """
    if days <= 0:
        return 0
    sub = await session.get(Subscription, user_id)
    if sub is None:
        return 0

    remaining_days = (sub.active_until - utcnow()).total_seconds() / 86400
    movable = int(remaining_days) - 1  # сутки оставляем активными
    moved = max(0, min(days, movable))
    if not moved:
        return 0

    sub.active_until = sub.active_until - timedelta(days=moved)
    sub.banked_days += moved
    await session.flush()
    return moved


async def unbank_days(session: AsyncSession, user_id: int, days: int) -> int:
    """Распределяет дни из копилки обратно в активный период.

    days <= 0 означает «вернуть всё». Возвращает, сколько дней вернулось.
    """
    sub = await session.get(Subscription, user_id)
    if sub is None:
        return 0

    moved = sub.banked_days if days <= 0 else min(days, sub.banked_days)
    if moved <= 0:
        return 0

    now = utcnow()
    base = max(now, sub.active_until) if sub.active_until and sub.active_until > now else now
    sub.active_until = base + timedelta(days=moved)
    sub.banked_days -= moved
    sub.reminded_at = None
    await session.flush()
    return moved


# ──────────────────────────────── Аккаунты ────────────────────────────────────


async def list_accounts(session: AsyncSession, user_id: int) -> Sequence[TelegramAccount]:
    result = await session.execute(
        select(TelegramAccount).where(TelegramAccount.user_id == user_id)
    )
    return result.scalars().all()


async def get_account(
    session: AsyncSession, account_id: int, user_id: int
) -> TelegramAccount | None:
    result = await session.execute(
        select(TelegramAccount).where(
            TelegramAccount.id == account_id, TelegramAccount.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def add_account(
    session: AsyncSession,
    user_id: int,
    phone: str,
    session_encrypted: str,
) -> TelegramAccount:
    account = TelegramAccount(
        user_id=user_id, phone=phone, session_encrypted=session_encrypted
    )
    session.add(account)
    await session.flush()
    return account


async def set_account_error(
    session: AsyncSession, account: TelegramAccount, error: str | None
) -> None:
    account.last_error = error
    account.is_active = error is None
    await session.flush()


async def all_active_accounts(session: AsyncSession) -> Sequence[TelegramAccount]:
    result = await session.execute(
        select(TelegramAccount).where(TelegramAccount.is_active.is_(True))
    )
    return result.scalars().all()


# ────────────────────────────────── Правила ───────────────────────────────────


async def list_rules(
    session: AsyncSession, user_id: int, include_archived: bool = True
) -> Sequence[Rule]:
    """Правила пользователя. Архив по умолчанию включён — так было и раньше."""
    query = select(Rule).where(Rule.user_id == user_id)
    if not include_archived:
        query = query.where(Rule.archived.is_(False))
    result = await session.execute(query.order_by(Rule.id))
    return result.scalars().all()


async def get_rule(session: AsyncSession, rule_id: int, user_id: int) -> Rule | None:
    result = await session.execute(
        select(Rule).where(Rule.id == rule_id, Rule.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def count_rules(
    session: AsyncSession, user_id: int, include_archived: bool = True
) -> int:
    query = select(func.count()).select_from(Rule).where(Rule.user_id == user_id)
    if not include_archived:
        query = query.where(Rule.archived.is_(False))
    result = await session.execute(query)
    return int(result.scalar() or 0)


async def set_rule_archived(session: AsyncSession, rule: Rule, archived: bool) -> None:
    """Убирает задачу в архив или возвращает из него."""
    rule.archived = archived
    if archived:
        # архивная задача не должна ловить сообщения, даже если её вернут в работу
        rule.enabled = False
    await session.flush()


async def add_rule(
    session: AsyncSession,
    user_id: int,
    account_id: int,
    source_id: int,
    source_title: str,
    target_id: int,
    target_title: str,
) -> Rule:
    rule = Rule(
        user_id=user_id,
        account_id=account_id,
        source_id=source_id,
        source_title=source_title,
        target_id=target_id,
        target_title=target_title,
    )
    session.add(rule)
    await session.flush()
    return rule


async def delete_rule(session: AsyncSession, rule: Rule) -> None:
    await session.delete(rule)
    await session.flush()


async def rules_for_source(
    session: AsyncSession, account_id: int, source_id: int
) -> Sequence[Rule]:
    """Активные правила, которые слушают этот чат на этом аккаунте."""
    result = await session.execute(
        select(Rule).where(
            Rule.account_id == account_id,
            Rule.source_id == source_id,
            Rule.enabled.is_(True),
        )
    )
    return result.scalars().all()


async def bump_forwarded(session: AsyncSession, rule_id: int, count: int = 1) -> None:
    """Считает срабатывания правила. Рассылка за раз может дать несколько."""
    if count <= 0:
        return
    rule = await session.get(Rule, rule_id)
    if rule is not None:
        rule.forwarded_count += count
        await session.flush()


async def total_forwarded(session: AsyncSession) -> int:
    result = await session.execute(select(func.coalesce(func.sum(Rule.forwarded_count), 0)))
    return int(result.scalar() or 0)


# ──────────────────── Что насобирали парсер и ловец чеков ─────────────────────


async def add_collected_items(
    session: AsyncSession,
    rule_id: int,
    user_id: int,
    kind: str,
    payloads: Sequence[dict],
) -> int:
    """Сохраняет результаты задачи-сборщика. Возвращает, сколько записано."""
    added = 0
    for payload in payloads:
        session.add(
            CollectedItem(
                rule_id=rule_id, user_id=user_id, kind=kind, payload=dict(payload)
            )
        )
        added += 1
    if added:
        await session.flush()
    return added


async def list_collected_items(
    session: AsyncSession, rule_id: int, limit: int = 100
) -> Sequence[CollectedItem]:
    result = await session.execute(
        select(CollectedItem)
        .where(CollectedItem.rule_id == rule_id)
        .order_by(CollectedItem.id.desc())
        .limit(max(1, min(limit, 1000)))
    )
    return result.scalars().all()


async def count_collected_items(session: AsyncSession, rule_id: int) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(CollectedItem)
        .where(CollectedItem.rule_id == rule_id)
    )
    return int(result.scalar() or 0)


# ────────────────────────────────── Платежи ───────────────────────────────────


async def create_payment(
    session: AsyncSession,
    user_id: int,
    provider: str,
    amount: float,
    currency: str,
    months: int = 1,
    external_id: str | None = None,
    memo: str | None = None,
) -> Payment:
    payment = Payment(
        user_id=user_id,
        provider=provider,
        amount=amount,
        currency=currency,
        months=months,
        external_id=external_id,
        memo=memo,
    )
    session.add(payment)
    await session.flush()
    return payment


async def get_payment_by_memo(session: AsyncSession, memo: str) -> Payment | None:
    result = await session.execute(select(Payment).where(Payment.memo == memo))
    return result.scalar_one_or_none()


async def mark_payment_paid(session: AsyncSession, payment: Payment) -> None:
    payment.status = "paid"
    payment.paid_at = utcnow()
    await session.flush()


async def pending_payments(session: AsyncSession, provider: str) -> Sequence[Payment]:
    result = await session.execute(
        select(Payment).where(Payment.provider == provider, Payment.status == "pending")
    )
    return result.scalars().all()


# ─────────────────────────────── Незавершённый вход ───────────────────────────


async def save_pending_login(
    session: AsyncSession,
    user_id: int,
    phone: str,
    session_encrypted: str,
    phone_code_hash: str,
    stage: str = "waiting_code",
) -> PendingLogin:
    pending = await session.get(PendingLogin, user_id)
    if pending is None:
        pending = PendingLogin(user_id=user_id)
        session.add(pending)
    pending.phone = phone
    pending.session_encrypted = session_encrypted
    pending.phone_code_hash = phone_code_hash
    pending.stage = stage
    pending.created_at = utcnow()
    await session.flush()
    return pending


async def get_pending_login(session: AsyncSession, user_id: int) -> PendingLogin | None:
    return await session.get(PendingLogin, user_id)


async def delete_pending_login(session: AsyncSession, user_id: int) -> None:
    pending = await session.get(PendingLogin, user_id)
    if pending is not None:
        await session.delete(pending)
        await session.flush()


# ──────────────────────────────────── Логи ────────────────────────────────────


async def log_forward(
    session: AsyncSession,
    rule_id: int,
    user_id: int,
    source_msg_id: int,
    target_msg_id: int | None,
    status: str = "ok",
    error: str | None = None,
) -> None:
    session.add(
        ForwardLog(
            rule_id=rule_id,
            user_id=user_id,
            source_msg_id=source_msg_id,
            target_msg_id=target_msg_id,
            status=status,
            error=error,
        )
    )
    await session.flush()
