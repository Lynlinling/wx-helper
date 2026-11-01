"""按群和账号隔离的三层聊天记忆管理。"""
from __future__ import annotations

from wxbot_framework.llm import LLMError
from wxbot_framework.members import MemberError, SnapshotExpired
from wxbot_framework.mentions import strip_at_labels
from wxbot_framework.summary import ChatSummarizer, conversation_rows, split_text


COMMANDS = {"/记忆整理", "/记住", "/忘记冷", "/忘记温", "/bot记忆", "/记忆列表", "/清空记忆"}


class Plugin:
    def on_load(self, ctx):
        self.members = ctx.members
        if not self.members.history_enabled:
            raise ValueError("三层记忆需要 members_config.history_enabled: true")
        aliases = ctx.config.get("owner_aliases", ["主人"])
        if (not isinstance(aliases, list) or not aliases or
                not all(isinstance(alias, str) and alias.strip() for alias in aliases)):
            raise ValueError("memory.owner_aliases 必须是已绑定成员称呼列表")
        self.owner_ids = {}
        for room in self.members.groups:
            ids = set()
            for alias in aliases:
                try:
                    member = self.members.resolve(alias, room)
                except MemberError:
                    continue
                if member.wxid:
                    ids.add(member.wxid)
            if len(ids) > 1:
                raise ValueError("memory.owner_aliases 在同一群不能绑定到多个成员")
            if ids:
                self.owner_ids[room] = next(iter(ids))
        if not self.owner_ids:
            raise ValueError("memory.owner_aliases 未绑定微信成员；拒绝开放记忆管理")
        ctx.llm  # 启动时核对模型配置，不请求接口。
        ctx.register_service("memory", self)

    def on_unload(self, ctx):
        ctx.unregister_service("memory", self)

    def context(self, room, query):
        return self.members.tiered_memory_context(room, query)

    def on_message(self, message, ctx):
        if (message.type != 1 or not message.is_group or message.chat_id not in self.members.groups or
                message.data.get("bot_generated")):
            return
        text = strip_at_labels(message.text).strip()
        words = text.split(maxsplit=1)
        if not words or words[0] not in COMMANDS:
            return
        if self.owner_ids.get(message.chat_id) != message.sender_id:
            ctx.reply(message, "记忆管理只认主人。")
            return
        if words[0] == "/记忆整理":
            if len(words) != 1:
                ctx.reply(message, "用法：/记忆整理（整理今天的群聊）")
                return
            self._summarize_today(message, ctx)
        elif words[0] == "/记住":
            self._remember(message, text, ctx)
        elif words[0] in ("/忘记冷", "/忘记温"):
            self._forget(message, words[0], words[1] if len(words) == 2 else "", ctx)
        elif words[0] == "/清空记忆":
            if len(words) != 1:
                ctx.reply(message, "用法：/清空记忆（清除本群温、冷记忆）")
                return
            removed = self.members.clear_tiered_memories(message.chat_id)
            ctx.reply(message, f"本群温记忆清除 {removed['warm']} 条，冷记忆清除 {removed['cold']} 条。")
        else:
            if len(words) != 1:
                ctx.reply(message, f"用法：{words[0]}")
                return
            self._list(message, ctx)

    def _summarize_today(self, message, ctx):
        try:
            snapshot = self.members.today_group_snapshot(message.chat_id)
            rows = conversation_rows(snapshot)
            if not rows:
                ctx.reply(message, "今天还没有可整理的群聊内容。")
                return
            personality = ctx.personality(message)
            summarizer = ChatSummarizer(ctx.llm_for(message, personality=personality))
            summary = summarizer.summarize(snapshot, rows,
                                           current=lambda: self.members.snapshot_is_current(snapshot),
                                           memory_mode=True)
            with self.members.delivery_guard(snapshot):
                memory_id = self.members.store_warm_memory(snapshot.room, snapshot.day, summary, len(rows))
            ctx.reply(message, f"今天的群聊已整理进温记忆（编号 {memory_id}，{len(rows)} 条有效发言）。",
                      delivery_guard=lambda: self.members.delivery_guard(snapshot))
        except SnapshotExpired:
            ctx.reply(message, "整理时聊天记录已变化，这次先不保存；再发一次 /记忆整理 就好。")
        except (MemberError, LLMError) as error:
            ctx.logger.warning("群聊记忆整理失败：%s", error)
            try:
                ctx.reply(message, "暂时没整理成功，稍后再试一次 /记忆整理。")
            except SnapshotExpired:
                pass

    def _remember(self, message, text, ctx):
        parts = text.split(maxsplit=2)
        if len(parts) != 3 or parts[1] not in ("事实", "偏好", "边界"):
            ctx.reply(message, "用法：/记住 事实|偏好|边界 内容（本群可见）")
            return
        try:
            memory_id, created = self.members.add_cold_memory(message.chat_id, parts[1], parts[2])
        except MemberError as error:
            ctx.reply(message, str(error))
            return
        ctx.reply(message, f"{ '记下了' if created else '这条已经记过' }，冷记忆编号 {memory_id}。")

    def _forget(self, message, command, raw_id, ctx):
        if not raw_id.isdigit() or int(raw_id) <= 0:
            ctx.reply(message, f"用法：{command} 编号")
            return
        memory_id = int(raw_id)
        try:
            removed = (self.members.forget_cold_memory(message.chat_id, memory_id)
                       if command == "/忘记冷" else
                       self.members.forget_warm_memory(message.chat_id, memory_id))
        except MemberError as error:
            ctx.reply(message, str(error))
            return
        ctx.reply(message, "已忘记这条记忆。" if removed else "没找到这个编号。")

    def _list(self, message, ctx):
        memories = self.members.list_tiered_memories(message.chat_id, warm_limit=3, cold_limit=8)
        lines = ["当前群的记忆（群内可见）："]
        if memories["warm"]:
            lines.append("温记忆：")
            for row in memories["warm"]:
                summary = row["summary"]
                if len(summary) > 220:
                    summary = summary[:219].rstrip() + "…"
                lines.append(f"温#{row['id']} {row['period_key']}：{summary}")
        else:
            lines.append("温记忆：暂无")
        if memories["cold"]:
            lines.append("冷记忆：")
            lines.extend(f"冷#{row['id']} [{row['kind']}] {row['content']}" for row in memories["cold"])
        else:
            lines.append("冷记忆：暂无")
        lines.append("管理：/记忆整理、/记住 类型 内容、/忘记温 编号、/忘记冷 编号、/清空记忆")
        for part in split_text("\n".join(lines), byte_limit=850):
            ctx.reply(message, part)
