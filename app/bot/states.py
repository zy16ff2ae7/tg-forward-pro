"""Состояния диалогов (FSM)."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class LoginStates(StatesGroup):
    """Вход в личный аккаунт Telegram: номер или QR-код."""

    choice = State()
    keys = State()
    phone = State()
    code = State()
    password = State()
    qr = State()
    qr_password = State()


class RuleStates(StatesGroup):
    """Создание правила: выбор источника и приёмника."""

    source = State()
    target = State()


class EditStates(StatesGroup):
    """Редактирование числовых и текстовых настроек правила."""

    delay = State()
    blacklist = State()
    whitelist = State()
    append = State()
    replace = State()


class PromoStates(StatesGroup):
    """Ввод промокода следующим сообщением."""

    waiting_code = State()


class GiftStates(StatesGroup):
    """Подарок: следующим сообщением — кому (id или @username)."""

    waiting_friend = State()


class OwnerStates(StatesGroup):
    """Панель владельца: кому выдать абонемент, что разослать, какой код создать."""

    grant_user = State()
    broadcast_text = State()
    promo_code = State()
    promo_custom = State()
