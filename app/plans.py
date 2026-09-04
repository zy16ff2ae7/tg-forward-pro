"""Сроки абонемента: один список на весь сервис.

Срок можно выбрать в двух местах — кнопками в боте и через API кабинета.
Если держать списки отдельно, они расходятся: в боте появится срок, которого
нет в кабинете, или наоборот. Поэтому каatalog сроков лежит здесь, а бот и
``webapp_api`` только читают его.
"""
from __future__ import annotations

from app.config import settings

# Сроки в месяцах. Менять можно только всем списком: цены считаются
# умножением, а не таблицей, поэтому «2 месяца» не сломает расчёт —
# но в меню бота появится только то, что есть в PERIODS.
PERIODS: tuple[int, ...] = (1, 3, 6, 12)

DEFAULT_MONTHS = 1
MAX_MONTHS = max(PERIODS)

# Описание счёта. Одинаковое у бота и кабинета: пользователь не должен
# гадать, один ли это сервис, глядя на два разных текста в инвойсе.
STARS_DESCRIPTION = (
    "Автоматическая пересылка сообщений: безлимит правил, 24/7, "
    "без метки «Переслано от»."
)


def is_valid_period(months: int) -> bool:
    """Проверяет, что срок есть в каталоге."""
    return months in PERIODS


def months_from_callback(data: str) -> int | None:
    """Достаёт срок из ``callback_data`` вида ``pay:stars:3``.

    ``None`` — если в данных не три части, в них не число или срока нет
    в каталоге: молча подставить месяц вместо левого значения нельзя,
    пользователь заплатит не за то, что выбирал.
    """
    parts = data.split(":")
    if len(parts) != 3:
        return None
    try:
        months = int(parts[2])
    except ValueError:
        return None
    return months if is_valid_period(months) else None


def stars_amount(months: int) -> int:
    """Сумма в звёздах за срок. Считается на сервере — клиент цену не диктует."""
    return settings.price_stars * months


def rub_amount(months: int) -> int:
    """Сумма в рублях за срок (карта/СБП и заявка администратору)."""
    return settings.price_rub * months


def usdt_amount(months: int) -> float:
    """Сумма в USDT за срок.

    Округление до цента: метка платежа занимает третий знак после запятой
    (``12.017``), поэтому в самой цене третьего знака быть не должно — иначе
    метка и цена перепутаются.
    """
    return round(settings.price_usdt * months, 2)


def periods_text() -> str:
    """Список сроков для сообщения: «1, 3, 6 или 12 месяцев»."""
    head, last = ", ".join(str(item) for item in PERIODS[:-1]), PERIODS[-1]
    return f"{head} или {last} месяцев"
