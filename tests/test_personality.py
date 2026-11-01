"""群人格持久性、统一风格、真实艾特与十条上下文及取消竞态。"""
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import test_chat_summary as daily
from plugins.mention_chat import Plugin as ChatPlugin
from plugins.personality import Plugin as PersonalityPlugin
from plugins.idiom_chain import Plugin as IdiomPlugin
from wxbot_framework.dialogue import Dialogue
from wxbot_framework.members import MemberError, SnapshotExpired
from wxbot_framework.mentions import parse_at_members, strip_at_labels
from wxbot_framework.personality import PROMPTS, PersonalityExpired, PersonalityState, prepare_switch, game_reply
from wxbot_framework.runtime import BotApplication
from wxbot_framework.sdk import Message
from wxbot_framework.text import TextRouter

ROOM, CAT, ROBOT, OTHER_ROOM, OTHER = daily.ROOM, daily.CAT, daily.ROBOT, daily.OTHER_ROOM, daily.OTHER


class PersonalityTests(unittest.TestCase):
    setUp = daily.DailySummaryTests.setUp
    tearDown = daily.DailySummaryTests.tearDown
    new_store = daily.DailySummaryTests.new_store
    message = daily.DailySummaryTests.message
    model = daily.DailySummaryTests.model
    snapshot = daily.DailySummaryTests.snapshot

    def chat(self, text="你怎么看？", *, sequence=100, sender=CAT, at_members=(ROBOT,), **kwargs):
        msg = self.message("@机器人\u2005" + text, sequence=sequence, sender=sender,
                           data={"at_members": at_members, "is_at_self": True, **kwargs})
        self.store.record(msg)
        return msg

    def setup_chat(self, provider=None):
        self.backend.send_at_text = lambda room, text, ids: self.backend.sent.append((room, text, ids)) or True
        self.ctx._llm = Mock(complete_json=provider or Mock(return_value={"reply": "这是回答。", "summary": "这是总结。"}))
        plugin = ChatPlugin()
        plugin.on_load(self.ctx)
        return plugin

    def test_personality_persists_and_isolated_from_other_groups_accounts_and_clear(self):
        state = self.store.set_personality(ROOM, "嘴臭")
        self.assertEqual(self.store.personality(OTHER_ROOM).name, "鲸鱼娘")
        self.assertEqual(self.new_store("wxid_second_robot").personality(ROOM).name, "鲸鱼娘")
        self.store.clear(ROOM)
        self.now += timedelta(seconds=1)
        self.store.rollover()
        self.assertEqual(self.store.personality(ROOM), state)
        self.assertEqual(self.new_store().personality(ROOM), state)

    def test_new_room_uses_whale_default_and_cancel_restores_it(self):
        self.assertEqual(self.store.personality(ROOM).name, "鲸鱼娘")
        self.store.set_personality(ROOM, "夸夸")
        self.assertEqual(self.store.set_personality(ROOM, "默认").name, "鲸鱼娘")
        self.assertEqual(self.store.default_personality, "鲸鱼娘")

    def test_same_selection_keeps_generation_unknown_name_does_not_change_it(self):
        state = self.store.set_personality(ROOM, "卑微")
        self.assertEqual(self.store.set_personality(ROOM, "卑微"), state)
        with self.assertRaises(MemberError):
            self.store.set_personality(ROOM, "不存在")
        self.assertEqual(self.store.personality(ROOM), state)
        self.assertGreater(self.store.set_personality(ROOM, "夸夸").generation, state.generation)

    def test_cancel_removes_style_persists_and_keeps_records_and_other_groups(self):
        initial = self.store.personality(ROOM)
        repeated = prepare_switch(self.message("/切换人格 取消"), self.store)
        self.assertEqual(repeated.data["personality_switch"], initial)
        self.store.record(self.message("今天写了文档", sequence=1))
        old = self.store.set_personality(ROOM, "嘴臭")
        other = self.store.set_personality(OTHER_ROOM, "夸夸")
        prepared = prepare_switch(self.message("/切换人格 取消"), self.store)
        state = prepared.data["personality_switch"]
        self.assertEqual(state.name, "鲸鱼娘")
        self.assertGreater(state.generation, old.generation)
        plugin = PersonalityPlugin()
        plugin.on_load(self.ctx)
        plugin.on_message(prepared, self.ctx)
        self.assertIn("鲸鱼娘回来", self.backend.sent[-1][1])
        self.assertEqual(len(self.store.today_snapshot("测试成员", ROOM).messages), 1)
        self.assertEqual(self.store.personality(OTHER_ROOM), other)
        self.assertEqual(self.new_store().personality(ROOM), state)
        self.assertEqual(prepare_switch(self.message("/切换人格 取消"), self.store).data["personality_switch"], state)
        provider = Mock(return_value={"reply": "正常回答"})
        self.ctx._llm = Mock(complete_json=provider)
        self.ctx.llm_for(self.message("问题")).complete_json("原功能提示", "输入")
        self.assertIn("当前人格：鲸鱼娘", provider.call_args.args[0])

    def test_cancel_during_model_request_discards_old_personality_reply(self):
        state = self.store.set_personality(ROOM, "嘴臭")
        def cancel(system, user):
            prepare_switch(self.message("/切换人格 取消"), self.store)
            return {"reply": "旧人格回答"}
        self.ctx._llm = Mock(complete_json=cancel)
        client = self.ctx.llm_for(self.message("问题"), personality=state)
        with self.assertRaises(PersonalityExpired):
            client.complete_json("功能", "输入")
        with self.assertRaises(PersonalityExpired):
            with self.store.personality_delivery_guard(state):
                self.fail("取消后不能发送旧人格回答")
        self.assertEqual(self.store.personality(ROOM).name, "鲸鱼娘")

    def test_runtime_switches_before_dispatch_and_ack_uses_same_state(self):
        app = object.__new__(BotApplication)
        app.config, app.members, app.plugins = {"chat_allowlist": []}, self.store, Mock()
        app._emit(self.message("/切换人格 嘴臭"))
        delivered = app.plugins.dispatch.call_args.args[0]
        self.assertEqual(self.store.personality(ROOM).name, "嘴臭")
        self.assertEqual(delivered.data["personality_switch"].name, "嘴臭")
        plugin = PersonalityPlugin()
        plugin.on_load(self.ctx)
        plugin.on_message(delivered, self.ctx)
        self.assertIn("已切换：嘴臭", self.backend.sent[-1][1])

    def test_missing_or_unknown_switch_returns_usage_without_changing_persona(self):
        plugin = PersonalityPlugin()
        plugin.on_load(self.ctx)
        for command in ("/切换人格", "/切换人格 未知", "/切换人格 嘴臭 卑微"):
            prepared = prepare_switch(self.message(command), self.store)
            plugin.on_message(prepared, self.ctx)
            self.assertIn("用法", self.backend.sent[-1][1])
        self.assertEqual(self.store.personality(ROOM).name, "鲸鱼娘")

    def test_disabled_personality_plugin_does_not_apply_switch(self):
        app = object.__new__(BotApplication)
        app.config, app.members, app.plugins = {"chat_allowlist": []}, self.store, Mock()
        app.plugins.is_active.return_value = False
        app._emit(self.message("/切换人格 嘴臭"))
        self.assertEqual(self.store.personality(ROOM).name, "鲸鱼娘")
        prepare_switch(self.message("/切换人格 嘴臭", data={"bot_generated": True}), self.store)
        self.assertEqual(self.store.personality(ROOM).name, "鲸鱼娘")

    def test_owner_phone_can_switch_and_default_style_does_not_modify_system(self):
        router = TextRouter(include_self_groups=True)
        router.set_account(ROBOT)
        data = router.convert(ROOM, ROBOT, ROBOT + ":\n/切换人格 夸夸")
        self.assertTrue(data["dispatch_to_plugins"])
        provider = Mock(return_value={"reply": "回答"})
        self.ctx._llm = Mock(complete_json=provider)
        self.ctx.llm_for(self.message("问题")).complete_json("原功能提示", "输入")
        self.assertIn("当前人格：鲸鱼娘", provider.call_args.args[0])

    def test_personality_prompt_applies_to_member_and_group_summaries(self):
        self.store.record(self.message("今天要认真工作"))
        for name in PROMPTS:
            self.store.set_personality(ROOM, name)
            provider = Mock(return_value={"summary": "总结内容"})
            self.ctx._llm = Mock(complete_json=provider)
            for command in ("/总结 测试成员", "/总结群聊"):
                self.plugin.on_message(self.message(command), self.ctx)
                system = provider.call_args.args[0]
                self.assertTrue(system.startswith(f"当前人格：{name}"))
                self.assertIn("事实与安全边界 > 工具和任务约束", system)
                self.assertIn("JSON", system)

    def test_personality_change_during_model_request_discards_summary(self):
        self.store.record(self.message("当天记录"))
        self.store.set_personality(ROOM, "嘴臭")
        def change(system, user):
            self.store.set_personality(ROOM, "卑微")
            return {"summary": "旧人格结果"}
        self.ctx._llm = Mock(complete_json=change)
        self.plugin.on_message(self.message("/总结群聊"), self.ctx)
        self.assertEqual(self.backend.sent, [])

    def test_old_llm_session_does_not_call_provider_after_switch(self):
        state = self.store.set_personality(ROOM, "嘴臭")
        provider = Mock(return_value={"reply": "旧结果"})
        self.ctx._llm = Mock(complete_json=provider)
        client = self.ctx.llm_for(self.message("问题"), personality=state)
        self.store.set_personality(ROOM, "卑微")
        with self.assertRaises(PersonalityExpired):
            client.complete_json("功能", "输入")
        provider.assert_not_called()

    def test_context_is_previous_ten_from_same_author_and_room_not_later_messages(self):
        for index in range(12):
            self.store.record(self.message(f"前文{index}", sequence=index + 1))
        self.store.record(self.message("其他人的话", sender=OTHER))
        self.store.record(self.message("其他群的话", room=OTHER_ROOM))
        self.store.record(self.message("/切换人格 嘴臭"))
        self.store.record(self.message("机器人输出", data={"bot_generated": True}))
        msg = self.chat("当前提问")
        self.store.record(self.message("当前提问之后才收到的消息", sequence=101))
        snapshot = self.store.conversation_snapshot(msg)
        self.assertEqual([row["text"] for row in snapshot.messages], [f"前文{i}" for i in range(2, 12)])
        self.assertEqual(snapshot.current, msg.text)

    def test_true_mention_dialogue_uses_persona_context_and_returns_real_at(self):
        self.store.set_personality(ROOM, "嘴臭")
        for index in range(3):
            self.store.record(self.message(f"我今天第{index}次摸鱼", sequence=index + 1))
        provider = Mock(return_value={"reply": "你这效率，进度条看了都想请假。"})
        plugin = self.setup_chat(provider)
        msg = self.chat("我今天又摸鱼了")
        plugin.on_message(msg, self.ctx)
        system, user = provider.call_args.args
        self.assertTrue(system.startswith("当前人格：嘴臭"))
        payload = json.loads(user)
        self.assertEqual(len(payload["previous"]), 3)
        self.assertEqual(payload["current"], "我今天又摸鱼了")
        self.assertNotIn(CAT, user)
        self.assertEqual(self.backend.sent[-1][2], (CAT,))
        self.assertTrue(self.backend.sent[-1][1].startswith("@测试成员\u2005"))

    def test_read_only_backend_records_but_does_not_call_model_or_send(self):
        provider = Mock(return_value={"reply": "不应生成回复。"})
        plugin = self.setup_chat(provider)
        msg = self.chat("在吗", reply_capable=False)
        plugin.on_message(msg, self.ctx)
        provider.assert_not_called()
        self.assertEqual(self.backend.sent, [])

    def test_group_context_includes_other_speakers_and_recent_bot_reply(self):
        self.store.record(self.message("刚才说的插件有点怪", sequence=1, sender=CAT))
        self.store.record(self.message("我也觉得，像是没传上下文", sequence=2, sender=OTHER))
        self.store.record(self.message("我去翻下代码", sequence=3, sender=ROBOT,
                                       data={"bot_generated": True}))
        provider = Mock(return_value={"reply": "我接着看看。"})
        plugin = self.setup_chat(provider)
        plugin.on_message(self.chat("那你看下", sequence=4), self.ctx)

        payload = json.loads(provider.call_args.args[1])
        context = payload["group_context"]
        self.assertEqual([row["text"] for row in context], [
            "刚才说的插件有点怪", "我也觉得，像是没传上下文", "我去翻下代码",
        ])
        self.assertEqual(context[-1]["speaker_name"], "机器人")
        self.assertTrue(context[-1]["is_bot"])
        self.assertEqual(payload["current"], "那你看下")
        self.assertTrue(all("sender_id" not in row for row in context))

    def test_mention_dialogue_receives_warm_and_cold_memory(self):
        self.store.store_warm_memory(ROOM, "2026-10-10", "群友约好周末一起开荒。", 8)
        self.store.add_cold_memory(ROOM, "偏好", "喜欢合作游戏")
        from plugins.memory import Plugin as MemoryPlugin
        memory_plugin = MemoryPlugin()
        memory_plugin.members = self.store
        self.ctx._services["memory"] = memory_plugin
        provider = Mock(return_value={"reply": "那就继续组队开荒呗。"})
        plugin = self.setup_chat(provider)
        plugin.on_message(self.chat("周末玩合作游戏吗？"), self.ctx)

        payload = json.loads(provider.call_args.args[1])
        self.assertEqual(payload["memory"]["warm"][0]["summary"], "群友约好周末一起开荒。")
        self.assertEqual(payload["memory"]["cold"][0]["text"], "喜欢合作游戏")
        self.assertIn("memory.warm", provider.call_args.args[0])

    def test_mention_dialogue_does_not_inject_stored_memory_when_plugin_disabled(self):
        self.store.store_warm_memory(ROOM, "2026-10-10", "群友约好周末一起开荒。", 8)
        provider = Mock(return_value={"reply": "好呀。"})
        plugin = self.setup_chat(provider)
        plugin.on_message(self.chat("周末安排？"), self.ctx)

        payload = json.loads(provider.call_args.args[1])
        self.assertNotIn("memory", payload)

    def test_true_mention_dialogue_passes_fenced_minecraft_identity_to_model(self):
        """群聊模型请求必须带上当前 fixture 的身份链，而不是只带可读状态。"""
        provider = Mock(return_value={"reply": "现在在线，刚刚还在出生点附近。"})
        mc_status = Mock()
        mc_status.context.return_value = {
            "connected": True,
            "state_revision": 42,
            "fixture_id": "fixture-test-001",
            "world_epoch": "epoch-test-007",
            "bot_session": "session-test-009",
            "fixture_phase": "ready",
            "health": {"current": 20, "max": 20},
            "hunger": 18,
            "location": {"dimension": "minecraft:overworld", "x": 3.5, "y": 66, "z": 0.5},
            "task": {"name": "nearby_scan", "status": "succeeded"},
            "inventory": [{"name": "minecraft:bread", "count": 2}],
            "memories": [{"kind": "observation", "memory_key": "spawn", "text": "出生点在橡树林边", "created_at": "2026-10-10T00:00:00Z"}],
        }
        self.ctx._services["mc_status"] = mc_status
        plugin = self.setup_chat(provider)
        plugin.on_message(self.chat("你现在在哪里？"), self.ctx)

        _system, user = provider.call_args.args
        payload = json.loads(user)
        self.assertEqual(payload["minecraft"]["state_revision"], 42)
        self.assertEqual(payload["minecraft"]["fixture_id"], "fixture-test-001")
        self.assertEqual(payload["minecraft"]["world_epoch"], "epoch-test-007")
        self.assertEqual(payload["minecraft"]["bot_session"], "session-test-009")
        self.assertEqual(payload["minecraft"]["fixture_phase"], "ready")
        self.assertEqual(payload["minecraft"]["memories"][0]["memory_key"], "spawn")
        mc_status.context.assert_called_once_with()

    def test_current_activity_question_gets_shared_journal_but_smalltalk_does_not(self):
        self.store.set_personality(ROOM, "鲸鱼娘")
        app_dir = self.root / "MC agent" / "wechat-candidates" / "wx-helper"
        journal = self.root / "MC agent" / "docs" / "MC_AGENT_ACTIVITY_JOURNAL.md"
        live_journal = self.root / "MC agent" / ".runtime" / "MC_AGENT_ACTIVITY_JOURNAL_LIVE.md"
        app_dir.mkdir(parents=True)
        journal.parent.mkdir(parents=True)
        live_journal.parent.mkdir(parents=True)
        journal.write_text("""# MC Agent 活动日记

## 当前状态
- **更新时间：** 2026-10-31 20:05（Asia/Shanghai）
- **当前实例：** Paper 26.3 隔离服 `127.0.0.1:25566`; Core `127.0.0.1:8788`; bot session `session-secret`。
- **运行情况：** PID 10552 正在运行。
- **慢思考：** 最近决策是安全探索；原因是食物短缺。
- **游戏状态：** 生命 20，饥饿 20，任务 idle，背包有 22 个泥土。
- **未证实事项：** 没有确认完成食物补给。

## 下一步
1. 观察探索航点是否扩展覆盖。

## 时间线
- 日志路径：.runtime/private.jsonl
""", encoding="utf-8")
        live_journal.write_text("""# MC Agent 活动日记（实时镜像）

## 当前状态
- **更新时间：** 2026-10-31 21:43:28 +08:00
- **当前实例：** Paper 26.3 隔离服；Paper 端口未监听。
- **慢思考：** 模型请求 planner.error latency_ms=15482；动作来源 circuit_fallback。
- **游戏状态：** 当前动作待机。
- **未证实事项：** 真实采集尚未确认。

## 下一步
1. 恢复隔离服后再测。
""", encoding="utf-8")
        self.ctx.config = {
            "activity_journal_file": "../../docs/MC_AGENT_ACTIVITY_JOURNAL.md",
            "activity_live_file": "../../.runtime/MC_AGENT_ACTIVITY_JOURNAL_LIVE.md",
        }
        self.ctx.app_dir = app_dir
        provider = Mock(return_value={"reply": "日记里记着正在安全探索。"})
        plugin = self.setup_chat(provider)

        plugin.on_message(self.chat("你现在在做什么？", sequence=101), self.ctx)
        system, user = provider.call_args.args
        payload = json.loads(user)
        self.assertEqual(payload["mc_activity_journal"]["source"], "MC_AGENT_ACTIVITY_JOURNAL.md")
        joined = json.dumps(payload["mc_activity_journal"], ensure_ascii=False)
        self.assertIn("食物短缺", joined)
        self.assertIn("观察探索航点", joined)
        self.assertIn("2026-10-31 21:43:28", joined)
        self.assertIn("Paper 端口未监听", joined)
        self.assertIn("15482", joined)
        self.assertNotIn("session-secret", joined)
        self.assertNotIn("private.jsonl", joined)
        self.assertIn("优先使用 live_snapshot", system)
        self.assertIn("不能说成已完成", system)

        plugin.on_message(self.chat("晚上一起打游戏吗？", sequence=102), self.ctx)
        payload = json.loads(provider.call_args.args[1])
        self.assertNotIn("mc_activity_journal", payload)

    def test_mention_dialogue_degrades_to_plain_chat_when_minecraft_service_fails(self):
        provider = Mock(return_value={"reply": "我先陪你聊会儿。"})
        mc_status = Mock()
        mc_status.context.side_effect = TimeoutError("fixture state unavailable")
        self.ctx._services["mc_status"] = mc_status
        plugin = self.setup_chat(provider)
        plugin.on_message(self.chat("还在吗？"), self.ctx)

        _system, user = provider.call_args.args
        payload = json.loads(user)
        self.assertNotIn("minecraft", payload)
        self.assertTrue(self.backend.sent[-1][1].startswith("@测试成员\u2005"))

    def test_short_mouth_dialogue_is_plain_and_keeps_real_at_and_context(self):
        self.store.set_personality(ROOM, "嘴臭")
        self.store.record(self.message("今天写了三行代码", sequence=1))
        provider = Mock(return_value={"reply": "```text\n**三行代码就敢吹，先把手上的任务搞定再说。**\n```"})
        plugin = self.setup_chat(provider)
        plugin.on_message(self.chat("我已经很强了"), self.ctx)
        sent = self.backend.sent[-1]
        body = strip_at_labels(sent[1])
        self.assertEqual(sent[2], (CAT,))
        self.assertLessEqual(len(body), 20)
        self.assertTrue(body.startswith("三行代码"))
        self.assertFalse(any(char in body for char in "`#*-|"))
        self.assertEqual(len(json.loads(provider.call_args.args[1])["previous"]), 1)
        self.assertIn("不与功能格式冲突时生效", provider.call_args.args[0])
        provider.return_value = {"reply": "#**```"}
        plugin.on_message(self.chat(sequence=101), self.ctx)
        self.assertIn("暂时无法回复", self.backend.sent[-1][1])

    def test_short_mouth_summaries_omit_titles_and_cancel_restores_them(self):
        self.store.record(self.message("今天写了三行代码，剩下的明天做"))
        self.store.set_personality(ROOM, "嘴臭")
        self.ctx._llm = Mock(complete_json=Mock(return_value={"summary": "**代码写三行，吹牛倒是写满了一整本。剩下的明天再说。**"}))
        for command in ("/总结 测试成员", "/总结群聊"):
            self.plugin.on_message(self.message(command), self.ctx)
            body = self.backend.sent[-1][1]
            self.assertLessEqual(len(body), 20)
            self.assertTrue(body.startswith("代码写三行"))
            self.assertNotIn("今日", body)
            self.assertNotIn("*", body)
        prepare_switch(self.message("/切换人格 取消"), self.store)
        self.plugin.on_message(self.message("/总结 测试成员"), self.ctx)
        self.assertTrue(self.backend.sent[-1][1].startswith("测试成员 今日聊天"))

    def test_short_mouth_idiom_reply_keeps_the_next_word_without_formatting(self):
        success = game_reply("嘴臭", True, "", "一心一意", "意气风发", "发愤图强")
        failure = game_reply("嘴臭", False, "**末字和首字不相同，请认真核对接字**", "一心一意", "乱写", "一心一意")
        self.assertIn("发愤图强", success)
        self.assertIn("一心一意", failure)
        for text in (success, failure):
            self.assertLessEqual(len(text), 20)
            self.assertFalse(any(char in text for char in "`#*-|"))

    def test_mention_of_other_person_and_bot_output_do_not_call_model(self):
        provider = Mock(return_value={"reply": "回答"})
        plugin = self.setup_chat(provider)
        for msg in (self.chat(at_members=(OTHER,)), self.chat(sequence=101, bot_generated=True),
                    self.message("@机器人 只是可见文字"), Message("私聊", CAT)):
            plugin.on_message(msg, self.ctx)
        provider.assert_not_called()
        self.assertEqual(self.backend.sent, [])

    def test_empty_bot_mention_uses_short_rotating_reply_without_old_context(self):
        provider = Mock(return_value={"reply": "还没好，当前没连代码仓库，没法直接改。"})
        plugin = self.setup_chat(provider)
        self.store.record(self.message("旧话题：检查启动脚本", sequence=1))
        messages = []
        for sequence in (101, 102, 103):
            msg = self.message("@鲸鱼娘", sequence=sequence,
                               data={"at_members": (ROBOT,), "is_at_self": True,
                                     "bot_mention_alias": "鲸鱼娘"})
            self.store.record(msg)
            messages.append(msg)
        for msg in messages:
            plugin.on_message(msg, self.ctx)
        provider.assert_not_called()
        replies = [row[1].split("\u2005\n", 1)[-1] for row in self.backend.sent]
        self.assertEqual(len(set(replies)), 3)
        self.assertNotIn("还没好", " ".join(replies))
        self.assertTrue(all(len(reply) <= 12 for reply in replies))

    def test_unbound_person_can_chat_and_bot_does_not_reply_to_id_text(self):
        plugin = self.setup_chat()
        plugin.on_message(self.chat(sender="wxid_unbound"), self.ctx)
        self.assertTrue(self.backend.sent[-1][1].startswith("@群友\u2005"))
        self.assertEqual(self.backend.sent[-1][2], ("wxid_unbound",))

    def test_cleared_current_message_or_day_change_does_not_reuse_history(self):
        plugin = self.setup_chat()
        msg = self.chat()
        self.store.clear(ROOM)
        plugin.on_message(msg, self.ctx)
        self.assertEqual(self.backend.sent, [])
        msg = self.chat(sequence=102)
        self.now += timedelta(seconds=1)
        plugin.on_message(msg, self.ctx)
        self.assertEqual(self.backend.sent, [])

    def test_clear_and_personality_change_during_chat_request_cancel_reply(self):
        for mutation in (lambda: self.store.clear(ROOM), lambda: self.store.set_personality(ROOM, "卑微")):
            self.store.set_personality(ROOM, "嘴臭")
            def change(system, user):
                mutation()
                return {"reply": "失效的结果"}
            plugin = self.setup_chat(change)
            plugin.on_message(self.chat(), self.ctx)
        self.assertEqual(self.backend.sent, [])

    def test_personality_change_during_at_send_pacing_cancels_native_send(self):
        self.setup_chat()
        msg = self.chat()
        snapshot = self.store.conversation_snapshot(msg)
        state = self.store.set_personality(ROOM, "嘴臭")
        cancel = Mock(is_set=lambda: False)
        def wait(seconds):
            self.store.set_personality(ROOM, "卑微")
            return False
        cancel.wait = wait
        self.service._cancel = cancel
        with self.assertRaises(PersonalityExpired):
            self.ctx.reply_at(msg, "旧回复", display_name="测试成员",
                              delivery_guard=lambda: self.ctx.response_guard(snapshot, state))
        self.assertEqual(self.backend.sent, [])

    def test_long_reply_respects_byte_limit_including_mention_nickname(self):
        self.store.register("🌸" * 64, ROOM, CAT)
        plugin = self.setup_chat(Mock(return_value={"reply": "中文🌸" * 190}))
        plugin.on_message(self.chat(), self.ctx)
        self.assertGreater(len(self.backend.sent), 1)
        self.assertTrue(all(len(row[1].encode("utf-8")) <= 1023 for row in self.backend.sent))
        self.assertTrue(all(row[2] == (CAT,) for row in self.backend.sent))

    def test_invalid_model_reply_is_reported_without_deleting_context(self):
        self.store.record(self.message("历史", sequence=1))
        plugin = self.setup_chat(Mock(return_value={"reply": ""}))
        plugin.on_message(self.chat(), self.ctx)
        self.assertIn("暂时无法回复", self.backend.sent[-1][1])
        self.assertEqual(len(self.store.conversation_snapshot(self.chat(sequence=101)).messages), 2)

    def test_idiom_game_skips_at_dialogue_and_style_keeps_correct_next_word(self):
        plugin = IdiomPlugin()
        plugin.on_load(self.ctx)
        plugin.games[ROOM] = Mock()
        plugin.on_message(self.chat(), self.ctx)
        self.assertEqual(self.backend.sent, [])
        for name in PROMPTS:
            reply = game_reply(name, True, "正确", "一心一意", "意气风发", "发扬光大")
            self.assertIn("发扬光大", reply)
            self.assertIn("以“大”", reply)


class NativeAtReceiveTests(unittest.TestCase):
    def test_cdata_and_multiple_members_are_parsed_without_guessing_names(self):
        self.assertEqual(parse_at_members("<msgsource><atuserlist><![CDATA[wxid_a,amy123,wxid_a]]></atuserlist></msgsource>"),
                         ("wxid_a", "amy123"))
        self.assertEqual(parse_at_members("<msgsource><atuserlist>filehelper,坏名字,wxid_b</atuserlist></msgsource>"), ("wxid_b",))
        for source in ("", "@机器人", "<msgsource>", "<!DOCTYPE a [<!ENTITY x 'abc'>]><a>&x;</a>", "x" * 4096):
            self.assertEqual(parse_at_members(source), ())

    def test_visible_labels_are_removed_on_unicode_at_boundary(self):
        self.assertEqual(strip_at_labels("@机器 人\u2005@小明\u2005请看看这句话"), "请看看这句话")
        self.assertEqual(strip_at_labels("普通 @example.com 文本"), "普通 @example.com 文本")

    def test_native_metadata_marks_only_actual_robot_group_at(self):
        native = daily.ROOT / "native"
        if str(native) not in sys.path:
            sys.path.insert(0, str(native))
        from text_service import NativeTextService
        from filehelper_service import ReceivedTextMessage
        service = NativeTextService(pid=123)
        service.router.set_account(ROBOT)
        msg = ReceivedTextMessage(type=1, sender=ROOM.encode(), receiver=ROBOT.encode(),
                                  text=(CAT + ":\n@机器人\u2005你好").encode(),
                                  mention_source=f"<msgsource><atuserlist>{ROBOT}</atuserlist></msgsource>".encode())
        self.assertTrue(service._map_message(msg)["is_at_self"])
        msg.mention_source = f"<msgsource><atuserlist>{OTHER}</atuserlist></msgsource>".encode()
        self.assertFalse(service._map_message(msg)["is_at_self"])
        msg.mention_source = b""
        self.assertFalse(service._map_message(msg)["is_at_self"])
        msg.sender, msg.text = CAT.encode(), "普通私聊".encode()
        msg.mention_source = f"<msgsource><atuserlist>{ROBOT}</atuserlist></msgsource>".encode()
        self.assertFalse(service._map_message(msg)["is_at_self"])


if __name__ == "__main__":
    unittest.main()
