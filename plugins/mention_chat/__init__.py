"""真正艾特机器人后，用本群人格回复当前发言者。"""
from pathlib import Path

from wxbot_framework.activity_journal import is_current_activity_query, read_activity_journal
from wxbot_framework.dialogue import Dialogue
from wxbot_framework.llm import LLMError
from wxbot_framework.members import SnapshotExpired
from wxbot_framework.mentions import strip_at_labels
from wxbot_framework.personality import personality_text
from wxbot_framework.summary import split_text


MC_STATUS_COMMANDS = {"状态", "背包", "位置", "任务", "生命", "记忆", "/状态", "/背包", "/位置", "/任务", "/生命", "/记忆"}
PING_REPLIES = {
    "鲸鱼娘": ("咋啦？", "叫我干嘛呀？", "在，怎么啦？"),
    "嘴臭": ("干嘛？", "叫我干嘛？", "啥事？"),
    "卑微": ("爷，您叫我？", "小的在，您说。", "爷，咋啦？"),
    "夸夸": ("我在，怎么啦？", "叫我有好事？", "来啦，咋啦？"),
    "默认": ("咋啦？", "叫我吗？", "在，什么事？"),
}


class Plugin:
    def on_load(self, ctx):
        self.members = ctx.members
        if not self.members.history_enabled:
            raise ValueError("艾特上下文对话需要 history_enabled: true")
        ctx.llm  # 启动时核对配置，不请求接口。

    def on_message(self, message, ctx):
        if (message.type != 1 or not message.is_group or message.chat_id not in self.members.groups or
                "WXBOT_AT_CHAT_PROBE" in message.text or
                message.data.get("bot_generated") or not message.data.get("is_at_self") or
                self.members.account not in message.data.get("at_members", ())):
            return
        if message.data.get("reply_capable") is False:
            ctx.logger.info("当前微信适配器只读；记录消息但跳过模型回复")
            return
        ctx.logger.info("群聊 @ 已通过 mention_chat 过滤；开始路由")
        command = strip_at_labels(message.text).strip()
        alias = message.data.get("bot_mention_alias")
        if isinstance(alias, str) and alias and command.startswith("@" + alias):
            # UIA 有时只暴露裸 @昵称，没有原生消息里的分隔符 U+2005。
            command = command[len(alias) + 1:].lstrip("\u2005 \t")
        if command in MC_STATUS_COMMANDS:
            return
        # 命令消息由对应命令插件处理；这里不再用大模型回一遍，避免一次艾特两条回复。
        if command.startswith("/"):
            return
        if not command:
            # 纯 @ 没有当前问题。不要让模型从旧群聊里猜任务或续答旧话题。
            personality = ctx.personality(message)
            snapshot = self.members.conversation_snapshot(message)
            guard = lambda: ctx.response_guard(snapshot, personality)
            replies = PING_REPLIES.get(personality.name, PING_REPLIES["默认"])
            reply = replies[message.sequence % len(replies)]
            display = self.members.display_name(message.sender_id, message.chat_id) or snapshot.speaker_name
            if not ctx.reply_at(message, reply, display_name=display, delivery_guard=guard):
                raise RuntimeError("原生艾特发送未成功")
            ctx.logger.info("空艾特使用短回应：room=%s persona=%s", message.chat_id, personality.name)
            return
        local_control = ctx.get_service("local_control")
        if local_control is not None and local_control.try_reply(message, ctx):
            return
        personality = ctx.personality(message)
        try:
            snapshot = self.members.conversation_snapshot(message)
            guard = lambda: ctx.response_guard(snapshot, personality)
            minecraft = None
            mc_status = ctx.get_service("mc_status")
            if mc_status is not None:
                try:
                    minecraft = mc_status.context()
                except Exception as error:
                    ctx.logger.debug("MC 状态未能加入聊天上下文：%s", error)
            dialogue_context = {"minecraft": minecraft} if minecraft else {}
            memory_service = ctx.get_service("memory")
            if memory_service is not None:
                memory = memory_service.context(snapshot.room, snapshot.current)
                if memory["warm"] or memory["cold"]:
                    dialogue_context["memory"] = memory
            if is_current_activity_query(snapshot.current, snapshot.group_context):
                journal_file = ctx.config.get("activity_journal_file")
                if journal_file:
                    journal_path = Path(journal_file)
                    if not journal_path.is_absolute():
                        base = Path(ctx.app_dir) if ctx.app_dir is not None else Path(__file__).resolve().parents[2]
                        journal_path = base / journal_path
                    live_file = ctx.config.get("activity_live_file")
                    live_path = Path(live_file) if live_file else None
                    if live_path is not None and not live_path.is_absolute():
                        base = Path(ctx.app_dir) if ctx.app_dir is not None else Path(__file__).resolve().parents[2]
                        live_path = base / live_path
                    journal = read_activity_journal(journal_path, live_path=live_path)
                    if journal:
                        dialogue_context["mc_activity_journal"] = journal
                    else:
                        ctx.logger.debug("MC 活动日记未能加入聊天上下文")
            reply = personality_text(Dialogue(ctx.llm_for(message, personality=personality)).reply(
                snapshot, context=dialogue_context or None),
                                     personality.name)
            display = self.members.display_name(message.sender_id, message.chat_id) or snapshot.speaker_name
            budget = 1023 - len(("@" + display + "\u2005\n").encode("utf-8"))
            for part in split_text(reply, byte_limit=budget):
                if not ctx.reply_at(message, part, display_name=display, delivery_guard=guard):
                    raise RuntimeError("原生艾特发送未成功")
            ctx.logger.info("艾特对话完成：room=%s persona=%s context=%d", message.chat_id,
                            personality.name, len(snapshot.messages))
        except SnapshotExpired:
            ctx.logger.info("艾特对话已取消：记录或人格状态已变更")
        except LLMError as error:
            ctx.logger.warning("艾特对话失败：%s", error)
            try:
                display = self.members.display_name(message.sender_id, message.chat_id) or snapshot.speaker_name
                if not ctx.reply_at(message, "暂时无法回复，请稍后再艾特我。",
                                    display_name=display, delivery_guard=guard):
                    ctx.logger.warning("大模型失败提示未成功提交发送队列")
            except SnapshotExpired:
                pass
