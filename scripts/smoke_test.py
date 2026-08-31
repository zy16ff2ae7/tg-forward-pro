#!/usr/bin/env python3
"""Дымовой тест: БД, подписка, фильтры. Запуск: python scripts/smoke_test.py"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from app.config import settings
from app.db import repo
from app.db.database import SessionLocal, dispose_db, init_db
from app.security import decrypt_session, encrypt_session
from app.telegram_client.filters import (
    FilterConfig,
    default_filters,
    parse_words,
    should_forward,
    transform_text,
)


class FakeMessage:
    """Минимальная замена telegram-сообщения для проверки фильтров."""

    def __init__(self, text: str = "", kind: str = "text", forwarded: bool = False):
        self.message = text
        self.forward = object() if forwarded else None
        if kind != "text":
            self.media = type("Media", (), {kind: object()})()
        else:
            self.media = None


async def main() -> None:
    await init_db()
    print("✓ Таблицы созданы:", settings.database_url.split("///")[-1])

    user_id = 999_000_111

    async with SessionLocal() as session:
        user, created = await repo.get_or_create_user(
            session, user_id, username="tester", full_name="Тестовый Пользователь"
        )
        trial_until = await repo.grant_trial(session, user_id)
        await session.commit()

        print(f"✓ Пользователь создан: {created}, пробный период до {trial_until}")

        active = await repo.has_active_subscription(session, user_id)
        print(f"✓ Подписка активна после триала: {active}")

        account = await repo.add_account(
            session,
            user_id=user_id,
            phone="+79000000000",
            session_encrypted=encrypt_session("fake-telethon-session-string"),
        )
        await session.commit()

        rule = await repo.add_rule(
            session,
            user_id=user_id,
            account_id=account.id,
            source_id=-1001234567890,
            source_title="Новости",
            target_id=-1009876543210,
            target_title="Мой канал",
        )
        rule.filters = default_filters()
        await session.commit()
        rule_id = rule.id
        print(f"✓ Аккаунт и правило #{rule_id} созданы")

        # Шифрование сессий туда-обратно
        restored = await session.get(type(account), account.id)
        assert restored is not None
        assert decrypt_session(restored.session_encrypted) == "fake-telethon-session-string"
        print("✓ Сессия шифруется и расшифровывается")

        loaded = await repo.get_rule(session, rule_id, user_id)
        assert loaded is not None
        print(f"✓ Правило читается: {loaded.source_title} → {loaded.target_title}")

    # ── Фильтры ──
    config = FilterConfig.from_dict(
        {
            "blacklist": ["реклама"],
            "media_types": ["text", "photo"],
            "remove_links": True,
            "replace": [{"from": "конкурент", "to": "я"}],
            "append_text": "Подписывайтесь!",
        }
    )
    assert not should_forward(FakeMessage("тут реклама"), config), "стоп-слово должно отсекать"
    assert not should_forward(FakeMessage("видео", kind="video"), config), "видео не в списке медиа"
    assert should_forward(FakeMessage("обычный пост"), config)
    assert not should_forward(FakeMessage("репост", forwarded=True), FilterConfig.from_dict({"skip_forwards": True}))

    out = transform_text("Ссылка t.me/other и текст конкурента", config)
    assert "t.me/other" not in out and "я" in out and out.endswith("Подписывайтесь!")
    print("✓ Фильтры и замены работают:", out)

    assert parse_words("слово1, слово2\nслово3") == ["слово1", "слово2", "слово3"]
    print("✓ Разбор списков слов работает")

    # ── Окончание подписки ──
    async with SessionLocal() as session:
        sub = await repo.get_subscription(session, user_id)
        assert sub is not None
        sub.active_until = repo.utcnow() - timedelta(days=1)
        await session.commit()
        active = await repo.has_active_subscription(session, user_id)
        print(f"✓ После истечения срока подписка неактивна: {not active}")

        until = await repo.activate_subscription(session, user_id, 1)
        await session.commit()
        print(f"✓ Продление на месяц: до {until:%d.%m.%Y}")

        user = await repo.get_user(session, user_id)
        if user is not None:
            await session.delete(user)
            await session.commit()
            print("✓ Тестовые данные удалены")

    await dispose_db()
    print("\nВсе проверки пройдены ✅")


if __name__ == "__main__":
    asyncio.run(main())
