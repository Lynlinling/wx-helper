"""三层群聊记忆隔离、保留、管理命令与整理流程。"""
import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from plugins.memory import Plugin
from wxbot_framework.members import MemberError, MemberStore
from wxbot_framework.sdk import Message


ROOM = "12345678@chatroom"
ROBOT = "wxid_robot"
OWNER = "wxid_lyn"
FRIEND = "wxid_friend"
SCRATCH = Path(tempfile.gettempdir()) / "wx-helper-tests"


class FakeContext:
    def __init__(self, members):
        self.members = members
        self.config = {"owner_aliases": ["主人"]}
        self.logger = logging.getLogger("MemoryTests")
        self.llm = Mock()
        self.llm.complete_json.return_value = {"summary": "群友讨论了周末玩游戏，并约好下次一起开荒。"}
        self.replies = []
        self.services = {}

    def register_service(self, name, service):
        self.services[name] = service

    def unregister_service(self, name, service=None):
        if service is None or self.services.get(name) is service:
            self.services.pop(name, None)

    def personality(self, message):
        return self.members.personality(message.chat_id)

    def llm_for(self, message, *, personality=None):
        return self.llm

    def reply(self, message, text, *, delivery_guard=None):
        if delivery_guard:
            with delivery_guard():
                self.replies.append(text)
        else:
            self.replies.append(text)
        return True


class MemoryTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temp.name).resolve()
        self.now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
        self.path = self.root / "members.json"
        self.path.write_text(json.dumps({
            "database_file": "data.sqlite3", "history_enabled": True, "daily_clear": True,
            "warm_retention_days": 7,
            "groups": {ROOM: [{"key": "lyn", "name": "主人", "display_name": "主人",
                               "aliases": ["主人"], "wxid": OWNER}]},
        }, ensure_ascii=False), encoding="utf-8")
        self.stores = []
        self.store = self.new_store()
        self.ctx = FakeContext(self.store)
        self.plugin = Plugin()
        self.plugin.on_load(self.ctx)
        self.assertIs(self.ctx.services["memory"], self.plugin)

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.assertTrue(self.root.is_relative_to(SCRATCH.resolve()))
        self.temp.cleanup()

    def new_store(self, account=ROBOT):
        store = MemberStore(self.path, account, now=lambda: self.now)
        self.stores.append(store)
        store.initialize()
        return store

    def message(self, text, sender=FRIEND, sequence=1):
        return Message(text, ROOM, sender_id=sender, is_group=True, sequence=sequence,
                       session_id="memory-test", data={"self_wxid": ROBOT})

    def test_warm_and_cold_survive_daily_hot_clear_and_are_account_scoped(self):
        self.store.record(self.message("今晚一起玩游戏", sequence=1))
        self.store.store_warm_memory(ROOM, "2026-11-01", "群友约好周末一起开荒。", 1)
        self.store.add_cold_memory(ROOM, "偏好", "喜欢一起玩合作游戏")

        self.now += timedelta(days=1)
        self.store.rollover()
        self.assertEqual(self.store.history("主人", ROOM), [])
        memory = self.store.tiered_memory_context(ROOM, "合作游戏")
        self.assertEqual(memory["warm"][0]["summary"], "群友约好周末一起开荒。")
        self.assertEqual(memory["cold"][0]["text"], "喜欢一起玩合作游戏")

        other_account = self.new_store("wxid_second_bot")
        self.assertEqual(other_account.tiered_memory_context(ROOM, "合作游戏"), {"warm": [], "cold": []})

    def test_warm_retention_and_explicit_memory_deletion(self):
        warm_id = self.store.store_warm_memory(ROOM, "2026-11-01", "今晚约好打游戏。", 2)
        cold_id, created = self.store.add_cold_memory(ROOM, "事实", "群里常玩合作游戏")
        self.assertTrue(created)
        self.assertEqual(self.store.add_cold_memory(ROOM, "事实", "群里常玩合作游戏"), (cold_id, False))
        self.assertTrue(self.store.forget_warm_memory(ROOM, warm_id))
        self.assertTrue(self.store.forget_cold_memory(ROOM, cold_id))
        self.assertFalse(self.store.forget_cold_memory(ROOM, cold_id))

        self.store.store_warm_memory(ROOM, "2026-11-01", "七天后过期。", 1)
        self.now += timedelta(days=8)
        self.assertEqual(self.store.tiered_memory_context(ROOM, "过期")["warm"], [])

    def test_memory_deletion_invalidates_in_flight_chat_snapshot(self):
        msg = self.message("这条记忆还有效吗？", sender=OWNER, sequence=20)
        self.store.record(msg)
        snapshot = self.store.conversation_snapshot(msg)
        memory_id, _ = self.store.add_cold_memory(ROOM, "事实", "喜欢合作游戏")

        self.assertTrue(self.store.forget_cold_memory(ROOM, memory_id))
        self.assertFalse(self.store.snapshot_is_current(snapshot))

    def test_memory_rejects_secrets_ids_and_invalid_values(self):
        with self.assertRaises(MemberError):
            self.store.add_cold_memory(ROOM, "事实", "api_key=super-secret-value")
        with self.assertRaises(MemberError):
            self.store.add_cold_memory(ROOM, "事实", "wxid_123456789")
        with self.assertRaises(MemberError):
            self.store.store_warm_memory(ROOM, "2026-11-01", "群里写了 " + "sk-" + "x" * 24, 2)
        with self.assertRaises(MemberError):
            self.store.add_cold_memory(ROOM, "未知", "无效类别")

    def test_owner_commands_save_list_and_forget_memories(self):
        self.store.record(self.message("周末一起开荒吧", sequence=1))
        self.store.record(self.message("/记忆整理", sender=OWNER, sequence=2))
        self.plugin.on_message(self.message("/记忆整理", sender=OWNER, sequence=2), self.ctx)
        self.assertIn("已整理进温记忆", self.ctx.replies[-1])
        warm_id = self.store.list_tiered_memories(ROOM)["warm"][0]["id"]

        self.plugin.on_message(self.message("/记住 偏好 喜欢合作游戏", sender=OWNER, sequence=3), self.ctx)
        cold_id = self.store.list_tiered_memories(ROOM)["cold"][0]["id"]
        self.plugin.on_message(self.message("/bot记忆", sender=OWNER, sequence=4), self.ctx)
        self.assertTrue(any("喜欢合作游戏" in part for part in self.ctx.replies))
        self.plugin.on_message(self.message(f"/忘记冷 {cold_id}", sender=OWNER, sequence=5), self.ctx)
        self.plugin.on_message(self.message(f"/忘记温 {warm_id}", sender=OWNER, sequence=6), self.ctx)
        self.assertEqual(self.store.list_tiered_memories(ROOM)["cold"], [])
        self.assertEqual(self.store.list_tiered_memories(ROOM)["warm"], [])

    def test_non_owner_cannot_write_or_read_memory(self):
        self.plugin.on_message(self.message("/记住 事实 假装是主人", sender=FRIEND), self.ctx)
        self.plugin.on_message(self.message("/bot记忆", sender=FRIEND, sequence=2), self.ctx)
        self.assertEqual(self.ctx.replies, ["记忆管理只认主人。", "记忆管理只认主人。"])
        self.assertEqual(self.store.list_tiered_memories(ROOM)["cold"], [])

    def test_owner_can_clear_warm_and_cold_without_touching_hot_history(self):
        self.store.store_warm_memory(ROOM, "2026-11-01", "今夜一起开荒。", 1)
        self.store.add_cold_memory(ROOM, "偏好", "喜欢合作游戏")
        self.store.record(self.message("这条热记忆要保留", sender=OWNER, sequence=9))
        self.plugin.on_message(self.message("/清空记忆", sender=OWNER, sequence=10), self.ctx)

        self.assertEqual(self.store.list_tiered_memories(ROOM)["warm"], [])
        self.assertEqual(self.store.list_tiered_memories(ROOM)["cold"], [])
        self.assertEqual(self.store.history("主人", ROOM)[0]["text"], "这条热记忆要保留")

    def test_unload_unregisters_memory_context_service(self):
        self.plugin.on_unload(self.ctx)
        self.assertNotIn("memory", self.ctx.services)


if __name__ == "__main__":
    unittest.main()


