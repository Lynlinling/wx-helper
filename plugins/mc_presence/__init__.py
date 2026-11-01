"""把 Minecraft 的少量重要事件转成低频群内播报。"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import threading
import time
from urllib.parse import quote, urlsplit
from urllib.error import HTTPError
from urllib.request import Request, urlopen


SIGNIFICANT = {
    "player.connected",
    "player.disconnected",
    "player.death",
    "player.vitals_low",
    "task.completed",
    "task.failed",
    "task.cancelled",
}


CORE_EVENT_CURSOR_SCHEMA = "game-event-cursor-v1"


def _chain_integer(value, field):
    """Accept only JSON integers; bool, float, and numeric strings are invalid."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Core event chain {field} must be an integer")
    return value


def _event_chain_genesis(game_id):
    return hashlib.sha256(f"game-agent-core-event-chain-v1\0{game_id}".encode("utf-8")).hexdigest()


def _advance_event_chain(previous, game_id, event_seq, event):
    payload = {
        "game_id": game_id,
        "event_seq": int(event_seq),
        "id": int(event["id"]),
        "kind": str(event["kind"]),
        "payload": event["payload"],
        "occurred_at": str(event["occurred_at"]),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(bytes.fromhex(previous) + len(encoded).to_bytes(8, "big") + encoded).hexdigest()


class Plugin:
    def on_load(self, ctx):
        self.ctx = ctx
        self.base_url = str(ctx.config.get("base_url", "http://127.0.0.1:8787")).rstrip("/")
        parsed = urlsplit(self.base_url)
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or
                parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("mc_presence.base_url 必须是本机回环 HTTP 地址")
        self.game_id = str(ctx.config.get("game_id", "minecraft:local-test")).strip()
        if not self.game_id or len(self.game_id) > 160 or any(ch in self.game_id for ch in " /\\?#\t\r\n"):
            raise ValueError("mc_presence.game_id 无效")
        groups = ctx.config.get("group_ids", [])
        if not isinstance(groups, list) or not groups or not all(isinstance(x, str) and x.endswith("@chatroom") for x in groups):
            raise ValueError("mc_presence.group_ids 必须明确配置至少一个微信群 ID")
        if any(x not in ctx.members.groups for x in groups):
            raise ValueError("mc_presence.group_ids 必须全部位于成员目录白名单")
        self.groups = tuple(dict.fromkeys(groups))
        self.timeout = float(ctx.config.get("timeout_seconds", 1.5))
        self.poll_seconds = float(ctx.config.get("poll_seconds", 3.0))
        self.cooldown_seconds = float(ctx.config.get("cooldown_seconds", 60.0))
        if not 0.2 <= self.timeout <= 5 or not 1 <= self.poll_seconds <= 60 or not 10 <= self.cooldown_seconds <= 3600:
            raise ValueError("mc_presence 的 timeout/poll/cooldown 超出范围")
        self.stop_event = threading.Event()
        self._checkpoint_path = self._resolve_checkpoint_path()
        self.thread = threading.Thread(target=self._run, name="MinecraftPresence", daemon=True)
        self._seen_ids = set()
        self._last_sent = 0.0
        self.thread.start()

    def on_unload(self, _ctx):
        self.stop_event.set()
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=min(3, self.poll_seconds + 0.5))

    def on_message(self, _message, _ctx):
        return

    def _url(self, path):
        return self.base_url + path

    def _fetch(self, path):
        request = Request(self._url(path), headers={"Accept": "application/json"}, method="GET")
        with urlopen(request, timeout=self.timeout) as response:
            if response.status != 200:
                raise OSError(f"status={response.status}")
            raw = response.read(256 * 1024)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Core 响应不是 JSON 对象")
        return value

    def _events(self, after_id=None):
        encoded = quote(self.game_id, safe="")
        # Omitting after_id selects Core's legacy "recent events" endpoint
        # shape, which has no event-chain proof. An explicit zero requests a
        # verifiable full baseline and keeps all reads on one cursor contract.
        query = f"limit=100&after_id={int(after_id or 0)}"
        page = self._fetch(f"/v1/games/{encoded}/events?{query}")
        response_game_id = page.get("game_id")
        if response_game_id is not None and response_game_id != self.game_id:
            raise ValueError("Core 事件响应的 game_id 与配置不一致")
        return page

    def _state(self):
        encoded = quote(self.game_id, safe="")
        return self._fetch(f"/v1/games/{encoded}/state")

    def _resolve_checkpoint_path(self):
        """Return a per-game cursor file under the bot runtime directory."""
        configured = self.ctx.config.get("cursor_file")
        app_dir = getattr(self.ctx, "app_dir", None)
        if configured:
            path = Path(str(configured))
            if not path.is_absolute() and app_dir:
                path = Path(app_dir) / path
            return path
        if not app_dir:
            # Unit-test contexts and embedders without an application root can
            # keep the historical in-process behavior without writing cwd.
            return None
        token = hashlib.sha256(self.game_id.encode("utf-8")).hexdigest()[:16]
        return Path(app_dir) / ".runtime" / f"mc_presence_{token}.json"

    def _load_checkpoint(self):
        path = getattr(self, "_checkpoint_path", None)
        if path is None:
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if (value.get("schema") != "mc-presence-cursor-v1"
                    or value.get("game_id") != self.game_id):
                return None
            cursor = value.get("event_cursor")
            if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
                return None
            return cursor
        except (OSError, ValueError, TypeError):
            return None

    def _load_chain_checkpoint(self):
        path = getattr(self, "_checkpoint_path", None)
        if path is None:
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if (value.get("schema") != "mc-presence-cursor-v1"
                    or value.get("game_id") != self.game_id):
                return None
            cursor = value.get("event_chain_cursor")
            if not isinstance(cursor, dict) or cursor.get("game_id") != self.game_id:
                return None
            event_id = cursor.get("event_id")
            event_seq = cursor.get("event_seq")
            root = cursor.get("chain_sha256")
            if (isinstance(event_id, bool) or not isinstance(event_id, int) or event_id < 0
                    or isinstance(event_seq, bool) or not isinstance(event_seq, int) or event_seq < 0
                    or not isinstance(root, str) or len(root) != 64
                    or any(char not in "0123456789abcdef" for char in root)):
                return None
            return {"game_id": self.game_id, "event_id": event_id,
                    "event_seq": event_seq, "chain_sha256": root}
        except (OSError, ValueError, TypeError):
            return None

    def _save_checkpoint(self, cursor, chain_cursor=None):
        path = getattr(self, "_checkpoint_path", None)
        if path is None or isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema": "mc-presence-cursor-v1", "game_id": self.game_id,
                   "event_cursor": cursor}
        if isinstance(chain_cursor, dict):
            payload["event_chain_cursor"] = dict(chain_cursor)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def _validate_chain_page(self, page, after_id, previous):
        """Validate Core's additive event chain and return page/end anchors.

        The legacy Core response has no chain metadata and remains accepted
        when no chain checkpoint exists. Once a chain checkpoint is present,
        a missing or contradictory chain is a recoverable cursor boundary and
        must be re-baselined instead of silently broadcasting unverified rows.
        """
        cursor = page.get("event_cursor") if isinstance(page, dict) else None
        if not isinstance(cursor, dict):
            if previous is not None:
                raise ValueError("Core event chain cursor missing")
            return None, {}
        if cursor.get("schema") != CORE_EVENT_CURSOR_SCHEMA or cursor.get("game_id") != self.game_id:
            raise ValueError("Core event chain schema/game mismatch")
        try:
            core_after_id = _chain_integer(cursor.get("after_id"), "after_id")
            core_after_seq = _chain_integer(cursor.get("after_event_seq"), "after_event_seq")
            core_next_seq = _chain_integer(cursor.get("next_event_seq"), "next_event_seq")
            snapshot_count = _chain_integer(cursor.get("snapshot_event_count"), "snapshot_event_count")
            snapshot_high = _chain_integer(cursor.get("snapshot_high_watermark"), "snapshot_high_watermark")
            core_next_id = _chain_integer(page.get("next_cursor"), "next_cursor")
        except (TypeError, ValueError) as error:
            raise ValueError("Core event chain cursor malformed") from error
        high_watermark = page.get("high_watermark")
        if isinstance(high_watermark, bool) or not isinstance(high_watermark, int):
            raise ValueError("Core event chain high_watermark malformed")
        if not isinstance(page.get("has_more"), bool):
            raise ValueError("Core event chain has_more malformed")
        if not isinstance(page.get("events", []), list):
            raise ValueError("Core event chain events list malformed")
        after_root = cursor.get("after_chain_sha256")
        next_root = cursor.get("next_chain_sha256")
        snapshot_root = cursor.get("snapshot_chain_sha256")
        if (core_after_id != int(after_id or 0) or core_next_id < core_after_id
                or not isinstance(after_root, str) or len(after_root) != 64
                or not isinstance(next_root, str) or len(next_root) != 64
                or not isinstance(snapshot_root, str) or len(snapshot_root) != 64
                or snapshot_count < core_next_seq or snapshot_high < core_next_id
                or high_watermark != snapshot_high):
            raise ValueError("Core event chain anchor malformed")
        if previous is None:
            if core_after_id != 0:
                raise ValueError("Core event chain resume anchor was not persisted")
            current_seq, current_root = core_after_seq, after_root
            if core_after_id == 0 and (core_after_seq != 0 or after_root != _event_chain_genesis(self.game_id)):
                raise ValueError("Core event chain genesis anchor mismatch")
        else:
            if (previous.get("event_id") != core_after_id
                    or previous.get("event_seq") != core_after_seq
                    or previous.get("chain_sha256") != after_root):
                raise ValueError("Core event chain start anchor changed")
            current_seq, current_root = core_after_seq, after_root
        anchors = {}
        previous_id = core_after_id
        for raw_event in page.get("events", []):
            if not isinstance(raw_event, dict):
                raise ValueError("Core event chain event malformed")
            try:
                event_id = _chain_integer(raw_event.get("id"), "event.id")
                event_seq = _chain_integer(raw_event.get("event_seq"), "event.event_seq")
            except (TypeError, ValueError) as error:
                raise ValueError("Core event chain event sequence malformed") from error
            if event_id <= previous_id or event_seq != current_seq + 1:
                raise ValueError("Core event chain is not contiguous")
            if not isinstance(raw_event.get("kind"), str) or "payload" not in raw_event or "occurred_at" not in raw_event:
                raise ValueError("Core event chain event missing canonical fields")
            calculated = _advance_event_chain(current_root, self.game_id, event_seq, raw_event)
            if raw_event.get("chain_sha256") != calculated:
                raise ValueError("Core event chain hash mismatch")
            current_seq, current_root, previous_id = event_seq, calculated, event_id
            anchors[event_id] = {"game_id": self.game_id, "event_id": event_id,
                                 "event_seq": event_seq, "chain_sha256": calculated}
        if current_seq != core_next_seq or current_root != next_root or previous_id != core_next_id:
            raise ValueError("Core event chain page end mismatch")
        has_more = bool(page.get("has_more"))
        if (not has_more and (current_seq != snapshot_count or current_root != snapshot_root
                              or core_next_id != snapshot_high)):
            raise ValueError("Core final event page does not reach its snapshot root")
        if has_more and (current_seq >= snapshot_count and current_root != snapshot_root):
            raise ValueError("Core event page passed its snapshot root")
        return {"game_id": self.game_id, "event_id": core_next_id,
                "event_seq": core_next_seq, "chain_sha256": next_root}, anchors

    def _baseline(self, page=None):
        """Consume the current Core snapshot without broadcasting its history.

        A baseline is deliberately established from a fresh ``after_id=0``
        read.  When Core exposes its event chain, every page is checked before
        the final snapshot anchor is persisted.  This makes a stale, reset, or
        tampered event store a safe retry condition rather than a source of
        unverified group messages.
        """
        page = self._events(None) if page is None else page
        previous = None
        last_id = 0
        legacy = False
        while True:
            final_anchor, _anchors = self._validate_chain_page(page, last_id, previous)
            self._seen_ids.update(
                event.get("id") for event in page.get("events", [])
                if isinstance(event, dict) and event.get("id") is not None)
            if final_anchor is None:
                legacy = True
                next_id = int(page.get("next_cursor", page.get("high_watermark", last_id)) or last_id)
                last_id = max(last_id, next_id)
            else:
                previous = final_anchor
                last_id = final_anchor["event_id"]
            if not page.get("has_more"):
                break
            next_page = self._events(last_id)
            if next_page.get("next_cursor") == last_id and not next_page.get("events"):
                raise ValueError("Core baseline pagination made no progress")
            page = next_page
        if legacy:
            self._save_checkpoint(last_id)
            return last_id, None
        # The final page's cursor is the trusted snapshot boundary.  It may be
        # empty when the stream has no events, in which case the Core genesis
        # anchor is returned by _validate_chain_page.
        self._save_checkpoint(last_id, previous)
        return last_id, previous

    @staticmethod
    def _matches_current_world(event, state):
        """Suppress fixture events unless both identities match current online state."""
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        event_fixture = payload.get("fixture_id")
        event_epoch = payload.get("world_epoch")
        data = state.get("data") if isinstance(state.get("data"), dict) else {}
        state_fixture = state.get("fixture_id")
        state_epoch = state.get("world_epoch")
        if not event_fixture and not event_epoch:
            # Legacy, non-fixture events remain supported, but cannot leak into
            # an active fixture episode where state carries a world identity.
            return not state_fixture and not state_epoch
        if not (event_fixture and event_epoch and state_fixture and state_epoch):
            return False
        # A disconnect event is itself the signal that the player is offline.
        # Keep identity/epoch/phase fencing below, but do not require the
        # disconnected player to be online before telling the group about it.
        if not state.get("connected") and event.get("kind") != "player.disconnected":
            return False
        event_phase = payload.get("fixture_phase")
        state_phase = data.get("fixture_phase")
        if event_phase is not None and event_phase != "ready":
            return False
        if state_phase is not None and state_phase != "ready":
            return False
        # A reset-time task fence is useful information for the group even
        # though its task carries the previous epoch. Allow only this explicit
        # safety outcome, and only for the same fixture installation; all other
        # stale events remain suppressed.
        if event_fixture == state_fixture and event_epoch != state_epoch:
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            return event.get("kind") == "task.failed" and error.get("code") == "world_identity_stale"
        # A Paper restart can keep the same fixture/world epoch while issuing
        # a new bot session.  Once both sides expose a session, suppress late
        # events from the previous session.  The disconnect that explains an
        # offline state remains visible even though its session is ending.
        event_session = payload.get("bot_session")
        state_session = data.get("bot_session")
        if (isinstance(event_session, str) and event_session
                and isinstance(state_session, str) and state_session
                and event_session != state_session
                and not (event.get("kind") == "player.disconnected" and not state.get("connected"))):
            return False
        return event_fixture == state_fixture and event_epoch == state_epoch

    @staticmethod
    def _text(event):
        kind = event.get("kind")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if kind == "player.connected":
            return "我上线了，先在服务器里转两圈。"
        if kind == "player.disconnected":
            return "我掉线了，正在尝试重新连回服务器。"
        if kind == "player.death":
            return "刚刚没打过，寄了。我准备重新整理一下。"
        if kind == "player.vitals_low":
            health, food = payload.get("health"), payload.get("food")
            return f"血量有点危险（生命 {health}，饥饿 {food}），我先找安全位置。"
        if kind == "task.completed":
            action = str(payload.get("kind") or "动作")
            result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            if action == "collect":
                block = str(result.get("block") or "资源")[:32]
                count = result.get("collected", result.get("requested", ""))
                return f"刚采集了 {block}（{count} 个），背包已核对。"
            if action == "return_home":
                return "我已经回到家附近，先整理一下。"
            if action == "say":
                return "刚在游戏里说了句话。"
            return f"刚完成了 {action}。"
        if kind == "task.failed":
            action = str(payload.get("kind") or "动作")[:32]
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            code = str(error.get("code") or "未知原因").split(":", 1)[0][:40]
            if code == "world_identity_stale":
                return f"世界刚切换，旧的{action}已安全废止，不会写回新世界。"
            return f"{action} 没完成（{code}），我先停下来检查。"
        if kind == "task.cancelled":
            return f"{str(payload.get('kind') or '动作')[:32]} 已取消。"
        return None

    def _send(self, text):
        now = time.monotonic()
        if now - self._last_sent < self.cooldown_seconds:
            return False
        for group in self.groups:
            self.ctx.send_text(group, text)
        self._last_sent = now
        return True

    def _run(self):
        initialized = False
        checkpoint = self._load_checkpoint()
        chain_cursor = self._load_chain_checkpoint()
        resume = checkpoint is not None
        event_cursor = checkpoint if resume else 0
        if chain_cursor is not None and chain_cursor.get("event_id") != event_cursor:
            # The integer cursor is retained for older readers, but it may
            # never disagree with the cryptographic resume anchor.
            chain_cursor = None
        while not self.stop_event.is_set():
            try:
                current_state = self._state()
                page = self._events(event_cursor if (initialized or resume) else None)
                if not isinstance(page, dict):
                    raise ValueError("Core 事件分页响应不是对象")
                baseline_only = False
                if not initialized and not resume:
                    # Ignore all history that predates plugin startup. Read
                    # every page so the persisted boundary is backed by the
                    # complete Core chain rather than only the first page.
                    event_cursor, chain_cursor = self._baseline(page)
                    initialized = True
                    baseline_only = True
                elif not initialized and resume:
                    watermark = page.get("high_watermark")
                    if (isinstance(watermark, int) and not isinstance(watermark, bool)
                            and watermark < event_cursor):
                        # Core's event store was reset or compacted below the
                        # persisted cursor. Rebuild a verified baseline and do
                        # not replay history from the new store.
                        event_cursor, chain_cursor = self._baseline()
                        self._seen_ids.clear()
                        initialized = True
                        baseline_only = True
                    else:
                        initialized = True

                if not baseline_only:
                    while True:
                        try:
                            page_end, anchors_by_event_id = self._validate_chain_page(
                                page, event_cursor, chain_cursor)
                        except ValueError as chain_error:
                            # A changed/missing anchor, bad page hash, or Core
                            # rollback breaks the incremental proof. Re-read
                            # from genesis and silently establish a fresh
                            # baseline; never broadcast the unverified page.
                            event_cursor, chain_cursor = self._baseline()
                            self._seen_ids.clear()
                            self.ctx.logger.warning(
                                "MC 事件链游标失效，已静默重建基线：%s", chain_error)
                            break
                        events = page.get("events", [])
                        deferred = False
                        for event in events:
                            event_id = event.get("id")
                            if event_id is not None and event_id in self._seen_ids:
                                event_cursor = max(event_cursor, int(event_id))
                                chain_cursor = anchors_by_event_id.get(event_cursor, chain_cursor)
                                self._save_checkpoint(event_cursor, chain_cursor)
                                continue
                            if event.get("kind") not in SIGNIFICANT:
                                if event_id is not None:
                                    self._seen_ids.add(event_id)
                                    event_cursor = max(event_cursor, int(event_id))
                                    chain_cursor = anchors_by_event_id.get(event_cursor, chain_cursor)
                                    self._save_checkpoint(event_cursor, chain_cursor)
                                continue
                            if not self._matches_current_world(event, current_state):
                                if event_id is not None:
                                    self._seen_ids.add(event_id)
                                    event_cursor = max(event_cursor, int(event_id))
                                    chain_cursor = anchors_by_event_id.get(event_cursor, chain_cursor)
                                    self._save_checkpoint(event_cursor, chain_cursor)
                                continue
                            text = self._text(event)
                            if text and self._send(text) is False:
                                # Preserve this event at the cursor boundary;
                                # the next poll retries it after the cooldown.
                                deferred = True
                                break
                            # Mark only after a successful send. If the chat
                            # transport raises, the unchanged page cursor will
                            # make this event eligible for retry next poll.
                            if event_id is not None:
                                self._seen_ids.add(event_id)
                                event_cursor = max(event_cursor, int(event_id))
                                chain_cursor = anchors_by_event_id.get(event_cursor, chain_cursor)
                                self._save_checkpoint(event_cursor, chain_cursor)
                        if deferred:
                            break
                        next_cursor = int(page.get("next_cursor", event_cursor) or event_cursor)
                        event_cursor = max(event_cursor, next_cursor)
                        if page_end is not None:
                            chain_cursor = page_end
                        self._save_checkpoint(event_cursor, chain_cursor)
                        if not page.get("has_more"):
                            break
                        page = self._events(event_cursor)
                    if len(self._seen_ids) > 500:
                        self._seen_ids = set(sorted(self._seen_ids)[-300:])
            except Exception as error:
                self.ctx.logger.warning("MC 主动播报轮询失败：%s", error)
            self.stop_event.wait(self.poll_seconds)
