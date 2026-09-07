"""Подсказка команд по «/»: она должна совпадать с тем, что бот умеет.

Список команд Telegram не публиковался вовсе — про ``/bonus``, ``/rules`` и
``/bank`` можно было узнать только из ``/help``. Кнопку «Меню» у поля ввода
занимает кабинет, поэтому подсказка по «/» — единственное место, где команды
видно.

Список, объявленный руками, живёт своей жизнью: команду переименовали в
хендлере — в подсказке осталась старая, и бот на неё молчит. Поэтому главная
проверка здесь сверяет объявленное с тем, что реально зарегистрировано в
роутерах aiogram.
"""
from __future__ import annotations

from aiogram.filters import Command
from aiogram.types import BotCommandScopeAllPrivateChats, BotCommandScopeChat

from app.bot import commands as bot_commands
from app.bot.handlers import accounts, admin, menu, rules, subscription
from app.config import settings

ROUTERS = (menu.router, accounts.router, rules.router, subscription.router, admin.router)


def handled_commands() -> set[str]:
    """Команды, на которые у бота есть хендлер — прямо из фильтров роутеров."""
    found: set[str] = set()
    for router in ROUTERS:
        for handler in router.message.handlers:
            for flt in handler.filters or ():
                callback = getattr(flt, "callback", flt)
                if isinstance(callback, Command):
                    found.update(str(name) for name in callback.commands)
    return found


class ListingBot:
    """Bot API в объёме публикации списка: только ``set_my_commands``."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[tuple[list[str], object]] = []
        self.error = error

    async def set_my_commands(self, commands, scope=None, **_kwargs):
        if self.error is not None:
            raise self.error
        self.calls.append(([item.command for item in commands], scope))
        return True


def test_every_announced_command_has_a_handler():
    """Главная проверка: в подсказке нет команд, на которые бот не отвечает."""
    announced = {item.command for item in bot_commands.admin_commands()}

    assert announced <= handled_commands(), announced - handled_commands()


def test_descriptions_fit_the_narrow_menu():
    """Telegram обрезает описания: 1–256 символов, и коротко читается лучше."""
    for item in bot_commands.admin_commands():
        assert item.command == item.command.lower(), item.command
        assert 1 <= len(item.description) <= 60, item


def test_the_cabinet_comes_first():
    """Кабинет — то, за чем открывают бота: он и должен быть первым в списке."""
    assert bot_commands.user_commands()[0].command == "app"


def test_admin_menu_is_not_poorer_than_the_common_one():
    """Админ остаётся пользователем: его команды идут вдогонку, а не вместо."""
    common = [item.command for item in bot_commands.user_commands()]
    for_admin = [item.command for item in bot_commands.admin_commands()]

    assert for_admin[: len(common)] == common
    assert set(for_admin) - set(common) == {"admin", "users", "grant", "promo_new"}


def test_admin_commands_are_hidden_from_everyone_else():
    """/grant и /users в общем списке — приглашение постучаться в них."""
    common = {item.command for item in bot_commands.user_commands()}

    assert "grant" not in common and "users" not in common and "admin" not in common


def test_bonus_shows_up_only_when_the_gift_works(monkeypatch):
    """Отключённый подарок в подсказке — обещание, на которое бот ответит отказом."""
    monkeypatch.setattr(settings, "bonus_channel", "@papin4_do4a")
    assert "bonus" in {item.command for item in bot_commands.user_commands()}

    monkeypatch.setattr(settings, "bonus_channel", None)
    assert "bonus" not in {item.command for item in bot_commands.user_commands()}


def test_the_gift_stands_next_to_the_subscription(monkeypatch):
    """Подарок — про дни доступа, и место ему сразу после «Абонемента»."""
    monkeypatch.setattr(settings, "bonus_channel", "@papin4_do4a")

    names = [item.command for item in bot_commands.user_commands()]

    assert names[names.index("sub") + 1] == "bonus"


async def test_list_goes_to_private_chats_and_to_each_admin(monkeypatch):
    """Область важна: в группах бот не работает, и «Мои задачи» там ни к чему."""
    monkeypatch.setattr(settings, "admin_ids", [111, 222])
    bot = ListingBot()

    await bot_commands.publish(bot)

    scopes = [scope for _, scope in bot.calls]
    assert isinstance(scopes[0], BotCommandScopeAllPrivateChats)
    assert [getattr(scope, "chat_id", None) for scope in scopes[1:]] == [111, 222]
    assert "grant" in bot.calls[1][0], "админу список уходит расширенный"
    assert "grant" not in bot.calls[0][0], "всем остальным — обычный"


async def test_without_admins_only_the_common_list_is_published(monkeypatch):
    """Пустой ADMIN_IDS — не повод пропускать публикацию для всех."""
    monkeypatch.setattr(settings, "admin_ids", [])
    bot = ListingBot()

    await bot_commands.publish(bot)

    assert len(bot.calls) == 1


async def test_a_telegram_failure_does_not_stop_the_start(monkeypatch):
    """Список команд — украшение: из-за него бот запускаться не перестаёт."""
    monkeypatch.setattr(settings, "admin_ids", [111])

    await bot_commands.publish(ListingBot(error=RuntimeError("Bad Gateway")))
