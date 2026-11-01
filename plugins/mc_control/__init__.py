"""明确 @ 命令进入 Minecraft 任务队列；不让聊天模型直接操纵游戏。"""
from __future__ import annotations

import json
import math
import hashlib
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from wxbot_framework.mentions import strip_at_labels


class Plugin:
    def on_load(self, ctx):
        self.ctx = ctx
        self.members = ctx.members
        self.base_url = str(ctx.config.get("base_url", "http://127.0.0.1:8787")).rstrip("/")
        parsed = urlsplit(self.base_url)
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or
                parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("mc_control.base_url 必须是本机回环 HTTP 地址")
        self.game_id = str(ctx.config.get("game_id", "minecraft:local-test")).strip()
        self.allow_legacy_identity = bool(ctx.config.get("allow_legacy_identity", False))
        if not self.game_id or len(self.game_id) > 160 or any(ch in self.game_id for ch in " /\\?#\t\r\n"):
            raise ValueError("mc_control.game_id 无效")
        senders = ctx.config.get("allowed_sender_ids", [])
        groups = ctx.config.get("group_ids", [])
        if not isinstance(senders, list) or not all(isinstance(x, str) and x.strip() for x in senders):
            raise ValueError("mc_control.allowed_sender_ids 必须是微信 ID 列表")
        if not isinstance(groups, list) or not all(isinstance(x, str) and x.endswith("@chatroom") for x in groups):
            raise ValueError("mc_control.group_ids 必须是微信群 ID 列表")
        if any(group not in self.members.groups for group in groups):
            raise ValueError("mc_control.group_ids 必须全部位于成员目录白名单")
        self.allowed_senders = frozenset(senders)
        self.groups = frozenset(groups)
        self.timeout = float(ctx.config.get("timeout_seconds", 1.5))
        if not 0.2 <= self.timeout <= 5:
            raise ValueError("mc_control.timeout_seconds 应为 0.2–5 秒")

    def on_message(self, message, ctx):
        if (message.type != 1 or not message.is_group or message.chat_id not in self.groups or
                message.data.get("bot_generated") or not message.data.get("is_at_self") or
                self.members.account not in message.data.get("at_members", ()) or
                message.sender_id not in self.allowed_senders):
            return
        text = strip_at_labels(message.text).strip()
        if not text.lower().startswith(("/mc", "mc")):
            return
        try:
            task = self._parse(text)
            result = self._create(task, message)
            task_id = result.get("task", {}).get("task_id", "?")
            display = self.members.display_name(message.sender_id, message.chat_id)
            ctx.reply_at(message, f"已排队：{task['kind']}（{task_id[:12]}）", display_name=display)
        except ValueError as error:
            display = self.members.display_name(message.sender_id, message.chat_id)
            ctx.reply_at(message, f"MC 指令未执行：{error}", display_name=display)
        except HTTPError as error:
            display = self.members.display_name(message.sender_id, message.chat_id)
            if error.code == 404:
                text = f"MC 实例「{self.game_id}」不存在；请核对 game_id，任务没有提交。"
            else:
                text = "MC 任务服务暂时不可用。"
            ctx.reply_at(message, text, display_name=display)
        except (URLError, OSError, TimeoutError):
            display = self.members.display_name(message.sender_id, message.chat_id)
            ctx.reply_at(message, "MC 任务服务暂时不可用。", display_name=display)

    @staticmethod
    def _number(value, name):
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} 必须是数字")
        if not math.isfinite(number) or abs(number) > 30_000_000:
            raise ValueError(f"{name} 超出范围")
        return number

    def _parse(self, text):
        words = text.split(maxsplit=5)
        if not words or words[0].lower() not in ("mc", "/mc"):
            raise ValueError("用法：@机器人 /mc 走|看|采集|回家 ...")
        if len(words) < 2:
            raise ValueError("用法：/mc 走 x y z；/mc 看 x y z；/mc 采集 oak_log 4")
        action = words[1].lower()
        if action in ("走", "移动", "move"):
            if len(words) != 5:
                raise ValueError("移动用法：/mc 走 x y z")
            return {"kind": "move_to", "request": {"x": self._number(words[2], "x"), "y": self._number(words[3], "y"), "z": self._number(words[4], "z")}}
        if action in ("看", "look"):
            if len(words) != 5:
                raise ValueError("看用法：/mc 看 x y z")
            return {"kind": "look_at", "request": {"x": self._number(words[2], "x"), "y": self._number(words[3], "y"), "z": self._number(words[4], "z")}}
        if action in ("回家", "home"):
            if len(words) != 5:
                raise ValueError("回家用法：/mc 回家 x y z")
            return {"kind": "return_home", "request": {"home": {"x": self._number(words[2], "x"), "y": self._number(words[3], "y"), "z": self._number(words[4], "z")}}}
        if action in ("采集", "挖", "collect"):
            if len(words) not in (3, 4):
                raise ValueError("采集用法：/mc 采集 oak_log [数量]")
            count = 1 if len(words) == 3 else int(words[3]) if words[3].isdigit() else 0
            if not 1 <= count <= 16:
                raise ValueError("数量必须是 1–16")
            block = words[2].lower().removeprefix("minecraft:")
            if not block or len(block) > 64 or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_" for ch in block):
                raise ValueError("方块名无效")
            return {"kind": "collect", "request": {"block": block, "count": count}}
        if action in ("说", "say"):
            message = text.split(maxsplit=2)[2].strip() if len(text.split(maxsplit=2)) == 3 else ""
            if not message or len(message) > 200:
                raise ValueError("说用法：/mc 说 一句不超过 200 字的内容")
            return {"kind": "say", "request": {"message": message}}
        raise ValueError("只支持走、看、采集、回家、说")

    def _create(self, task, message):
        encoded = quote(self.game_id, safe="")
        sequence = message.sequence
        if not isinstance(sequence, int) or sequence <= 0:
            sequence = hashlib.sha256(
                f"{message.chat_id}\0{message.sender_id}\0{message.received_at:.6f}\0{message.text}".encode("utf-8")
            ).hexdigest()[:24]
        state = self._fetch_state(encoded)
        fixture_id = state.get("fixture_id")
        world_epoch = state.get("world_epoch")
        if fixture_id is None and world_epoch is None:
            if not getattr(self, "allow_legacy_identity", False):
                raise ValueError("Core 未提供 fixture 身份；任务已停止提交。")
        else:
            if (not isinstance(fixture_id, str) or not fixture_id.strip()
                    or not isinstance(world_epoch, str) or not world_epoch.strip()):
                raise ValueError("Core fixture 身份不完整；任务已停止提交。")
            data = state.get("data") if isinstance(state.get("data"), dict) else {}
            if (not state.get("connected") or data.get("session_identity_stale")
                    or data.get("fixture_phase") != "ready"):
                raise ValueError("世界未通过 ready 身份校验；任务已停止提交。")
        preconditions = dict(task.get("preconditions") or {})
        preconditions.setdefault("connected", True)
        body = dict(task, preconditions=preconditions, priority=70,
                    idempotency_key=f"wechat:{message.chat_id}:{sequence}:{message.sender_id}")
        revision = state.get("state_revision")
        if isinstance(revision, int) and revision >= 0:
            body["expected_state_revision"] = revision
        if fixture_id is not None:
            body["expected_fixture_id"] = fixture_id
            body["expected_world_epoch"] = world_epoch
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(f"{self.base_url}/v1/games/{encoded}/tasks", data=raw,
                          headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
        with urlopen(request, timeout=self.timeout) as response:
            if response.status not in (200, 202):
                raise OSError(f"status={response.status}")
            result = json.loads(response.read(128 * 1024).decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("Core 返回格式异常")
        return result

    def _fetch_state(self, encoded_game_id):
        request = Request(f"{self.base_url}/v1/games/{encoded_game_id}/state",
                          headers={"Accept": "application/json"}, method="GET")
        with urlopen(request, timeout=self.timeout) as response:
            if response.status != 200:
                raise OSError(f"status={response.status}")
            value = json.loads(response.read(128 * 1024).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Core 状态格式异常")
        return value
