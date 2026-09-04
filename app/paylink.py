"""Подписанная ссылка на внешнюю страницу оплаты.

Страница ``/pay`` живёт вне Telegram: там нет ``initData``, и подтвердить, что
пришёл именно наш пользователь, нечем. Поэтому ссылка сама несёт подпись:
``user_id.months.срок_годности.HMAC``. Ключ — ``SECRET_KEY``, тот же, которым
шифруются сессии, так что подделать токен без доступа к серверу нельзя.

Почему подпись, а не «просто user_id в адресе»:

* без подписи любой мог бы открыть ``/pay?user=123`` и создать счёт на чужого
  пользователя — а после оплаты абонемент достался бы не плательщику;
* срок годности (час) ограничивает и утечку ссылки из истории браузера: ссылка
  из вчерашнего чата уже ничего не создаёт.

Токен даёт право ровно на одно действие — создать счёт на свой user_id. Он не
пускает в кабинет и не читает данные: страница оплаты умеет только выставить
счёт и показать реквизиты.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from app.config import settings

# Час — компромисс: хватает, чтобы уйти в браузер, найти карту и оплатить,
# и мало, чтобы ссылка жила в истории как рабочий вход.
TOKEN_TTL_SECONDS = 60 * 60


@dataclass(frozen=True, slots=True)
class PayLink:
    """Разобранный токен: кому и за сколько месяцев выставлять счёт."""

    user_id: int
    months: int
    expires_at: int


def _sign(payload: str) -> str:
    digest = hmac.new(
        settings.secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).digest()
    # base64url без «=» — чтобы токен не ломался в адресной строке и логах.
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def make_token(user_id: int, months: int = 1, *, ttl: int = TOKEN_TTL_SECONDS) -> str:
    """Токен для страницы оплаты. Живёт ``ttl`` секунд."""
    payload = f"{int(user_id)}.{int(months)}.{int(time.time()) + int(ttl)}"
    return f"{payload}.{_sign(payload)}"


def parse_token(token: str) -> PayLink | None:
    """Разбирает токен. None — подпись не сходится, срок вышел или это мусор."""
    parts = (token or "").split(".")
    if len(parts) != 4:
        return None
    payload = ".".join(parts[:3])
    # compare_digest вместо == : сравнение подписи не должно зависеть от того,
    # на каком символе она разошлась.
    if not hmac.compare_digest(_sign(payload), parts[3]):
        return None
    try:
        user_id, months, expires_at = (int(part) for part in parts[:3])
    except ValueError:
        return None
    if user_id <= 0 or months <= 0:
        return None
    if expires_at < int(time.time()):
        return None
    return PayLink(user_id=user_id, months=months, expires_at=expires_at)


def pay_url(user_id: int, months: int = 1) -> str | None:
    """Полный адрес страницы оплаты для пользователя.

    None — внешний контур не настроен (нет адреса или нет ни одного способа):
    звать пользователя на страницу, где платить нечем, незачем.
    """
    base = settings.pay_page_url
    if not base or not settings.external_payment_methods():
        return None
    return f"{base}?{urlencode({'t': make_token(user_id, months)})}"
