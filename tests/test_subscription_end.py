"""Конец абонемента: сказать вовремя, сказать один раз и не врать до этого.

Боевая база показала три разные беды на одном сроке.

**Просят продлить в первую же минуту.** Пробный период — три дня, напоминать
велено за три: остаток меньше порога сразу, и «Абонемент заканчивается,
продлите» уходило вместе с приветствием. Семь пробных периодов из девяти
получили его, не начавшись: #7889436407 — через 1,2 секунды после ``/start``,
#7970947870 — через 22 секунды, #6628776632 — через 48, #7868642254 — через
2 минуты 6 секунд.

**Когда срок действительно кончается — тишина.** Выборка истёкших подписок в
репозитории была, но её никто не вызывал. Пять абонементов закончились один-три
дня назад, и сервис не сказал ни слова: пересылка просто перестала работать
(``forwarder`` пропускает сообщения без абонемента).

**И кабинет при этом врал.** Карточка задачи показывала «работает» ровно тогда,
когда задача стояла — тот же обман, от которого её уже отучили в случае
отключившегося аккаунта.

Здесь проверяются все три: порог напоминания, письмо о конце срока и то, что
метка в БД появляется на старом файле базы сама.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text

from app import main
from app.config import settings
from app.db import repo
from app.db.database import SessionLocal, engine, ensure_schema, session_scope
from app.db.models import Subscription
from tests.helpers import RecordingBot, add_rule


async def get_sub(user_id: int) -> Subscription:
    async with SessionLocal() as session:
        sub = await session.get(Subscription, user_id)
        assert sub is not None
        return sub


async def set_period(user_id: int, *, started_days_ago: float, days_left: float) -> None:
    """Абонемент с известными началом и остатком: ждать реального срока нечем."""
    async with session_scope() as session:
        sub = await session.get(Subscription, user_id)
        assert sub is not None
        now = repo.utcnow()
        sub.period_start = now - timedelta(days=started_days_ago)
        sub.active_until = now + timedelta(days=days_left)
        sub.reminded_at = None
        sub.expired_notified_at = None


# ───────────────── напоминание не раньше середины периода ─────────────────────


async def test_trial_does_not_ask_for_money_at_signup(create_user, monkeypatch):
    """Тот самый случай: «продлите» через 1,2 секунды после ``/start``.

    Пробный период короче порога напоминания целиком, поэтому одного условия
    «осталось меньше трёх дней» хватало, чтобы просить денег у человека,
    который ещё ничего не попробовал.
    """
    # Короткую подписку для проверки механики включаем явно: автовыдачи
    # пробного в продукте больше нет.
    monkeypatch.setattr(settings, "trial_days", 3)
    user_id = await create_user()
    async with session_scope() as session:
        await repo.grant_trial(session, user_id)
    bot = RecordingBot()

    await main.notify_expiring(bot)

    assert bot.messages == [], "новичка просят продлить в первую же минуту"
    assert (await get_sub(user_id)).reminded_at is None


async def test_trial_is_reminded_past_the_middle(create_user, monkeypatch):
    """Молчать до конца — тоже плохо: за половину пробного срока сказать пора."""
    monkeypatch.setattr(settings, "trial_days", 3)
    user_id = await create_user()
    async with session_scope() as session:
        await repo.grant_trial(session, user_id)
    await set_period(user_id, started_days_ago=2, days_left=1)
    bot = RecordingBot()

    await main.notify_expiring(bot)

    assert bot.recipients == [user_id]
    assert "заканчивается" in bot.messages[0][1]


async def test_monthly_subscription_keeps_the_old_window(create_user):
    """У месяца порог как был: половина периода — 15 дней, напоминание за три."""
    user_id = await create_user()
    async with session_scope() as session:
        await repo.activate_subscription(session, user_id, months=1)
    await set_period(user_id, started_days_ago=28, days_left=2)
    bot = RecordingBot()

    await main.notify_expiring(bot)

    assert bot.recipients == [user_id]


async def test_a_row_without_period_start_behaves_as_before(create_user):
    """Боевые строки старше колонки: остаток — единственное, что о них известно."""
    user_id = await create_user()
    async with session_scope() as session:
        session.add(
            Subscription(
                user_id=user_id,
                active_until=repo.utcnow() + timedelta(days=1),
            )
        )
    assert (await get_sub(user_id)).period_start is None
    bot = RecordingBot()

    await main.notify_expiring(bot)

    assert bot.recipients == [user_id]


async def test_renewal_of_a_live_subscription_keeps_the_start(create_user):
    """Доступ не прерывался — точка отсчёта прежняя, а не «сегодня».

    Иначе продление за день до конца обнуляло период, и напоминание о следующем
    конце опять пришлось бы ждать до половины нового срока — то есть человек,
    заплативший заранее, получал бы предупреждение позже, чем забывчивый.
    """
    user_id = await create_user()
    async with session_scope() as session:
        await repo.activate_subscription(session, user_id, months=1)
    await set_period(user_id, started_days_ago=29, days_left=1)
    started = (await get_sub(user_id)).period_start

    async with session_scope() as session:
        await repo.activate_subscription(session, user_id, months=1)

    assert (await get_sub(user_id)).period_start == started


async def test_paying_after_the_end_starts_a_new_period(create_user):
    """Истёкший абонемент оплачивают заново — и середину считают заново."""
    user_id = await create_user()
    async with session_scope() as session:
        await repo.activate_subscription(session, user_id, months=1)
    await set_period(user_id, started_days_ago=40, days_left=-10)
    old_start = (await get_sub(user_id)).period_start

    async with session_scope() as session:
        await repo.activate_subscription(session, user_id, months=1)

    sub = await get_sub(user_id)
    assert sub.period_start is not None and sub.period_start > old_start


# ──────────────────────── письмо о конце абонемента ───────────────────────────


async def expired_with_rules(create_user, create_account, rules: int = 1) -> int:
    """Пользователь, у которого срок вышел, а задачи остались включёнными."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    for _ in range(rules):
        await add_rule(user_id, account_id)
    async with session_scope() as session:
        session.add(
            Subscription(
                user_id=user_id,
                active_until=repo.utcnow() - timedelta(days=1),
            )
        )
    return user_id


async def test_the_end_of_the_term_is_announced(create_user, create_account):
    """Те самые пять молчаливых окончаний: теперь про каждое говорят."""
    user_id = await expired_with_rules(create_user, create_account)
    bot = RecordingBot()

    await main.notify_expired(bot)

    assert bot.recipients == [user_id]
    assert "закончился" in bot.messages[0][1]
    assert (await get_sub(user_id)).expired_notified_at is not None


async def test_the_notice_names_what_stopped(create_user, create_account):
    """Сколько задач встало — это и есть цена кончившегося срока."""
    user_id = await expired_with_rules(create_user, create_account, rules=3)
    bot = RecordingBot()

    await main.notify_expired(bot)

    assert "задач — 3" in bot.messages[0][1]


async def test_each_owner_is_told_about_his_own_tasks(create_user, create_account):
    """Считаем задачи того, кому пишем: чужие в цену чужого простоя не входят."""
    first = await expired_with_rules(create_user, create_account, rules=1)
    second = await expired_with_rules(create_user, create_account, rules=4)
    bot = RecordingBot()

    await main.notify_expired(bot)

    sent = dict(bot.messages)
    assert "задач — 1" in sent[first]
    assert "задач — 4" in sent[second]


async def test_stopped_tasks_are_not_counted(create_user, create_account):
    """Пауза и архив стояли и до конца срока — абонемент их не останавливал."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    await add_rule(user_id, account_id)
    await add_rule(user_id, account_id, enabled=False)
    await add_rule(user_id, account_id, archived=True)
    async with session_scope() as session:
        session.add(
            Subscription(
                user_id=user_id, active_until=repo.utcnow() - timedelta(hours=2)
            )
        )
    bot = RecordingBot()

    await main.notify_expired(bot)

    assert "задач — 1" in bot.messages[0][1]


async def test_nobody_is_bothered_when_nothing_stopped(create_user):
    """Без включённых задач конец срока ничего не изменил — и письма не за что."""
    user_id = await create_user()
    async with session_scope() as session:
        session.add(
            Subscription(user_id=user_id, active_until=repo.utcnow() - timedelta(days=2))
        )
    bot = RecordingBot()

    await main.notify_expired(bot)

    assert bot.messages == []
    # Метку всё равно ставим: иначе тот же проход будет пересчитывать задачи
    # этого человека каждые пять минут до самой оплаты.
    assert (await get_sub(user_id)).expired_notified_at is not None


async def test_the_notice_offers_the_way_to_pay(create_user, create_account):
    """Письмо без кнопки оплаты отправляет человека искать её в меню."""
    user_id = await expired_with_rules(create_user, create_account)
    bot = RecordingBot()

    await main.notify_expired(bot)

    labels = [button.text for row in bot.markups[0].inline_keyboard for button in row]
    assert any("Stars" in label for label in labels)


async def test_the_notice_is_not_repeated_every_five_minutes(create_user, create_account):
    """Фоновый цикл ходит каждые пять минут — письмо всё равно одно."""
    user_id = await expired_with_rules(create_user, create_account)
    bot = RecordingBot()

    await main.notify_expired(bot)
    await main.notify_expired(bot)
    await main.notify_expired(bot)

    assert bot.recipients == [user_id]


async def test_a_second_end_is_announced_again(create_user, create_account):
    """Продлили и снова дошли до конца — это новый конец, а не тот же самый."""
    user_id = await expired_with_rules(create_user, create_account)
    bot = RecordingBot()
    await main.notify_expired(bot)

    async with session_scope() as session:  # оплата снимает метку
        await repo.activate_subscription(session, user_id, months=1)
    assert (await get_sub(user_id)).expired_notified_at is None

    await set_period(user_id, started_days_ago=31, days_left=-1)
    await main.notify_expired(bot)

    assert bot.recipients == [user_id, user_id]


async def test_a_live_subscription_is_left_alone(create_user, create_account):
    user_id = await create_user()
    account_id = await create_account(user_id)
    await add_rule(user_id, account_id)
    async with session_scope() as session:
        await repo.activate_subscription(session, user_id, months=1)
    bot = RecordingBot()

    await main.notify_expired(bot)

    assert bot.messages == []
    assert (await get_sub(user_id)).expired_notified_at is None


async def test_a_blocked_bot_does_not_break_the_pass(create_user, create_account):
    """Один заблокировавший бота не должен лишать письма остальных."""
    blocked = await expired_with_rules(create_user, create_account)
    other = await expired_with_rules(create_user, create_account)
    bot = RecordingBot(fail_for={blocked})

    await main.notify_expired(bot)

    assert bot.recipients == [other]
    assert (await get_sub(blocked)).expired_notified_at is not None


async def test_the_pass_is_wired_into_the_background_loop(create_user, create_account):
    """Проверка, которой нет в фоновом цикле, не сработает никогда."""
    user_id = await expired_with_rules(create_user, create_account)
    bot = RecordingBot()

    await main.run_background_checks(bot)

    assert user_id in bot.recipients


async def test_the_two_notices_do_not_double_up(create_user, create_account):
    """Одно окончание — одно письмо: «скоро конец» и «конец» не про одно и то же."""
    user_id = await expired_with_rules(create_user, create_account)
    bot = RecordingBot()

    await main.notify_expiring(bot)
    await main.notify_expired(bot)

    assert bot.recipients == [user_id]
    assert "закончился" in bot.messages[0][1]


# ─────────────────────────── база старше правки ───────────────────────────────


async def test_an_old_database_gets_the_new_columns(create_user, create_account):
    """Боевая БД старше этой правки: колонки доливает старт, руками — ничего.

    ``create_all`` дополняет только новые таблицы, поэтому обе колонки в живой
    базе появляются через ``ADDED_COLUMNS``. Без них первый же фоновый проход
    упал бы на «no such column», и письмо не ушло бы никому.
    """
    user_id = await expired_with_rules(create_user, create_account)

    async with engine.begin() as conn:  # база, какой она была до правки
        for column in ("period_start", "expired_notified_at"):
            await conn.execute(text(f'ALTER TABLE subscriptions DROP COLUMN "{column}"'))
        rows = await conn.execute(text('PRAGMA table_info("subscriptions")'))
        names = {row[1] for row in rows}
        assert "period_start" not in names and "expired_notified_at" not in names

    await ensure_schema()

    bot = RecordingBot()
    await main.notify_expired(bot)

    assert bot.recipients == [user_id]
    assert (await get_sub(user_id)).expired_notified_at is not None
