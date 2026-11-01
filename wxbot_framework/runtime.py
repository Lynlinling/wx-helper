"""长期运行、连接监督与本地控制台命令。"""
from __future__ import annotations

import json
import logging
import queue
import shlex
import sys
import threading
import time

from .backend import (CAPABILITIES, TEXT_CAPABILITIES, NATIVE_42_CAPABILITIES, UIA_READ_ONLY_CAPABILITIES,
                      NativeFileHelperBackend, NativeTextBackend, UIAutomationTextBackend,
                      DatabaseReadonlyTextBackend, DatabaseNative42TextBackend)
from .plugins import PluginManager
from .sdk import ServiceFacade
from .members import MemberStore
from .personality import prepare_switch
from .usage import UsageStore
from .codex_inbox import CodexInbox

logger = logging.getLogger("Bot")


class BotApplication:
    def __init__(self, root, store, *, backend_factory=None):
        self.root, self.store = root, store
        self.config = store.load()
        self.service = ServiceFacade(text_delay=self.config.get("text_send_delay"),
                                     send_guard=self.config.get("send_guard"))
        # 未指定账号时，成员历史要等原生桥接报告当前登录账号后再绑定。
        # 这样专用号换号后无需改配置，SQLite 仍按账号隔离旧记录。
        self._dynamic_member_store = bool(self.config.get("members_config_file") and
                                          not self.config.get("wechat_account"))
        self.members = MemberStore.from_config(self.config, self.store.path.parent)
        self.usage_store = UsageStore(root / ".runtime/llm_usage.sqlite3")
        backend_name = self.config.get("backend")
        self.auto_backend = backend_name == "auto"
        self._custom_backend_factory = backend_factory is not None
        effective_name = ("native_text" if self.auto_backend and self._custom_backend_factory
                          else "uia_text" if self.auto_backend else backend_name)
        generic = effective_name in ("native_text", "uia_text")
        capabilities = (UIA_READ_ONLY_CAPABILITIES if effective_name == "uia_text" else
                        NATIVE_42_CAPABILITIES if effective_name == "native_42_text" else
                        TEXT_CAPABILITIES if generic else CAPABILITIES)
        self.plugins = PluginManager(root / "plugins", store, self.service, capabilities,
                                     self.config.get("plugin_queue_size", 128), member_store=self.members,
                                     usage_store=self.usage_store)
        inbox_options = dict(self.config.get("codex_inbox", {}))
        inbox_options["reply_chat_allowlist"] = list(self.config.get("chat_allowlist", []))
        if inbox_options.get("enabled", False):
            self.codex_inbox = CodexInbox(root, inbox_options)
            self.plugins.services["codex_inbox"] = self.codex_inbox
        else:
            self.codex_inbox = None
        if effective_name == "native_42_text":
            backend_type = DatabaseNative42TextBackend
        elif effective_name == "uia_text":
            backend_type = UIAutomationTextBackend
        else:
            backend_type = NativeTextBackend if generic else NativeFileHelperBackend
        self.backend_factory = backend_factory or (lambda: backend_type(root, self.config))
        self.backend = None
        self.stop_event = threading.Event()
        self.commands = queue.Queue()
        self.last_cleanup = None
        self._connection_ready = False
        self._last_connection_log = 0
        self._last_connection_error = ""

    def _set_connection_capabilities(self, capabilities):
        changed = self.plugins.set_capabilities(capabilities)
        if changed and not self.stop_event.is_set():
            # 原生后端恢复后重新加载发送插件；只读后端则自动停用它们。
            self.plugins.reload()
        return changed

    def _candidate_backend(self):
        if not self.auto_backend:
            return self.backend_factory(), None
        if self._custom_backend_factory:
            return self.backend_factory(), TEXT_CAPABILITIES
        # 先做只读版本预检。只有匹配已验证客户端才创建原生后端；否则直接
        # 使用 UIA 只读，不触碰输入框、鼠标或键盘。
        try:
            native = self.root / "native"
            if str(native) not in sys.path:
                sys.path.insert(0, str(native))
            from text_service import find_account_process, inspect_account
            from run_selftest import modules, verify_core
            pid = self.config.get("wechat_pid")
            if pid is None:
                pid = find_account_process(self.config.get("wechat_account"))
            core = modules(pid).get("weixin.dll")
            if not core:
                raise RuntimeError("微信核心模块尚未加载")
            verify_core(core[1])
            if self.config.get("wechat_account") and inspect_account(pid) != self.config.get("wechat_account"):
                raise RuntimeError("当前进程不是绑定的机器人账号")
            return NativeTextBackend(self.root, self.config), TEXT_CAPABILITIES
        except Exception as error:
            logger.info("原生后端预检未通过，进入只读降级：%s", error)
            try:
                database = DatabaseReadonlyTextBackend(self.root, self.config)
                # 预检只加载本地只读代码与依赖，不读取微信窗口、不注入进程。
                database.preflight()
                return database, UIA_READ_ONLY_CAPABILITIES
            except Exception as db_error:
                logger.info("数据库只读后端不可用，回退 UIA：%s", db_error)
                return UIAutomationTextBackend(self.root, self.config), UIA_READ_ONLY_CAPABILITIES

    @staticmethod
    def _backend_account(candidate):
        """取得连接实际使用的账号；UIA 没有账号读取能力时使用进程分区。"""
        raw = getattr(candidate, "raw", candidate)
        account = getattr(raw, "self_wxid", None)
        if account:
            return account
        account = getattr(raw, "preferred_account", None)
        if account:
            return account
        if isinstance(candidate, (UIAutomationTextBackend, DatabaseReadonlyTextBackend)) and candidate.pid:
            return f"uia_{candidate.pid}"
        return None

    def _bind_runtime_member_store(self, candidate):
        """在连接成功后绑定当前账号，并让插件拿到新的上下文。"""
        if not self.config.get("members_config_file"):
            return False
        account = self._backend_account(candidate) or self.config.get("wechat_account")
        if not account:
            raise RuntimeError("已连接微信，但尚未识别当前登录账号")
        if self.members is not None and self.members.account == account:
            return False
        replacement = MemberStore.from_config(self.config, self.store.path.parent, account=account)
        if replacement is None:
            raise RuntimeError("成员目录配置无法绑定当前微信账号")
        replacement.initialize()
        previous = self.members
        self.members = replacement
        self.plugins.set_member_store(replacement)
        if previous is not None:
            previous.close()
        if previous is not None:
            logger.info("成员历史已切换到当前账号：%s", account)
        return True

    def _prepare_message_identity(self, message):
        # UIA 只读模式没有真实 self_wxid；用本次动态账号作为历史分区的
        # 连接身份，避免消息被 MemberStore 当成其他账号而丢弃。
        if self.members is not None and isinstance(message.data, dict):
            if not message.data.get("self_wxid"):
                message.data["self_wxid"] = self.members.account
            if not message.data.get("native_receiver"):
                message.data["native_receiver"] = self.members.account
        return message

    @property
    def connected(self):
        """当前接收后端是否已成功启动。"""
        return bool(self._connection_ready and self.backend is not None)

    @property
    def send_enabled(self):
        return "text.send" in self.plugins.capabilities

    @property
    def pid(self):
        return self.backend.pid if self.backend is not None else None

    @property
    def last_connection_error(self):
        return self._last_connection_error

    def _log_connection_error(self, error):
        text = f"{type(error).__name__}: {error}"
        now = time.monotonic()
        if text != self._last_connection_error or now - self._last_connection_log >= 30:
            logger.warning("等待微信连接：%s", text)
            self._last_connection_error, self._last_connection_log = text, now

    def _connect(self):
        candidate = None
        pending = []
        ready = False
        pending_lock = threading.Lock()

        def emit(message):
            nonlocal ready
            with pending_lock:
                if not ready:
                    pending.append(message)
                    return
            if ready:
                self._emit(message)
        try:
            candidate, capabilities = self._candidate_backend()
            self.backend = candidate
            self.service.bind(candidate)
            # 启动期间原生桥接/UIA 可能同步交付可见历史；账号与插件上下文
            # 完成前先缓冲，避免历史落入旧账号或因插件尚未加载而丢失。
            candidate.start(emit)
            member_store_changed = self._bind_runtime_member_store(candidate)
            capabilities_changed = False
            if capabilities is not None:
                capabilities_changed = self._set_connection_capabilities(capabilities)
            if member_store_changed and not capabilities_changed and not self.stop_event.is_set():
                # 登录账号切换但后端能力未变时，插件仍必须重建以拿到新分区。
                self.plugins.reload()
            # 排空启动期间的消息；在排空前保持 ready=False，避免并发轮询
            # 把新消息插到旧的可见历史之前。
            while True:
                with pending_lock:
                    queued = tuple(pending)
                    pending.clear()
                    if not queued:
                        ready = True
                        break
                for message in queued:
                    self._emit(message)
            self._connection_ready = True
            self._last_connection_error = ""
            logger.info("微信已连接，PID=%s；收发范围：%s", candidate.pid,
                        getattr(candidate, "scope", "文本"))
        except Exception as error:
            pending.clear()
            self._connection_ready = False
            self._log_connection_error(error)
            self._disconnect()

    def _emit(self, message):
        message = self._prepare_message_identity(message)
        allowed = self.config.get("chat_allowlist", [])
        if not allowed or message.chat_id in allowed:
            if self.members is not None:
                try:
                    self.members.record(message)
                except Exception:
                    logger.exception("群文本保存失败，消息继续分发到插件")
            if message.data.get("dispatch_to_plugins", True):
                if self.plugins.is_active("personality"):
                    message = prepare_switch(message, self.members)
                self.plugins.dispatch(message)

    def _disconnect(self):
        self._connection_ready = False
        try:
            if self.auto_backend and not self.stop_event.is_set():
                self._set_connection_capabilities(UIA_READ_ONLY_CAPABILITIES)
            self.last_cleanup = self.service.close()
            self.backend = None
            return True
        except Exception as error:
            # 清理忙时保留实例，只重试清理，不覆盖 DLL、不再次注入。
            self._log_connection_error(error)
            return False

    def execute(self, command):
        words = shlex.split(command)
        if not words:
            return
        action = words[0].lower()
        if action in ("quit", "exit", "stop") and len(words) == 1:
            self.stop_event.set()
        elif action == "help" and len(words) == 1:
            logger.info("命令：status / chats / members [群ID] / history 称呼 [条数] / plugins / enable 名称 / disable 名称 / reload [名称] / quit")
        elif action == "status" and len(words) == 1:
            logger.info("状态：%s", json.dumps({"connected": self._connection_ready,
                                               "pid": self.backend.pid if self.backend else None,
                                               "native": self.backend.diagnostics() if self.backend and hasattr(self.backend, "diagnostics") else {},
                                               "plugins": self.plugins.status()}, ensure_ascii=False))
        elif action == "chats" and len(words) == 1:
            rows = self.backend.chats() if self.backend and hasattr(self.backend, "chats") else []
            logger.info("本次连接已接收的会话：%s", json.dumps(rows, ensure_ascii=False))
        elif action == "members" and len(words) in (1, 2):
            if self.members is None:
                raise ValueError("尚未配置成员目录")
            rows = [{"name": m.name, "display_name": m.display_name, "wxid": m.wxid,
                     "room": m.room, "bound": bool(m.wxid)} for m in self.members.list_members(words[1] if len(words) == 2 else None)]
            logger.info("成员目录：%s", json.dumps(rows, ensure_ascii=False))
        elif action == "history" and len(words) in (2, 3):
            if self.members is None:
                raise ValueError("尚未配置成员目录")
            rows = self.members.history(words[1], limit=int(words[2]) if len(words) == 3 else 10)
            logger.info("成员聊天记录：%s", json.dumps(rows, ensure_ascii=False))
        elif action == "plugins" and len(words) == 1:
            for row in self.plugins.status():
                state = "运行" if row["active"] else "停用"
                logger.info("%s [%s] 已处理=%s 异常=%s 丢弃=%s %s", row["name"], state,
                            row["handled"], row["errors"], row["dropped"], row["error"])
        elif action in ("enable", "disable") and len(words) == 2:
            getattr(self.plugins, action)(words[1])
        elif action == "reload" and len(words) in (1, 2):
            self.plugins.reload(words[1] if len(words) == 2 else None)
        else:
            raise ValueError("未知命令；输入 help 查看用法")

    def _read_console(self):
        # 无控制台的窗口程序（pythonw / 打包后的 GUI）没有 stdin，直接跳过。
        if sys.stdin is None:
            return
        try:
            for line in sys.stdin:
                if self.stop_event.is_set():
                    break
                self.commands.put(line.strip())
        except (OSError, ValueError, TypeError):
            pass

    def run(self, *, seconds=0):
        started = time.monotonic()
        next_connect = 0
        next_rollover = 0
        try:
            if self.codex_inbox is not None:
                self.codex_inbox.start()
            if self.members is not None:
                self.members.initialize()
            # 动态账号模式要等连接后的真实账号绑定成员目录，再加载依赖
            # ctx.members 的插件；固定账号和无成员目录仍按原流程启动。
            if not self._dynamic_member_store:
                self.plugins.reload()
            if self.config.get("console", True):
                threading.Thread(target=self._read_console, name="BotConsole", daemon=True).start()
            logger.info("框架已启动；输入 help 查看插件管理命令，Ctrl+C 退出")
            while not self.stop_event.is_set():
                now = time.monotonic()
                if self.members is not None and now >= next_rollover:
                    self.members.rollover()
                    next_rollover = now + 0.5
                if seconds and now - started >= seconds:
                    break
                while True:
                    try:
                        command = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        self.execute(command)
                    except Exception:
                        logger.exception("控制台命令执行失败")
                if self.stop_event.is_set():
                    break
                if self.codex_inbox is not None and self.service.connected:
                    result = self.codex_inbox.dispatch_one(self.service.send_text)
                    if result.get("status") == "failed":
                        logger.error("Codex 结果回发未完成：reply_id=%s error=%s",
                                     result.get("reply_id"), result.get("error_code"))
                if self._connection_ready and self.backend.last_error:
                    self._log_connection_error(self.backend.last_error)
                    self._disconnect()
                    next_connect = now + self.config.get("reconnect_seconds", 5)
                elif not self._connection_ready and now >= next_connect:
                    if self.backend is None or self._disconnect():
                        self._connect()
                    next_connect = time.monotonic() + self.config.get("reconnect_seconds", 5)
                self.stop_event.wait(0.1)
        except KeyboardInterrupt:
            logger.info("收到退出指令")
        finally:
            self.stop()

    def stop(self):
        self.stop_event.set()
        self.service.cancel_pending()
        self.plugins.shutdown()
        if self.codex_inbox is not None:
            self.codex_inbox.stop()
        cleaned = self._disconnect()
        if self.members is not None:
            self.members.close()
        if self.usage_store is not None:
            self.usage_store.close()
        if not cleaned:
            raise RuntimeError("原生连接未能清理；保留 DLL，请查看日志后再启动")
        logger.info("框架已停止；连接清理：%s", self.last_cleanup)
