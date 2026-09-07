"""Приём USDT (TRC-20) с автопроверкой через TronGrid.

Схема без генерации адресов: каждому платежу выдаётся уникальная сумма
(например 12.017 вместо 12.000), по ней и находим перевод на общем кошельке.
Отсюда два обязательных условия, без которых схема начинает зачислять чужое:

* метка-сумма уникальна среди **ожидающих** платежей (``reserve_memo``);
* найденный перевод привязывается к платежу по хешу транзакции (``tx_id``),
  и один хеш закрывает ровно один платёж.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiohttp
from loguru import logger

from app import referral
from app.config import settings

TRONGRID_URL = "https://api.trongrid.io/v1/accounts/{address}/transactions/trc20"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_DECIMALS = 6
# Шаг метки: 0.001 USDT = 1000 микроединиц контракта.
MEMO_STEP_MICRO = 1000
# Сколько сдвигов метки пробуем, прежде чем сдаться (0.5 USDT сверху максимум)
MEMO_MAX_TRIES = 500
# Сколько ждём перевод. Срок щедрый (в сети TRC-20 перевод идёт минуты), но
# конечный: метка-сумма у брошенного счёта иначе занята навсегда, а лимит
# висящих счетов постепенно запирает человека без нового счёта.
PENDING_TTL = timedelta(hours=72)


def is_configured() -> bool:
    # Единый источник правды — settings.usdt_ready: он дополнительно
    # проверяет, что кошелёк похож на настоящий адрес TRC-20.
    return settings.usdt_ready


def to_micro(amount: float | str) -> int:
    """USDT → целые микроединицы контракта. Только так и можно сравнивать суммы.

    Раньше перевод искался с допуском ``abs(value - expected) < 0.001``. Метки
    соседних платежей отличаются ровно на 0.001, а разность double для таких
    чисел выходит вроде 0.00099999999999989 — меньше допуска. То есть перевод
    по метке 12.018 попадал и в проверку платежа с меткой 12.017: доступ
    получал не тот, кто заплатил. В целых микроединицах этой щели нет.
    """
    return int(round(float(amount) * (10**USDT_DECIMALS)))


def unique_amount(base_amount: float, payment_id: int) -> float:
    """Уникальная сумма к оплате: к базовой цене прибавляем номер платежа/1000.

    Номер берём по модулю 1000: иначе сквозной счётчик всех платежей уведёт
    цену в бесконечность (тариф 12 USDT после ~12 тыс. платежей стал бы 24).
    Коллизии стартовых меток разруливает reserve_memo сдвигом вверх.
    """
    return round(base_amount + (payment_id % 1000) / 1000.0, 3)


async def reserve_memo(session, base_amount: float, payment_id: int) -> str:
    """Подбирает метку-сумму, не занятую другим ожидающим платежом.

    ``base_amount + payment_id/1000`` сам по себе уникальности не даёт: цены
    разных тарифов отличаются на целые единицы, поэтому платёж #1500 по цене 10
    и платёж #500 по цене 11 дают одну и ту же метку 11.500. Кто заплатит —
    неизвестно, а зачтётся первому найденному. Поэтому занятые метки исключаем
    и сдвигаем свою на 0.001 вверх, пока не найдём свободную.
    """
    from app.db import repo

    taken = await repo.reserved_memos(session, "usdt")
    micro = to_micro(unique_amount(base_amount, payment_id))
    for _ in range(MEMO_MAX_TRIES):
        memo = f"{micro / (10**USDT_DECIMALS):.3f}"
        if memo not in taken:
            return memo
        micro += MEMO_STEP_MICRO
    # Столько одновременных платежей — это уже не «занято», а что-то не так.
    logger.error("USDT: не нашли свободную метку для платежа #{}", payment_id)
    return f"{micro / (10**USDT_DECIMALS):.3f}"


async def _fetch_transactions(session: aiohttp.ClientSession, since_ms: int = 0) -> list[dict]:
    url = TRONGRID_URL.format(address=settings.usdt_wallet)
    params = {
        "limit": "100",
        "only_to": "true",
        "contract_address": USDT_CONTRACT,
        "only_confirmed": "true" if _confirmed_only() else "false",
    }
    if since_ms:
        params["min_timestamp"] = str(since_ms)
    headers = {}
    if settings.trongrid_api_key:
        headers["TRON-PRO-API-KEY"] = settings.trongrid_api_key

    async with session.get(url, params=params, headers=headers, timeout=30) as response:
        if response.status != 200:
            logger.warning("TronGrid вернул статус {}", response.status)
            return []
        payload = await response.json(content_type=None)
    return payload.get("data") or []


def _confirmed_only() -> bool:
    """Брать ли у TronGrid только подтверждённые переводы.

    У метода нет «минимум N подтверждений» — только флаги confirmed /
    unconfirmed. Поэтому USDT_MIN_CONFIRMATIONS читается как выключатель:
    >0 — только подтверждённые (по умолчанию), 0 — брать и свежие:
    зачисление быстрее, но перевод теоретически могут откатить.
    """
    try:
        return int(settings.usdt_min_confirmations or 0) > 0
    except (TypeError, ValueError):
        return True


def _since_ms(since: datetime | None) -> int:
    """Метка времени для TronGrid. В БД даты — UTC без tzinfo."""
    if since is None:
        return 0
    moment = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    # Минута назад: между записью платежа и появлением перевода в индексе
    # TronGrid бывает расхождение часов, и слишком строгая граница режет свой же
    # перевод.
    return max(0, int(moment.timestamp() * 1000) - 60_000)


def _is_our_usdt(tx: dict) -> bool:
    """Наш ли это перевод: тот контракт, тот кошелёк, тот токен.

    Символ токена подделывается тривиально — свой контракт с символом «USDT»
    выпускает любой. Поэтому решает адрес контракта, а получатель проверяется
    отдельно: ``only_to`` в запросе — обещание TronGrid, а не наша проверка.
    """
    info = tx.get("token_info") or {}
    if (info.get("address") or "") != USDT_CONTRACT:
        return False
    wallet = (settings.usdt_wallet or "").strip()
    return bool(wallet) and (tx.get("to") or "") == wallet


def _match_amount(transactions: list[dict], expected_amount: float | str) -> list[dict]:
    """Переводы из готового списка ровно на ``expected_amount`` (свежие — первыми).

    Отдельно от запроса к TronGrid, чтобы проверка пачки платежей ходила в
    сеть один раз, а не по разу на каждый висящий счёт.
    """
    expected_micro = to_micro(expected_amount)
    matches: list[dict] = []
    for tx in transactions:
        if not _is_our_usdt(tx):
            continue
        try:
            value_micro = int(tx.get("value", 0))
        except (TypeError, ValueError):
            continue
        if value_micro == expected_micro:
            matches.append(tx)
    return matches


async def find_incoming_matches(
    expected_amount: float | str, since: datetime | None = None
) -> list[dict]:
    """Все входящие переводы ровно на ``expected_amount`` (свежие — первыми).

    Список, а не первый попавшийся: если две метки всё же совпали, по сумме
    подойдут два разных перевода, и платёж должен взять тот, который ещё никому
    не зачтён. С единственным результатом второй плательщик остался бы без
    доступа при том, что деньги пришли.
    """
    if not is_configured():
        return []

    try:
        async with aiohttp.ClientSession() as http:
            transactions = await _fetch_transactions(http, _since_ms(since))
    except Exception as exc:  # noqa: BLE001
        logger.warning("TronGrid недоступен: {}", exc)
        return []

    return _match_amount(transactions, expected_amount)


async def find_incoming(
    expected_amount: float | str, since: datetime | None = None
) -> dict | None:
    """Первый входящий перевод ровно на ``expected_amount``."""
    matches = await find_incoming_matches(expected_amount, since)
    return matches[0] if matches else None


async def check_pending(bot) -> int:
    """Проверяет ожидающие крипто-платежи. Возвращает количество зачисленных."""
    from app.db import repo
    from app.db.database import SessionLocal, session_scope
    from app.db.models import Payment

    if not is_configured():
        return 0

    # Брошенные счёта закрываем сразу: каждый из них — занятая метка-сумма и
    # лишний запрос в TronGrid на каждом проходе.
    async with session_scope() as session:
        dropped = await repo.expire_stale_payments(session, "usdt", older_than=PENDING_TTL)
    if dropped:
        logger.info("USDT: закрыли {} брошенных счетов", dropped)

    async with SessionLocal() as session:
        payments = list(await repo.pending_payments(session, "usdt"))

    if not payments:
        return 0

    # Один запрос к TronGrid на всех: переводы одни и те же, а висящих счетов
    # может быть много — иначе упрёмся в троттлинг API. Граница «с» — самая
    # ранняя из счетов, чтобы не отрезать чужой перевод.
    since_all = min((p.created_at for p in payments if p.created_at), default=None)
    try:
        async with aiohttp.ClientSession() as http:
            transactions = await _fetch_transactions(http, _since_ms(since_all))
    except Exception as exc:  # noqa: BLE001
        logger.warning("TronGrid недоступен: {}", exc)
        return 0

    activated = 0
    for payment in payments:
        if not payment.memo:
            continue
        try:
            to_micro(payment.memo)
        except (TypeError, ValueError):
            logger.warning("Платёж #{}: метка «{}» не похожа на сумму", payment.id, payment.memo)
            continue

        matches = _match_amount(transactions, payment.memo)
        candidates = [
            tx_id
            for tx_id in (str(tx.get("transaction_id") or "").strip() for tx in matches)
            if tx_id
        ]
        if not candidates:
            continue

        # Каждый платёж считается отдельной транзакцией БД: сбой на одном не
        # должен откатывать зачисление остальных.
        until = None
        reward = None
        friend_name = "друг"
        try:
            async with session_scope() as session:
                fresh = await session.get(Payment, payment.id)
                if fresh is None or fresh.status != "pending":
                    continue
                # Один перевод закрывает ровно один платёж: берём первый хеш,
                # который ещё никому не зачтён. Иначе повторная проверка
                # начисляла бы месяц за тот же перевод по кругу.
                free = None
                for tx_id in candidates:
                    if await repo.payment_with_tx(session, tx_id) is None:
                        free = tx_id
                        break
                if free is None:
                    logger.warning(
                        "Платёж #{}: все переводы на {} USDT уже зачтены другим платежам",
                        payment.id,
                        payment.memo,
                    )
                    continue
                if not await repo.claim_payment(session, fresh, tx_id=free):
                    logger.info("Платёж #{} закрыт кем-то другим — пропускаем", payment.id)
                    continue
                until = await repo.activate_subscription(session, fresh.user_id, fresh.months)
                # Первый оплаченный абонемент — награда пригласившему.
                reward = await repo.reward_referrer(
                    session, fresh.user_id,
                    settings.referral_days, referral.discount_percent(),
                )
                friend = await repo.get_user(session, fresh.user_id)
                if friend is not None:
                    friend_name = friend.mention
        except Exception as exc:  # noqa: BLE001 — один платёж не рушит проверку
            logger.exception("Платёж #{}: не смогли зачислить: {}", payment.id, exc)
            continue

        if until is None:
            continue
        activated += 1
        try:
            await bot.send_message(
                payment.user_id,
                "✅ <b>Оплата USDT получена</b>\n\n"
                f"Абонемент активен до <b>{until:%d.%m.%Y %H:%M}</b> (UTC).\n"
                "Пересылка продолжает работать.",
            )
        except Exception:  # noqa: BLE001
            logger.debug("Не смогли уведомить {} об оплате", payment.user_id)
        if reward is not None:
            referrer_id, deal_code = reward
            try:
                await bot.send_message(
                    referrer_id,
                    referral.referrer_reward_message(
                        friend_name, settings.referral_days, deal_code
                    ),
                )
            except Exception:  # noqa: BLE001 — награда начислена, весть вторична
                logger.debug("Не смогли уведомить {} о награде", referrer_id)
    return activated
