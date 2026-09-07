"""Слушатель слов: совпало — прислал, нет — промолчал."""
from types import SimpleNamespace

from app.telegram_client import jobs
from app.telegram_client.filters import FilterConfig
from tests.helpers import TEST_USER_ID
from tests.test_delivery_fixes import _db_rule, _snapshot
from tests.test_many_chats import (  # noqa: F401
    login_open,
    many_chats_resolved,
    one_shot_stubbed,
)


class ListenClient:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.sent.append((chat_id, text))
        return SimpleNamespace(id=len(self.sent))


def _post(text: str):
    return SimpleNamespace(id=5, message=text, media=None)


async def test_match_sends_with_chat_header(create_user, create_account):
    rule = await _db_rule(create_user, create_account, kind="listener", with_trial=True)
    snapshot = _snapshot(
        rule,
        filters=FilterConfig(keywords=["скидка"]),
        source_title="Барахолка",
    )
    client = ListenClient()
    await jobs.run_job(client, _post("Большая СКИДКА сегодня"), snapshot)
    assert client.sent == [(-100200, "🔔 Барахолка\n\nБольшая СКИДКА сегодня")]


async def test_no_match_stays_silent(create_user, create_account):
    rule = await _db_rule(create_user, create_account, kind="listener", with_trial=True)
    snapshot = _snapshot(rule, filters=FilterConfig(keywords=["скидка"]))
    client = ListenClient()
    await jobs.run_job(client, _post("Просто новости дня"), snapshot)
    assert client.sent == []


async def test_empty_text_stays_silent(create_user, create_account):
    rule = await _db_rule(create_user, create_account, kind="listener", with_trial=True)
    snapshot = _snapshot(rule, filters=FilterConfig(keywords=["скидка"]))
    client = ListenClient()
    await jobs.run_job(client, _post(""), snapshot)
    assert client.sent == []


def test_listener_title_counts_words():
    rule = SimpleNamespace(
        kind="listener",
        source_title="src",
        target_title="dst",
        filters={"keywords": ["a", "b", " "]},
    )
    assert jobs.task_title(rule) == "Слушатель: src → dst · 2 сл."


async def test_api_listener_requires_keywords(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    base = {
        "command": "listener",
        "account_id": account_id,
        "source": "@ch-1",
        "target": "@ch-2",
    }
    resp = await client.post(
        "/api/tasks", json={**base, "keywords": ["скидка", "акция"]},
        headers=auth_headers,
    )
    assert resp.status == 201, await resp.text()
    task = (await resp.json())["task"]
    assert task["title"].startswith("Слушатель:")
    assert task["keywords_count"] == 2
    assert task["edit"]["keywords"] == "скидка, акция"
    # Без слов — не слушатель, а пересылка: отказываем сразу.
    resp = await client.post("/api/tasks", json=base, headers=auth_headers)
    assert resp.status == 400
    assert "слова" in (await resp.json())["error"]
    # Правка тоже не должна обнулять слова.
    resp = await client.patch(
        f"/api/tasks/{task['id']}", json={"keywords": []}, headers=auth_headers
    )
    assert resp.status == 400
