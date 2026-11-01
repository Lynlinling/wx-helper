"""原生收发后端；框架和插件只依赖公开接口。"""
from __future__ import annotations

import sys
import re
import uuid
import json
import importlib
import threading
import time
from xml.etree import ElementTree
from pathlib import Path

from .sdk import CapabilityError, Message

CAPABILITIES = frozenset({"text.receive", "text.send"})
TEXT_CAPABILITIES = CAPABILITIES | frozenset({"friend.text.receive", "friend.text.send",
                                           "group.text.receive", "group.text.send", "group.text.mention"})
UIA_READ_ONLY_CAPABILITIES = frozenset({"text.receive", "group.text.receive"})
NATIVE_42_CAPABILITIES = frozenset({"text.receive", "text.send", "group.text.receive",
                                    "group.text.send", "group.text.mention"})


class UIAutomationTextBackend:
    """通过微信桌面版公开的 Windows UI Automation 控件只读收取文本。

    这是给未被原生桥接验证的微信版本使用的降级后端。它不读取进程内存、
    不注入 DLL，只观察消息列表，不操作输入框或鼠标。微信必须保持登录，
    且目标群在会话列表中可见；当前版本 4.2.0.12 已验证这些控件存在。
    """

    scope = "Windows UI Automation：当前窗口群聊只读监听"
    supports_native_mentions = False

    def __init__(self, root, config):
        self.config = dict(config)
        self.session_id = uuid.uuid4().hex
        self.pid = config.get("wechat_pid")
        self.raw = self
        self._emit = None
        self._stop = __import__("threading").Event()
        self._thread = None
        self._lock = __import__("threading").RLock()
        self._seen = set()
        self._sequence = 0
        self._sent = []
        self._last_error = None
        self._window = None
        self._message_list = None
        self._input = None
        self._send_button = None
        self.chat_name = str(config.get("uia_chat_name") or "").strip()
        allowed = config.get("chat_allowlist") or []
        chat_ids = [x for x in allowed if isinstance(x, str) and x.endswith("@chatroom")]
        if not chat_ids:
            member_config = config.get("members_config_file", "members_config.json")
            try:
                member_data = json.loads((Path(root) / member_config).read_text(encoding="utf-8-sig"))
                chat_ids = [x for x in member_data.get("groups", {})
                            if isinstance(x, str) and x.endswith("@chatroom")]
            except (OSError, ValueError, TypeError, AttributeError):
                chat_ids = []
        if len(chat_ids) != 1:
            raise RuntimeError("UIA 只读后端需要在会话白名单或成员配置中指定唯一目标群")
        self.chat_id = chat_ids[0]
        if not self.chat_name:
            raise RuntimeError("UIA 只读后端需要在配置中填写当前已打开的目标群名称")
        self._import_history = bool(config.get("uia_import_visible_history", True))
        self._poll_seconds = max(0.1, float(config.get("uia_poll_seconds", 0.5)))
        aliases = config.get("uia_mention_aliases", ["鲸鱼娘", "大肥鱼", "机器人"])
        self.mention_aliases = tuple(x.strip() for x in aliases
                                     if isinstance(x, str) and x.strip()) if isinstance(aliases, list) else ()

    @property
    def last_error(self):
        return self._last_error

    def _find_window(self):
        from pywinauto import Desktop
        desktop = Desktop(backend="uia")
        candidates = []
        if self.pid:
            candidates = desktop.windows(process=self.pid, class_name="mmui::MainWindow")
            if not candidates:
                # WeChat 4.2 keeps the top-level shell as Qt51514QWindowIcon
                # until its accessibility tree is materialized.  Resolving
                # the shell is read-only; we never activate the accessibility
                # gate or alter the process.
                candidates = desktop.windows(process=self.pid, class_name="Qt51514QWindowIcon")
            if not candidates:
                candidates = [window for window in desktop.windows(process=self.pid)
                              if window.is_visible()]
        if not candidates and self.pid:
            raise RuntimeError(f"找不到指定的微信进程：PID {self.pid}")
        if not candidates:
            candidates = desktop.windows(class_name="mmui::MainWindow")
        if not candidates:
            candidates = desktop.windows(class_name="Qt51514QWindowIcon")
        if not candidates:
            raise RuntimeError("未找到已打开的微信 4.x 窗口")
        if self.pid:
            window = candidates[0]
        else:
            # 多开时优先选出目前可见且标题为微信的主窗体；只有一个时直接使用。
            window = next((item for item in candidates if item.is_visible()), candidates[0])
        self.pid = window.element_info.process_id
        # Desktop.windows() 返回已经解析的 wrapper；重新构造 WindowSpecification，
        # 这样后续 child_window() 可以在每次轮询时重新解析 Qt 控件。
        return desktop.window(handle=window.handle)

    def _bind_controls(self):
        self._window = self._find_window()
        # 连接重启后仍使用同一个会话标识，配合稳定的 UIA runtime id，
        # 让已导入的可见历史由数据库唯一索引去重。
        self.session_id = f"uia:{self.pid}:{self.chat_id}"
        # 不自动切换群聊，避免抢焦点、打断用户当前操作。
        title = self._window.child_window(auto_id="content_view.top_content_view.title_h_view.left_v_view.left_content_v_view.left_ui_.big_title_line_h_view.current_chat_name_label")
        current_name = title.element_info.name if title.exists() else ""
        if current_name != self.chat_name:
            raise RuntimeError(f"目标群“{self.chat_name}”当前未打开；为避免打断操作，UIA 不会自动切换会话")
        self._message_list = self._window.child_window(auto_id="chat_message_list")
        self._input = self._window.child_window(auto_id="chat_input_field")
        self._send_button = self._window.child_window(title="发送", control_type="Button")
        if not self._message_list.exists() or not self._input.exists() or not self._send_button.exists():
            raise RuntimeError("微信目标群缺少消息列表、输入框或发送按钮")

    @staticmethod
    def _item_key(item):
        try:
            return tuple(item.element_info.runtime_id)
        except Exception:
            return (id(item),)

    @staticmethod
    def _item_text(item):
        try:
            value = item.element_info.rich_text or item.element_info.name or ""
        except Exception:
            value = ""
        return str(value).replace("\r", "").strip()

    def _items(self):
        title = None
        if self._window is not None:
            title = self._window.child_window(auto_id="content_view.top_content_view.title_h_view.left_v_view.left_content_v_view.left_ui_.big_title_line_h_view.current_chat_name_label")
        current_name = title.element_info.name if title is not None and title.exists() else ""
        if (self._message_list is None or not self._message_list.exists() or
                current_name != self.chat_name):
            self._bind_controls()
        return list(self._message_list.children())

    def _consume_sent(self, text):
        now = __import__("time").monotonic()
        self._sent[:] = [(value, stamp) for value, stamp in self._sent if now - stamp < 30]
        for index, (value, _stamp) in enumerate(self._sent):
            if value == text:
                self._sent.pop(index)
                return True
        return False

    @staticmethod
    def _matched_mention_alias(text, aliases):
        """UIA 只给可见文本；必须匹配已配置机器人昵称，不能把任意 @ 当作 @机器人。"""
        for alias in aliases:
            if re.search(r"@" + re.escape(alias) + r"(?=[\s\u2005\u2006\u2009,，:：]|$)", text,
                         flags=re.IGNORECASE):
                return alias
        return None

    @staticmethod
    def _mentions_bot(text, aliases):
        return UIAutomationTextBackend._matched_mention_alias(text, aliases) is not None

    def _message_from_item(self, item, text, *, bootstrap=False):
        key = self._item_key(item)
        try:
            sequence = abs(int(key[-1])) or (self._sequence + 1)
        except (TypeError, ValueError, IndexError):
            self._sequence += 1
            sequence = self._sequence
        is_self = self._consume_sent(text)
        # UIA 不暴露气泡对应的微信 ID；先用稳定的本地未知成员标识保存历史。
        sender = self.config.get("uia_unknown_sender", "wxid_uia_unknown")
        if is_self:
            sender = self.config.get("wechat_account") or "uia:self"
        mention_alias = self._matched_mention_alias(text, self.mention_aliases)
        mention = mention_alias is not None
        data = {
            "msg": text,
            "content": text,
            "from_wxid": sender,
            "room_wxid": self.chat_id,
            "chat_id": self.chat_id,
            "sender_id": sender,
            "native_sender": sender,
            "native_receiver": self.config.get("wechat_account", ""),
            "is_self": is_self,
            "self_wxid": self.config.get("wechat_account", ""),
            "is_at_self": mention and not is_self,
            "at_members": (self.config.get("wechat_account"),) if mention and not is_self else (),
            "bot_mention_alias": mention_alias or "",
            "dispatch_to_plugins": not bootstrap and not is_self,
            "bot_generated": is_self,
            "reply_capable": bool(getattr(self, "native_send_enabled", False)),
            "uia_bootstrap": bootstrap,
            "uia_runtime_id": self._item_key(item),
        }
        return Message(text=text, chat_id=self.chat_id, sender_id=sender,
                       receiver_id=self.config.get("wechat_account", ""),
                       is_group=True, is_self=is_self, sequence=sequence,
                       session_id=self.session_id, data=data)

    def _scan(self, *, bootstrap=False, emit=True):
        current = []
        for item in self._items():
            # RecyclerListView 也暴露时间分隔条、系统提示等 ListItem；
            # 只把文本气泡交给历史层，避免把“21:35”之类时间写成聊天消息。
            try:
                if item.element_info.class_name != "mmui::ChatTextItemView":
                    continue
            except Exception:
                continue
            text = self._item_text(item)
            if not text:
                continue
            key = self._item_key(item)
            current.append(key)
            if key in self._seen:
                continue
            self._seen.add(key)
            if emit and self._emit is not None:
                self._emit(self._message_from_item(item, text, bootstrap=bootstrap))
        # Qt recycles rows after scrolling; bounding this set avoids an unbounded process.
        if len(self._seen) > 2000:
            self._seen.intersection_update(current)

    def _run(self):
        import time
        while not self._stop.is_set():
            try:
                self._scan()
                self._last_error = None
            except Exception as error:
                self._last_error = str(error)
            self._stop.wait(self._poll_seconds)

    def start(self, emit):
        if self._thread is not None and self._thread.is_alive():
            return True
        self._emit = emit
        self._stop.clear()
        with self._lock:
            self._bind_controls()
            # 导入当前 UIA 已加载的可见历史，写入成员数据库但不触发模型回复。
            self._scan(bootstrap=self._import_history, emit=self._import_history)
        import threading
        self._thread = threading.Thread(target=self._run, name="WeChatUIAutomation", daemon=True)
        self._thread.start()
        return True

    def send_text(self, chat_id, text):
        # UIA ValuePattern 会留下草稿，而 Qt 按钮 Invoke 没有可靠发送回执。
        # 此后端严格只读，避免再次改动用户输入框或抢占交互。
        raise CapabilityError("UI Automation 后端当前只读；拒绝改动微信输入框或发送消息")

    def send_at_text(self, room_wxid, content, at_list):
        # Qt UIA 没有公开微信原生 mention API；群内只发正文，不伪造 @ 提醒。
        return self.send_text(room_wxid, content)

    def diagnostics(self):
        return {"backend": "uia_text", "pid": self.pid, "chat": self.chat_id,
                "chat_name": self.chat_name, "seen": len(self._seen),
                "last_error": self._last_error}

    def chats(self):
        return [{"chat_id": self.chat_id, "name": self.chat_name, "backend": "uia_text"}]

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._thread = None
        self._emit = None
        self._window = self._message_list = self._input = self._send_button = None
        self._seen.clear()
        return {"backend": "uia_text", "stopped": True}


class DatabaseReadonlyTextBackend:
    """通过微信 4.x 本地加密数据库只读监听群文本。

    该后端只读取已登录进程中的数据库密钥和本地消息库，不打开窗口、不
    激活会话、不操作输入框。它是 ``auto`` 在新版本微信上的首选接收降级，
    发送仍明确拒绝，避免把“收到消息”误报成“可以回复”。实现复用同一
    工作区的 ``wechatauto-replica`` 只读 DB 模块；模块缺失时由 runtime
    继续降级到 UIA。
    """

    scope = "微信本地数据库：群聊文本只读监听"
    supports_native_mentions = False

    def __init__(self, root, config):
        self.root = Path(root)
        self.config = dict(config)
        self.session_id = uuid.uuid4().hex
        self.pid = config.get("wechat_pid")
        self.raw = self
        self._emit = None
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.RLock()
        self._last_error = None
        self._db = None
        self._db_class = None
        self._account = None
        self._self_name = ""
        self._watermarks = {}
        self._sequence = 0
        self._started_at = 0.0
        self.chat_ids = tuple(dict.fromkeys(
            x for x in (config.get("chat_allowlist") or [])
            if isinstance(x, str) and x.endswith("@chatroom")))
        if not self.chat_ids:
            configured = config.get("db_chat_id")
            if isinstance(configured, str) and configured.endswith("@chatroom"):
                self.chat_ids = (configured,)
        if not self.chat_ids and config.get("members_config_file"):
            member_path = Path(config["members_config_file"])
            if not member_path.is_absolute():
                member_path = self.root / member_path
            try:
                member_options = json.loads(member_path.read_text(encoding="utf-8-sig"))
                groups = member_options.get("groups", {}) if isinstance(member_options, dict) else {}
                self.chat_ids = tuple(x for x in groups if isinstance(x, str) and x.endswith("@chatroom"))
            except (OSError, ValueError, TypeError):
                self.chat_ids = ()
        if not self.chat_ids:
            raise RuntimeError("数据库只读后端需要 chat_allowlist 或 db_chat_id 中的群 ID")
        aliases = config.get("uia_mention_aliases", [])
        aliases = aliases if isinstance(aliases, list) else []
        self.mention_aliases = tuple(x.strip() for x in aliases
                                     if isinstance(x, str) and x.strip())
        self._poll_seconds = max(0.5, float(config.get("db_poll_seconds", 1.0)))
        self._bootstrap_limit = max(0, min(2000, int(config.get("db_bootstrap_limit", 200))))

    @property
    def self_wxid(self):
        return self._account or ""

    @property
    def last_error(self):
        return self._last_error

    def _load_db_class(self):
        package_dir = self.config.get("db_backend_root")
        if package_dir:
            package_dir = Path(package_dir)
        else:
            package_dir = self.root.parent / "wechatauto-replica"
        package_dir = package_dir / "wechatauto"
        if not package_dir.is_dir():
            raise RuntimeError(f"未找到只读数据库模块：{package_dir}")
        # 只读模块是一个独立源码包，但其 __init__ 会导入可选的 UI 依赖。
        # 用最小包壳加载 db.py，避免为了接收消息初始化 GUI/剪贴板模块。
        package = sys.modules.get("wechatauto")
        created = package is None
        if created:
            import types
            package = types.ModuleType("wechatauto")
            package.__path__ = [str(package_dir)]
            sys.modules["wechatauto"] = package
        else:
            paths = list(getattr(package, "__path__", []))
            if str(package_dir) not in paths:
                paths.append(str(package_dir))
                package.__path__ = paths
        module_name = "_wxhelper_wechatauto_db"
        try:
            if module_name in sys.modules:
                return sys.modules[module_name].WeChatDB
            module = importlib.import_module("wechatauto.db")
            # Keep the module under its stable namespace so its relative imports
            # work without executing wechatauto.__init__ and GUI dependencies.
            sys.modules[module_name] = module
            return module.WeChatDB
        except Exception:
            if created:
                for name in list(sys.modules):
                    if name == "wechatauto" or name.startswith("wechatauto."):
                        sys.modules.pop(name, None)
            raise

    def preflight(self):
        """验证本地只读依赖；失败时 runtime 可继续选 UIA 后端。"""
        self._db_class = self._load_db_class()
        return True

    @staticmethod
    def _row_sequence(row):
        try:
            return int(row.get("sort_seq") or 0)
        except (TypeError, ValueError):
            return 0

    def _aliases(self):
        values = list(self.mention_aliases)
        if self._self_name:
            values.append(self._self_name)
        return tuple(dict.fromkeys(x for x in values if x))

    @staticmethod
    def _chat_text_from_row(row):
        row_type = row.get("type")
        content = str(row.get("content") or "").replace("\r", "").strip()
        if row_type in (None, 1, "文本", "text"):
            return content or None
        # WeChat quote replies are appmsg cards. Use only the top-level title
        # (the sender's current text) and never the nested refermsg content;
        # otherwise an @ label from the quoted message can trigger a false reply.
        if row_type not in (49, "文件/链接/卡片") or not content or len(content) > 32768:
            return None
        upper = content.upper()
        if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
            return None
        try:
            root = ElementTree.fromstring(content)
        except (ElementTree.ParseError, ValueError):
            return None
        appmsg = root if root.tag == "appmsg" else root.find(".//appmsg")
        if appmsg is None or appmsg.find("refermsg") is None:
            return None
        current = appmsg.findtext("title") or ""
        current = " ".join(current.split())
        return current[:4000] or None

    def _message_from_row(self, chat_id, row, *, bootstrap=False):
        content = self._chat_text_from_row(row)
        if not content:
            return None
        if not content:
            return None
        sender = str(row.get("sender_username") or "").strip()
        body = content
        if chat_id.endswith("@chatroom"):
            author, separator, parsed = content.partition(":\n")
            if separator and author and "@" not in author:
                sender, body = author.strip(), parsed.strip()
        if not sender:
            return None
        sequence = self._row_sequence(row)
        if sequence <= 0:
            self._sequence += 1
            sequence = self._sequence
        is_self = sender == self._account
        mention_alias = None
        for alias in self._aliases():
            if re.search(r"@" + re.escape(alias) + r"(?=[\s\u2005\u2006\u2009,，:：]|$)",
                         body, flags=re.IGNORECASE):
                mention_alias = alias
                break
        data = {
            "msg": body,
            "content": body,
            "from_wxid": sender,
            "room_wxid": chat_id,
            "chat_id": chat_id,
            "sender_id": sender,
            "native_sender": sender,
            "native_receiver": self._account or "",
            "is_self": is_self,
            "self_wxid": self._account or "",
            "is_at_self": bool(mention_alias and not is_self),
            "at_members": (self._account,) if mention_alias and not is_self else (),
            "bot_mention_alias": mention_alias or "",
            "dispatch_to_plugins": bool(not bootstrap and not is_self),
            "bot_generated": False,
            "reply_capable": bool(getattr(self, "native_send_enabled", False) and not bootstrap),
            "db_bootstrap": bootstrap,
            "db_local_id": row.get("local_id"),
        }
        return Message(text=body, chat_id=chat_id, sender_id=sender,
                       receiver_id=self._account or "", is_group=True, is_self=is_self,
                       sequence=sequence, session_id=self.session_id + ":" + chat_id,
                       data=data)

    def accepts_session_id(self, session_id):
        """Accept a chat-scoped message only from this connection generation."""
        if not isinstance(session_id, str):
            return False
        prefix = self.session_id + ":"
        return session_id.startswith(prefix) and session_id[len(prefix):] in self.chat_ids

    def _bootstrap(self):
        for chat_id in self.chat_ids:
            # Capture a sequence boundary before loading context. SQLite's
            # create_time is only second-precision, so timestamp comparison can
            # either drop a message sent during startup or replay one on restart.
            latest = self._db.get_messages(chat_id, limit=1)
            baseline = max((self._row_sequence(row) for row in latest), default=0)
            self._watermarks[chat_id] = baseline
            rows = self._db.get_messages(chat_id, limit=self._bootstrap_limit)
            rows = list(reversed(rows))
            for row in rows:
                sequence = self._row_sequence(row)
                # Rows newer than the captured boundary are intentionally left
                # to the incremental query below, which drains them in order.
                if sequence > baseline:
                    continue
                message = self._message_from_row(chat_id, row, bootstrap=True)
                if message is not None and self._emit is not None:
                    self._emit(message)
        # Drain messages that arrived after the baseline while history loaded.
        # The normal poll loop continues from the advanced watermark afterward.
        self._poll_once()

    def _poll_once(self):
        for chat_id in self.chat_ids:
            watermark = self._watermarks.get(chat_id, 0)
            rows = self._db.get_new_messages(chat_id, since_seq=watermark, limit=200)
            for row in rows:
                sequence = self._row_sequence(row)
                self._watermarks[chat_id] = max(self._watermarks.get(chat_id, 0), sequence)
                message = self._message_from_row(chat_id, row)
                if message is not None and self._emit is not None:
                    self._emit(message)

    def _run(self):
        while not self._stop.is_set():
            try:
                self._poll_once()
                self._last_error = None
            except Exception as error:
                self._last_error = str(error)
            self._stop.wait(self._poll_seconds)

    def start(self, emit):
        if self._thread is not None and self._thread.is_alive():
            return True
        self._emit = emit
        self._stop.clear()
        db_class = self._db_class or self._load_db_class()
        requested_account = self.config.get("wechat_account") or None
        if requested_account:
            # WeChatDB uses the on-disk account-directory name, which may have a
            # four-character suffix beyond the wxid stored in bot_config.json.
            # Resolve by normalized identity and refuse ambiguous matches.
            module = sys.modules.get("wechatauto.db")
            db_dir = module.auto_detect_db_dir() if module else None
            matches = []
            if db_dir and module:
                for path in module._find_account_dirs(db_dir):
                    directory_account = Path(path).name
                    normalized = re.sub(r"_\w{4}$", "", directory_account)
                    if directory_account == requested_account or normalized == requested_account:
                        matches.append(directory_account)
            if len(matches) != 1:
                raise RuntimeError("固定 wechat_account 未唯一匹配到本地微信数据库目录")
            self._db = db_class(db_dir=db_dir, account=matches[0])
        else:
            self._db = db_class()
        self._account = self._db.get_self_info().get("username") or self._db.wxid
        if requested_account and self._account != requested_account:
            raise RuntimeError("数据库当前账号与配置 wechat_account 不一致")
        self._self_name = str(self._db.get_self_info().get("nick_name") or "").strip()
        # A reconnect must produce a fresh token so replies from a previous
        # connection can never be sent through the new native sender.
        self.session_id = f"db:{self._account}:{uuid.uuid4().hex}"
        expected_group_name = getattr(self, "expected_group_name", None)
        if expected_group_name:
            if len(self.chat_ids) != 1 or self._db.group_id_to_name(self.chat_ids[0]) != expected_group_name:
                raise RuntimeError(f"绑定群与预期群名不一致：{expected_group_name}")
        self._started_at = time.time()
        self._bootstrap()
        self._thread = threading.Thread(target=self._run, name="WeChatDatabaseReadonly", daemon=True)
        self._thread.start()
        return True

    def send_text(self, chat_id, text):
        raise CapabilityError("数据库只读后端不发送消息；避免操作微信窗口或留下草稿")

    def send_at_text(self, room_wxid, content, at_list):
        return self.send_text(room_wxid, content)

    def diagnostics(self):
        return {"backend": "db_readonly", "pid": self.pid, "account": self._account,
                "chats": list(self.chat_ids), "watermarks": dict(self._watermarks),
                "last_error": self._last_error}

    def chats(self):
        return [{"chat_id": chat, "backend": "db_readonly"} for chat in self.chat_ids]

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._thread = None
        self._emit = None
        self._db = None
        return {"backend": "db_readonly", "stopped": True}


class DatabaseNative42TextBackend(DatabaseReadonlyTextBackend):
    """4.2 专用混合后端：只读数据库接收，4.2 原生调度器发送。"""

    scope = "微信数据库只读接收 + 4.2 原生文本发送（不操作窗口）"
    supports_native_mentions = False
    native_send_enabled = True

    def __init__(self, root, config):
        super().__init__(root, config)
        allowed = config.get("chat_allowlist")
        if (not isinstance(allowed, list) or len(allowed) != 1 or
                not isinstance(allowed[0], str) or not allowed[0].endswith("@chatroom")):
            raise RuntimeError("4.2 专用后端要求 chat_allowlist 只包含一个群")
        self.allowed_group = allowed[0]
        if self.chat_ids != (self.allowed_group,):
            raise RuntimeError("数据库接收范围必须与唯一群白名单一致")
        if not self.config.get("wechat_account"):
            raise RuntimeError("4.2 专用后端必须固定绑定机器人账号")
        binding_path = self.root / ".runtime" / "target_group.private.json"
        try:
            binding = json.loads(binding_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            raise RuntimeError("缺少有效的私有群绑定文件") from error
        if binding.get("group_id") != self.allowed_group:
            raise RuntimeError("私有群绑定与唯一群白名单不一致")
        self._sender_module = None
        self._send_lock = threading.Lock()
        self._last_send = None
        self._last_target_check = 0.0
        self._target_check_seconds = max(
            2.0, float(config.get("wechat_target_check_seconds", 5.0))
        )

    def _load_sender(self):
        for directory in (self.root / "native", self.root / "tools"):
            value = str(directory.resolve())
            if value not in sys.path:
                sys.path.insert(0, value)
        module = sys.modules.get("run_text_sender_42")
        expected = (self.root / "tools" / "run_text_sender_42.py").resolve()
        if module is None:
            module = importlib.import_module("run_text_sender_42")
        if Path(module.__file__).resolve() != expected:
            raise RuntimeError("加载到的 4.2 发送器路径不符合当前工作区")
        self._sender_module = module
        return module

    def start(self, emit):
        sender = self._load_sender()
        pid, account = sender.select_target()
        if account != self.config.get("wechat_account"):
            raise RuntimeError("当前已登录账号不是绑定的专用机器人号")
        loaded = sender.modules(pid)
        core = loaded.get("weixin.dll")
        if not core:
            raise RuntimeError("微信核心模块尚未加载")
        sender.verify_wechat_42_core(core[1])
        self.pid = pid
        return super().start(emit)

    def _check_live_target(self):
        """周期性确认微信重启/切号后仍是同一个可发送进程。

        数据库文件可能在微信重启后短时间内仍可读；只靠数据库轮询会把
        已失效连接误报为在线。这里重新按账号发现进程；仅当 PID 变化时
        校验新进程的核心指纹并让 runtime 走统一断开/重连路径。
        """
        now = time.monotonic()
        if now - self._last_target_check < self._target_check_seconds:
            return
        sender = self._sender_module or self._load_sender()
        pid, account = sender.select_target()
        if account != self.config.get("wechat_account"):
            raise RuntimeError("当前已登录账号不是绑定的专用机器人号")
        if pid != self.pid:
            loaded = sender.modules(pid)
            core = loaded.get("weixin.dll")
            if not core:
                raise RuntimeError("微信核心模块尚未加载")
            sender.verify_wechat_42_core(core[1])
            raise RuntimeError(f"微信进程已更换（PID {self.pid} -> {pid}），准备重连")
        self._last_target_check = now

    def send_text(self, chat_id, text):
        if chat_id != self.allowed_group:
            raise CapabilityError("4.2 专用发送器只允许向唯一绑定的白名单群发送")
        if not isinstance(text, str) or not text or len(text.encode("utf-8")) > 1023:
            raise ValueError("文本必须为 1–1023 个 UTF-8 字节")
        if self._db is None or self._thread is None or not self._thread.is_alive():
            raise RuntimeError("只读接收会话尚未就绪，拒绝发送")
        with self._send_lock:
            sender = self._sender_module or self._load_sender()
            result = sender.send(chat_id, self.allowed_group, text)
            self.pid = result["pid"]
            self._last_send = {"status": result["status"], "result": result["result"],
                               "reused_loaded_dll": result.get("reused_loaded_dll", False)}
            return result

    def _poll_once(self):
        self._check_live_target()
        return super()._poll_once()

    def send_at_text(self, room_wxid, content, at_list):
        # 当前 4.2 文本构造器只构造正文；不伪造微信原生 @ 通知。
        return self.send_text(room_wxid, content)

    def diagnostics(self):
        result = super().diagnostics()
        result.update({"backend": "native_42_text", "send_scope": "configured_single_group",
                       "last_send": self._last_send, "native_mentions": False})
        return result

    def chats(self):
        return [{"chat_id": self.allowed_group, "name": "已配置白名单群",
                 "backend": "native_42_text"}]


class NativeFileHelperBackend:
    scope = "文件传输助手文本"
    def __init__(self, root, config):
        native = Path(root) / "native"
        if not native.is_dir():
            raise RuntimeError("未找到已验证的原生适配目录")
        # 后端模块按需导入；查看插件和检查配置不需要打开微信。
        if str(native) not in sys.path:
            sys.path.insert(0, str(native))
        from filehelper_service import NativeFileHelperService
        self.raw = NativeFileHelperService(pid=config.get("wechat_pid"), preserve_reports=False)
        self.session_id = uuid.uuid4().hex

    def start(self, emit):
        self.raw.register_msg_handler(lambda _client, protocol, data, _service:
                                      emit(Message.from_native(protocol, data, self.session_id)))
        return self.raw.start()

    @property
    def last_error(self):
        return self.raw.last_error

    @property
    def pid(self):
        return self.raw.pid

    def send_text(self, chat_id, text):
        return self.raw.send_text(chat_id, text)

    def stop(self):
        return self.raw.stop()


class NativeTextBackend(NativeFileHelperBackend):
    scope = "文件传输助手、好友和群聊文本"

    def __init__(self, root, config):
        native = Path(root) / "native"
        if not native.is_dir():
            raise RuntimeError("未找到原生适配目录")
        if str(native) not in sys.path:
            sys.path.insert(0, str(native))
        from text_service import NativeTextService
        self.raw = NativeTextService(pid=config.get("wechat_pid"), account=config.get("wechat_account"),
                                     preserve_reports=False)
        self.session_id = uuid.uuid4().hex

    def diagnostics(self):
        router = self.raw.router
        return {"account_ready": bool(self.raw.self_wxid),
                "account_source": "current_process",
                "self_wxid": self.raw.self_wxid,
                "bound_account": self.raw.preferred_account,
                "unidentified_messages": router.unidentified,
                "unparsed_groups": router.unparsed_groups,
                "ignored_self_messages": router.ignored_self,
                "mention_messages": self.raw.mention_messages}

    def chats(self):
        return list(self.raw.chats.values())

    def send_at_text(self, room_wxid, content, at_list):
        return self.raw.send_at_text(room_wxid, content, at_list)
