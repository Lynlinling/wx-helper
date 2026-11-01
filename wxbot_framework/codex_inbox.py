"""本机 Bot -> Codex 的受限消息收件箱。

只绑定 loopback，不会主动唤醒 Codex。Bot 通过 HTTP POST 投递一条短消息，
Codex 在下一次活动时读取 pending；完成时可将结果放入受限回程队列，Bot
只回发到原始白名单群。收件箱按持久化状态做全局冷却，默认每 600 秒最多
接受/交付一条新消息；重复 idempotency key 可安全重试。
"""
from __future__ import annotations

import hashlib
import html
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


logger = logging.getLogger("CodexInbox")
GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}@chatroom$")
SENSITIVE_RE = re.compile(
    r"(?i)(?:\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|secret)"
    r"\b\s*[:=]\s*[\"']?[^\s\"']{8,}|\bsk-[A-Za-z0-9_-]{20,}\b|"
    r"\bAKIA[A-Z0-9]{16}\b|\bwxid_[A-Za-z0-9_.-]{3,}\b|\b[A-Za-z0-9_.-]{5,}@chatroom\b)"
)


def _text(value: Any, limit: int, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    value = value.replace("\x00", "").strip()
    if not value or len(value) > limit:
        raise ValueError(f"{field} 长度必须为 1–{limit} 字符")
    return value


class InboxError(ValueError):
    def __init__(self, message: str, status: int = 400, *, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class CodexInbox:
    """线程安全、原子保存的短消息队列。"""

    def __init__(self, root: str | Path, options: dict[str, Any] | None = None):
        options = dict(options or {})
        self.root = Path(root).resolve()
        if type(options.get("enabled", True)) is not bool:
            raise ValueError("codex_inbox.enabled 必须是布尔值")
        self.enabled = options.get("enabled", True)
        self.host = options.get("host", "127.0.0.1")
        if self.host != "127.0.0.1":
            raise ValueError("codex_inbox.host 只能是 127.0.0.1")
        port = options.get("port", 8799)
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("codex_inbox.port 应为 0–65535 的整数")
        self.port = port
        interval = options.get("min_interval_seconds", 600)
        if type(interval) not in (int, float) or interval < 600 or interval > 86400:
            raise ValueError("codex_inbox.min_interval_seconds 应为 600–86400")
        self.min_interval = float(interval)
        limit = options.get("max_pending", 32)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("codex_inbox.max_pending 应为 1–100")
        self.max_pending = limit
        reply_chats = options.get("reply_chat_allowlist", [])
        if (not isinstance(reply_chats, list) or
                any(not isinstance(chat, str) or not GROUP_ID_RE.fullmatch(chat) for chat in reply_chats)):
            raise ValueError("codex_inbox.reply_chat_allowlist 必须是群聊 ID 列表")
        self.reply_chat_allowlist = frozenset(reply_chats)
        self.db_path = self._path(options.get("database_file", ".runtime/codex_inbox.sqlite3"))
        self.token_path = self._path(options.get("token_file", ".runtime/codex_inbox.token"))
        self.archive_dir = self._path(options.get("archive_dir", "codex-inbox"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.token = self._load_or_create_token()
        self._lock = threading.RLock()
        self._archive_lock = threading.RLock()
        self._initialize()
        self._sync_archive()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def _path(self, value: Any) -> Path:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError("codex_inbox 路径无效")
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.root / path
        return path.resolve()

    def _load_or_create_token(self) -> str:
        try:
            token = self.token_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            token = ""
        except (OSError, UnicodeError) as error:
            raise ValueError("codex_inbox.token_file 无法读取") from error
        if token and 32 <= len(token) <= 256:
            return token
        token = secrets.token_urlsafe(32)
        temporary = self.token_path.with_name(self.token_path.name + f".{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="ascii", newline="\n") as output:
                output.write(token + "\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.token_path)
            try:
                os.chmod(self.token_path, 0o600)
            except OSError:
                pass
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return token

    def _connect(self):
        db = sqlite3.connect(str(self.db_path), timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _connection(self):
        db = self._connect()
        try:
            yield db
            if db.in_transaction:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self) -> None:
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS codex_inbox (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                idempotency_hash TEXT UNIQUE,
                text TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','delivered','acked'))
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS codex_inbox_pending ON codex_inbox(status, seq)")
            columns = {row[1] for row in db.execute("PRAGMA table_info(codex_inbox)")}
            if "reply_to_chat_id" not in columns:
                db.execute("ALTER TABLE codex_inbox ADD COLUMN reply_to_chat_id TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS codex_inbox_meta (
                name TEXT PRIMARY KEY,
                value REAL NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS codex_inbox_replies (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                inbox_id TEXT NOT NULL UNIQUE,
                chat_id TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','failed')),
                error_code TEXT NOT NULL DEFAULT '',
                sent_at TEXT
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS codex_inbox_replies_pending "
                       "ON codex_inbox_replies(status, seq)")
            db.execute("""CREATE TABLE IF NOT EXISTS codex_inbox_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                inbox_id TEXT NOT NULL,
                event TEXT NOT NULL,
                created_at TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT ''
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS codex_inbox_events_item "
                       "ON codex_inbox_events(inbox_id, seq)")
            # Older databases already retain messages and their current state,
            # but not transitions. Record a truthful migration snapshot rather
            # than inventing timestamps for those historical transitions.
            for row in db.execute("SELECT id,created_at,status FROM codex_inbox").fetchall():
                exists = db.execute("SELECT 1 FROM codex_inbox_events WHERE inbox_id=? LIMIT 1",
                                    (row["id"],)).fetchone()
                if exists:
                    continue
                self._add_event(db, row["id"], "accepted", row["created_at"])
                if row["status"] != "pending":
                    self._add_event(db, row["id"], "legacy_state_observed",
                                    self._timestamp(time.time()),
                                    f"迁移时状态：{row['status']}；原始变更时间未记录")
            for row in db.execute("SELECT * FROM codex_inbox_replies").fetchall():
                queued = db.execute("SELECT 1 FROM codex_inbox_events WHERE inbox_id=? "
                                    "AND event='reply_queued' LIMIT 1", (row["inbox_id"],)).fetchone()
                if queued:
                    continue
                self._add_event(db, row["inbox_id"], "reply_queued", row["created_at"])
                if row["status"] in ("sent", "failed"):
                    event = "reply_sent" if row["status"] == "sent" else "reply_failed"
                    when = row["sent_at"] or self._timestamp(time.time())
                    detail = "" if row["status"] == "sent" else "历史失败状态；原始变更时间未记录"
                    self._add_event(db, row["inbox_id"], event, when, detail)

    @staticmethod
    def _timestamp(value: float) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))

    @staticmethod
    def _add_event(db: sqlite3.Connection, inbox_id: str, event: str,
                   created_at: str | None = None, detail: str = "") -> None:
        db.execute("INSERT INTO codex_inbox_events(inbox_id,event,created_at,detail) VALUES(?,?,?,?)",
                   (inbox_id, event, created_at or CodexInbox._timestamp(time.time()), detail[:160]))

    def _limited(self, db: sqlite3.Connection, key: str, now: float) -> int:
        row = db.execute("SELECT value FROM codex_inbox_meta WHERE name=?", (key,)).fetchone()
        then = float(row["value"]) if row else 0.0
        remaining = self.min_interval - (now - then)
        return max(0, int(remaining + 0.999)) if remaining > 0 else 0

    @staticmethod
    def _set_meta(db: sqlite3.Connection, key: str, value: float) -> None:
        db.execute("INSERT INTO codex_inbox_meta(name,value) VALUES(?,?) "
                   "ON CONFLICT(name) DO UPDATE SET value=excluded.value", (key, value))

    @staticmethod
    def _idempotency_hash(key: str) -> str | None:
        return hashlib.sha256(key.encode("utf-8")).hexdigest() if key else None

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, str]:
        return {"id": row["id"], "text": row["text"], "source": row["source"],
                "created_at": row["created_at"], "status": row["status"]}

    def _archive_path(self, message_id: str) -> Path:
        """Return a stable, filesystem-safe Markdown path for one inbox item."""
        return self.archive_dir / (message_id.replace("/", "_") + ".md")

    def _write_archive_file(self, row: sqlite3.Row, reply: sqlite3.Row | None = None,
                            events: list[sqlite3.Row] | None = None) -> None:
        body = html.escape(str(row["text"]), quote=False)
        reply_block = ""
        if reply is not None:
            reply_text = html.escape(str(reply["text"]), quote=False)
            sent_at = f"\n发送时间：`{reply['sent_at']}`" if reply["sent_at"] else ""
            reply_block = (f"\n### Codex 回报\n\n状态：`{reply['status']}` · 创建时间：`{reply['created_at']}`"
                           f"{sent_at}\n\n"
                           f"<pre>{reply_text}</pre>\n")
        event_labels = {
            "accepted": "收到请求", "delivered": "交付给处理端", "acked": "已确认",
            "completed": "已完成处理", "reply_queued": "结果进入回程队列",
            "reply_sending": "正在回发结果", "reply_sent": "结果已回发",
            "reply_failed": "结果回发失败", "reply_interrupted": "回发中断后停止自动重试",
            "legacy_state_observed": "迁移时观察到旧状态",
        }
        timeline = "\n## 状态历史\n\n"
        for event in events or []:
            label = event_labels.get(event["event"], event["event"])
            detail = f" — {html.escape(event['detail'], quote=False)}" if event["detail"] else ""
            timeline += f"- `{event['created_at']}`：{label}{detail}\n"
        content = ("# Bot → Codex 收件箱记录\n\n"
                   f"- 编号：`{row['id']}`\n"
                   f"- 创建时间：`{row['created_at']}`\n"
                   f"- 来源：`{row['source']}`\n"
                   f"- 当前状态：`{row['status']}`\n\n"
                   "## 收件内容\n\n<pre>" + body + "</pre>\n" + timeline + reply_block)
        target = self._archive_path(row["id"])
        temporary = target.with_name(target.name + f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(content, encoding="utf-8", newline="\n")
        try:
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _sync_archive(self) -> None:
        """Materialize the SQLite history as user-readable Markdown files."""
        with self._archive_lock:
            try:
                with self._connection() as db:
                    rows = db.execute("SELECT * FROM codex_inbox ORDER BY seq").fetchall()
                    replies = {r["inbox_id"]: r for r in db.execute(
                        "SELECT * FROM codex_inbox_replies").fetchall()}
                    event_rows = db.execute("SELECT * FROM codex_inbox_events ORDER BY seq").fetchall()
                events_by_id: dict[str, list[sqlite3.Row]] = {}
                for event in event_rows:
                    events_by_id.setdefault(event["inbox_id"], []).append(event)
                for row in rows:
                    self._write_archive_file(row, replies.get(row["id"]), events_by_id.get(row["id"], []))
                index = ["# Bot → Codex 收件箱历史", "",
                         "此文件夹由 wx-helper 自动维护；每条请求一个 Markdown 文件。",
                         "SQLite 是状态机，Markdown 是便于人工查看的镜像。", ""]
                for row in rows:
                    index.append(f"- [{row['id']}]({self._archive_path(row['id']).name}) · "
                                 f"{row['created_at']} · `{row['status']}`")
                temporary = self.archive_dir / f"INDEX.md.{uuid.uuid4().hex}.tmp"
                temporary.write_text("\n".join(index) + "\n", encoding="utf-8", newline="\n")
                try:
                    os.replace(temporary, self.archive_dir / "INDEX.md")
                finally:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
                readme = self.archive_dir / "README.md"
                if not readme.exists():
                    readme.write_text("# Codex 收件箱\n\n打开 `INDEX.md` 查看历史。此目录含本机聊天转交内容，已加入 Git 忽略规则。\n",
                                      encoding="utf-8", newline="\n")
            except (OSError, sqlite3.Error) as error:
                # 归档是可恢复的镜像，不能阻止收件箱继续工作。
                logger.warning("收件箱历史镜像更新失败：%s", type(error).__name__)

    def archive_location(self) -> str:
        return str(self.archive_dir)

    def accept(self, text: str, *, source: str = "bot", idempotency_key: str = "",
               reply_to_chat_id: str | None = None) -> dict[str, Any]:
        text = _text(text, 2000, "text")
        if SENSITIVE_RE.search(text):
            raise InboxError("消息包含疑似密钥或身份标识，已拒收", 400)
        source = _text(source or "bot", 80, "source")
        if source not in {"bot", "wechat_bot", "local_api", "deepseek_harness", "agent_adapter"}:
            raise InboxError("source 不受支持", 400)
        if idempotency_key:
            idempotency_key = _text(idempotency_key, 128, "idempotency_key")
        if reply_to_chat_id is not None:
            if (not isinstance(reply_to_chat_id, str) or not GROUP_ID_RE.fullmatch(reply_to_chat_id) or
                    reply_to_chat_id not in self.reply_chat_allowlist):
                raise InboxError("回报目标不在群聊白名单中", 403)
        key_hash = self._idempotency_hash(idempotency_key)
        now = time.time()
        with self._lock:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                if key_hash:
                    duplicate = db.execute("SELECT id,status,reply_to_chat_id FROM codex_inbox WHERE idempotency_hash=?",
                                           (key_hash,)).fetchone()
                    if duplicate:
                        if duplicate["reply_to_chat_id"] != reply_to_chat_id:
                            raise InboxError("幂等键对应的回报目标不一致", 409)
                        db.commit()
                        return {"accepted": True, "duplicate": True, "id": duplicate["id"],
                                "status": duplicate["status"]}
                remaining = self._limited(db, "last_accepted", now)
                if remaining:
                    raise InboxError("收件箱冷却中", 429, retry_after=remaining)
                pending = db.execute("SELECT COUNT(*) FROM codex_inbox WHERE status='pending'").fetchone()[0]
                if pending >= self.max_pending:
                    raise InboxError("收件箱待处理消息已满", 429, retry_after=60)
                message_id = "ci-" + uuid.uuid4().hex[:16]
                db.execute("INSERT INTO codex_inbox(id,idempotency_hash,text,source,created_at,status,reply_to_chat_id) "
                           "VALUES(?,?,?,?,?,'pending',?)",
                           (message_id, key_hash, text, source, self._timestamp(now), reply_to_chat_id))
                self._add_event(db, message_id, "accepted", self._timestamp(now))
                self._set_meta(db, "last_accepted", now)
                db.commit()
                self._sync_archive()
                return {"accepted": True, "duplicate": False, "id": message_id, "status": "pending"}

    def receive(self) -> dict[str, Any]:
        """取出一条消息；跨进程持久化限制每 600 秒最多交付一次。"""
        now = time.time()
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            remaining = self._limited(db, "last_delivered", now)
            if remaining:
                db.commit()
                return {"status": "cooldown", "retry_after": remaining}
            row = db.execute("SELECT * FROM codex_inbox WHERE status='pending' ORDER BY seq LIMIT 1").fetchone()
            if row is None:
                db.commit()
                return {"status": "empty"}
            db.execute("UPDATE codex_inbox SET status='delivered' WHERE id=?", (row["id"],))
            self._add_event(db, row["id"], "delivered", self._timestamp(now))
            self._set_meta(db, "last_delivered", now)
            db.commit()
            result = self._record(row)
            result["status"] = "delivered"
            self._sync_archive()
            return result

    def ack(self, ids: list[str]) -> int:
        if not isinstance(ids, list) or not ids or len(ids) > 100:
            raise InboxError("ids 必须是 1–100 项数组")
        values = {_text(item, 80, "id") for item in ids}
        changed = 0
        with self._lock:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                for item in values:
                    cursor = db.execute("UPDATE codex_inbox SET status='acked' WHERE id=? AND status='delivered'",
                                        (item,))
                    changed += cursor.rowcount
                    if cursor.rowcount:
                        self._add_event(db, item, "acked")
                db.commit()
        self._sync_archive()
        return changed

    def complete(self, inbox_id: str, text: str) -> dict[str, Any]:
        """原子确认一条已交付请求，并排入仅回原白名单群的简短结果。"""
        inbox_id = _text(inbox_id, 80, "id")
        text = _text(text, 1200, "text")
        if SENSITIVE_RE.search(text):
            raise InboxError("回报包含疑似密钥或身份标识，已拒收", 400)
        if len(text.encode("utf-8")) > 850:
            raise InboxError("回报超过 850 个 UTF-8 字节，请先压缩结果", 400)
        now = time.time()
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,reply_to_chat_id FROM codex_inbox WHERE id=?", (inbox_id,)).fetchone()
            if row is None:
                raise InboxError("收件箱请求不存在", 404)
            existing = db.execute("SELECT id,status,text FROM codex_inbox_replies WHERE inbox_id=?",
                                  (inbox_id,)).fetchone()
            if existing is not None:
                if existing["text"] != text:
                    raise InboxError("该请求已有不同回报，拒绝覆盖", 409)
                db.commit()
                return {"queued": existing["status"] != "failed", "duplicate": True,
                        "reply_id": existing["id"], "status": existing["status"]}
            if row["status"] != "delivered":
                raise InboxError("请求尚未交付或已确认", 409)
            chat_id = row["reply_to_chat_id"]
            if not chat_id or chat_id not in self.reply_chat_allowlist:
                raise InboxError("请求没有有效的白名单回报目标", 409)
            reply_id = "cr-" + uuid.uuid4().hex[:16]
            db.execute("INSERT INTO codex_inbox_replies(id,inbox_id,chat_id,text,created_at,status) "
                       "VALUES(?,?,?,?,?,'pending')",
                       (reply_id, inbox_id, chat_id, text, self._timestamp(now)))
            db.execute("UPDATE codex_inbox SET status='acked' WHERE id=? AND status='delivered'", (inbox_id,))
            self._add_event(db, inbox_id, "completed", self._timestamp(now))
            self._add_event(db, inbox_id, "reply_queued", self._timestamp(now))
            db.commit()
            self._sync_archive()
            return {"queued": True, "duplicate": False, "reply_id": reply_id, "status": "pending"}

    def reply_status(self, inbox_id: str) -> dict[str, Any]:
        inbox_id = _text(inbox_id, 80, "id")
        with self._connection() as db:
            row = db.execute("SELECT id,status,error_code,created_at FROM codex_inbox_replies "
                             "WHERE inbox_id=?", (inbox_id,)).fetchone()
            if row is None:
                return {"status": "none"}
            return {"reply_id": row["id"], "status": row["status"],
                    "error_code": row["error_code"], "created_at": row["created_at"]}

    def _claim_reply(self) -> dict[str, str] | None:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id,inbox_id,chat_id,text FROM codex_inbox_replies "
                             "WHERE status='pending' ORDER BY seq LIMIT 1").fetchone()
            if row is None:
                db.commit()
                return None
            db.execute("UPDATE codex_inbox_replies SET status='sending' WHERE id=? AND status='pending'",
                       (row["id"],))
            self._add_event(db, row["inbox_id"], "reply_sending")
            db.commit()
            result = {"id": row["id"], "chat_id": row["chat_id"], "text": row["text"]}
        self._sync_archive()
        return result

    def _finish_reply(self, reply_id: str, *, sent: bool, error_code: str = "") -> None:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE codex_inbox_replies SET status=?,error_code=?,sent_at=? "
                       "WHERE id=? AND status='sending'",
                       ("sent" if sent else "failed", "" if sent else error_code[:80],
                        self._timestamp(time.time()) if sent else None, reply_id))
            row = db.execute("SELECT inbox_id FROM codex_inbox_replies WHERE id=?", (reply_id,)).fetchone()
            if row is not None:
                self._add_event(db, row["inbox_id"], "reply_sent" if sent else "reply_failed",
                                detail="" if sent else error_code[:80])
            db.commit()
        self._sync_archive()

    def dispatch_one(self, send_text) -> dict[str, Any]:
        """回发一条完成结果；失败不自动重试，避免不确定发送造成重复消息。"""
        item = self._claim_reply()
        if item is None:
            return {"status": "empty"}
        if item["chat_id"] not in self.reply_chat_allowlist:
            self._finish_reply(item["id"], sent=False, error_code="target_not_allowlisted")
            return {"status": "failed", "reply_id": item["id"], "error_code": "target_not_allowlisted"}
        body = "Codex 处理结果：\n" + item["text"]
        try:
            result = send_text(item["chat_id"], body)
            if result is False:
                raise RuntimeError("send_not_confirmed")
        except Exception as error:
            code = type(error).__name__
            self._finish_reply(item["id"], sent=False, error_code=code)
            logger.warning("Codex 结果回发失败：reply_id=%s error=%s", item["id"], code)
            return {"status": "failed", "reply_id": item["id"], "error_code": code}
        self._finish_reply(item["id"], sent=True)
        logger.info("Codex 结果已回发：reply_id=%s", item["id"])
        return {"status": "sent", "reply_id": item["id"]}

    def recover_interrupted_replies(self) -> int:
        """进程重启后将发送结果不确定的工作项标为失败，禁止自动重复发送。"""
        with self._lock, self._connection() as db:
            interrupted = db.execute("SELECT inbox_id FROM codex_inbox_replies WHERE status='sending'").fetchall()
            cursor = db.execute("UPDATE codex_inbox_replies SET status='failed',error_code='interrupted' "
                                "WHERE status='sending'")
            for row in interrupted:
                self._add_event(db, row["inbox_id"], "reply_interrupted", detail="interrupted")
            changed = cursor.rowcount
        if changed:
            self._sync_archive()
        return changed

    def start(self) -> None:
        if not self.enabled or self._server is not None:
            return
        recovered = self.recover_interrupted_replies()
        if recovered:
            logger.warning("发现 %d 条结果回发状态不确定的记录；已停止自动重试", recovered)
        inbox = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "CodexInbox/1"

            def log_message(self, fmt, *args):
                return

            def _authorized(self) -> bool:
                supplied = self.headers.get("Authorization", "")
                if not supplied.startswith("Bearer "):
                    return False
                return hmac.compare_digest(supplied[7:].strip(), inbox.token)

            def _send(self, status: int, data: dict[str, Any], *, retry_after: int | None = None):
                body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                if retry_after is not None:
                    self.send_header("Retry-After", str(retry_after))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not self._authorized():
                    self._send(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.path.split("?", 1)[0] != "/v1/inbox":
                    self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                result = inbox.receive()
                status = HTTPStatus.TOO_MANY_REQUESTS if result.get("status") == "cooldown" else HTTPStatus.OK
                self._send(status, result, retry_after=result.get("retry_after"))

            def do_POST(self):
                if not self._authorized():
                    self._send(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                path = self.path.split("?", 1)[0]
                if path not in ("/v1/inbox", "/v1/inbox/ack", "/v1/inbox/complete"):
                    self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                except ValueError:
                    length = -1
                if length < 0 or length > 16 * 1024:
                    self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body_too_large"})
                    return
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(body, dict):
                        raise ValueError("JSON 必须是对象")
                    if path == "/v1/inbox/ack":
                        changed = inbox.ack(body.get("ids"))
                        self._send(HTTPStatus.OK, {"acked": changed})
                        return
                    if path == "/v1/inbox/complete":
                        result = inbox.complete(body.get("id"), body.get("text"))
                        self._send(HTTPStatus.OK, result)
                        return
                    result = inbox.accept(body.get("text"), source=body.get("source", "bot"),
                                         idempotency_key=body.get("idempotency_key", ""))
                    self._send(HTTPStatus.OK, result)
                except InboxError as error:
                    self._send(error.status, {"error": str(error)}, retry_after=error.retry_after)
                except (UnicodeError, json.JSONDecodeError, ValueError, TypeError) as error:
                    self._send(HTTPStatus.BAD_REQUEST, {"error": str(error)[:120]})

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, name="CodexInbox", daemon=True)
        self._thread.start()
        logger.info("Codex 收件箱已启动（仅本机，端口=%d）", self.port)

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)

