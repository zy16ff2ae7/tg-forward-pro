"""Explicit, bounded plans for Telegram account setup and scheduled activity."""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.account_profile import validate_changes
from app.errors import ValidationError
from app.telegram_client.jobs import explicit_join_target

DAY = 86400
BIOS = (
    "Заметки, идеи и немного вдохновения.",
    "Место для интересного и хороших разговоров.",
    "Сохраняю то, к чему хочется вернуться.",
)
CAPTIONS = (
    "Пауза тоже часть пути.",
    "Пусть сегодня найдётся время для простого.",
    "Иногда достаточно замедлиться и посмотреть вокруг.",
    "Небольшие шаги тоже ведут вперёд.",
    "Хороший момент не обязательно должен быть громким.",
    "Побольше воздуха, поменьше спешки.",
    "Оставим место для новых идей.",
)
STEP_LABELS = {"avatar": "Аватарка", "bio": "Описание", "birthday": "Дата рождения",
               "join": "Вступление", "story": "История", "gift": "Подарок себе"}


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _integer(body: dict, key: str, default: int, low: int, high: int) -> int:
    value = body.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise ValidationError(f"{key}: укажите целое число от {low} до {high}")
    return value


def random_birthday(config: dict, account_id: int) -> dict | None:
    """Return a stable adult birthday so preview and execution always agree."""
    if config.get("birthday"):
        return config["birthday"]
    if not config.get("random_birthday", False):
        return None
    start = datetime.fromtimestamp(config["start_at"], timezone.utc).date()

    def years_before(value: date, years: int) -> date:
        try:
            return value.replace(year=value.year - years)
        except ValueError:  # 29 February in a non-leap target year
            return value.replace(year=value.year - years, day=28)

    # Every date in this interval represents an age from 24 through 42.
    earliest = years_before(start, 43) + timedelta(days=1)
    latest = years_before(start, 24)
    seed = f"{config['request_key']}:{account_id}:birthday".encode()
    offset = int.from_bytes(hashlib.sha256(seed).digest()[:8], "big") % ((latest - earliest).days + 1)
    chosen = earliest + timedelta(days=offset)
    return {"day": chosen.day, "month": chosen.month, "year": chosen.year}


def normalize(body: Any, *, now: float) -> dict:
    if not isinstance(body, dict):
        raise ValidationError("Нужен JSON-объект")
    ids = body.get("account_ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= 10 or any(type(i) is not int or i < 1 for i in ids):
        raise ValidationError("Выберите от 1 до 10 аккаунтов")
    ids = list(dict.fromkeys(ids))
    days = _integer(body, "days", 7, 1, 30)
    daily_joins = _integer(body, "daily_joins", 3, 1, 10)
    gap_minutes = _integer(body, "gap_minutes", 60, 30, 120)
    budget = _integer(body, "gift_budget", 0, 0, 1000)
    toggles = {}
    for key, default in (("avatar", True), ("bio", True), ("stories", True),
                         ("random_birthday", True), ("paid_gift", False)):
        value = body.get(key, default)
        if type(value) is not bool:
            raise ValidationError(f"{key}: нужен переключатель")
        toggles[key] = value
    if budget and not toggles["paid_gift"]:
        raise ValidationError("Подтвердите платный подарок или поставьте бюджет 0")
    if toggles["paid_gift"] and not budget:
        raise ValidationError("Укажите максимальную стоимость подарка в Stars")
    targets = body.get("targets", [])
    if not isinstance(targets, list) or len(targets) > 300 or any(not isinstance(t, str) or len(t) > 256 for t in targets):
        raise ValidationError("Чаты — список @username или ссылок Telegram")
    normalized = []
    for value in targets:
        target = explicit_join_target(value)
        if not target:
            raise ValidationError(f"Неверная ссылка на чат: {value[:60]}")
        if target not in normalized:
            normalized.append(target)
    if len(normalized) > days * daily_joins:
        raise ValidationError(f"За {days} дней помещается до {days * daily_joins} чатов. Увеличьте срок или сократите список.")
    birthday = body.get("birthday")
    if birthday is not None:
        validate_changes({"birthday": birthday})
    about = body.get("about", "")
    if not isinstance(about, str):
        raise ValidationError("Описание должно быть текстом")
    if about:
        validate_changes({"about": about})
    privacy = body.get("story_privacy", "contacts")
    if privacy not in ("contacts", "everyone"):
        raise ValidationError("Аудитория историй — контакты или все")
    start = now + 60
    if body.get("start_at"):
        try:
            when = datetime.fromisoformat(body["start_at"].replace("Z", "+00:00"))
            if when.tzinfo is None:
                raise ValueError
            start = when.timestamp()
        except (TypeError, ValueError, AttributeError):
            raise ValidationError("Проверьте время старта") from None
        if not now - 60 <= start <= now + 30 * DAY:
            raise ValidationError("Старт — сейчас или в ближайшие 30 дней")
        start = max(start, now + 10)
    request_key = body.get("request_key", "")
    if not isinstance(request_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_key):
        raise ValidationError("Не удалось определить запуск. Откройте форму заново")
    if not (toggles["avatar"] or toggles["bio"] or toggles["stories"] or
            toggles["random_birthday"] or birthday or normalized or budget):
        raise ValidationError("Выберите хотя бы одно действие")
    return {"account_ids": ids, "days": days, "daily_joins": daily_joins, "gap_minutes": gap_minutes,
            "gift_budget": budget, "birthday": birthday, "about": about, "story_privacy": privacy,
            "targets": normalized, "start_at": start, "request_key": request_key, **toggles}


def build(config: dict, account_id: int) -> list[dict]:
    """All actions and their content are fixed before the run starts."""
    steps = []
    start = config["start_at"]
    def add(kind: str, at: float, **values: Any) -> None:
        steps.append({"id": len(steps) + 1, "kind": kind, "due_at": at, "status": "pending", **values})
    if config["avatar"]:
        add("avatar", start)
    if config["bio"]:
        add("bio", start + 5 * 60, text=config["about"] or BIOS[account_id % len(BIOS)])
    birthday = random_birthday(config, account_id)
    if birthday:
        add("birthday", start + 10 * 60, birthday=birthday)
    targets = list(config["targets"])
    for day in range(config["days"]):
        base = start + day * DAY + 30 * 60
        joined_today = 0
        for index in range(config["daily_joins"]):
            if not targets:
                break
            add("join", base + index * config["gap_minutes"] * 60, target=targets.pop(0))
            joined_today += 1
        if config["stories"]:
            add("story", base + joined_today * config["gap_minutes"] * 60,
                text=CAPTIONS[day % len(CAPTIONS)], random_id=secrets.randbits(63) or 1,
                privacy=config["story_privacy"])
    if config["gift_budget"]:
        add("gift", start + (config["days"] - 1) * DAY + 23 * 3600, budget=config["gift_budget"])
    return sorted(steps, key=lambda step: (step["due_at"], step["id"]))


def public_steps(steps: list[dict]) -> list[dict]:
    return [{"id": s["id"], "kind": s["kind"], "label": STEP_LABELS[s["kind"]],
             "at": iso(s["due_at"]), "status": s["status"], "target": s.get("target"),
             "text": s.get("text"), "birthday": s.get("birthday"), "note": s.get("note"),
             "budget": s.get("budget"), "spent": s.get("spent", 0)} for s in steps]
