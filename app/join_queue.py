"""Background join jobs with durable progress and explicit manual resume."""
import asyncio
import time
from contextvars import ContextVar
from functools import wraps
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from app.db import repo
from app.db.database import SessionLocal

_current: ContextVar[tuple[Any, dict[str, Any]] | None] = ContextVar("join_job", default=None)
_jobs: dict[int, tuple[asyncio.Task, int]] = {}
_rule_locks: dict[int, asyncio.Lock] = {}


def rule_lock(rule_id: int) -> asyncio.Lock:
    return _rule_locks.setdefault(rule_id, asyncio.Lock())


def edit_guard(handler):
    @wraps(handler)
    async def guarded(request):
        async with rule_lock(int(request.match_info["task_id"])):
            return await handler(request)
    return guarded


def running(rule_id: int) -> bool:
    item = _jobs.get(rule_id)
    return bool(item and not item[0].done())


def view(rule: Any) -> dict[str, Any]:
    report = dict((rule.filters or {}).get("join_queue") or {})
    if report.get("state") == "running" and not running(rule.id):
        report.update(state="stopped", error="Запуск прерван. Нажмите «Запустить», чтобы продолжить.")
    return report


async def save(rule: Any, report: dict[str, Any]) -> None:
    async with rule_lock(rule.id), SessionLocal() as session:
        stored = await repo.get_rule(session, rule.id, rule.user_id)
        if stored:
            raw = dict(stored.filters or {})
            raw["join_queue"] = report
            stored.filters = raw
            await session.commit()


async def progress(target: str | None = None, status: str | None = None, **values: Any) -> None:
    context = _current.get()
    if context is None:
        return
    rule, report = context
    if target is not None:
        report["current"] = target
    if status:
        items = dict(report.get("items") or {})
        items[target] = {"status": status, **values}
        report["items"] = items
    else:
        report.update(values)
    await save(rule, report)


def active() -> bool:
    return _current.get() is not None


def completed_targets() -> set[str]:
    context = _current.get()
    if not context:
        return set()
    return {target for target, item in context[1].get("items", {}).items()
            if item.get("status") in ("joined", "already", "requested")}


async def start(manager: Any, rule: Any) -> dict[str, Any]:
    if running(rule.id):
        return {"ok": True, "queued": True}
    if any(not task.done() and account_id == rule.account_id for task, account_id in _jobs.values()):
        return {"ok": False, "error": "На этом аккаунте уже идёт очередь вступлений"}
    # Read the latest settings after an editor commits. Once the lock is released,
    # reserve without awaiting so an editor either sees running or finishes first.
    async with rule_lock(rule.id), SessionLocal() as session:
        latest = await repo.get_rule(session, rule.id, rule.user_id)
        if latest is None or not latest.enabled or latest.archived:
            return {"ok": False, "error": "Задача удалена или не активна"}
        rule = latest
    if running(rule.id):
        return {"ok": True, "queued": True}
    if any(not task.done() and account_id == rule.account_id for task, account_id in _jobs.values()):
        return {"ok": False, "error": "На этом аккаунте уже идёт очередь вступлений"}
    report = view(rule)
    if float(report.get("retry_timestamp") or 0) > time.time():
        return {"ok": False, "error": "Telegram просит подождать до " + str(report.get("retry_at"))}
    report.update(state="running", error=None, current=None, limited=False, remaining=0, paused=False, retry_at=None, retry_timestamp=0)
    # Reserve synchronously before the first await, including across different tasks.
    gate = asyncio.Event()

    async def worker():
        token = _current.set((rule, report))
        try:
            await gate.wait()
            result = await manager.run_task_now(rule)
            report.update(result)
            report["state"] = "stopped" if (not result.get("ok") or result.get("limited") or report.get("remaining")) else "done"
        except asyncio.CancelledError:
            report.update(state="stopped", error="Очередь остановлена. Можно продолжить кнопкой «Запустить».")
            raise
        except Exception:
            logger.exception("Join queue #{} failed", rule.id)
            report.update(state="stopped", error="Очередь прервана из-за ошибки сервиса")
        finally:
            report["current"] = None
            try:
                await save(rule, report)
            finally:
                _current.reset(token)
                _jobs.pop(rule.id, None)

    task = asyncio.create_task(worker())
    _jobs[rule.id] = (task, rule.account_id)
    def finished(done):
        if _jobs.get(rule.id, (None,))[0] is done:
            _jobs.pop(rule.id, None)
        if not done.cancelled() and done.exception():
            logger.error("Join queue #{} could not save its final state: {}", rule.id, type(done.exception()).__name__)
    task.add_done_callback(finished)
    try:
        await save(rule, report)
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    gate.set()
    return {"ok": True, "queued": True}


async def stop(rule_id: int) -> None:
    item = _jobs.get(rule_id)
    if item:
        item[0].cancel()
        await asyncio.gather(item[0], return_exceptions=True)


async def stop_account(account_id: int) -> None:
    for rule_id, (_, account) in list(_jobs.items()):
        if account == account_id:
            await stop(rule_id)


async def cancel_inactive(active_ids: set[int]) -> None:
    tasks = [task for rule_id, (task, _) in list(_jobs.items()) if rule_id not in active_ids]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def flood_wait(seconds: int) -> None:
    context = _current.get()
    if context:
        from app.telegram_client.manager import manager
        await manager.note_join_wait(context[0], seconds)
    stamp = time.time() + max(1, seconds)
    await progress(retry_timestamp=stamp, retry_at=datetime.fromtimestamp(stamp, timezone.utc).isoformat())
