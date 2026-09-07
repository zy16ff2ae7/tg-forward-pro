"""Шаблоны задач: ссылаются на живые команды и заводятся одним нажатием."""
from tests.helpers import TEST_USER_ID
from tests.test_many_chats import (  # noqa: F401
    login_open,
    many_chats_resolved,
    one_shot_stubbed,
)


async def _templates(client, auth_headers):
    resp = await client.get("/api/templates", headers=auth_headers)
    assert resp.status == 200
    return (await resp.json())["templates"]


async def test_templates_reference_live_commands(client, auth_headers):
    templates = await _templates(client, auth_headers)
    assert len(templates) >= 3
    resp = await client.get("/api/commands", headers=auth_headers)
    commands = {item["id"]: item for item in (await resp.json())["commands"]}
    for tpl in templates:
        assert set(tpl) >= {"id", "command", "title", "fill"}, tpl
        command = commands.get(tpl["command"])
        assert command is not None, tpl
        allowed = set(command["needs"]) | set(command["optional"])
        for key in tpl["fill"]:
            assert key in allowed, (tpl["id"], key)


async def test_template_fill_creates_task(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    templates = await _templates(client, auth_headers)
    mirror = next(item for item in templates if item["id"] == "mirror")
    resp = await client.post(
        "/api/tasks",
        json={
            "command": mirror["command"],
            "account_id": account_id,
            "source": "@ch-1",
            "target": "@ch-2",
            **mirror["fill"],
        },
        headers=auth_headers,
    )
    assert resp.status == 201, await resp.text()
    task = (await resp.json())["task"]
    assert task["edit"]["history"] == 50
    deals = next(item for item in templates if item["id"] == "deals")
    resp = await client.post(
        "/api/tasks",
        json={
            "command": deals["command"],
            "account_id": account_id,
            "source": "@ch-1",
            "target": "@ch-2",
            **deals["fill"],
        },
        headers=auth_headers,
    )
    assert resp.status == 201, await resp.text()
    assert (await resp.json())["task"]["keywords_count"] == 5
