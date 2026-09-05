"""Общие помощники тестов: подписанный initData, пользователь и бот-заглушка.

Подпись initData нужна и тестам кабинета, и тестам оплаты вне Telegram. Две
копии одной HMAC-логики расходятся молча: сначала в тестах, потом в ожиданиях
от сервера, — поэтому она здесь одна.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

from app.config import settings
from app.db.database import session_scope
from app.db.models import Rule

TEST_USER_ID = 768_000_001


def sign_init_data(
    user_id: int = TEST_USER_ID,
    *,
    auth_date: int | None = None,
    token: str | None = None,
    corrupt_hash: bool = False,
    first_name: str = "Тест",
) -> str:
    """Подписанный initData ровно в том формате, какой шлёт Telegram.

    Важно: сервер сравнивает подпись по РАСКОДИРОВАННЫМ значениям (parse_qsl),
    поэтому подписывать надо исходные строки, а не urlencode-результат.
    """
    data = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(
            {"id": user_id, "first_name": first_name, "username": "tester"},
            ensure_ascii=False,
        ),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret = hmac.new(
        b"WebAppData", (token or settings.bot_token).encode(), hashlib.sha256
    ).digest()
    signature = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    data["hash"] = "0" * 64 if corrupt_hash else signature
    return urlencode(data)


class RecordingBot:
    """Бот-заглушка: запоминает отправленное и умеет назвать себя.

    Фоновые проверки и хендлеры общаются с Telegram через ``send_message`` и
    ``get_me`` — больше от бота ничего не нужно. ``fail_for`` изображает
    пользователя, который заблокировал бота: Bot API в этом случае отвечает
    ошибкой, и она не должна останавливать рассылку остальным.
    """

    def __init__(
        self, username: str = "my_forward_bot", *, fail_for: set[int] | None = None
    ) -> None:
        self.username = username
        self.fail_for = set(fail_for or ())
        self.messages: list[tuple[int, str]] = []
        # Клавиатуры держим отдельным списком: у сообщения бывает кнопка, и
        # проверять её приходится (письмо про выпавший аккаунт без кнопки
        # «Подключить заново» заставляет искать вход по меню).
        self.markups: list[object] = []
        # Отправленные файлы: (кому, имя файла, байты, подпись). Выгрузка
        # собранного уходит документом, и проверять надо именно содержимое —
        # «письмо ушло» о правильности CSV ничего не говорит.
        self.documents: list[tuple[int, str, bytes, str]] = []

    async def get_me(self) -> SimpleNamespace:
        return SimpleNamespace(username=self.username)

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        if chat_id in self.fail_for:
            raise RuntimeError("bot was blocked by the user")
        self.messages.append((chat_id, text))
        self.markups.append(kwargs.get("reply_markup"))

    async def send_document(self, chat_id: int, document, **kwargs) -> None:
        if chat_id in self.fail_for:
            raise RuntimeError("bot was blocked by the user")
        self.documents.append(
            (
                chat_id,
                getattr(document, "filename", ""),
                getattr(document, "data", b""),
                kwargs.get("caption") or "",
            )
        )

    @property
    def recipients(self) -> list[int]:
        return [chat_id for chat_id, _ in self.messages]


async def add_rule(user_id: int, account_id: int, **fields) -> None:
    """Задача с обязательным минимумом полей: остальное — по вкусу теста.

    Нужна везде, где проверяется «сколько задач встало»: и у выпавшего
    аккаунта, и у кончившегося абонемента. Обязательные колонки одни и те же,
    поэтому и заготовка одна.
    """
    async with session_scope() as session:
        session.add(
            Rule(
                user_id=user_id,
                account_id=account_id,
                source_id=-1001,
                target_id=-1002,
                **fields,
            )
        )
