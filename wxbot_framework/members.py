"""框架共用的成员身份和新收到的群文本记录，按账号、群及成员 ID 隔离。"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from .account import validate_account
from .config import validate_name
from .text import validate_mentions


class MemberError(ValueError):
    pass


class SnapshotExpired(RuntimeError):
    """清空或跨天后，取消仍在生成/等待发送的总结。"""


BEIJING = timezone(timedelta(hours=8))
MEMORY_SECRET_RE = re.compile(
    r"(?i)(?:\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|passwd|secret)\b\s*[:=]\s*\S+|"
    r"\bsk-[A-Za-z0-9_-]{20,}\b|\bwxid_[A-Za-z0-9_.-]{3,}\b|\b[A-Za-z0-9_.-]{5,}@chatroom\b)"
)


def _memory_terms(value):
    terms = set()
    for run in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", str(value or "").casefold()):
        if run and "\u4e00" <= run[0] <= "\u9fff":
            if len(run) == 1:
                terms.add(run)
            else:
                terms.update(run[index:index + 2] for index in range(len(run) - 1))
        elif len(run) >= 2:
            terms.add(run)
    return terms


@dataclass(frozen=True)
class ChatSnapshot:
    member: Member
    day: str
    generation: int
    messages: tuple

    @property
    def room(self):
        return self.member.room


@dataclass(frozen=True)
class GroupChatSnapshot:
    room: str
    day: str
    generation: int
    messages: tuple


@dataclass(frozen=True)
class ConversationSnapshot:
    room: str
    day: str
    generation: int
    speaker_name: str
    messages: tuple
    current: str
    group_context: tuple = ()


@dataclass(frozen=True)
class Member:
    room: str
    name: str
    display_name: str
    wxid: str | None
    aliases: tuple[str, ...]
    self_named: bool = False


def name_key(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 64 or any(c in value for c in "\0\n\r"):
        raise MemberError("成员称呼应为 1–64 字符的单行文本")
    return value.strip().casefold()


class MemberStore:
    def __init__(self, config_file, account, *, now=None):
        self.account = validate_account(account)
        self.config_file = Path(config_file).resolve()
        options = json.loads(self.config_file.read_text(encoding="utf-8-sig"))
        if not isinstance(options, dict) or not isinstance(options.get("groups"), dict):
            raise MemberError("成员配置应包含 groups 对象")
        database = options.get("database_file", ".runtime/member_data.sqlite3")
        if not isinstance(database, str) or not database.strip() or "\0" in database:
            raise MemberError("database_file 应为数据库路径")
        self.path = (self.config_file.parent / database).resolve()
        self.history_enabled = options.get("history_enabled", False)
        if type(self.history_enabled) is not bool:
            raise MemberError("history_enabled 必须是布尔值")
        self.daily_clear = options.get("daily_clear", False)
        if type(self.daily_clear) is not bool:
            raise MemberError("daily_clear 必须是布尔值")
        self.warm_retention_days = options.get("warm_retention_days", 30)
        if type(self.warm_retention_days) is not int or not 7 <= self.warm_retention_days <= 365:
            raise MemberError("warm_retention_days 应为 7–365 的整数")
        # 新群在尚未写入 room_personalities 前使用这个安全默认值。
        # 保留“默认”兼容旧配置；发布模板可以明确选择鲸鱼娘，而不用把
        # 人格硬编码到每个群 ID 或本机数据库里。
        self.default_personality = options.get("default_personality", "鲸鱼娘")
        if self.default_personality not in {"默认", "鲸鱼娘", "嘴臭", "卑微", "夸夸"}:
            raise MemberError("default_personality 应为 默认、鲸鱼娘、嘴臭、卑微 或 夸夸")
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._active_day = None
        self.groups = set(options["groups"])
        self.profiles = []
        for room, profiles in options["groups"].items():
            validate_mentions(room, [self.account])
            if not isinstance(profiles, list):
                raise MemberError("每个群的成员配置应为列表")
            keys, names, ids = set(), set(), set()
            for profile in profiles:
                if not isinstance(profile, dict):
                    raise MemberError("成员资料应为 JSON 对象")
                key = validate_name(profile.get("key"))
                name = profile.get("name")
                display = profile.get("display_name", name)
                aliases = profile.get("aliases", [])
                if not isinstance(aliases, list):
                    raise MemberError("aliases 应为称呼列表")
                normalized = {name_key(value) for value in [name, display, *aliases]}
                wxid = profile.get("wxid")
                if wxid is not None:
                    validate_mentions(room, [wxid])
                if key in keys or normalized & names or (wxid and wxid in ids):
                    raise MemberError("同一群的成员标识、称呼和微信 ID 不能重复")
                keys.add(key)
                names.update(normalized)
                if wxid:
                    ids.add(wxid)
                self.profiles.append((room, key, name.strip(), display.strip(), wxid, normalized))
        self._lock = threading.RLock()
        self._db = None
        self._closed = False

    @classmethod
    def from_config(cls, config, directory, account=None):
        filename = config.get("members_config_file")
        if filename is None:
            return None
        if not isinstance(filename, str) or not filename.strip() or "\0" in filename:
            raise MemberError("members_config_file 应为成员配置文件路径或 null")
        # 允许运行时从原生桥接读取当前登录账号。固定账号仍可由配置传入，
        # 账号为空时由 BotApplication 在连接成功后绑定，历史按账号分区保存。
        account = account if account is not None else config.get("wechat_account")
        if not account:
            return None
        return cls(Path(directory) / filename, account)

    def _connection(self):
        if self._closed:
            raise RuntimeError("成员目录已关闭")
        if self._db is not None:
            return self._db
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS people (
                    account TEXT NOT NULL, room TEXT NOT NULL, person_key TEXT NOT NULL,
                    name TEXT NOT NULL, display_name TEXT NOT NULL, wxid TEXT,
                    PRIMARY KEY(account, room, person_key), UNIQUE(account, room, wxid));
                CREATE TABLE IF NOT EXISTS aliases (
                    account TEXT NOT NULL, room TEXT NOT NULL, alias TEXT NOT NULL, person_key TEXT NOT NULL,
                    PRIMARY KEY(account, room, alias),
                    FOREIGN KEY(account, room, person_key) REFERENCES people(account, room, person_key));
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, account TEXT NOT NULL, room TEXT NOT NULL, sender_id TEXT NOT NULL,
                    text TEXT NOT NULL, received_at TEXT NOT NULL, session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL, is_self INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS messages_by_member ON messages(account, room, sender_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS messages_by_sequence
                    ON messages(account, session_id, sequence) WHERE sequence > 0;
                CREATE TABLE IF NOT EXISTS chat_buffers (
                    account TEXT NOT NULL, room TEXT NOT NULL, local_day TEXT NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(account,room));
                CREATE TABLE IF NOT EXISTS room_personalities (
                    account TEXT NOT NULL, room TEXT NOT NULL, name TEXT NOT NULL,
                    generation INTEGER NOT NULL, PRIMARY KEY(account,room));
                CREATE TABLE IF NOT EXISTS wechat_names (
                    account TEXT NOT NULL, wxid TEXT NOT NULL, name TEXT NOT NULL, name_key TEXT NOT NULL,
                    PRIMARY KEY(account,wxid));
                CREATE TABLE IF NOT EXISTS room_members (
                    account TEXT NOT NULL, room TEXT NOT NULL, wxid TEXT NOT NULL,
                    PRIMARY KEY(account,room,wxid));
                CREATE TABLE IF NOT EXISTS warm_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account TEXT NOT NULL, room TEXT NOT NULL, period_key TEXT NOT NULL,
                    summary TEXT NOT NULL, message_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    UNIQUE(account,room,period_key));
                CREATE INDEX IF NOT EXISTS warm_memories_recent
                    ON warm_memories(account,room,period_key DESC);
                CREATE TABLE IF NOT EXISTS cold_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account TEXT NOT NULL, room TEXT NOT NULL, kind TEXT NOT NULL,
                    content TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL,
                    UNIQUE(account,room,content));
                CREATE INDEX IF NOT EXISTS cold_memories_room
                    ON cold_memories(account,room,id DESC);
                INSERT OR IGNORE INTO room_members SELECT DISTINCT account,room,sender_id FROM messages;
            """)
            if "self_named" not in {row["name"] for row in db.execute("PRAGMA table_info(people)")}:
                db.execute("ALTER TABLE people ADD COLUMN self_named INTEGER NOT NULL DEFAULT 0")
            if "bot_generated" not in {row["name"] for row in db.execute("PRAGMA table_info(messages)")}:
                db.execute("ALTER TABLE messages ADD COLUMN bot_generated INTEGER NOT NULL DEFAULT 0")
            with db:
                for room, key, name, display, wxid, aliases in self.profiles:
                    existing = db.execute("SELECT wxid FROM people WHERE account=? AND room=? AND person_key=?",
                                          (self.account, room, key)).fetchone()
                    if existing and existing["wxid"] and wxid and existing["wxid"] != wxid:
                        raise MemberError(f"{name} 的配置 ID 与已保存绑定不同，请先核对")
                    db.execute("""INSERT INTO people(account,room,person_key,name,display_name,wxid) VALUES(?,?,?,?,?,?)
                        ON CONFLICT(account,room,person_key) DO UPDATE SET
                            name=CASE WHEN people.self_named=0 THEN excluded.name ELSE people.name END,
                            display_name=CASE WHEN people.self_named=0 THEN excluded.display_name ELSE people.display_name END,
                            wxid=COALESCE(people.wxid,excluded.wxid)""", (self.account, room, key, name, display, wxid))
                    for alias in aliases:
                        old = db.execute("SELECT person_key FROM aliases WHERE account=? AND room=? AND alias=?",
                                         (self.account, room, alias)).fetchone()
                        if old and old["person_key"] != key:
                            raise MemberError(f"称呼 {alias} 已属于另一位成员")
                        db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?,?,?)", (self.account, room, alias, key))
            self._db = db
            return db
        except BaseException:
            db.close()
            raise

    def initialize(self):
        with self._lock:
            self._connection()
            self.rollover()

    def _day_bounds(self):
        local = self._now().astimezone(BEIJING)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return (start.date().isoformat(), start.astimezone(timezone.utc).isoformat(),
                (start + timedelta(days=1)).astimezone(timezone.utc).isoformat())

    def rollover(self):
        """即使没有新消息也可调用；停机期间跨天则在启动时补清理。"""
        with self._lock:
            day, start, _ = self._day_bounds()
            if day == self._active_day:
                return False
            with self._connection() as db:
                for room in self.groups:
                    db.execute("""INSERT INTO chat_buffers(account,room,local_day) VALUES(?,?,?)
                        ON CONFLICT(account,room) DO UPDATE SET local_day=excluded.local_day,
                        generation=chat_buffers.generation+1 WHERE chat_buffers.local_day<>excluded.local_day""",
                               (self.account, room, day))
                    if self.daily_clear:
                        db.execute("DELETE FROM messages WHERE account=? AND room=? AND received_at<?",
                                   (self.account, room, start))
            self._active_day = day
            return True

    def clear(self, room):
        if room not in self.groups:
            raise MemberError("这个群尚未启用聊天记录")
        with self._lock:
            self.rollover()
            with self._connection() as db:
                count = db.execute("DELETE FROM messages WHERE account=? AND room=?", (self.account, room)).rowcount
                db.execute("UPDATE chat_buffers SET generation=generation+1 WHERE account=? AND room=?",
                           (self.account, room))
            return count

    def today_snapshot(self, name, room):
        """完整读取当天的成员文本，数量不受控制台 history 的条数限制。"""
        with self._lock:
            self.rollover()
            member = self.resolve(name, room)
            if member.wxid is None:
                raise MemberError(f"{member.name} 尚未绑定微信 ID")
            day, start, end = self._day_bounds()
            db = self._connection()
            state = db.execute("SELECT generation FROM chat_buffers WHERE account=? AND room=?",
                               (self.account, room)).fetchone()
            if state is None:
                raise MemberError("这个群尚未启用聊天记录")
            rows = db.execute("""SELECT * FROM messages WHERE account=? AND room=? AND sender_id=?
                AND received_at>=? AND received_at<? AND bot_generated=0 ORDER BY id""",
                              (self.account, room, member.wxid, start, end)).fetchall()
            return ChatSnapshot(member, day, state[0], tuple(dict(row) for row in rows))

    def today_group_snapshot(self, room):
        """完整读取本群今天所有成员的文本，未绑定者也保留身份区分。"""
        if room not in self.groups:
            raise MemberError("这个群尚未启用聊天记录")
        with self._lock:
            self.rollover()
            day, start, end = self._day_bounds()
            db = self._connection()
            state = db.execute("SELECT generation FROM chat_buffers WHERE account=? AND room=?",
                               (self.account, room)).fetchone()
            rows = db.execute("""SELECT m.*, p.name AS speaker_name FROM messages m
                LEFT JOIN people p ON p.account=m.account AND p.room=m.room AND p.wxid=m.sender_id
                WHERE m.account=? AND m.room=? AND m.received_at>=? AND m.received_at<?
                AND m.bot_generated=0 ORDER BY m.id""", (self.account, room, start, end)).fetchall()
            speakers, messages = {}, []
            for row in rows:
                item = dict(row)
                number = speakers.setdefault(item["sender_id"], len(speakers) + 1)
                # 发给模型的稳定编号替代真实微信 ID；昵称只用于可读性。
                item["speaker"] = f"P{number}"
                item["speaker_name"] = item["speaker_name"] or self.wechat_name(item["sender_id"]) or f"未绑定成员{number}"
                messages.append(item)
            return GroupChatSnapshot(room, day, state[0], tuple(messages))

    def snapshot_is_current(self, snapshot):
        with self._lock:
            self.rollover()
            state = self._connection().execute("SELECT local_day,generation FROM chat_buffers WHERE account=? AND room=?",
                                               (self.account, snapshot.room)).fetchone()
            return bool(state and state[0] == snapshot.day and state[1] == snapshot.generation)

    def personality(self, room):
        from .personality import PersonalityState
        with self._lock:
            row = self._connection().execute("SELECT name,generation FROM room_personalities WHERE account=? AND room=?",
                                              (self.account, room)).fetchone()
            name = (self.default_personality if row and row[0] == "默认" else
                    row[0] if row else self.default_personality)
            return (PersonalityState(room, name, row[1]) if row else
                    PersonalityState(room, self.default_personality))

    def set_personality(self, room, name):
        from .personality import PROMPTS
        if room not in self.groups or (name not in PROMPTS and name != "默认"):
            raise MemberError("人格应为 鲸鱼娘、嘴臭、卑微、夸夸 或 默认，且在已启用的群中切换")
        if name == "默认":
            name = self.default_personality
        with self._lock:
            current = self.personality(room)
            if current.name == name:
                return current
            with self._connection() as db:
                db.execute("""INSERT INTO room_personalities VALUES(?,?,?,1)
                    ON CONFLICT(account,room) DO UPDATE SET name=excluded.name,
                    generation=room_personalities.generation+1 WHERE room_personalities.name<>excluded.name""",
                           (self.account, room, name))
            return self.personality(room)

    def personality_is_current(self, state):
        return self.personality(state.room) == state

    @contextmanager
    def personality_delivery_guard(self, state):
        from .personality import PersonalityExpired
        with self._lock:
            if not self.personality_is_current(state):
                raise PersonalityExpired("人格已切换")
            yield

    def conversation_snapshot(self, message):
        """返回发言者上下文和本群最近消息；都严格截止于当前消息之前。"""
        from .summary import CONTROL
        if message.chat_id not in self.groups:
            raise MemberError("这个群尚未启用聊天记录")
        with self._lock:
            self.rollover()
            day, start, end = self._day_bounds()
            db = self._connection()
            current = db.execute("""SELECT id FROM messages WHERE account=? AND room=? AND sender_id=?
                AND session_id=? AND sequence=? AND text=? AND received_at>=? AND received_at<?
                ORDER BY id DESC LIMIT 1""", (self.account, message.chat_id, message.sender_id,
                message.session_id, message.sequence, message.text, start, end)).fetchone()
            if current is None:
                raise SnapshotExpired("当前消息已被清空或已跨天")
            cursor = db.execute("""SELECT * FROM messages WHERE account=? AND room=? AND sender_id=?
                AND id<? AND received_at>=? AND received_at<? AND bot_generated=0 ORDER BY id DESC""",
                (self.account, message.chat_id, message.sender_id, current[0], start, end))
            previous = []
            for row in cursor:
                text = row["text"].strip()
                if text and "WXBOT_AT_CHAT_PROBE" not in text and text.split(maxsplit=1)[0] not in CONTROL:
                    previous.append(dict(row))
                    if len(previous) == 10:
                        break
            group_rows = db.execute("""SELECT m.*, COALESCE(p.display_name,p.name,w.name) AS context_speaker_name
                FROM messages m
                LEFT JOIN people p ON p.account=m.account AND p.room=m.room AND p.wxid=m.sender_id
                LEFT JOIN wechat_names w ON w.account=m.account AND w.wxid=m.sender_id
                WHERE m.account=? AND m.room=? AND m.id<? AND m.received_at>=? AND m.received_at<?
                ORDER BY m.id DESC LIMIT 200""",
                (self.account, message.chat_id, current[0], start, end)).fetchall()
            recent_group = []
            for row in group_rows:
                text = row["text"].strip()
                if (not text or "WXBOT_AT_CHAT_PROBE" in text or text.startswith("WXBOT_") or
                        text.split(maxsplit=1)[0] in CONTROL):
                    continue
                recent_group.append(row)
                if len(recent_group) == 20:
                    break
            speaker_labels, group_context = {}, []
            for row in reversed(recent_group):
                sender = row["sender_id"]
                speaker = speaker_labels.setdefault(sender, f"P{len(speaker_labels) + 1}")
                is_bot = bool(row["bot_generated"]) or sender == self.account
                group_context.append({
                    "time": datetime.fromisoformat(row["received_at"]).astimezone(BEIJING).strftime("%H:%M"),
                    "speaker": speaker,
                    "speaker_name": "机器人" if is_bot else (row["context_speaker_name"] or "群友"),
                    "text": row["text"].strip()[:400],
                    "is_bot": is_bot,
                })
            member = self.find_by_wxid(message.sender_id, message.chat_id)
            state = db.execute("SELECT generation FROM chat_buffers WHERE account=? AND room=?",
                               (self.account, message.chat_id)).fetchone()
            return ConversationSnapshot(message.chat_id, day, state[0], member.name if member else
                                        self.wechat_name(message.sender_id) or "群友",
                                        tuple(reversed(previous)), message.text, tuple(group_context))

    @contextmanager
    def delivery_guard(self, snapshot):
        # 仅原生发送期间持锁，随机等待和大模型调用不会阻塞 /清空。
        with self._lock:
            if not self.snapshot_is_current(snapshot):
                raise SnapshotExpired("聊天记录已清空或已跨天")
            yield

    def _member(self, row):
        if row["person_key"] is None:
            return Member(row["room"], row["name"], row["name"], row["wxid"], (), False)
        aliases = self._connection().execute(
            "SELECT alias FROM aliases WHERE account=? AND room=? AND person_key=? ORDER BY alias",
            (self.account, row["room"], row["person_key"])).fetchall()
        return Member(row["room"], row["name"], row["display_name"], row["wxid"], tuple(r[0] for r in aliases),
                      bool(row["self_named"]))

    def remember_wechat_name(self, wxid, name):
        """微信原始名字按账号 ID 缓存，不产生任何群内绑定或别名。"""
        validate_account(wxid)
        normalized = name_key(name)
        if any(ord(ch) < 32 for ch in name):
            raise MemberError("微信名不能包含控制字符")
        with self._lock, self._connection() as db:
            db.execute("""INSERT INTO wechat_names VALUES(?,?,?,?) ON CONFLICT(account,wxid)
                DO UPDATE SET name=excluded.name,name_key=excluded.name_key""",
                (self.account, wxid, name.strip(), normalized))

    def wechat_name(self, wxid):
        with self._lock:
            row = self._connection().execute("SELECT name FROM wechat_names WHERE account=? AND wxid=?",
                                             (self.account, wxid)).fetchone()
            return row[0] if row else None

    def display_name(self, wxid, room):
        """群内绑定优先；其他群的绑定永远不作为默认名字。"""
        member = self.find_by_wxid(wxid, room)
        return member.display_name if member else self.wechat_name(wxid)

    def _row(self, name, room=None):
        sql = """SELECT p.* FROM people p JOIN aliases a USING(account,room,person_key)
                 WHERE a.account=? AND a.alias=?"""
        values = [self.account, name_key(name)]
        if room is not None:
            sql += " AND a.room=?"
            values.append(room)
        rows = self._connection().execute(sql, values).fetchall()
        if not rows:
            # 未绑定也可按已核对的微信名查询，但必须确实在这个群说过话。
            query = """SELECT DISTINCT m.room,n.name,n.wxid FROM wechat_names n JOIN room_members m
                ON m.account=n.account AND m.wxid=n.wxid WHERE n.account=? AND n.name_key=?"""
            parameters = [self.account, name_key(name)]
            if room is not None:
                query += " AND m.room=?"
                parameters.append(room)
            names = self._connection().execute(query, parameters).fetchall()
            if len(names) > 1:
                raise MemberError(f"微信名 {name} 对应多个成员或群，请用本群绑定称呼查询")
            if not names:
                raise MemberError(f"未登记称呼：{name}")
            bound = self._connection().execute("SELECT * FROM people WHERE account=? AND room=? AND wxid=?",
                (self.account, names[0]["room"], names[0]["wxid"])).fetchone()
            if bound:
                return bound
            return {"room": names[0]["room"], "name": names[0]["name"], "wxid": names[0]["wxid"],
                    "person_key": None, "display_name": names[0]["name"], "self_named": 0}
        if len(rows) != 1:
            raise MemberError(f"多个群都有 {name}，请指定群 ID")
        return rows[0]

    def resolve(self, name, room=None):
        with self._lock:
            return self._member(self._row(name, room))

    def list_members(self, room=None):
        with self._lock:
            sql, values = "SELECT * FROM people WHERE account=?", [self.account]
            if room is not None:
                sql += " AND room=?"
                values.append(room)
            return [self._member(row) for row in self._connection().execute(sql + " ORDER BY room,person_key", values).fetchall()]

    def find_by_wxid(self, wxid, room):
        with self._lock:
            row = self._connection().execute("SELECT * FROM people WHERE account=? AND room=? AND wxid=?",
                                             (self.account, room, wxid)).fetchone()
            return self._member(row) if row else None

    def bind(self, name, room, wxid):
        validate_mentions(room, [wxid])
        with self._lock:
            row = self._row(name, room)
            if row["person_key"] is None:
                raise MemberError("请本人发送 /绑定 昵称 来设置本群称呼")
            if row["wxid"] and row["wxid"] != wxid:
                raise MemberError(f"{row['name']} 已绑定其他账号，未修改原绑定")
            other = self._connection().execute("SELECT name FROM people WHERE account=? AND room=? AND wxid=?",
                                                (self.account, room, wxid)).fetchone()
            if other and other["name"] != row["name"]:
                raise MemberError(f"这个微信账号已绑定为 {other['name']}，未修改原绑定")
            with self._connection() as db:
                db.execute("UPDATE people SET wxid=? WHERE account=? AND room=? AND person_key=?",
                           (wxid, self.account, room, row["person_key"]))
            return self._member(self._row(name, room))

    def register(self, name, room, wxid):
        """本人注册新称呼或改称呼；身份、旧称呼和记录仍由同一个 ID 关联。"""
        alias = name_key(name)
        name = name.strip()
        validate_mentions(room, [wxid])
        if room not in self.groups:
            raise MemberError("这个群尚未启用成员目录")
        with self._lock:
            db = self._connection()
            by_name = db.execute("""SELECT p.* FROM people p JOIN aliases a USING(account,room,person_key)
                WHERE a.account=? AND a.room=? AND a.alias=?""", (self.account, room, alias)).fetchone()
            by_id = db.execute("SELECT * FROM people WHERE account=? AND room=? AND wxid=?",
                               (self.account, room, wxid)).fetchone()
            if by_name and by_name["wxid"] and by_name["wxid"] != wxid:
                raise MemberError(f"称呼 {name} 已被其他成员绑定，请换一个昵称")
            if by_name and by_id and by_name["person_key"] != by_id["person_key"]:
                raise MemberError(f"称呼 {name} 已属于另一份成员资料，请换一个昵称")
            row = by_id or by_name
            key = row["person_key"] if row else "member_" + uuid.uuid4().hex
            display = name  # 本人 /绑定 的名字同时用于本群查询和艾特显示。
            with db:
                if row:
                    db.execute("UPDATE people SET name=?,display_name=?,wxid=?,self_named=1 WHERE account=? AND room=? AND person_key=?",
                               (name, display, wxid, self.account, room, key))
                else:
                    db.execute("""INSERT INTO people(account,room,person_key,name,display_name,wxid,self_named)
                        VALUES(?,?,?,?,?,?,1)""", (self.account, room, key, name, name, wxid))
                db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?,?,?)", (self.account, room, alias, key))
            return self._member(self._row(name, room))

    def record(self, message):
        if not self.history_enabled or message.type != 1 or not message.is_group or message.chat_id not in self.groups:
            return False
        if message.data.get("self_wxid", self.account) != self.account:
            return False
        validate_mentions(message.chat_id, [message.sender_id])
        if not isinstance(message.text, str) or not message.session_id:
            raise MemberError("群记录缺少正文或连接标识")
        native_name = message.data.get("sender_wechat_name")
        if native_name:
            self.remember_wechat_name(message.sender_id, native_name)
        with self._lock, self._connection() as db:
            self.rollover()
            db.execute("INSERT OR IGNORE INTO room_members VALUES(?,?,?)",
                       (self.account, message.chat_id, message.sender_id))
            result = db.execute("""INSERT OR IGNORE INTO messages
                (account,room,sender_id,text,received_at,session_id,sequence,is_self,bot_generated) VALUES(?,?,?,?,?,?,?,?,?)""",
                (self.account, message.chat_id, message.sender_id, message.text,
                 self._now().astimezone(timezone.utc).isoformat(), message.session_id, message.sequence,
                 int(message.is_self), int(bool(message.data.get("bot_generated")))))
            return result.rowcount > 0

    def history(self, name, room=None, *, limit=100, since=None):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise MemberError("历史查询 limit 应为 1–1000 的整数")
        if since is not None:
            if not isinstance(since, datetime) or since.tzinfo is None or since.utcoffset() is None:
                raise MemberError("since 应为带时区的 datetime")
            since = since.astimezone(timezone.utc).isoformat()
        with self._lock:
            self.rollover()
            member = self.resolve(name, room)
            if member.wxid is None:
                raise MemberError(f"{member.name} 尚未绑定微信 ID")
            sql = "SELECT * FROM messages WHERE account=? AND room=? AND sender_id=?"
            values = [self.account, member.room, member.wxid]
            if since is not None:
                sql += " AND received_at>=?"
                values.append(since)
            rows = self._connection().execute(sql + " ORDER BY id DESC LIMIT ?", [*values, limit]).fetchall()
            return [dict(row) for row in reversed(rows)]

    def store_warm_memory(self, room, period_key, summary, message_count):
        """保存一份短期群聊摘要；原始聊天仍遵循 daily_clear 清理策略。"""
        if room not in self.groups:
            raise MemberError("这个群尚未启用记忆")
        try:
            date.fromisoformat(period_key)
        except (TypeError, ValueError):
            raise MemberError("温记忆 period_key 必须是 YYYY-MM-DD") from None
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 800:
            raise MemberError("温记忆摘要应为 1–800 字符")
        summary = summary.strip()
        if MEMORY_SECRET_RE.search(summary):
            raise MemberError("摘要包含疑似密钥或身份标识，拒绝保存")
        if type(message_count) is not int or not 0 <= message_count <= 100_000:
            raise MemberError("温记忆 message_count 无效")
        now = self._now().astimezone(timezone.utc)
        created_at = now.isoformat()
        expires_at = (now + timedelta(days=self.warm_retention_days)).isoformat()
        with self._lock, self._connection() as db:
            db.execute("""INSERT INTO warm_memories(account,room,period_key,summary,message_count,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(account,room,period_key) DO UPDATE SET
                summary=excluded.summary,message_count=excluded.message_count,
                created_at=excluded.created_at,expires_at=excluded.expires_at""",
                (self.account, room, period_key, summary, message_count, created_at, expires_at))
            db.execute("DELETE FROM warm_memories WHERE account=? AND room=? AND expires_at<=?",
                       (self.account, room, created_at))
            row = db.execute("SELECT id FROM warm_memories WHERE account=? AND room=? AND period_key=?",
                             (self.account, room, period_key)).fetchone()
            return int(row[0])

    def add_cold_memory(self, room, kind, content):
        """只接收主人明确写入的稳定事实、偏好或边界，不自动抽取私人信息。"""
        if room not in self.groups:
            raise MemberError("这个群尚未启用记忆")
        if kind not in ("事实", "偏好", "边界"):
            raise MemberError("冷记忆类型仅支持 事实、偏好、边界")
        if not isinstance(content, str):
            raise MemberError("冷记忆内容必须是文本")
        content = " ".join(content.replace("\x00", "").split()).strip()
        if not content or len(content) > 240:
            raise MemberError("冷记忆内容应为 1–240 字符")
        if MEMORY_SECRET_RE.search(content):
            raise MemberError("内容包含疑似密钥或身份标识，拒绝保存")
        with self._lock, self._connection() as db:
            existing = db.execute("SELECT id FROM cold_memories WHERE account=? AND room=? AND content=?",
                                  (self.account, room, content)).fetchone()
            if existing:
                return int(existing[0]), False
            count = db.execute("SELECT COUNT(*) FROM cold_memories WHERE account=? AND room=?",
                               (self.account, room)).fetchone()[0]
            if count >= 50:
                raise MemberError("冷记忆已达 50 条上限，请先忘记过期内容")
            created_at = self._now().astimezone(timezone.utc).isoformat()
            cursor = db.execute("INSERT INTO cold_memories(account,room,kind,content,created_at,created_by) "
                                "VALUES(?,?,?,?,?,'owner')",
                                (self.account, room, kind, content, created_at))
            return int(cursor.lastrowid), True

    def forget_cold_memory(self, room, memory_id):
        if room not in self.groups or type(memory_id) is not int or memory_id <= 0:
            raise MemberError("记忆编号无效")
        with self._lock, self._connection() as db:
            cursor = db.execute("DELETE FROM cold_memories WHERE id=? AND account=? AND room=?",
                                (memory_id, self.account, room))
            if cursor.rowcount:
                db.execute("UPDATE chat_buffers SET generation=generation+1 WHERE account=? AND room=?",
                           (self.account, room))
            return cursor.rowcount > 0

    def forget_warm_memory(self, room, memory_id):
        if room not in self.groups or type(memory_id) is not int or memory_id <= 0:
            raise MemberError("记忆编号无效")
        with self._lock, self._connection() as db:
            cursor = db.execute("DELETE FROM warm_memories WHERE id=? AND account=? AND room=?",
                                (memory_id, self.account, room))
            if cursor.rowcount:
                db.execute("UPDATE chat_buffers SET generation=generation+1 WHERE account=? AND room=?",
                           (self.account, room))
            return cursor.rowcount > 0

    def clear_tiered_memories(self, room):
        """清空指定群的温/冷层；不触碰热聊天记录。"""
        if room not in self.groups:
            raise MemberError("这个群尚未启用记忆")
        with self._lock, self._connection() as db:
            warm = db.execute("DELETE FROM warm_memories WHERE account=? AND room=?",
                              (self.account, room)).rowcount
            cold = db.execute("DELETE FROM cold_memories WHERE account=? AND room=?",
                              (self.account, room)).rowcount
            if warm or cold:
                db.execute("UPDATE chat_buffers SET generation=generation+1 WHERE account=? AND room=?",
                           (self.account, room))
            return {"warm": warm, "cold": cold}

    def tiered_memory_context(self, room, query, *, exclude_period=None, warm_limit=3, cold_limit=6):
        """返回有限的温/冷记忆；热层继续由 conversation_snapshot 提供。"""
        if room not in self.groups:
            return {"warm": [], "cold": []}
        if type(warm_limit) is not int or not 0 <= warm_limit <= 6:
            raise MemberError("warm_limit 应为 0–6")
        if type(cold_limit) is not int or not 0 <= cold_limit <= 10:
            raise MemberError("cold_limit 应为 0–10")
        now = self._now().astimezone(timezone.utc).isoformat()
        with self._lock:
            db = self._connection()
            warm_rows = db.execute("""SELECT period_key,summary,message_count FROM warm_memories
                WHERE account=? AND room=? AND expires_at>? AND (? IS NULL OR period_key<>?)
                ORDER BY period_key DESC LIMIT ?""",
                (self.account, room, now, exclude_period, exclude_period, warm_limit)).fetchall()
            cold_rows = db.execute("SELECT id,kind,content,created_at FROM cold_memories "
                                   "WHERE account=? AND room=? ORDER BY id DESC",
                                   (self.account, room)).fetchall()
        query_terms = _memory_terms(query)
        ranked = []
        for row in cold_rows:
            memory_terms = _memory_terms(row["content"])
            score = len(query_terms & memory_terms) if query_terms else 0
            ranked.append((score, row["id"], row))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        selected = [item[2] for item in ranked[:cold_limit]]
        return {
            "warm": [{"period": row["period_key"], "summary": row["summary"],
                      "message_count": row["message_count"]} for row in warm_rows],
            "cold": [{"id": row["id"], "kind": row["kind"], "text": row["content"]}
                     for row in selected],
        }

    def list_tiered_memories(self, room, *, warm_limit=5, cold_limit=20):
        if room not in self.groups:
            raise MemberError("这个群尚未启用记忆")
        if type(warm_limit) is not int or not 0 <= warm_limit <= 20:
            raise MemberError("warm_limit 应为 0–20")
        if type(cold_limit) is not int or not 0 <= cold_limit <= 50:
            raise MemberError("cold_limit 应为 0–50")
        now = self._now().astimezone(timezone.utc).isoformat()
        with self._lock:
            db = self._connection()
            warm = db.execute("""SELECT id,period_key,summary,message_count FROM warm_memories
                WHERE account=? AND room=? AND expires_at>? ORDER BY period_key DESC LIMIT ?""",
                (self.account, room, now, warm_limit)).fetchall()
            cold = db.execute("SELECT id,kind,content FROM cold_memories WHERE account=? AND room=? "
                              "ORDER BY id DESC LIMIT ?", (self.account, room, cold_limit)).fetchall()
        return {"warm": [dict(row) for row in warm], "cold": [dict(row) for row in cold]}

    def close(self):
        with self._lock:
            if self._db:
                self._db.close()
                self._db = None
            self._closed = True
