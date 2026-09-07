"""Регрессии мастера рассылки: Telegram-папки, ссылки и дедупликация."""
from types import SimpleNamespace

from app.telegram_client.jobs import chat_recipients, explicit_join_target
from app.telegram_client.manager import _dialog_matches_folder
from app.telegram_client.filters import FilterConfig


def test_folder_merges_categories_and_excludes_muted_archived():
    folder = {
        "include_peers": [-1003],
        "exclude_peers": [-1004],
        "groups": True,
        "broadcasts": False,
        "exclude_muted": True,
        "exclude_archived": True,
    }
    assert _dialog_matches_folder(
        {"id": -1001, "is_group": True, "muted": False, "archived": False}, folder
    )
    assert _dialog_matches_folder(
        {"id": -1003, "is_group": False, "muted": False, "archived": False}, folder
    )
    assert not _dialog_matches_folder(
        {"id": -1002, "is_group": True, "muted": True, "archived": False}, folder
    )
    assert not _dialog_matches_folder(
        {"id": -1004, "is_group": True, "muted": False, "archived": False}, folder
    )
    assert not _dialog_matches_folder(
        {"id": -1005, "is_group": True, "muted": False, "archived": True}, folder
    )


def test_mixed_folder_and_manual_chat_ids_are_deduplicated_before_delivery():
    rule = SimpleNamespace(
        target_id=-1001,
        source_id=0,
        filters=SimpleNamespace(targets=[-1002, -1001, -1003, -1002]),
    )
    assert chat_recipients(rule) == [-1001, -1002, -1003]


def test_only_explicit_link_field_can_become_a_join_target():
    assert explicit_join_target("https://t.me/public_channel") == "public_channel"
    assert explicit_join_target("https://t.me/+InviteHash") == "+InviteHash"
    assert explicit_join_target("https://t.me/joinchat/InviteHash") == "joinchat/InviteHash"
    assert explicit_join_target("@public_channel") == "public_channel"
    assert explicit_join_target("обычный текст без адреса") is None


def test_subscribe_and_folder_settings_survive_rule_snapshot_restart():
    config = FilterConfig(
        subscribe_to=["https://t.me/+InviteHash"],
        folder_ids=[0, 7],
        folder_titles={"0": "Все чаты", "7": "Работа"},
        subscribe_done=False,
    )
    restored = FilterConfig.from_dict(config.to_dict())
    assert restored.subscribe_to == ["https://t.me/+InviteHash"]
    assert restored.folder_ids == [0, 7]
    assert restored.folder_titles["7"] == "Работа"
    assert restored.subscribe_done is False
