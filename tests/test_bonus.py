"""Подарок за подписку на канал сервиса: разово, честно и с разными отказами.

Здесь проверяется то, из-за чего подарок мог бы стать дырой в кассе или обидой:

* дни начисляются ровно один раз на аккаунт — второе нажатие «Проверить
  подписку» и два одновременных запроса не дают второго подарка;
* точка отсчёта: активный абонемент продлевается, истёкший начинается заново,
  иначе подарок достался бы прошлому и человек не увидел бы ни дня;
* «не подписан» и «не смогли проверить» — разные ответы: во втором случае
  виноваты мы (бот не администратор канала), и звать подписаться заново нельзя;
* отказ не оставляет следов: метка «подарок выдан» не должна ставиться, если
  дни не начислены.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from aiogram.enums import ChatMemberStatus

from app import bonus
from app.config import settings
from app.db import repo
from app.db.database import SessionLocal, session_scope
from app.db.models import Subscription, User
from app.timeutil import utcnow
from tests.helpers import TEST_USER_ID

CHANNEL = "@papin4_do4a"
DAYS = 3


@pytest.fixture
def bonus_on(monkeypatch):
    """Настроенный подарок: канал и число дней.

    Настройки — один объект на всё приложение, поэтому правим его поля: и
    app.bonus, и webapp_api, и тексты бота смотрят на тот же ``settings``.
    """
    monkeypatch.setattr(settings, "bonus_channel", CHANNEL)
    monkeypatch.setattr(settings, "bonus_days", DAYS)
    return settings


@pytest.fixture
def bonus_off(monkeypatch):
    monkeypatch.setattr(settings, "bonus_channel", None)
    return settings


class MemberBot:
    """Bot API в объёме проверки подписки: один ``getChatMember``.

    ``status`` намеренно принимается любым — и строкой, и str-Enum aiogram:
    именно на Enum ломалось наивное сравнение ``str(status) == "member"``.
    """

    def __init__(self, status="member", *, is_member: bool = True, error=None) -> None:
        self.status = status
        self.is_member = is_member
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def get_chat_member(self, chat_id, user_id):
        self.calls.append((chat_id, user_id))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(status=self.status, is_member=self.is_member)


async def user_row(user_id: int) -> User:
    async with SessionLocal() as session:
        row = await session.get(User, user_id)
        assert row is not None
        return row


async def claim_for(user_id: int, bot) -> bonus.Bonus:
    """Как в кабинете и в боте: коммит только при удаче, иначе откат."""
    async with SessionLocal() as session:
        result = await bonus.claim(session, bot, user_id)
        if result.granted:
            await session.commit()
        else:
            await session.rollback()
    return result


# ─────────────────────────── начисление дней (repo) ───────────────────────────


async def test_bonus_extends_active_subscription(create_user, bonus_on):
    """Дни складываются с текущим абонементом — ничего не сгорает."""
    user_id = await create_user()
    async with session_scope() as session:
        session.add(
            Subscription(user_id=user_id, active_until=utcnow() + timedelta(days=10))
        )

    async with session_scope() as session:
        until = await repo.claim_channel_bonus(session, user_id, DAYS)

    assert until is not None
    assert 12.9 < (until - utcnow()).total_seconds() / 86400 < 13.1
    assert (await user_row(user_id)).channel_bonus_at is not None


async def test_bonus_starts_from_now_when_subscription_expired(create_user, bonus_on):
    """Истёкшая подписка не съедает подарок задним числом."""
    user_id = await create_user()
    async with session_scope() as session:
        session.add(
            Subscription(user_id=user_id, active_until=utcnow() - timedelta(days=30))
        )

    async with session_scope() as session:
        until = await repo.claim_channel_bonus(session, user_id, DAYS)

    assert 2.9 < (until - utcnow()).total_seconds() / 86400 < 3.1


async def test_bonus_creates_subscription_when_there_is_none(create_user, bonus_on):
    user_id = await create_user()

    async with session_scope() as session:
        until = await repo.claim_channel_bonus(session, user_id, DAYS)

    assert 2.9 < (until - utcnow()).total_seconds() / 86400 < 3.1
    async with SessionLocal() as session:
        assert await session.get(Subscription, user_id) is not None


async def test_bonus_granted_only_once(create_user, bonus_on):
    """Отписка-подписка второго подарка не даёт: метка одна на аккаунт."""
    user_id = await create_user()
    async with session_scope() as session:
        first = await repo.claim_channel_bonus(session, user_id, DAYS)
    async with session_scope() as session:
        second = await repo.claim_channel_bonus(session, user_id, DAYS)

    assert first is not None
    assert second is None
    async with SessionLocal() as session:
        sub = await session.get(Subscription, user_id)
        assert sub.active_until == first  # дни не добавились второй раз


async def test_bonus_ignores_unknown_user(bonus_on):
    """Нет пользователя — нет подарка, а не подписка из ниоткуда."""
    async with session_scope() as session:
        assert await repo.claim_channel_bonus(session, 424_242, DAYS) is None
    async with SessionLocal() as session:
        assert await session.get(Subscription, 424_242) is None


async def test_add_subscription_days_clears_reminder(create_user):
    """Продление снимает метку «предупредили об окончании»."""
    user_id = await create_user()
    async with session_scope() as session:
        session.add(
            Subscription(
                user_id=user_id,
                active_until=utcnow() + timedelta(days=1),
                reminded_at=utcnow(),
            )
        )

    async with session_scope() as session:
        await repo.add_subscription_days(session, user_id, DAYS)

    async with SessionLocal() as session:
        assert (await session.get(Subscription, user_id)).reminded_at is None


# ──────────────────────── проверка подписки (app.bonus) ───────────────────────


@pytest.mark.parametrize(
    "status",
    ["creator", "administrator", "member", ChatMemberStatus.MEMBER, "MEMBER"],
)
async def test_claim_grants_to_subscriber(create_user, bonus_on, status):
    user_id = await create_user()
    bot = MemberBot(status)

    result = await claim_for(user_id, bot)

    assert result.granted is True
    assert result.days == DAYS
    assert result.until is not None
    assert bot.calls == [(CHANNEL, user_id)]


@pytest.mark.parametrize("status", ["left", "kicked", "banned", ""])
async def test_claim_refuses_non_subscriber(create_user, bonus_on, status):
    user_id = await create_user()

    result = await claim_for(user_id, MemberBot(status))

    assert result.status == "not_member"
    assert (await user_row(user_id)).channel_bonus_at is None  # метки нет


@pytest.mark.parametrize("still_in, expected", [(True, "granted"), (False, "not_member")])
async def test_claim_counts_restricted_only_while_in_channel(
    create_user, bonus_on, still_in, expected
):
    """Ограниченный участник — участник, пока не вышел."""
    user_id = await create_user()

    result = await claim_for(user_id, MemberBot("restricted", is_member=still_in))

    assert result.status == expected


async def test_claim_without_bot_is_unavailable(create_user, bonus_on):
    """Кабинет без живого бота проверить подписку не может — но и не врёт."""
    user_id = await create_user()

    result = await claim_for(user_id, None)

    assert result.status == "unavailable"
    assert (await user_row(user_id)).channel_bonus_at is None


async def test_claim_survives_bot_api_failure(create_user, bonus_on):
    """Бот не администратор канала / Telegram недоступен — «попробуйте позже»."""
    user_id = await create_user()
    bot = MemberBot(error=RuntimeError("CHAT_ADMIN_REQUIRED"))

    result = await claim_for(user_id, bot)

    assert result.status == "unavailable"
    assert "позже" in bonus.message(result)
    assert (await user_row(user_id)).channel_bonus_at is None


async def test_claim_when_bonus_disabled(create_user, bonus_off):
    """Выключенный подарок не ходит в Telegram вовсе."""
    user_id = await create_user()
    bot = MemberBot()

    result = await claim_for(user_id, bot)

    assert result.status == "disabled"
    assert bot.calls == []


async def test_second_claim_does_not_touch_bot_api(create_user, bonus_on):
    """Уже получил — отвечаем сразу, не тратя запрос к Telegram."""
    user_id = await create_user()
    bot = MemberBot()

    assert (await claim_for(user_id, bot)).granted is True
    second = await claim_for(user_id, bot)

    assert second.status == "already"
    assert len(bot.calls) == 1


def test_bonus_info_hides_everything_when_disabled(bonus_off):
    info = bonus.info(None)
    assert info["enabled"] is False
    assert info["days"] == 0
    assert bonus.offer() == ""


def test_bonus_info_describes_offer(bonus_on):
    info = bonus.info(None)
    assert info == {
        "enabled": True,
        "channel": CHANNEL,
        "url": f"https://t.me/{CHANNEL[1:]}",
        "days": DAYS,
        "claimed": False,
        "claimed_at": None,
    }
    assert bonus.offer() == f"{DAYS} дн. за подписку на канал"


def test_bonus_info_remembers_claim(bonus_on):
    moment = utcnow()
    info = bonus.info(moment)
    assert info["claimed"] is True
    assert info["claimed_at"] == moment.isoformat()


def test_bonus_messages_are_distinct(bonus_on):
    """Каждый отказ говорит своё — общий текст запутал бы человека."""
    texts = {
        status: bonus.message(bonus.Bonus(status, days=DAYS))
        for status in ("granted", "already", "not_member", "unavailable", "disabled")
    }
    assert len(set(texts.values())) == len(texts)
    assert CHANNEL in texts["not_member"]
    assert "один раз" in texts["already"]


# ───────────────────────────── кабинет (HTTP API) ─────────────────────────────


async def test_api_requires_auth(client):
    assert (await client.post("/api/subscription/bonus")).status == 401


async def test_api_grants_days_and_updates_me(bot_client, auth_headers, bonus_on):
    """Начисление видно там же, где живёт подписка: в /api/me."""
    test_client = await bot_client(MemberBot())

    before = await (await test_client.get("/api/me", headers=auth_headers)).json()
    assert before["bonus"] == {
        "enabled": True,
        "channel": CHANNEL,
        "url": f"https://t.me/{CHANNEL[1:]}",
        "days": DAYS,
        "claimed": False,
        "claimed_at": None,
    }

    response = await test_client.post("/api/subscription/bonus", headers=auth_headers)
    assert response.status == 200
    body = await response.json()
    assert body["granted"] is True
    assert body["days"] == DAYS
    assert body["until"]
    assert body["url"] == f"https://t.me/{CHANNEL[1:]}"

    after = await (await test_client.get("/api/me", headers=auth_headers)).json()
    assert after["bonus"]["claimed"] is True
    assert after["bonus"]["claimed_at"]
    days_grew = after["subscription"]["days_left"] - before["subscription"]["days_left"]
    assert days_grew == DAYS


async def test_api_second_call_is_conflict(bot_client, auth_headers, bonus_on):
    test_client = await bot_client(MemberBot())
    assert (await test_client.post("/api/subscription/bonus", headers=auth_headers)).status == 200

    response = await test_client.post("/api/subscription/bonus", headers=auth_headers)
    assert response.status == 409
    body = await response.json()
    assert body["status"] == "already"
    assert body["granted"] is False
    # Старые клиенты читают только error — текст там тот же
    assert body["error"] == body["message"]


async def test_api_not_member_is_forbidden(bot_client, auth_headers, bonus_on):
    """403 — сигнал кабинету открыть канал, а не «сервер сломался»."""
    test_client = await bot_client(MemberBot("left"))

    response = await test_client.post("/api/subscription/bonus", headers=auth_headers)

    assert response.status == 403
    body = await response.json()
    assert body["status"] == "not_member"
    assert CHANNEL in body["message"]


async def test_api_refusal_leaves_no_mark(bot_client, auth_headers, bonus_on):
    """Отказ откатывается целиком, но пользователь в базе остаётся."""
    test_client = await bot_client(MemberBot("left"))

    assert (await test_client.post("/api/subscription/bonus", headers=auth_headers)).status == 403

    row = await user_row(TEST_USER_ID)
    assert row.channel_bonus_at is None

    # После подписки подарок доступен — отказ его не потратил
    test_client = await bot_client(MemberBot())
    assert (await test_client.post("/api/subscription/bonus", headers=auth_headers)).status == 200


async def test_api_bot_failure_is_service_unavailable(bot_client, auth_headers, bonus_on):
    test_client = await bot_client(MemberBot(error=RuntimeError("Bot API is down")))

    response = await test_client.post("/api/subscription/bonus", headers=auth_headers)

    assert response.status == 503
    assert (await response.json())["status"] == "unavailable"


async def test_api_disabled_bonus_is_service_unavailable(client, auth_headers, bonus_off):
    response = await client.post("/api/subscription/bonus", headers=auth_headers)

    assert response.status == 503
    assert (await response.json())["status"] == "disabled"


async def test_me_hides_bonus_when_disabled(client, auth_headers, bonus_off):
    body = await (await client.get("/api/me", headers=auth_headers)).json()
    assert body["bonus"]["enabled"] is False
    assert body["bonus"]["days"] == 0


# ──────────────────────────────── бот ─────────────────────────────────────────


def menu_labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def test_payment_menu_offers_free_days_first(bonus_on):
    """Самый дешёвый для человека путь — первой строкой, а не под оплатой."""
    from app.bot import keyboards as kb

    labels = menu_labels(kb.payment_menu(777))
    assert labels[0] == f"🎁 {DAYS} дн. за подписку на канал"


def test_payment_menu_without_bonus(bonus_off):
    from app.bot import keyboards as kb

    assert not any("🎁" in text for text in menu_labels(kb.payment_menu(777)))


def test_bonus_menu_drops_check_after_claim(bonus_on):
    from app.bot import keyboards as kb

    fresh = menu_labels(kb.bonus_menu(claimed=False))
    assert fresh == ["📣 Открыть канал", "🔄 Проверить подписку", "◀️ Назад"]
    # Повторное нажатие могло бы ответить только «уже получено»
    assert "🔄 Проверить подписку" not in menu_labels(kb.bonus_menu(claimed=True))


def test_bonus_menu_without_public_link(monkeypatch, bonus_on):
    """У канала по числовому id нет ссылки — кнопку «Открыть» не рисуем."""
    from app.bot import keyboards as kb

    monkeypatch.setattr(settings, "bonus_channel", "-1001234567890")
    assert "📣 Открыть канал" not in menu_labels(kb.bonus_menu())


def test_welcome_advertises_bonus_and_command(bonus_on):
    from app.bot import texts

    text = texts.welcome("Тест")
    assert f"{DAYS} дн." in text
    assert CHANNEL in text
    assert "/bonus" in text


def test_welcome_silent_without_bonus(bonus_off):
    from app.bot import texts

    assert "/bonus" not in texts.welcome("Тест")


def test_bonus_card_texts(bonus_on):
    from app.bot import texts

    offer = texts.bonus_card(claimed=False)
    assert CHANNEL in offer and f"{DAYS} дн." in offer
    assert "уже начислены" in texts.bonus_card(claimed=True)


def test_bonus_card_when_disabled(bonus_off):
    from app.bot import texts

    assert "не настроен" in texts.bonus_card(claimed=False)


def test_bonus_command_is_registered():
    """Приветствие обещает /bonus — команда обязана существовать."""
    from app.bot.handlers import subscription as handlers

    names = [handler.callback.__name__ for handler in handlers.router.message.handlers]
    assert "cmd_bonus" in names
