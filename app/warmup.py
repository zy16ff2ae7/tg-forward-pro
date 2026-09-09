"""Durable account automation. One scheduled action per account at a time."""
from __future__ import annotations

import asyncio
import io
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from PIL import Image
from sqlalchemy import select
from telethon.errors import FloodWaitError, PeerFloodError, RPCError
from telethon.tl import functions, types

from app import account_profile, join_queue, warmup_plan
from app.db import repo
from app.db.database import SessionLocal
from app.db.models import Rule
from app.errors import AppError, ConflictError
from app.telegram_client.forwarder import subscription_active

ASSETS = Path(__file__).resolve().parent.parent / "webapp" / "assets" / "warmup"
_workers: dict[int, tuple[asyncio.Task, int]] = {}
_create_locks: dict[int, asyncio.Lock] = {}
TERMINAL = {"done", "skipped"}
JOURNAL_STATUSES = {"running", "done", "skipped", "waiting", "blocked", "uncertain"}


class Deferred(Exception):
    def __init__(self, note: str, until: float):
        super().__init__(note)
        self.until = until


class Skipped(Exception):
    pass


class Uncertain(Exception):
    pass


def create_lock(user_id: int) -> asyncio.Lock:
    return _create_locks.setdefault(user_id, asyncio.Lock())


def busy_account(account_id: int) -> bool:
    return any(not worker.done() and account == account_id for worker, account in _workers.values())


def report(rule: Rule) -> dict:
    raw = rule.filters or {}
    config = raw.get("warmup") or {}
    steps = list(raw.get("warmup_steps") or [])
    done = sum(s.get("status") in TERMINAL for s in steps)
    next_step = next((s for s in steps if s.get("status") not in TERMINAL), None)
    status = "done" if next_step is None else "scheduled"
    if next_step and next_step.get("status") in ("uncertain", "running"):
        status = "running" if rule.id in _workers else "review"
    if not rule.enabled and status not in ("done", "review"):
        status = "paused"
    due = None
    if next_step:
        due = due_at(raw, next_step)
    return {"state": status, "days": config.get("days", 7), "done": done, "total": len(steps),
            "next_at": warmup_plan.iso(due) if due is not None else None,
            "next_action": warmup_plan.STEP_LABELS.get(next_step["kind"]) if next_step else None,
            "next_step_id": next_step["id"] if next_step else None,
            "can_skip": bool(next_step and next_step["status"] in ("uncertain", "blocked", "running") and not busy_account(rule.account_id)),
            "note": next_step.get("note") if next_step else "Сценарий завершён",
            "spent": sum(s.get("spent", 0) for s in steps), "gift_budget": config.get("gift_budget", 0),
            "steps": warmup_plan.public_steps(steps)}


def due_at(raw: dict, step: dict) -> float:
    due = max(float(step["due_at"]), float(raw.get("warmup_last_action") or 0) + 60)
    if step["kind"] == "join":
        gap = int((raw.get("warmup") or {}).get("gap_minutes", 60)) * 60
        due = max(due, float(raw.get("warmup_last_join") or 0) + gap)
    if step["kind"] == "story":
        due = max(due, float(raw.get("warmup_last_story") or 0) + warmup_plan.DAY)
    return due


async def _mark(rule: Rule, step_id: int, *, disable: bool = False, **values: Any) -> None:
    async with join_queue.rule_lock(rule.id), SessionLocal() as session:
        stored = await repo.get_rule(session, rule.id, rule.user_id)
        if stored is None:
            if values.get("payment_started"):
                raise ConflictError("Сценарий удалён — оплата отменена")
            return
        if values.get("payment_started") and (not stored.enabled or stored.archived):
            raise ConflictError("Сценарий остановлен — оплата отменена")
        raw = dict(stored.filters or {})
        steps = [dict(s) for s in raw.get("warmup_steps", [])]
        event = None
        now = time.time()
        for step in steps:
            if step["id"] == step_id:
                previous = {key: step.get(key) for key in ("status", "note", "due_at")}
                step.update(values)
                status = values.get("status")
                if status == "running":
                    step["started_at"] = now
                    # A manually resumed blocked attempt has its own event in
                    # ForwardLog.  Keeping the old completion here would make
                    # the current attempt appear finished before it started.
                    step.pop("finished_at", None)
                    step["attempts"] = int(step.get("attempts") or 0) + 1
                if status in TERMINAL | {"blocked", "uncertain"}:
                    step["finished_at"] = now
                step["updated_at"] = now
                if values.get("status") in TERMINAL:
                    raw["warmup_last_action"] = now
                    if step["kind"] == "join":
                        raw["warmup_last_join"] = now
                    if step["kind"] == "story":
                        raw["warmup_last_story"] = now
                if status in JOURNAL_STATUSES and any(
                    previous[key] != step.get(key) for key in previous
                ):
                    event = dict(step)
                break
        raw["warmup_steps"] = steps
        stored.filters = raw
        if disable:
            stored.enabled = False
        if event is not None:
            status = event["status"]
            label = warmup_plan.STEP_LABELS.get(event["kind"], event["kind"])
            target = f" · {event['target']}" if event.get("target") else ""
            note = str(event.get("note") or "Без пояснения")
            prefixes = {
                "running": "▶️ Начато",
                "done": "✅ Выполнено",
                "skipped": "⏭ Пропущено",
                "waiting": "⏳ Перенесено",
                "blocked": "⛔ Остановлено",
                "uncertain": "⚠️ Нужна проверка",
            }
            if status == "waiting":
                note += f" · следующая попытка {warmup_plan.iso(float(event['due_at']))}"
            await repo.log_forward(session, rule_id=rule.id, user_id=rule.user_id,
                source_msg_id=0, target_msg_id=None,
                status="ok" if status == "done" else "error" if status in {"blocked", "uncertain"} else "info",
                error=f"{prefixes[status]}: {label}{target} — {note}")
        await session.commit()


def _media(name: str) -> io.BytesIO:
    # Names come only from the preset, never from a user-supplied path.
    with Image.open(ASSETS / name) as source:
        output = io.BytesIO()
        source.convert("RGB").save(output, "JPEG", quality=88)
        output.name = "warmup.jpg"
        output.seek(0)
        return output


async def execute(manager: Any, rule: Rule, step: dict) -> dict:
    """Perform one explicitly planned action; no retries or random activity."""
    from app.telegram_client import jobs
    from app.telegram_client.manager import _snapshot

    client = manager.profile_client(rule.account_id)
    kind = step["kind"]
    config = (rule.filters or {})["warmup"]
    if kind in ("avatar", "bio", "birthday"):
        profile = await account_profile.read_profile(client)
        if kind == "avatar":
            if profile["has_photo"]:
                raise Skipped("Аватарка уже есть — оставлена без изменений")
            changes = {"photo": _media("avatar.png")}
        elif kind == "bio":
            if profile["about"]:
                raise Skipped("Описание уже заполнено — оставлено без изменений")
            changes = {"about": step["text"]}
        else:
            if profile["birthday"]:
                raise Skipped("Дата рождения уже заполнена — оставлена без изменений")
            changes = {"birthday": step["birthday"]}
        await manager.update_account_profile(rule.account_id, changes)
        return {"note": "Заполнено пустое поле аккаунта"}
    if kind == "join":
        snapshot = _snapshot(rule)
        async with SessionLocal() as session:
            done_today = await repo.count_joins_today(session, rule.id)
        if done_today >= config["daily_joins"] or await jobs._join_account_allowance(snapshot) <= 0:
            tomorrow = (int(time.time()) // warmup_plan.DAY + 1) * warmup_plan.DAY
            raise Deferred("Дневной лимит вступлений — продолжение завтра", tomorrow)
        target = step["target"]
        if target.startswith("+") or target.lower().startswith("joinchat/"):
            invite = target[1:] if target.startswith("+") else target.split("/", 1)[1]
            request = functions.messages.ImportChatInviteRequest(invite)
        else:
            try:
                peer = await client.get_input_entity(target)
            except (TypeError, ValueError):
                raise Skipped("Чат не найден — проверьте ссылку") from None
            if isinstance(peer, types.InputPeerUser):
                raise Skipped("Указан пользователь или бот — вступить можно только в канал или группу")
            if not isinstance(peer, types.InputPeerChannel):
                raise Skipped("Ссылка ведёт не на публичный канал или группу")
            request = functions.channels.JoinChannelRequest(peer)
        try:
            await client(request, flood_sleep_threshold=0)
        except RPCError as exc:
            name = type(exc).__name__
            if name == "UserAlreadyParticipantError":
                raise Skipped("Уже участник чата") from None
            if name == "InviteRequestSentError":
                await jobs._log_join(snapshot)
                return {"note": "Заявка на вступление отправлена"}
            raise
        await jobs._log_join(snapshot)
        return {"note": "Вступление выполнено"}
    if kind == "story":
        allowed = await client(functions.stories.CanSendStoryRequest(types.InputPeerSelf()), flood_sleep_threshold=0)
        if getattr(allowed, "count_remains", 0) <= 0:
            raise Skipped("Telegram пока не разрешает публикацию истории")
        uploaded = await client.upload_file(_media("story.png"))
        privacy = types.InputPrivacyValueAllowContacts() if step["privacy"] == "contacts" else types.InputPrivacyValueAllowAll()
        await client(functions.stories.SendStoryRequest(
            peer=types.InputPeerSelf(), media=types.InputMediaUploadedPhoto(uploaded),
            privacy_rules=[privacy], caption=step["text"], random_id=step["random_id"],
            period=86400, pinned=False), flood_sleep_threshold=0)
        return {"note": "История опубликована на 24 часа"}
    if kind == "gift":
        if step.get("payment_started"):
            raise Uncertain("Оплата уже была начата. Проверьте подарок и баланс вручную")
        if not config.get("paid_gift") or not 0 < step.get("budget", 0) <= config.get("gift_budget", 0):
            raise ConflictError("Платный подарок не разрешён в плане")
        catalog = await client(functions.payments.GetStarGiftsRequest(hash=0), flood_sleep_threshold=0)
        choices = [g for g in catalog.gifts if type(getattr(g, "stars", None)) is int
                   and 0 < g.stars <= step["budget"] and not getattr(g, "sold_out", False)
                   and not getattr(g, "limited", False) and not getattr(g, "require_premium", False)]
        if not choices:
            raise Skipped("В пределах бюджета нет доступного обычного подарка")
        gift = min(choices, key=lambda g: g.stars)
        invoice = types.InputInvoiceStarGift(peer=types.InputPeerSelf(), gift_id=gift.id, include_upgrade=False)
        form = await client(functions.payments.GetPaymentFormRequest(invoice=invoice), flood_sleep_threshold=0)
        if not isinstance(form, (types.payments.PaymentFormStarGift, types.payments.PaymentFormStars)):
            raise ConflictError("Telegram вернул неподдерживаемый способ оплаты")
        prices = form.invoice.prices
        if form.invoice.currency != "XTR" or not prices or any(p.amount < 0 for p in prices):
            raise ConflictError("Не удалось проверить цену подарка в Stars")
        cost = sum(p.amount for p in prices)
        if cost != gift.stars or not 0 < cost <= step["budget"]:
            raise ConflictError("Цена подарка изменилась или превышает бюджет")
        # An interrupted payment is never repeated automatically, even after reboot.
        await _mark(rule, step["id"], payment_started=True, gift_id=gift.id, quoted_cost=cost)
        step["payment_started"] = True
        try:
            result = await client(functions.payments.SendStarsFormRequest(form_id=form.form_id, invoice=invoice), flood_sleep_threshold=0)
        except Exception as exc:
            if isinstance(exc, FloodWaitError):
                await manager.note_join_wait(rule, int(exc.seconds))
            elif isinstance(exc, PeerFloodError):
                await manager.note_peer_flood(rule.account_id, _snapshot(rule), " (автопрогрев)")
            raise Uncertain("Результат оплаты неизвестен. Проверьте подарок и баланс вручную") from exc
        if not isinstance(result, types.payments.PaymentResult):
            raise Uncertain("Telegram запросил дополнительную проверку оплаты. Проверьте подарок и баланс вручную")
        return {"note": "Подарок себе оплачен", "spent": cost}
    raise ConflictError("Неизвестный шаг сценария")


async def _run_step(manager: Any, rule_id: int, user_id: int) -> None:
    lock = manager._oneshot_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        await _run_locked_step(manager, rule_id, user_id)


async def _run_locked_step(manager: Any, rule_id: int, user_id: int) -> None:
    async with join_queue.rule_lock(rule_id), SessionLocal() as session:
        rule = await repo.get_rule(session, rule_id, user_id)
        if rule is None or not rule.enabled or rule.archived:
            return
        raw = rule.filters or {}
        step = next((dict(s) for s in raw.get("warmup_steps", []) if s["status"] not in TERMINAL), None)
    if not step:
        return
    if step["status"] in ("running", "uncertain") or step.get("payment_started"):
        await _mark(rule, step["id"], status="uncertain", disable=True,
                    note="Предыдущий шаг прерван. Проверьте результат в Telegram и пропустите этот шаг для продолжения")
        return
    if due_at(raw, step) > time.time() or not manager.is_online(rule.account_id):
        return
    if not await subscription_active(rule.user_id):
        return
    paused_until = manager.sending_paused_until(rule.account_id)
    if paused_until:
        await _mark(rule, step["id"], status="waiting", due_at=max(step["due_at"], paused_until),
                    note="Ожидает окончания защитной паузы аккаунта")
        return
    # Recheck pause/archive after the asynchronous subscription lookup.
    async with SessionLocal() as session:
        latest = await repo.get_rule(session, rule.id, rule.user_id)
        if latest is None or not latest.enabled or latest.archived:
            return
    await _mark(rule, step["id"], status="running", note="Выполняется")
    try:
        result = await execute(manager, rule, step)
        await _mark(rule, step["id"], status="done", **result)
    except Skipped as exc:
        await _mark(rule, step["id"], status="skipped", note=str(exc))
    except Deferred as exc:
        await _mark(rule, step["id"], status="waiting", due_at=exc.until, note=str(exc))
    except FloodWaitError as exc:
        await manager.note_join_wait(rule, int(exc.seconds))
        await _mark(rule, step["id"], status="waiting", due_at=time.time() + int(exc.seconds),
                    note=f"Telegram просит подождать {int(exc.seconds)} сек")
    except PeerFloodError:
        from app.telegram_client.manager import _snapshot
        await manager.note_peer_flood(rule.account_id, _snapshot(rule), " (автопрогрев)")
        await _mark(rule, step["id"], status="blocked", disable=True,
                    note="Telegram ограничил аккаунт. Сценарий остановлен; проверьте @SpamBot")
    except AppError as exc:
        if exc.details.get("code") == "PeerFloodError":
            from app.telegram_client.manager import _snapshot
            await manager.note_peer_flood(rule.account_id, _snapshot(rule), " (автопрогрев)")
            await _mark(rule, step["id"], status="blocked", disable=True,
                        note="Telegram ограничил аккаунт. Сценарий остановлен; проверьте @SpamBot")
        elif exc.status == 429 and exc.details.get("retry_at"):
            until = datetime.fromisoformat(exc.details["retry_at"]).timestamp()
            await manager.note_join_wait(rule, max(1, int(until - time.time())))
            await _mark(rule, step["id"], status="waiting", due_at=until, note=exc.message)
        else:
            await _mark(rule, step["id"], status="blocked", disable=True, note=exc.message)
    except RPCError as exc:
        name = type(exc).__name__
        if name in ("PremiumAccountRequiredError", "StoriesTooMuchError") and step["kind"] == "story":
            await _mark(rule, step["id"], status="skipped", note="Истории пока недоступны для аккаунта: " + name)
        elif "Flood" in name and getattr(exc, "seconds", None):
            await _mark(rule, step["id"], status="waiting", due_at=time.time() + int(exc.seconds), note="Лимит Telegram: " + name)
        else:
            await _mark(rule, step["id"], status="blocked", disable=True, note="Telegram отклонил шаг: " + name)
    except (OSError, TimeoutError, Uncertain, asyncio.CancelledError):
        await _mark(rule, step["id"], status="uncertain", disable=True,
                    note="Результат шага неизвестен. Проверьте Telegram перед продолжением; автоматического повтора не будет")
    except Exception:
        logger.exception("Warmup step failed for rule #{}", rule.id)
        await _mark(rule, step["id"], status="uncertain", disable=True,
                    note="Шаг прерван ошибкой сервиса. Проверьте результат в Telegram")


async def tick(manager: Any) -> None:
    async with SessionLocal() as session:
        rules = list((await session.scalars(select(Rule).where(
            Rule.kind == "warmup", Rule.enabled.is_(True), Rule.archived.is_(False)))).all())
    for rule in rules:
        if busy_account(rule.account_id) or join_queue.busy_account(rule.account_id):
            continue
        steps = (rule.filters or {}).get("warmup_steps", [])
        if not any(s["status"] not in TERMINAL for s in steps):
            continue
        worker = asyncio.create_task(_run_step(manager, rule.id, rule.user_id))
        _workers[rule.id] = (worker, rule.account_id)
        def finished(task, key=rule.id):
            if _workers.get(key, (None,))[0] is task:
                _workers.pop(key, None)
            if not task.cancelled() and task.exception():
                logger.error("Warmup #{} failed to persist progress: {}", key, type(task.exception()).__name__)
        worker.add_done_callback(finished)


async def cancel_inactive(active_ids: set[int] | None = None, *, account_id: int | None = None) -> None:
    tasks = [task for rule_id, (task, account) in list(_workers.items())
             if (active_ids is not None and rule_id not in active_ids) or account == account_id]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def skip_uncertain(rule: Rule) -> None:
    if busy_account(rule.account_id):
        raise ConflictError("Дождитесь остановки текущего шага")
    step = next((s for s in (rule.filters or {}).get("warmup_steps", []) if s["status"] not in TERMINAL), None)
    if step is None or step["status"] not in ("uncertain", "blocked", "running"):
        raise ConflictError("Нет шага, требующего ручной проверки")
    await _mark(rule, step["id"], status="skipped", note="Пропущено пользователем после проверки в Telegram")
