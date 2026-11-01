"""配置读取与插件启停状态保存。"""
from __future__ import annotations

import json
import math
import re
import os
import tempfile
from pathlib import Path

PLUGIN_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def validate_name(name):
    if not isinstance(name, str) or not PLUGIN_NAME.fullmatch(name):
        raise ValueError(f"插件目录名不合法：{name!r}")
    return name


class ConfigStore:
    def __init__(self, path):
        self.path = Path(path).resolve()

    def load(self):
        data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("配置文件必须是 JSON 对象")
        if data.get("backend", "native_filehelper") not in ("native_filehelper", "native_text", "native_42_text", "uia_text", "auto"):
            raise ValueError("backend 应为 native_filehelper、native_text、native_42_text、uia_text 或 auto")
        from .pacing import TextSendDelay
        TextSendDelay.from_config(data.get("text_send_delay"))
        from .sendguard import SendGuardConfig
        SendGuardConfig.from_config(data.get("send_guard"))
        llm_file = data.get("llm_config_file", "llm_config.json")
        if not isinstance(llm_file, str) or not llm_file.strip() or "\0" in llm_file:
            raise ValueError("llm_config_file 应为大模型配置文件路径")
        members_file = data.get("members_config_file")
        if members_file is not None and (not isinstance(members_file, str) or not members_file.strip() or "\0" in members_file):
            raise ValueError("members_config_file 应为成员配置文件路径或 null")
        if data.get("wechat_account") is not None:
            from .account import validate_account
            validate_account(data["wechat_account"])
        allowed = data.get("chat_allowlist", [])
        if not isinstance(allowed, list):
            raise ValueError("chat_allowlist 应为微信 ID 或群 ID 列表；空列表表示所有会话")
        from .text import validate_target
        for target in allowed:
            validate_target(target)
        pid = data.get("wechat_pid")
        if pid is not None and (type(pid) is not int or pid <= 0):
            raise ValueError("wechat_pid 应为正整数或 null")
        if data.get("uia_chat_name", "") is not None and not isinstance(data.get("uia_chat_name", ""), str):
            raise ValueError("uia_chat_name 应为字符串")
        if type(data.get("uia_poll_seconds", 0.5)) not in (int, float) or data.get("uia_poll_seconds", 0.5) < 0.1:
            raise ValueError("uia_poll_seconds 应为至少 0.1 的数值")
        if type(data.get("uia_import_visible_history", True)) is not bool:
            raise ValueError("uia_import_visible_history 必须是布尔值")
        interval = data.get("reconnect_seconds", 5)
        if type(interval) not in (int, float) or not math.isfinite(interval) or interval < 0.1:
            raise ValueError("reconnect_seconds 应为至少 0.1 的有限数值")
        size = data.get("plugin_queue_size", 128)
        if type(size) is not int or not 1 <= size <= 10000:
            raise ValueError("plugin_queue_size 应为 1–10000 的整数")
        if not isinstance(data.get("plugins", {}), dict):
            raise ValueError("plugins 必须是 JSON 对象")
        inbox = data.get("codex_inbox", {})
        if not isinstance(inbox, dict):
            raise ValueError("codex_inbox 必须是 JSON 对象")
        if "enabled" in inbox and type(inbox["enabled"]) is not bool:
            raise ValueError("codex_inbox.enabled 必须是布尔值")
        if inbox.get("host", "127.0.0.1") != "127.0.0.1":
            raise ValueError("codex_inbox.host 只能是 127.0.0.1")
        port = inbox.get("port", 8799)
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("codex_inbox.port 应为 0–65535 的整数")
        cooldown = inbox.get("min_interval_seconds", 600)
        if (type(cooldown) not in (int, float) or not math.isfinite(cooldown) or
                not 600 <= cooldown <= 86400):
            raise ValueError("codex_inbox.min_interval_seconds 应为 600–86400")
        pending = inbox.get("max_pending", 32)
        if type(pending) is not int or not 1 <= pending <= 100:
            raise ValueError("codex_inbox.max_pending 应为 1–100")
        for key in ("database_file", "token_file"):
            value = inbox.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip() or "\0" in value):
                raise ValueError(f"codex_inbox.{key} 应为有效路径")
        for name, options in data.get("plugins", {}).items():
            validate_name(name)
            if not isinstance(options, dict):
                raise ValueError(f"plugins.{name} 必须是 JSON 对象")
            if "enabled" in options and type(options["enabled"]) is not bool:
                raise ValueError(f"plugins.{name}.enabled 必须是布尔值")
        if data.get("log_level", "INFO").upper() not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError("log_level 不受支持")
        if type(data.get("console", True)) is not bool:
            raise ValueError("console 必须是布尔值")
        return data

    def set_enabled(self, name, enabled):
        validate_name(name)
        data = self.load()
        data.setdefault("plugins", {}).setdefault(name, {})["enabled"] = bool(enabled)
        # 在同一目录完成替换，避免中断写入留下半份 JSON。
        descriptor, temporary = tempfile.mkstemp(prefix=".bot_config_", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                output.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return data
