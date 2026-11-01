import pytest

from wxbot_framework.backend import DatabaseReadonlyTextBackend, UIAutomationTextBackend
from wxbot_framework.sdk import CapabilityError


def test_uia_only_accepts_configured_bot_mention_aliases():
    aliases = ("鲸鱼娘", "大肥鱼", "机器人")
    assert UIAutomationTextBackend._mentions_bot("@机器人\u2005好了吗", aliases)
    assert UIAutomationTextBackend._matched_mention_alias("@机器人", aliases) == "机器人"
    assert UIAutomationTextBackend._mentions_bot("@鲸鱼娘 在做什么", aliases)
    assert not UIAutomationTextBackend._mentions_bot("@某个群友 你来看看", aliases)
    assert not UIAutomationTextBackend._mentions_bot("大家随便@一下", aliases)


def test_uia_backend_refuses_all_outbound_without_touching_wechat():
    backend = UIAutomationTextBackend.__new__(UIAutomationTextBackend)
    with pytest.raises(CapabilityError, match="当前只读"):
        backend.send_text("test_group@chatroom", "test")


def test_database_readonly_backend_parses_group_sender_and_mention_without_outbound():
    backend = DatabaseReadonlyTextBackend.__new__(DatabaseReadonlyTextBackend)
    backend._account = "wxid_robot"
    backend._self_name = "机器人"
    backend.mention_aliases = ("鲸鱼娘",)
    backend._sequence = 0
    backend.session_id = "db:test"
    message = backend._message_from_row(
        "test_group@chatroom",
        {"sender_username": "wxid_friend", "content": "wxid_friend:\n@机器人\u2005好了吗",
         "sort_seq": 123},
    )
    assert message.sender_id == "wxid_friend"
    assert message.text == "@机器人\u2005好了吗"
    assert message.data["is_at_self"] is True
    assert message.data["dispatch_to_plugins"] is True
    bootstrap = backend._message_from_row(
        "test_group@chatroom",
        {"sender_username": "wxid_friend", "content": "wxid_friend:\n历史", "sort_seq": 124},
        bootstrap=True,
    )
    assert bootstrap.data["dispatch_to_plugins"] is False
    with pytest.raises(CapabilityError, match="数据库只读"):
        backend.send_text("test_group@chatroom", "回复")
