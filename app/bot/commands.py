"""Список команд, который Telegram показывает по «/» в поле ввода.

Бот отвечает на десяток команд, но Telegram о них не знал: список ни разу не
публиковался, и про ``/bonus`` или ``/rules`` можно было узнать только из
``/help`` — а до ``/help`` надо догадаться. Кнопку «Меню» рядом с полем ввода
занимает кабинет, поэтому подсказка по «/» — единственное место, где команды
видно.

Здесь один список на всё: он и уходит в ``setMyCommands``, и проверяется
тестом на то, что каждая объявленная команда действительно кем-то
обрабатывается. Команды админа отдельно и только в его личный чат — остальным
их видеть незачем.
"""
from __future__ import annotations

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat
from loguru import logger

from app.config import settings

# Порядок — по частоте: первым то, за чем открывают бота.
USER_COMMANDS: tuple[tuple[str, str], ...] = (
    ("app", "Открыть кабинет"),
    ("menu", "Главное меню"),
    ("rules", "Мои задачи"),
    ("accounts", "Аккаунты Telegram"),
    ("sub", "Абонемент и оплата"),
    ("bank", "Копилка дней"),
    ("stats", "Моя сводка"),
    ("help", "Как пользоваться"),
)

# Показываем только когда подарок включён: иначе меню обещает то, на что бот
# ответит «подарок отключён».
BONUS_COMMAND = ("bonus", "Подарок за подписку на канал")

ADMIN_COMMANDS: tuple[tuple[str, str], ...] = (
    ("users", "Все пользователи"),
    ("grant", "Выдать дни"),
)


def _commands(pairs: tuple[tuple[str, str], ...]) -> list[BotCommand]:
    return [BotCommand(command=name, description=text) for name, text in pairs]


def user_commands() -> list[BotCommand]:
    """Команды, которые видят все. Подарок — по настройке."""
    pairs = USER_COMMANDS
    if settings.bonus_enabled:
        # После «Абонемента»: подарок — это тоже про дни доступа.
        index = [name for name, _ in pairs].index("sub") + 1
        pairs = pairs[:index] + (BONUS_COMMAND,) + pairs[index:]
    return _commands(pairs)


def admin_commands() -> list[BotCommand]:
    """Команды админа идут вдогонку к общим: своё меню не должно быть беднее."""
    return user_commands() + _commands(ADMIN_COMMANDS)


async def publish(bot: Bot) -> None:
    """Отдаёт список Telegram. Неудача — не причина не запускать бота.

    Область — личные чаты: в группах бот не работает, и предлагать там
    «Мои задачи» незачем. Админам список уходит адресно, в их чат.
    """
    try:
        await bot.set_my_commands(
            user_commands(), scope=BotCommandScopeAllPrivateChats()
        )
        for admin_id in settings.admin_ids:
            await bot.set_my_commands(
                admin_commands(), scope=BotCommandScopeChat(chat_id=admin_id)
            )
    except Exception as exc:  # noqa: BLE001 — меню команд не стоит запуска бота
        logger.warning("Не удалось опубликовать список команд: {}", exc)
        return
    logger.info(
        "Список команд опубликован: {} для всех, {} у админов ({})",
        len(user_commands()),
        len(admin_commands()),
        len(settings.admin_ids),
    )
