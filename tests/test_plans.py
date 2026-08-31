"""Каталог сроков абонемента: бот и кабинет обязаны предлагать одно и то же."""
from __future__ import annotations

import pytest

from app.config import settings
from app.plans import (
    DEFAULT_MONTHS,
    MAX_MONTHS,
    PERIODS,
    is_valid_period,
    months_from_callback,
    periods_text,
    stars_amount,
)


def test_catalog_starts_with_one_month_and_grows():
    """Срок по умолчанию и верхняя граница берутся из каталога, а не прописаны отдельно."""
    assert PERIODS[0] == 1 == DEFAULT_MONTHS
    assert MAX_MONTHS == max(PERIODS)


@pytest.mark.parametrize("months", PERIODS)
def test_valid_periods_are_accepted(months):
    assert is_valid_period(months) is True


@pytest.mark.parametrize("months", [0, -1, 2, 5, 7, 13, 10_000])
def test_periods_outside_catalog_are_rejected(months):
    assert is_valid_period(months) is False


@pytest.mark.parametrize("months", PERIODS)
def test_months_from_callback_reads_period(months):
    assert months_from_callback(f"pay:stars:{months}") == months


@pytest.mark.parametrize(
    "data",
    [
        "pay:stars",  # срок не выбран — шаг «выберите срок»
        "pay:stars:",  # пусто
        "pay:stars:two",  # не число
        "pay:stars:2",  # число, но не из каталога
        "pay:stars:9999",  # гигантский срок
        "pay:stars:1:extra",
    ],
)
def test_months_from_callback_rejects_garbage(data):
    """Левый срок — None, а не «возьмём месяц»: иначе пользователь заплатит не за то."""
    assert months_from_callback(data) is None


def test_stars_amount_is_monthly_price_times_period():
    assert stars_amount(1) == settings.price_stars
    assert stars_amount(12) == settings.price_stars * 12


def test_periods_text_lists_the_whole_catalog():
    text = periods_text()
    for months in PERIODS:
        assert str(months) in text
    assert text.endswith("месяцев")
