import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from wxbot_framework.backend import DatabaseNative42TextBackend, DatabaseReadonlyTextBackend
from wxbot_framework.sdk import ServiceFacade


def make_backend(backend_type):
    backend = object.__new__(backend_type)
    backend._account = "wxid_bot"
    backend._self_name = ""
    backend._sequence = 0
    backend.session_id = "db:wxid_bot"
    backend.mention_aliases = ("鲸鱼娘",)
    backend._started_at = 0.0
    return backend


def make_row():
    return {
        "sender_username": "wxid_friend",
        "content": "@鲸鱼娘 在吗",
        "sort_seq": 42,
        "local_id": 1,
    }


def test_readonly_database_messages_cannot_reply():
    message = make_backend(DatabaseReadonlyTextBackend)._message_from_row(
        "room@chatroom", make_row()
    )
    assert message.data["reply_capable"] is False


def test_native42_live_messages_can_reply():
    message = make_backend(DatabaseNative42TextBackend)._message_from_row(
        "room@chatroom", make_row()
    )
    assert message.data["reply_capable"] is True
    assert message.data["dispatch_to_plugins"] is True


def test_native42_bootstrap_history_is_not_replyable_or_dispatched():
    message = make_backend(DatabaseNative42TextBackend)._message_from_row(
        "room@chatroom", make_row(), bootstrap=True
    )
    assert message.data["reply_capable"] is False
    assert message.data["dispatch_to_plugins"] is False


def test_quote_card_does_not_turn_nested_at_into_a_live_mention():
    backend = make_backend(DatabaseNative42TextBackend)
    row = {
        "sender_username": "wxid_friend",
        "content": "<appmsg><title>收到</title><refermsg>"
                   "<content>@鲸鱼娘 旧消息</content></refermsg></appmsg>",
        "type": "文件/链接/卡片",
        "sort_seq": 43,
        "local_id": 2,
    }
    message = backend._message_from_row("room@chatroom", row)
    assert message is not None
    assert message.text == "收到"
    assert message.data["is_at_self"] is False


def test_nonquote_card_title_cannot_fake_a_live_mention():
    backend = make_backend(DatabaseNative42TextBackend)
    row = {"sender_username": "wxid_friend",
           "content": "<appmsg><title>@鲸鱼娘 在吗</title></appmsg>",
           "type": "文件/链接/卡片"}
    assert backend._message_from_row("room@chatroom", row) is None


def test_quote_title_with_current_at_remains_replyable():
    backend = make_backend(DatabaseNative42TextBackend)
    row = {"sender_username": "wxid_friend",
           "content": "<appmsg><title>@鲸鱼娘 帮我看看</title><refermsg>"
                      "<content>旧引用</content></refermsg></appmsg>",
           "type": "文件/链接/卡片"}
    message = backend._message_from_row("room@chatroom", row)
    assert message is not None
    assert message.text == "@鲸鱼娘 帮我看看"
    assert message.data["is_at_self"] is True


def test_quote_text_keeps_only_current_title_without_nested_quote():
    backend = make_backend(DatabaseNative42TextBackend)
    row = {
        "sender_username": "wxid_friend",
        "content": "<appmsg><title>帮我看看这个</title><refermsg>"
                   "<content>@鲸鱼娘 旧消息</content></refermsg></appmsg>",
        "type": "文件/链接/卡片",
        "sort_seq": 44,
        "local_id": 3,
    }
    message = backend._message_from_row("room@chatroom", row)
    assert message is not None
    assert message.text == "帮我看看这个"


def test_bootstrap_drains_mentions_arriving_during_history_load():
    backend = make_backend(DatabaseReadonlyTextBackend)
    backend.chat_ids = ("room@chatroom",)
    backend._watermarks = {}
    backend._bootstrap_limit = 200
    backend._started_at = 1000
    old = {**make_row(), "sort_seq": 11, "local_id": 11, "create_time": 1000}
    arrived_during_bootstrap = {
        **make_row(), "sort_seq": 12, "local_id": 12, "create_time": 1000,
    }

    def get_messages(_chat_id, *, limit):
        if limit == 1:
            return [old]
        # The DB returns newest first; the new mention appears after baseline.
        return [arrived_during_bootstrap, old]

    def get_new_messages(_chat_id, *, since_seq, limit):
        return [arrived_during_bootstrap] if since_seq < 12 else []

    backend._db = SimpleNamespace(
        get_messages=get_messages,
        get_new_messages=get_new_messages,
    )
    messages = []
    backend._emit = messages.append

    backend._bootstrap()

    assert len(messages) == 2
    assert messages[0].data["db_bootstrap"] is True
    assert messages[0].data["dispatch_to_plugins"] is False
    assert messages[1].data["db_bootstrap"] is False
    assert messages[1].data["dispatch_to_plugins"] is True
    assert backend._watermarks["room@chatroom"] == 12


class SessionScopedBackend(DatabaseNative42TextBackend):
    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True

    def stop(self):
        return {"stopped": True}


def test_native42_group_scoped_message_session_sends_on_current_connection():
    backend = make_backend(SessionScopedBackend)
    backend.session_id = "db:wxid_bot:connection-a"
    backend.chat_ids = ("room@chatroom",)
    backend.sent = []
    message = backend._message_from_row("room@chatroom", make_row())
    service = ServiceFacade(text_delay={"enabled": False}, send_guard={"enabled": False})
    service.bind(backend)
    try:
        assert service.send_text(message.chat_id, "收到", expected_session=message.session_id) is True
        assert backend.sent == [("room@chatroom", "收到")]
    finally:
        service.close()


def test_native42_message_from_previous_connection_is_rejected():
    backend = make_backend(SessionScopedBackend)
    backend.session_id = "db:wxid_bot:connection-new"
    backend.chat_ids = ("room@chatroom",)
    backend.sent = []
    service = ServiceFacade(text_delay={"enabled": False}, send_guard={"enabled": False})
    service.bind(backend)
    try:
        with pytest.raises(RuntimeError, match="连接已结束"):
            service.send_text("room@chatroom", "不应发送",
                              expected_session="db:wxid_bot:connection-old:room@chatroom")
        assert backend.sent == []
    finally:
        service.close()


def make_live_backend(tmp_path, monkeypatch, selected_pid, selected_account):
    group_id = 'room@chatroom'
    runtime = tmp_path / '.runtime'
    runtime.mkdir()
    (runtime / 'target_group.private.json').write_text(
        json.dumps({'group_id': group_id}), encoding='utf-8'
    )
    core = tmp_path / 'Weixin.dll'
    core.write_bytes(b'test-only fixture')
    sender = SimpleNamespace(
        select_target=Mock(return_value=(selected_pid, selected_account)),
        modules=Mock(return_value={'weixin.dll': (0x1000, str(core))}),
        verify_wechat_42_core=Mock(return_value='verified-fixture'),
    )
    config = {
        'chat_allowlist': [group_id],
        'wechat_account': 'wxid_bot',
        # Simulate a stale process ID from a previous WeChat launch.
        'wechat_pid': 101,
    }
    backend = DatabaseNative42TextBackend(tmp_path, config)
    monkeypatch.setattr(backend, '_load_sender', lambda: sender)
    monkeypatch.setattr(DatabaseReadonlyTextBackend, 'start', lambda self, emit: True)
    return backend, sender


def test_native42_start_rediscovers_bound_account_when_configured_pid_is_stale(tmp_path, monkeypatch):
    backend, sender = make_live_backend(tmp_path, monkeypatch, 202, 'wxid_bot')

    assert backend.start(lambda _message: None) is True

    assert backend.pid == 202
    sender.select_target.assert_called_once_with()
    sender.modules.assert_called_once_with(202)
    sender.verify_wechat_42_core.assert_called_once()


def test_native42_start_rejects_different_live_account_even_if_pid_was_configured(tmp_path, monkeypatch):
    backend, sender = make_live_backend(tmp_path, monkeypatch, 202, 'wxid_other')

    with pytest.raises(RuntimeError, match='当前已登录账号不是绑定的专用机器人号'):
        backend.start(lambda _message: None)

    sender.modules.assert_not_called()
    assert backend._db is None


def test_native42_health_check_accepts_same_live_pid(tmp_path, monkeypatch):
    backend, sender = make_live_backend(tmp_path, monkeypatch, 202, 'wxid_bot')
    backend.pid = 202
    backend._sender_module = sender

    backend._check_live_target()

    sender.select_target.assert_called_once_with()
    sender.modules.assert_not_called()
    sender.verify_wechat_42_core.assert_not_called()


def test_native42_health_check_requests_reconnect_after_pid_change(tmp_path, monkeypatch):
    backend, sender = make_live_backend(tmp_path, monkeypatch, 202, 'wxid_bot')
    backend.pid = 101
    backend._sender_module = sender

    with pytest.raises(RuntimeError, match='微信进程已更换'):
        backend._check_live_target()

    sender.modules.assert_called_once_with(202)
    sender.verify_wechat_42_core.assert_called_once()
