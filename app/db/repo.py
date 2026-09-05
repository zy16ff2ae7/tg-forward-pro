"""Репозиторий: типовые запросы к БД."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import (
    CollectedItem,
    ForwardLog,
    Payment,
    PendingDelivery,
    PendingLogin,
    Rule,
    SavedMessage,
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


async def add_subscription_days(
    session: AsyncSession, user_id: int, days: int
) -> datetime:
    """Добавляет к подписке ``days`` суток. Возвращает новую дату окончания.

    От ``activate_subscription`` отличается только единицей счёта: там месяцы
    тарифа, здесь сутки подарка. Точка отсчёта общая — активный абонемент
    продлевается, истёкший начинается заново от «сейчас»: иначе подарок
    достался бы прошлому и человек не увидел бы ни дня.
    """
    now = utcnow()
    sub = await session.get(Subscription, user_id)
    base = max(now, sub.active_until) if sub and sub.active_until > now else now
    new_until = base + timedelta(days=max(0, days))

    if sub is None:
        sub = Subscription(user_id=user_id, active_until=new_until)
        session.add(sub)
    else:
        sub.active_until = new_until
        sub.reminded_at = None
    await session.flush()
    return new_until


async def claim_channel_bonus(
    session: AsyncSession, user_id: int, days: int
) -> datetime | None:
    """Отмечает подарок за подписку выданным и начисляет дни.

    ``None`` — подарок уже забирали (или пользователя нет). Метку ставит
    условный UPDATE ``WHERE channel_bonus_at IS NULL``: два одновременных
    нажатия «Проверить подписку» дадут ровно одну выдачу, потому что вторым
    запросом обновлять уже нечего. Проверка «а он подписан?» живёт выше, в
    app/bonus.py: репозиторий про Telegram ничего не знает.
    """
    result = await session.execute(
        update(User)
        .where(User.id == user_id, User.channel_bonus_at.is_(None))
        .values(channel_bonus_at=utcnow())
    )
    if result.rowcount != 1:
        return None
    return await add_subscription_days(session, user_id, days)


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


# Таблицы, строки которых принадлежат задаче и без неё не имеют смысла:
# журнал пересылок, находки («Результаты») и отложенные отправки.
RULE_OWNED = (ForwardLog, CollectedItem, PendingDelivery)


async def delete_rule(session: AsyncSession, rule: Rule) -> None:
    """Удаляет задачу вместе со всем, что она за собой оставила.

    Внешнего ключа на ``rules`` у этих таблиц нет, и удаление задачи оставляло
    их строки в базе навсегда. Само по себе это был мусор, но SQLite выдаёт
    задачам id по принципу «наибольший плюс один» — без ``AUTOINCREMENT`` номер
    удалённой задачи достаётся следующей созданной. Она получала вместе с ним
    чужую историю: красный «сбой» от предшественницы и её находки в
    «Результатах». Поэтому чистим здесь, в единственном месте удаления.
    """
    rule_id = rule.id
    await session.delete(rule)
    for model in RULE_OWNED:
        await session.execute(delete(model).where(model.rule_id == rule_id))
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


# ─────────────────────── Библиотека сохранённых сообщений ─────────────────────


async def list_saved_messages(
    session: AsyncSession, user_id: int, limit: int = 100
) -> Sequence[SavedMessage]:
    result = await session.execute(
        select(SavedMessage)
        .where(SavedMessage.user_id == user_id)
        .order_by(SavedMessage.id.desc())
        .limit(max(1, min(limit, 500)))
    )
    return result.scalars().all()


async def saved_messages_by_ids(
    session: AsyncSession, user_id: int, ids: Sequence[int]
) -> list[SavedMessage]:
    """Сообщения по списку id — в том порядке, в котором их выбрал человек.

    Порядок задаёт очередь рассылки, поэтому сортировку БД здесь применять
    нельзя: восстанавливаем её по ``ids``. Чужие и удалённые id молча
    отбрасываем — задача продолжает работать на том, что осталось.
    """
    wanted = [int(value) for value in ids if value]
    if not wanted:
        return []
    result = await session.execute(
        select(SavedMessage).where(
            SavedMessage.user_id == user_id, SavedMessage.id.in_(wanted)
        )
    )
    found = {item.id: item for item in result.scalars().all()}
    return [found[key] for key in wanted if key in found]


async def add_saved_message(
    session: AsyncSession,
    user_id: int,
    title: str = "",
    text: str = "",
    chat_id: int = 0,
    message_id: int = 0,
) -> SavedMessage:
    item = SavedMessage(
        user_id=user_id,
        title=(title or "")[:128],
        text=text or "",
        chat_id=int(chat_id or 0),
        message_id=int(message_id or 0),
    )
    session.add(item)
    await session.flush()
    return item


async def get_saved_message(
    session: AsyncSession, item_id: int, user_id: int
) -> SavedMessage | None:
    item = await session.get(SavedMessage, item_id)
    if item is None or item.user_id != user_id:
        return None
    return item


async def delete_saved_message(session: AsyncSession, item: SavedMessage) -> None:
    await session.delete(item)
    await session.flush()


async def count_saved_messages(session: AsyncSession, user_id: int) -> int:
    result = await session.execute(
        select(func.count()).select_from(SavedMessage).where(SavedMessage.user_id == user_id)
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


async def payment_with_tx(session: AsyncSession, tx_id: str) -> Payment | None:
    """Платёж, уже закрытый этой транзакцией блокчейна (защита от двойного зачёта)."""
    result = await session.execute(select(Payment).where(Payment.tx_id == tx_id))
    return result.scalars().first()


async def reserved_memos(session: AsyncSession, provider: str) -> set[str]:
    """Метки-суммы, которые уже заняты ожидающими платежами этого провайдера.

    Сумма-метка — единственное, чем мы отличаем один перевод от другого, поэтому
    двум одновременно висящим платежам одинаковую сумму давать нельзя: перевод
    зачли бы не тому. Берём только ``pending`` — закрытые платежи метку
    освобождают, их защищает уже ``tx_id``.
    """
    result = await session.execute(
        select(Payment.memo).where(
            Payment.provider == provider,
            Payment.status == "pending",
            Payment.memo.is_not(None),
        )
    )
    return {memo for memo in result.scalars().all() if memo}


async def mark_payment_paid(session: AsyncSession, payment: Payment) -> None:
    payment.status = "paid"
    payment.paid_at = utcnow()
    await session.flush()


async def claim_payment(
    session: AsyncSession, payment: Payment, *, tx_id: str | None = None
) -> bool:
    """Забирает платёж себе: pending → paid. False — его уже закрыл кто-то другой.

    Начисление подписки должно случиться ровно один раз, а закрыть платёж могут
    сразу двое: пользователь нажал «Проверить оплату» и в этот же момент по нему
    прошёл фоновый цикл. Раньше проверка была «прочитали status, увидели pending,
    начислили» — между чтением и записью влезал второй, и месяц начислялся дважды.

    Перевод состояния — одним ``UPDATE ... WHERE status = 'pending'``: СУБД
    гарантирует, что строку заберёт только один, а ``rowcount`` говорит, кто это
    был. Начислять подписку имеет право только тот, кому вернули True.
    """
    values: dict = {"status": "paid", "paid_at": utcnow()}
    if tx_id:
        values["tx_id"] = tx_id
    result = await session.execute(
        update(Payment)
        .where(Payment.id == payment.id, Payment.status == "pending")
        .values(**values)
    )
    if (result.rowcount or 0) != 1:
        return False
    # В объекте в памяти остались старые значения — подтягиваем записанные.
    await session.refresh(payment)
    return True


async def pending_payments(session: AsyncSession, provider: str) -> Sequence[Payment]:
    result = await session.execute(
        select(Payment).where(Payment.provider == provider, Payment.status == "pending")
    )
    return result.scalars().all()


async def count_pending_payments(session: AsyncSession, user_id: int, provider: str) -> int:
    """Сколько неоплаченных счетов уже висит у пользователя по этому способу.

    Нужно, чтобы страница оплаты не плодила счёта без счёта: каждый USDT-счёт
    занимает уникальную метку-сумму, а свободных меток конечное число.
    """
    result = await session.execute(
        select(func.count(Payment.id)).where(
            Payment.user_id == user_id,
            Payment.provider == provider,
            Payment.status == "pending",
        )
    )
    return int(result.scalar_one() or 0)


async def expire_stale_payments(
    session: AsyncSession, provider: str, *, older_than: timedelta
) -> int:
    """Закрывает брошенные счёта: pending → expired. Возвращает их количество.

    Счёт, по которому не заплатили, иначе висит вечно: фоновый цикл каждые пять
    минут спрашивает про него провайдера, метка-сумма остаётся занятой, а лимит
    висящих счетов (см. app/payments/service.py) со временем запирает человека
    без возможности выставить новый.

    Одним ``UPDATE``, без вычитки строк: счетов может накопиться много, а
    интересует нас только сам факт закрытия.
    """
    cutoff = utcnow() - older_than
    result = await session.execute(
        update(Payment)
        .where(
            Payment.provider == provider,
            Payment.status == "pending",
            Payment.created_at < cutoff,
        )
        .values(status="expired")
    )
    return int(result.rowcount or 0)


# ─────────────────────── Отправки, ждущие доведения до конца ──────────────────

# Дольше суток восстанавливать бессмысленно: в источнике пост уже неактуален,
# а «переслали вчерашнее» выглядит хуже, чем «не переслали».
PENDING_DELIVERY_MAX_AGE_HOURS = 24
PENDING_DELIVERY_RESTORE_LIMIT = 500


async def remember_pending_delivery(
    session: AsyncSession,
    *,
    rule_id: int,
    user_id: int,
    account_id: int,
    source_chat_id: int,
    message_id: int,
    delay_seconds: int = 0,
) -> int:
    """Записывает отправку как незавершённую и возвращает id записи.

    Повторный вызов с той же тройкой (правило, чат, сообщение) не создаёт вторую
    строку, а отдаёт уже существующую: иначе восстановление после перезапуска
    размножало бы отправки. Уникальный индекс ``ux_pending_delivery_msg`` держит
    это же правило на уровне БД.
    """
    existing = await session.execute(
        select(PendingDelivery.id).where(
            PendingDelivery.rule_id == rule_id,
            PendingDelivery.source_chat_id == source_chat_id,
            PendingDelivery.message_id == message_id,
        )
    )
    found = existing.scalars().first()
    if found is not None:
        return int(found)

    row = PendingDelivery(
        rule_id=rule_id,
        user_id=user_id,
        account_id=account_id,
        source_chat_id=source_chat_id,
        message_id=message_id,
        due_at=utcnow() + timedelta(seconds=max(0, int(delay_seconds))),
    )
    session.add(row)
    await session.flush()
    return int(row.id)


async def due_pending_deliveries(
    session: AsyncSession,
    *,
    limit: int = PENDING_DELIVERY_RESTORE_LIMIT,
    max_age_hours: int = PENDING_DELIVERY_MAX_AGE_HOURS,
) -> Sequence[PendingDelivery]:
    """Незавершённые отправки, которые ещё имеет смысл досылать.

    Порядок — по времени отправки: то, что должно было уйти раньше, уходит
    первым. Ограничение по количеству нужно, чтобы после долгого простоя
    восстановление не выплюнуло в Telegram тысячи сообщений разом.
    """
    threshold = utcnow() - timedelta(hours=max(1, int(max_age_hours)))
    result = await session.execute(
        select(PendingDelivery)
        .where(PendingDelivery.created_at >= threshold)
        .order_by(PendingDelivery.due_at)
        .limit(max(1, int(limit)))
    )
    return result.scalars().all()


async def drop_stale_pending_deliveries(
    session: AsyncSession, *, max_age_hours: int = PENDING_DELIVERY_MAX_AGE_HOURS
) -> int:
    """Убирает записи, которые уже поздно досылать. Возвращает число удалённых."""
    threshold = utcnow() - timedelta(hours=max(1, int(max_age_hours)))
    result = await session.execute(
        delete(PendingDelivery).where(PendingDelivery.created_at < threshold)
    )
    await session.flush()
    return int(result.rowcount or 0)


async def delete_pending_delivery(session: AsyncSession, delivery_id: int) -> None:
    await session.execute(
        delete(PendingDelivery).where(PendingDelivery.id == delivery_id)
    )
    await session.flush()


async def count_pending_deliveries(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(PendingDelivery))
    return int(result.scalar() or 0)


# ─────────────────────────────── Незавершённый вход ───────────────────────────


async def save_pending_login(
    session: AsyncSession,
    user_id: int,
    phone: str,
    session_encrypted: str,
    phone_code_hash: str,
    stage: str = "waiting_code",
    attempts: int = 0,
) -> PendingLogin:
    pending = await session.get(PendingLogin, user_id)
    if pending is None:
        pending = PendingLogin(user_id=user_id)
        session.add(pending)
    pending.phone = phone
    pending.session_encrypted = session_encrypted
    pending.phone_code_hash = phone_code_hash
    pending.stage = stage
    pending.attempts = int(attempts)
    pending.created_at = utcnow()
    await session.flush()
    return pending


async def bump_login_attempts(session: AsyncSession, user_id: int) -> int:
    """Отмечает неудачную попытку кода. Возвращает новое число попыток.

    ``created_at`` намеренно не трогаем: это время отправки кода, по нему
    считается пауза до повторного запроса.
    """
    pending = await session.get(PendingLogin, user_id)
    if pending is None:
        return 0
    pending.attempts = int(pending.attempts or 0) + 1
    await session.flush()
    return int(pending.attempts)


async def get_pending_login(session: AsyncSession, user_id: int) -> PendingLogin | None:
    return await session.get(PendingLogin, user_id)


async def delete_pending_login(session: AsyncSession, user_id: int) -> None:
    pending = await session.get(PendingLogin, user_id)
    if pending is not None:
        await session.delete(pending)
        await session.flush()


# ──────────────────────────────────── Логи ────────────────────────────────────


# Журнал держим месяц: он нужен, чтобы ответить «работает ли задача и на чём
# сломалась», а не быть вечным архивом. Одна рассылка пишет строку на каждую
# отправку, поэтому без чистки таблица растёт быстрее всех остальных.
FORWARD_LOG_TTL_DAYS = 30


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
            # Причина сбоя приходит из чужих исключений: обрезаем на входе, иначе
            # в базу уйдёт простыня, которую всё равно никто не прочитает.
            error=error[:1000] if error else None,
        )
    )
    await session.flush()


async def task_health(
    session: AsyncSession, rule_ids: Sequence[int]
) -> dict[int, dict]:
    """Чем закончились последние срабатывания задач: ``{rule_id: {...}}``.

    На каждую задачу: ``ok_at`` — когда последний раз сработала, ``error`` и
    ``error_at`` — последний сбой, ``failing`` — сломана ли она **сейчас**
    (после сбоя не было ни одного успеха). Без последнего признака старая
    ошибка вечно висела бы на карточке уже починенной задачи.

    Два запроса на любое число задач: список задач кабинета читается одним
    ответом, и запрос на правило превратил бы его в двадцать походов в базу.
    Задачи без журнала в ответе не появляются — вызывающий разбирает это
    как «сбоев не было».
    """
    ids = [int(value) for value in rule_ids if value]
    if not ids:
        return {}

    rows = await session.execute(
        select(
            ForwardLog.rule_id,
            func.max(ForwardLog.id),
            func.max(ForwardLog.created_at),
        )
        .where(ForwardLog.rule_id.in_(ids), ForwardLog.status == "ok")
        .group_by(ForwardLog.rule_id)
    )
    health: dict[int, dict] = {}
    last_ok: dict[int, int] = {}
    for rule_id, log_id, created_at in rows:
        last_ok[int(rule_id)] = int(log_id or 0)
        health[int(rule_id)] = {
            "ok_at": created_at,
            "error": None,
            "error_at": None,
            "failing": False,
        }

    # Последний сбой каждой задачи: строку выбираем по наибольшему id, а не по
    # времени, — id растёт монотонно, а две записи одной секунды по времени
    # неразличимы.
    newest = (
        select(func.max(ForwardLog.id))
        .where(ForwardLog.rule_id.in_(ids), ForwardLog.status != "ok")
        .group_by(ForwardLog.rule_id)
    )
    errors = await session.execute(select(ForwardLog).where(ForwardLog.id.in_(newest)))
    for log in errors.scalars():
        entry = health.setdefault(
            log.rule_id, {"ok_at": None, "error": None, "error_at": None, "failing": False}
        )
        entry["error"] = log.error or "неизвестная ошибка"
        entry["error_at"] = log.created_at
        entry["failing"] = log.id > last_ok.get(log.rule_id, 0)
    return health


async def trim_forward_logs(
    session: AsyncSession, *, older_than_days: int = FORWARD_LOG_TTL_DAYS
) -> int:
    """Убирает старые записи журнала. Возвращает число удалённых.

    Одним ``DELETE``, без вычитки строк: их может быть много, а интересен
    только сам факт чистки — для журнала в логе службы.
    """
    cutoff = utcnow() - timedelta(days=max(1, int(older_than_days)))
    result = await session.execute(
        delete(ForwardLog).where(ForwardLog.created_at < cutoff)
    )
    await session.flush()
    return int(result.rowcount or 0)


async def drop_orphan_records(session: AsyncSession) -> dict[str, int]:
    """Убирает строки, чья задача уже удалена. Возвращает ``{таблица: сколько}``.

    Удаление задачи чистит их само (см. :func:`delete_rule`), но в базах, где
    задачи удаляли до этого, мусор уже лежит — и достанется следующей задаче с
    тем же номером. Поэтому проход зовётся из фонового цикла: базы вылечиваются
    сами, без ручных запросов на сервере.
    """
    alive = select(Rule.id)
    dropped: dict[str, int] = {}
    for model in RULE_OWNED:
        result = await session.execute(
            delete(model).where(model.rule_id.not_in(alive))
        )
        if result.rowcount:
            dropped[model.__tablename__] = int(result.rowcount)
    await session.flush()
    return dropped
