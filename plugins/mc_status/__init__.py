"""为群聊提供确定性 Minecraft 状态查询，不调用大模型。"""
from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from wxbot_framework.mentions import strip_at_labels


COMMANDS = {
    "/状态": "state",
    "状态": "state",
    "/背包": "state",
    "背包": "state",
    "/位置": "state",
    "位置": "state",
    "/任务": "state",
    "任务": "state",
    "/生命": "state",
    "生命": "state",
    "/记忆": "memory",
    "记忆": "memory",
}


class Plugin:
    def on_load(self, ctx):
        self.ctx = ctx
        self.members = ctx.members
        self.base_url = str(ctx.config.get("base_url", "http://127.0.0.1:8787")).rstrip("/")
        self.timeout = float(ctx.config.get("timeout_seconds", 1.5))
        if not 0.2 <= self.timeout <= 5:
            raise ValueError("mc_status.timeout_seconds 应为 0.2–5 秒")
        parsed = urlsplit(self.base_url)
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or
                parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("mc_status.base_url 必须是本机回环 HTTP 地址")
        self.game_id = str(ctx.config.get("game_id", "minecraft:local-test")).strip()
        if not self.game_id or len(self.game_id) > 160 or any(ch in self.game_id for ch in " /\\?#\t\r\n"):
            raise ValueError("mc_status.game_id 无效")
        ctx.register_service("mc_status", self)

    def on_unload(self, ctx):
        ctx.unregister_service("mc_status", self)

    def context(self):
        """给 @ 对话的最小状态；失败由调用方降级为普通聊天。"""
        value = self._fetch("state")
        data = value.get("data") if isinstance(value.get("data"), dict) else {}
        context = {
            "game_id": self.game_id,
            "connected": bool(value.get("connected")),
            "state_revision": value.get("state_revision"),
            "fixture_id": value.get("fixture_id"),
            "world_epoch": value.get("world_epoch"),
            "health": value.get("health") or {},
            "hunger": value.get("hunger"),
            "location": value.get("location") or {},
            "task": value.get("task") or {},
            "held_item": data.get("held_item"),
            "time_of_day": data.get("time_of_day"),
            "inventory": (value.get("inventory") or [])[:12],
        }
        context["bot_session"] = data.get("bot_session")
        context["fixture_phase"] = data.get("fixture_phase")
        context["session_identity_stale"] = bool(data.get("session_identity_stale"))
        try:
            memories = self._fetch("memory").get("memories", [])
            if isinstance(memories, list):
                context["memories"] = [
                    {**{key: item.get(key) for key in ("kind", "memory_key", "text", "created_at")},
                     "provenance": "historical_summary; verify_task_receipt_before_treating_as_world_fact"}
                    for item in memories[:6] if isinstance(item, dict) and item.get("text")
                ]
        except Exception:
            pass
        return context

    def _fetch(self, path):
        if path == "state":
            encoded = quote(self.game_id, safe="")
            path = f"/v1/games/{encoded}/state"
        elif path == "memory":
            encoded = quote(self.game_id, safe="")
            path = f"/v1/games/{encoded}/memory?limit=6"
        request = Request(self.base_url + path, headers={"Accept": "application/json"}, method="GET")
        with urlopen(request, timeout=self.timeout) as response:
            if response.status != 200:
                raise URLError(f"status={response.status}")
            raw = response.read(256 * 1024)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("状态响应不是 JSON 对象")
        return value

    @staticmethod
    def _number(value):
        return "—" if value is None else str(value)

    def _format(self, command, value):
        if command in ("状态", "/状态"):
            health = value.get("health") or {}
            location = value.get("location") or {}
            task = value.get("task") or {}
            return (f"MC {'在线' if value.get('connected') else '离线'}｜"
                    f"生命 {self._number(health.get('current'))}/{self._number(health.get('max'))} "
                    f"饥饿 {self._number(value.get('hunger'))}｜"
                    f"位置 {location.get('dimension') or '未知'} "
                    f"({self._number(location.get('x'))}, {self._number(location.get('y'))}, {self._number(location.get('z'))})｜"
                    f"任务 {task.get('name') or '无'}")
        if command in ("背包", "/背包"):
            items = value.get("items", value.get("inventory")) or []
            if not items:
                return "背包为空或状态尚未同步。"
            parts = []
            for item in items[:12]:
                if isinstance(item, dict):
                    parts.append(f"{item.get('name', '未知')}×{item.get('count', 0)}")
            return "背包：" + "、".join(parts) if parts else "背包状态格式异常。"
        if command in ("位置", "/位置"):
            location = value.get("location") or {}
            return (f"位置：{location.get('dimension') or '未知'} "
                    f"({self._number(location.get('x'))}, {self._number(location.get('y'))}, {self._number(location.get('z'))})")
        if command in ("任务", "/任务"):
            task = value.get("task") or {}
            return f"任务：{task.get('name') or '无'}（{task.get('status') or 'idle'}）"
        if command in ("记忆", "/记忆"):
            memories = value.get("memories") or []
            lines = []
            for item in memories[:6]:
                if isinstance(item, dict):
                    kind = item.get("kind") or "记忆"
                    text = str(item.get("text") or "").replace("\n", " ").strip()
                    if text:
                        lines.append(f"[{kind}] {text}")
            return "最近记忆：" + "；".join(lines) if lines else "暂时没有记录的 MC 记忆。"
        health = value.get("health") or {}
        return (f"生命：{self._number(health.get('current'))}/{self._number(health.get('max'))}，"
                f"饥饿：{self._number(value.get('hunger'))}")

    @staticmethod
    def _limit(text):
        while len(text.encode("utf-8")) > 900:
            text = text[:-1]
        return text

    def on_message(self, message, ctx):
        if (message.type != 1 or not message.is_group or message.chat_id not in self.members.groups or
                message.data.get("bot_generated") or not message.data.get("is_at_self") or
                self.members.account not in message.data.get("at_members", ())):
            return
        if message.data.get("reply_capable") is False:
            return
        command = strip_at_labels(message.text).strip()
        if command not in COMMANDS:
            return
        try:
            text = self._limit(self._format(command, self._fetch(COMMANDS[command])))
        except HTTPError as error:
            if error.code == 404:
                text = f"找不到 MC 实例「{self.game_id}」；请核对 wx-helper 与 Agent Core 的 game_id。"
            else:
                text = "MC 状态服务暂时未连接。"
        except Exception:
            text = "MC 状态服务暂时未连接。"
        display = self.members.display_name(message.sender_id, message.chat_id)
        ctx.reply_at(message, text, display_name=display)
