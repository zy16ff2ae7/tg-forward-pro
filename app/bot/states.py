"""Состояния диалогов (FSM)."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class LoginStates(StatesGroup):
    """Вход в личный аккаунт Telegram."""

    phone = State()
    code = State()
    password = State()


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


class OwnerStates(StatesGroup):
    """Панель владельца: кому выдать абонемент и что разослать."""

    grant_user = State()
    broadcast_text = State()
