"""本机动作插件：命令负责审批，自然语言提案由 mention_chat 统一回复。"""
from __future__ import annotations

import hashlib
import re
import subprocess
import uuid
from pathlib import Path

from wxbot_framework.dialogue import Dialogue
from wxbot_framework.codex_inbox import InboxError
from wxbot_framework.llm import LLMError
from wxbot_framework.local_control import ProposalStore, SafeLocalExecutor
from wxbot_framework.members import SnapshotExpired
from wxbot_framework.mentions import strip_at_labels
from wxbot_framework.personality import personality_text
from wxbot_framework.summary import split_text


URL_RE = re.compile(r"https?://[^\s<>\u3000]+", re.IGNORECASE)
LOCAL_HINT_RE = re.compile(r"启动|打开|运行|电脑|本机|桌面|codex|代码|截图|程序", re.IGNORECASE)
EXPLICIT_LOCAL_INTENT_RE = re.compile(
    r"codex|本机|电脑|桌面|启动|打开|运行|截图|读一下|检查(?:一下|下)?|看看.{0,8}(?:文件|代码|项目)|"
    r"(?:告诉|发给|通知|传给|汇报).{0,10}codex|codex.{0,10}(?:告诉|发给|通知|传给|汇报)|"
    r"帮我.{0,12}(?:查|改|看|修|读)|(?:查|改|看|修).{0,12}(?:代码|文件|项目)", re.IGNORECASE)


def _codex_source_ref(sources, fallback="群消息"):
    for source in sources:
        if isinstance(source, str) and source.startswith("wechat:") and "#" in source:
            sequence = source.rsplit("#", 1)[1]
            if sequence.isdigit():
                return f"群消息 #{sequence}"
    return fallback
PUNCTUATION = "，。！？；：、）》】」』\"'.,!?;:)]}"


def _urls(text: str) -> set[str]:
    return {match.rstrip(PUNCTUATION) for match in URL_RE.findall(text or "")}


class Plugin:
    def on_load(self, ctx):
        self.ctx = ctx
        self.members = ctx.members
        options = dict(ctx.config)
        get_service = getattr(ctx, "get_service", None)
        self.codex_inbox = get_service("codex_inbox") if callable(get_service) else None
        self.codex_inform_enabled = options.get("codex_inform", False)
        if type(self.codex_inform_enabled) is not bool:
            raise ValueError("local_control.codex_inform 必须是布尔值")
        if self.codex_inform_enabled and self.codex_inbox is None:
            raise ValueError("local_control.codex_inform 需要启用 codex_inbox")
        aliases = options.get("owner_aliases", ["主人"])
        if not isinstance(aliases, list) or not aliases or not all(isinstance(x, str) and x.strip() for x in aliases):
            raise ValueError("local_control.owner_aliases 必须是已绑定成员称呼列表")
        self.owner_ids = {}
        for room in self.members.groups:
            ids = set()
            for alias in aliases:
                try:
                    member = self.members.resolve(alias, room)
                except Exception:
                    continue
                if member.wxid:
                    ids.add(member.wxid)
            if len(ids) > 1:
                raise ValueError(f"群 {room} 的主人称呼绑定到多个微信 ID")
            if ids:
                self.owner_ids[room] = next(iter(ids))
        if not self.owner_ids:
            raise ValueError("主人别名未绑定微信 ID；拒绝加载本机控制插件")
        state_file = Path(options.get("state_file", ".runtime/local_control.jsonl"))
        if not state_file.is_absolute():
            state_file = Path(ctx.app_dir) / state_file
        self.store = ProposalStore(state_file, ttl_seconds=int(options.get("proposal_ttl_seconds", 900)))
        self.executor = SafeLocalExecutor(options, audit=self._audit, app_dir=ctx.app_dir)
        self.codex_daily_limit = options.get("codex_daily_limit", 3)
        if type(self.codex_daily_limit) is not int or not 0 <= self.codex_daily_limit <= 20:
            raise ValueError("codex_daily_limit 应为 0–20")
        self.codex_readonly_autonomous = options.get("codex_readonly_autonomous", False)
        if type(self.codex_readonly_autonomous) is not bool:
            raise ValueError("codex_readonly_autonomous 必须是布尔值")
        if self.codex_readonly_autonomous and not options.get("codex_readonly", False):
            raise ValueError("codex_readonly_autonomous 需要同时启用 codex_readonly")
        self.catalog = []
        if self.executor.apps:
            self.catalog.append({"action": "launch_app", "description": "启动配置 allowlist 中的固定应用",
                                 "apps": sorted(self.executor.apps),
                                 "autonomous_apps": sorted(self.executor.autonomous_apps)})
        hosts = options.get("allowed_url_hosts", [])
        if hosts:
            self.catalog.append({"action": "open_url", "description": "打开 allowlist 域名的网页",
                                 "hosts": list(hosts)})
        if options.get("codex_readonly", False) and self.executor.codex_workspaces:
            self.catalog.append({"action": "codex_readonly", "description": "限定工作区的 Codex 只读查询",
                                 "workspaces": sorted(self.executor.codex_workspaces)})
        if self.codex_inform_enabled:
            self.catalog.append({"action": "codex_inform", "description":
                                 "向 Codex 本机收件箱递交一条简短、非敏感的技术说明；不会执行消息内容"})
        ctx.register_service("local_control", self)
        if not self.catalog:
            ctx.logger.warning("local_control 没有配置任何动作 allowlist；只提供能力查询")

    def on_unload(self, ctx):
        ctx.unregister_service("local_control", self)

    def _audit(self, row):
        self.store._append(row)

    def _is_owner(self, message):
        return self.owner_ids.get(message.chat_id) == message.sender_id

    def _reply(self, message, text, ctx, *, guard=None):
        display = self.members.display_name(message.sender_id, message.chat_id)
        for part in split_text(text, byte_limit=850):
            ctx.reply_at(message, part, display_name=display, delivery_guard=guard)

    def _new_proposal(self, message, action, args, reason, sources, *, source_digest=""):
        risk = "medium" if action in ("launch_app", "open_url") else "high"
        return self.store.create(action=action, args=args, reason=reason,
                                 sources=[f"wechat:{message.chat_id}#{message.sequence}", *sources],
                                 risk=risk, requester_id=message.sender_id, chat_id=message.chat_id,
                                 source_digest=source_digest)

    @staticmethod
    def _source_message(message):
        return {"chat_id": message.chat_id, "sequence": message.sequence,
                "sender_id": message.sender_id, "text": message.text}

    def _handle_command(self, message, ctx, command):
        words = command.split()
        if len(words) < 2:
            return False
        sub = words[1]
        if sub in ("能力", "capabilities"):
            text = "本机能力：" + ("、".join(item["action"] for item in self.catalog) or "未配置动作")
            self._reply(message, text, ctx)
            return True
        if sub in ("提案", "propose"):
            if not self._is_owner(message):
                self._reply(message, "本机动作只接受主人发起。", ctx)
                return True
            if len(words) == 4 and words[2] in ("启动", "launch"):
                if words[3] not in self.executor.apps:
                    raise ValueError("应用不在 allowlist 中")
                proposal = self._new_proposal(message, "launch_app", {"app": words[3]},
                                               "主人明确要求启动 allowlist 应用", [])
                self._reply(message, proposal.public_text(), ctx)
                return True
            self._reply(message, "用法：/本机 提案 启动 应用名", ctx)
            return True
        if sub in ("诏书", "approve", "拒绝", "reject"):
            if not self._is_owner(message) or len(words) != 4:
                self._reply(message, "用法：/本机 诏书|拒绝 提案ID 口令（仅主人可用）", ctx)
                return True
            approved = sub in ("诏书", "approve")
            proposal = self.store.decide(words[2], approver_id=message.sender_id, token=words[3],
                                         approved=approved, source_message=self._source_message(message),
                                         expected_chat_id=message.chat_id)
            if not approved:
                self._reply(message, "提案已拒绝。", ctx)
                return True
            self.store.record_execution_started(proposal.id)
            try:
                args = dict(proposal.args)
                if proposal.action == "codex_readonly":
                    args["source_ref"] = _codex_source_ref(proposal.sources, "诏书提案")
                result = self.executor.execute(proposal.action, args)
                self.store.record_execution(proposal.id, result)
                detail = f"PID={result['pid']}" if "pid" in result else (
                    f"报告编号={result['report_id']}" if "report_id" in result else "完成")
                if proposal.action == "codex_readonly":
                    summary = result.get("summary") or "Codex 没有返回可展示的摘要。"
                    self._reply(message, f"Codex 只读求助完成（{detail}）：\n{summary}", ctx)
                else:
                    self._reply(message, f"诏书已执行：{proposal.action}，{detail}。", ctx)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                result = {"ok": False, "error": str(error)[:500]}
                self.store.record_execution(proposal.id, result)
                self._reply(message, f"诏书已核准，但执行失败：{error}", ctx)
            return True
        return False

    def try_reply(self, message, ctx):
        """由 mention_chat 调用，确保同一条 @ 消息只有一条回复。"""
        if (not self.catalog or not message.data.get("is_at_self") or
                message.data.get("reply_capable") is False):
            return False
        # 动作意图必须出现在触发消息本身；不能从较早的群聊上下文推断执行 Codex。
        if not EXPLICIT_LOCAL_INTENT_RE.search(message.text):
            return False
        is_owner = self._is_owner(message)
        catalog = list(self.catalog)
        if not is_owner:
            # 群友可以请求只读 Codex 提案；执行仍需主人发送诏书。
            catalog = [item for item in catalog if item.get("action") == "codex_readonly"]
            if not catalog:
                if not LOCAL_HINT_RE.search(message.text):
                    return False
                self._reply(message, "本机动作只接受主人发起；群友可以让我解释能力。", ctx)
                return True
        personality = ctx.personality(message)
        snapshot = self.members.conversation_snapshot(message)
        if self.codex_daily_limit == 0 or self.store.count_recent(
                "codex_readonly", chat_id=message.chat_id, seconds=86400) >= self.codex_daily_limit:
            catalog = [item for item in catalog if item["action"] != "codex_readonly"]
        try:
            reply, proposal_data = Dialogue(ctx.llm_for(message, personality=personality)).reply_with_proposal(
                snapshot, catalog)
            reply = personality_text(reply, personality.name)
            known_urls = _urls(message.text)
            if proposal_data is not None:
                action, args = proposal_data["action"], proposal_data["args"]
                allowed = {item["action"] for item in catalog}
                if action not in allowed:
                    raise ValueError("模型提议了未注册的动作")
                if action == "launch_app" and (set(args) != {"app"} or args.get("app") not in self.executor.apps):
                    raise ValueError("模型提议的应用不在 allowlist 中")
                if action == "open_url":
                    if set(args) != {"url"} or args.get("url") not in known_urls:
                        raise ValueError("模型只能打开当前消息中真实出现的 URL")
                if action == "codex_readonly":
                    if (set(args) != {"workspace", "task"} or not isinstance(args.get("workspace"), str) or
                            not isinstance(args.get("task"), str) or len(args["task"].strip()) > 1200 or
                            args["workspace"] not in self.executor.codex_workspaces):
                        raise ValueError("模型提议的 Codex 工作区不在 allowlist 中")
                if action == "codex_inform":
                    if (not is_owner or not self.codex_inform_enabled or set(args) != {"text"} or
                            not isinstance(args.get("text"), str) or not 1 <= len(args["text"].strip()) <= 1600):
                        raise ValueError("Codex 回传只接受主人发起的一条简短说明")
                # User-facing URLs are sourced only from the triggering message itself.
                sources = sorted(_urls(message.text))
                digest = hashlib.sha256(
                    (message.chat_id + "\0" + str(message.sequence) + "\0" + message.text).encode()).hexdigest()
                if action == "codex_readonly" and self.codex_readonly_autonomous and is_owner:
                    source_ref = f"群消息 #{message.sequence}"
                    task_digest = hashlib.sha256(args["task"].encode("utf-8")).hexdigest()
                    run_id = "cr-" + uuid.uuid4().hex[:12]
                    run_record = {"run_id": run_id, "chat_id": message.chat_id, "source_ref": source_ref,
                                  "workspace": args["workspace"], "task_digest": task_digest}
                    self.store.record_codex_run(**run_record, status="started")
                    try:
                        result = self.executor.execute(action, {**args, "source_ref": source_ref})
                    except (OSError, ValueError, subprocess.SubprocessError):
                        self.store.record_codex_run(**run_record, status="failed")
                        raise
                    self.store.record_codex_run(**run_record, status="succeeded", report_id=result["report_id"])
                    summary = result.get("summary") or "Codex 没有返回可展示的摘要。"
                    reply += (f"\n\nCodex 只读求助（来源：群消息 #{message.sequence}）："
                              f"\n{summary}\n本机报告：{result['report_id']}")
                elif action == "codex_inform":
                    key = hashlib.sha256((message.session_id + "\0" + str(message.sequence)).encode()).hexdigest()
                    try:
                        accepted = self.codex_inbox.accept(args["text"], source="wechat_bot",
                                                           idempotency_key=key,
                                                           reply_to_chat_id=message.chat_id)
                        reply += ("\n已把简短说明交给 Codex；处理完会通过本群给你回报。"
                                  if not accepted.get("duplicate") else
                                  "\n这条说明已经递过了；Codex 处理完会在本群回报。")
                    except InboxError as error:
                        if error.status != 429:
                            raise
                        reply += "\nCodex 收件箱正在冷却，十分钟内不再收第二条。"
                elif action == "launch_app" and args["app"] in self.executor.autonomous_apps:
                    self._audit({"kind": "autonomous_policy", "action": action, "app": args["app"],
                                 "requester_id": message.sender_id, "chat_id": message.chat_id,
                                 "source_digest": digest})
                    result = self.executor.execute(action, args)
                    reply += f"\n已按主人请求启动 {args['app']}（PID={result['pid']}）。"
                else:
                    proposal = self._new_proposal(message, action, args, proposal_data["reason"],
                                                  sources, source_digest=digest)
                    reply += "\n\n" + proposal.public_text()
            display = self.members.display_name(message.sender_id, message.chat_id) or snapshot.speaker_name
            guard = lambda: ctx.response_guard(snapshot, personality)
            for part in split_text(reply, byte_limit=850):
                if not ctx.reply_at(message, part, display_name=display, delivery_guard=guard):
                    return True
            return True
        except SnapshotExpired:
            return True
        except (LLMError, ValueError, OSError) as error:
            ctx.logger.warning("本机动作提案失败：%s", error)
            # 提案判断失败不应吞掉普通聊天。交回 mention_chat 后，
            # 它会用普通对话提示再尝试一次，避免“@ 了却没有正常回应”。
            return False

    def on_message(self, message, ctx):
        if (message.type != 1 or not message.is_group or message.chat_id not in self.members.groups or
                message.data.get("bot_generated") or not message.data.get("is_at_self") or
                self.members.account not in message.data.get("at_members", ()) or
                message.data.get("reply_capable") is False):
            return
        command = strip_at_labels(message.text).strip()
        if command.startswith("/本机") or command.startswith("/local"):
            try:
                self._handle_command(message, ctx, command)
            except (ValueError, KeyError, OSError) as error:
                self._reply(message, f"动作未执行：{error}", ctx)
