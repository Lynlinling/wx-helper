import json
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from wxbot_framework.codex_inbox import CodexInbox, InboxError


def test_accept_is_idempotent_and_global_cooldown_persists(tmp_path):
    now = [1_800_000_000.0]
    with patch("wxbot_framework.codex_inbox.time.time", side_effect=lambda: now[0]):
        inbox = CodexInbox(tmp_path)
        accepted = inbox.accept("核对群聊回复失败分支", source="wechat_bot",
                                idempotency_key="session-one:42")
        duplicate = inbox.accept("核对群聊回复失败分支", source="wechat_bot",
                                idempotency_key="session-one:42")
        assert accepted["accepted"] and not accepted["duplicate"]
        assert duplicate["duplicate"] and duplicate["id"] == accepted["id"]
        with pytest.raises(InboxError) as caught:
            inbox.accept("另一条消息", idempotency_key="session-one:43")
        assert caught.value.status == 429
        assert caught.value.retry_after == 600

        # 重新实例化模拟 Bot/Codex 进程重启，限流仍由 SQLite 保留。
        again = CodexInbox(tmp_path)
        with pytest.raises(InboxError):
            again.accept("重启后仍受限", idempotency_key="session-two:1")
        now[0] += 600
        next_item = again.accept("十分钟后可收", idempotency_key="session-two:2")
        assert next_item["accepted"]

        # 幂等键只保存散列，原始会话标识不落盘。
        with sqlite3.connect(tmp_path / ".runtime/codex_inbox.sqlite3") as db:
            stored = db.execute("SELECT idempotency_hash FROM codex_inbox WHERE id=?",
                                (accepted["id"],)).fetchone()[0]
        assert stored != "session-one:42" and len(stored) == 64


def test_receive_delivers_one_item_at_most_every_ten_minutes(tmp_path):
    now = [1_800_000_000.0]
    with patch("wxbot_framework.codex_inbox.time.time", side_effect=lambda: now[0]):
        inbox = CodexInbox(tmp_path)
        first = inbox.accept("第一条短消息")
        now[0] += 600
        inbox.accept("第二条短消息")
        received = inbox.receive()
        assert received["status"] == "delivered"
        assert received["id"] == first["id"]
        assert inbox.receive() == {"status": "cooldown", "retry_after": 600}
        assert inbox.ack([received["id"]]) == 1
        now[0] += 600
        assert inbox.receive()["text"] == "第二条短消息"


def test_complete_queues_one_callback_to_the_original_allowlisted_group(tmp_path):
    room = "12345678901@chatroom"
    inbox = CodexInbox(tmp_path, {"reply_chat_allowlist": [room]})
    accepted = inbox.accept("只读检查人格提示词", source="wechat_bot",
                            idempotency_key="session:9", reply_to_chat_id=room)
    received = inbox.receive()
    assert received["id"] == accepted["id"]
    assert "reply_to_chat_id" not in received

    complete = inbox.complete(received["id"], "已检查：列出三处边界，未修改文件。")
    duplicate = inbox.complete(received["id"], "已检查：列出三处边界，未修改文件。")
    assert complete["queued"] and complete["status"] == "pending"
    assert duplicate["duplicate"]
    assert inbox.reply_status(received["id"])["status"] == "pending"

    sent = []
    result = inbox.dispatch_one(lambda chat, text: sent.append((chat, text)) or {"accepted": True})
    assert result["status"] == "sent"
    assert sent == [(room, "Codex 处理结果：\n已检查：列出三处边界，未修改文件。")]
    assert inbox.reply_status(received["id"])["status"] == "sent"
    assert inbox.dispatch_one(lambda *_: pytest.fail("no duplicate callback")) == {"status": "empty"}


def test_user_readable_archive_keeps_history_without_exporting_private_routing_data(tmp_path):
    room = "12345678901@chatroom"
    inbox = CodexInbox(tmp_path, {"reply_chat_allowlist": [room], "archive_dir": "许愿邮箱"})
    accepted = inbox.accept("查看 <script>alert('x')</script>", source="wechat_bot",
                            idempotency_key="private-session-key", reply_to_chat_id=room)
    received = inbox.receive()
    inbox.complete(received["id"], "已检查，完成。")
    inbox.dispatch_one(lambda *_: {"accepted": True})

    folder = tmp_path / "许愿邮箱"
    archive = folder / f"{accepted['id']}.md"
    text = archive.read_text(encoding="utf-8")
    assert (folder / "INDEX.md").is_file()
    assert accepted["id"] in (folder / "INDEX.md").read_text(encoding="utf-8")
    assert "&lt;script&gt;" in text and "<script>" not in text
    assert "收到请求" in text and "交付给处理端" in text
    assert "结果已回发" in text and "已检查，完成。" in text
    assert room not in text
    assert "private-session-key" not in text


def test_local_http_agent_can_complete_received_item(tmp_path):
    room = "12345678901@chatroom"
    inbox = CodexInbox(tmp_path, {"enabled": True, "port": 0,
                                  "reply_chat_allowlist": [room]})
    accepted = inbox.accept("请检查本机收件箱接口", source="wechat_bot",
                            reply_to_chat_id=room)
    inbox.start()
    try:
        base = f"http://127.0.0.1:{inbox.port}/v1/inbox"
        headers = {"Authorization": "Bearer " + inbox.token}
        get = urllib.request.Request(base, headers=headers)
        with urllib.request.urlopen(get, timeout=2) as response:
            delivered = json.loads(response.read())
        assert delivered["id"] == accepted["id"]

        payload = json.dumps({"id": accepted["id"], "text": "接口回报完成。"}).encode()
        request = urllib.request.Request(base + "/complete", data=payload, method="POST",
                                         headers={**headers, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=2) as response:
            result = json.loads(response.read())
        assert result["queued"] is True
        assert inbox.reply_status(accepted["id"])["status"] == "pending"
    finally:
        inbox.stop()


def test_complete_rejects_unallowlisted_routes_and_uncertain_sends_do_not_retry(tmp_path):
    allowed_room = "12345678901@chatroom"
    other_room = "12345678902@chatroom"
    inbox = CodexInbox(tmp_path, {"reply_chat_allowlist": [allowed_room]})
    with pytest.raises(InboxError) as caught:
        inbox.accept("检查", reply_to_chat_id=other_room)
    assert caught.value.status == 403

    accepted = inbox.accept("检查", reply_to_chat_id=allowed_room)
    received = inbox.receive()
    inbox.complete(received["id"], "已检查，无须修改。")
    failed = inbox.dispatch_one(lambda *_: (_ for _ in ()).throw(RuntimeError("transport")))
    assert failed["status"] == "failed"
    assert inbox.reply_status(accepted["id"])["status"] == "failed"
    assert inbox.dispatch_one(lambda *_: pytest.fail("uncertain send must not auto retry")) == {"status": "empty"}


@pytest.mark.parametrize("message", [
    "api_key=abcdefghijk12345", "sk-" + "x" * 24,
    "wxid_privateaccount123", "12345678901@chatroom",
])
def test_sensitive_values_are_rejected(message, tmp_path):
    inbox = CodexInbox(tmp_path)
    with pytest.raises(InboxError):
        inbox.accept(message)


def test_endpoint_requires_bearer_and_uses_loopback(tmp_path):
    inbox = CodexInbox(tmp_path, {"enabled": True, "port": 0})
    inbox.start()
    try:
        assert inbox._server.server_address[0] == "127.0.0.1"
        url = f"http://127.0.0.1:{inbox.port}/v1/inbox"
        unauth = urllib.request.Request(url, method="GET")
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(unauth, timeout=2)
        assert caught.value.code == 401

        payload = json.dumps({"text": "需要 Codex 看下回复超时路径", "source": "wechat_bot",
                              "idempotency_key": "evt-1"}).encode()
        post = urllib.request.Request(url, data=payload, method="POST",
                                      headers={"Authorization": "Bearer " + inbox.token,
                                               "Content-Type": "application/json"})
        with urllib.request.urlopen(post, timeout=2) as response:
            assert response.status == 200
            assert json.loads(response.read())["accepted"]

        get = urllib.request.Request(url, headers={"Authorization": "Bearer " + inbox.token})
        with urllib.request.urlopen(get, timeout=2) as response:
            assert json.loads(response.read())["text"].startswith("需要 Codex")
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(get, timeout=2)
        assert caught.value.code == 429
    finally:
        inbox.stop()


def test_non_loopback_binding_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="127.0.0.1"):
        CodexInbox(tmp_path, {"host": "0.0.0.0"})

