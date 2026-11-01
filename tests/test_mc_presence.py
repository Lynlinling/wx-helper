import json
import hashlib
import copy
import pytest

from plugins.mc_presence import Plugin, _advance_event_chain, _event_chain_genesis


def _chain_snapshot(game_id, raw_events, after_id=0, limit=100):
    """Build a Core-compatible page from a complete immutable event snapshot."""
    genesis = _event_chain_genesis(game_id)
    events = []
    root = genesis
    for sequence, raw in enumerate(raw_events, 1):
        event = {
            "id": sequence,
            "event_seq": sequence,
            "kind": raw["kind"],
            "payload": raw.get("payload", {}),
            "occurred_at": raw.get("occurred_at", f"2026-10-26T00:00:{sequence:02d}Z"),
        }
        root = _advance_event_chain(root, game_id, sequence, event)
        event["chain_sha256"] = root
        events.append(event)
    anchor_event = next((event for event in events if event["id"] == after_id), None)
    if after_id == 0:
        after_sequence, after_root = 0, genesis
    elif anchor_event is not None:
        after_sequence, after_root = anchor_event["event_seq"], anchor_event["chain_sha256"]
    else:
        raise ValueError("after_id is not in snapshot")
    remaining = [event for event in events if event["id"] > after_id]
    selected = remaining[:limit]
    if selected:
        next_id = selected[-1]["id"]
        next_sequence = selected[-1]["event_seq"]
        next_root = selected[-1]["chain_sha256"]
    else:
        next_id, next_sequence, next_root = after_id, after_sequence, after_root
    high_watermark = events[-1]["id"] if events else 0
    return {
        "events": selected,
        "next_cursor": next_id,
        "high_watermark": high_watermark,
        "has_more": len(remaining) > len(selected),
        "event_cursor": {
            "schema": "game-event-cursor-v1",
            "game_id": game_id,
            "after_id": after_id,
            "after_event_seq": after_sequence,
            "after_chain_sha256": after_root,
            "next_event_seq": next_sequence,
            "next_chain_sha256": next_root,
            "snapshot_event_count": len(events),
            "snapshot_high_watermark": high_watermark,
            "snapshot_chain_sha256": root,
        },
    }


class FakeLogger:
    def warning(self, *_args):
        pass


class FakeMembers:
    groups = {"room@chatroom"}


class FakeContext:
    members = FakeMembers()
    config = {
        "base_url": "http://127.0.0.1:8787",
        "game_id": "minecraft:local-test",
        "group_ids": ["room@chatroom"],
        "poll_seconds": 60,
        "cooldown_seconds": 10,
    }
    logger = FakeLogger()

    def __init__(self):
        self.sent = []

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))


def test_presence_sends_only_new_significant_events():
    plugin = Plugin()
    ctx = FakeContext()
    plugin.on_load(ctx)
    plugin.stop_event.set()
    plugin.thread.join(timeout=1)
    plugin._seen_ids = {1}
    plugin._events = lambda: [
        {"id": 1, "kind": "player.connected", "payload": {}},
        {"id": 2, "kind": "player.death", "payload": {}},
        {"id": 3, "kind": "chat.message", "payload": {}},
    ]
    plugin._last_sent = 0
    plugin._send = lambda text: ctx.sent.append(("room@chatroom", text))
    for event in plugin._events():
        if event["id"] not in plugin._seen_ids and event["kind"] in {"player.death"}:
            plugin._send(plugin._text(event))
    assert len(ctx.sent) == 1
    assert "寄了" in ctx.sent[0][1]
    plugin.on_unload(ctx)


def test_task_updates_use_the_verified_action_and_failure_reason():
    plugin = Plugin()
    completed = plugin._text({"kind": "task.completed", "payload": {
        "kind": "collect", "result": {"block": "oak_log", "collected": 2},
    }})
    failed = plugin._text({"kind": "task.failed", "payload": {
        "kind": "collect", "error": {"code": "collect_target_not_found:oak_log"},
    }})
    assert completed == "刚采集了 oak_log（2 个），背包已核对。"
    assert failed == "collect 没完成（collect_target_not_found），我先停下来检查。"


def test_core_event_chain_validates_pages_tampering_anchors_and_empty_incrementals():
    game_id = "minecraft:chain-test"
    plugin = Plugin.__new__(Plugin)
    plugin.game_id = game_id
    snapshot = _chain_snapshot(game_id, [{"kind": "task.failed", "payload": {"kind": "collect"}}])

    end, anchors = plugin._validate_chain_page(snapshot, 0, None)
    assert end == anchors[1]

    empty = _chain_snapshot(game_id, [{"kind": "task.failed", "payload": {"kind": "collect"}}], after_id=1)
    empty_end, empty_anchors = plugin._validate_chain_page(empty, 1, end)
    assert empty["events"] == []
    assert empty_end == end
    assert empty_anchors == {}

    tampered = copy.deepcopy(snapshot)
    tampered["events"][0]["payload"]["kind"] = "place"
    with pytest.raises(ValueError, match="hash mismatch"):
        plugin._validate_chain_page(tampered, 0, None)

    for invalid in (True, 1.0, "1"):
        malformed = copy.deepcopy(snapshot)
        malformed["events"][0]["event_seq"] = invalid
        with pytest.raises(ValueError, match="sequence malformed"):
            plugin._validate_chain_page(malformed, 0, None)

    malformed_cursor = copy.deepcopy(snapshot)
    malformed_cursor["event_cursor"]["after_event_seq"] = "0"
    with pytest.raises(ValueError, match="cursor malformed"):
        plugin._validate_chain_page(malformed_cursor, 0, None)

    malformed_high_watermark = copy.deepcopy(snapshot)
    malformed_high_watermark["high_watermark"] = True
    with pytest.raises(ValueError, match="high_watermark malformed"):
        plugin._validate_chain_page(malformed_high_watermark, 0, None)

    malformed_has_more = copy.deepcopy(snapshot)
    malformed_has_more["has_more"] = "false"
    with pytest.raises(ValueError, match="has_more malformed"):
        plugin._validate_chain_page(malformed_has_more, 0, None)

    with pytest.raises(ValueError, match="start anchor"):
        plugin._validate_chain_page(snapshot, 0, {
            "game_id": game_id,
            "event_id": 1,
            "event_seq": 1,
            "chain_sha256": "0" * 64,
        })


def test_events_explicitly_requests_genesis_anchor_for_a_full_read():
    plugin = Plugin.__new__(Plugin)
    plugin.game_id = "minecraft:chain-test"
    plugin.base_url = "http://127.0.0.1:8788"
    paths = []
    plugin._fetch = lambda path: (paths.append(path) or {"game_id": plugin.game_id})

    plugin._events(None)

    assert paths == ["/v1/games/minecraft%3Achain-test/events?limit=100&after_id=0"]


def test_presence_chain_checkpoint_survives_restart_and_empty_incremental(tmp_path):
    class StopAfterOnePoll:
        def __init__(self):
            self.waits = 0

        def is_set(self):
            return self.waits >= 1

        def wait(self, _seconds):
            self.waits += 1

    game_id = "minecraft:chain-restart"
    history = [
        {"kind": "chat.message", "payload": {}},
        {"kind": "task.failed", "payload": {
            "kind": "collect", "error": {"code": "collect_target_not_found"},
        }},
    ]
    path = tmp_path / "cursor.json"
    seed = Plugin.__new__(Plugin)
    seed.game_id = game_id
    seed._checkpoint_path = path
    first_page = _chain_snapshot(game_id, history, limit=1)
    first_anchor, _ = seed._validate_chain_page(first_page, 0, None)
    seed._save_checkpoint(1, first_anchor)

    def make_plugin():
        plugin = Plugin.__new__(Plugin)
        plugin.ctx = FakeContext()
        plugin.game_id = game_id
        plugin._checkpoint_path = path
        plugin.stop_event = StopAfterOnePoll()
        plugin._seen_ids = set()
        plugin._last_sent = 0.0
        plugin.poll_seconds = 60
        plugin.cooldown_seconds = 10
        plugin._state = lambda: {"connected": True, "data": {}}
        return plugin

    calls = []
    sent = []
    first = make_plugin()
    first._events = lambda after_id=None: (
        calls.append(after_id) or _chain_snapshot(game_id, history, after_id=after_id))
    first._send = lambda text: (sent.append(text) or True)
    first._run()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert calls == [1]
    assert saved["event_cursor"] == 2
    assert saved["event_chain_cursor"]["event_id"] == 2
    assert len(sent) == 1 and "collect_target_not_found" in sent[0]

    second = make_plugin()
    second_sent = []
    second._events = lambda after_id=None: (
        calls.append(after_id) or _chain_snapshot(game_id, history, after_id=after_id))
    second._send = lambda text: (second_sent.append(text) or True)
    second._run()
    assert calls == [1, 2]
    assert second_sent == []
    assert json.loads(path.read_text(encoding="utf-8"))["event_chain_cursor"] == saved["event_chain_cursor"]


def test_presence_never_broadcasts_a_tampered_incremental_page(tmp_path):
    class StopAfterOnePoll:
        def __init__(self):
            self.waits = 0

        def is_set(self):
            return self.waits >= 1

        def wait(self, _seconds):
            self.waits += 1

    game_id = "minecraft:chain-tamper"
    history = [
        {"kind": "world.tick", "payload": {"n": 1}},
        {"kind": "task.failed", "payload": {
            "kind": "collect", "error": {"code": "collect_target_not_found"},
        }},
    ]
    path = tmp_path / "cursor.json"
    plugin = Plugin.__new__(Plugin)
    plugin.game_id = game_id
    plugin._checkpoint_path = path
    first_page = _chain_snapshot(game_id, history, limit=1)
    first_anchor, _ = plugin._validate_chain_page(first_page, 0, None)
    plugin._save_checkpoint(1, first_anchor)

    plugin.ctx = FakeContext()
    plugin.stop_event = StopAfterOnePoll()
    plugin._seen_ids = set()
    plugin._last_sent = 0.0
    plugin.poll_seconds = 60
    plugin.cooldown_seconds = 10
    plugin._state = lambda: {"connected": True, "data": {}}
    valid_incremental = _chain_snapshot(game_id, history, after_id=1)
    tampered_incremental = copy.deepcopy(valid_incremental)
    tampered_incremental["events"][0]["payload"]["error"]["code"] = "forged"
    calls = []

    def events(after_id=None):
        calls.append(after_id)
        if after_id == 1:
            return tampered_incremental
        assert after_id is None
        return _chain_snapshot(game_id, history)

    plugin._events = events
    sent = []
    plugin._send = lambda text: sent.append(text) or True

    plugin._run()

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert calls == [1, None]
    assert sent == []
    assert saved["event_cursor"] == 2
    assert saved["event_chain_cursor"]["event_seq"] == 2
    assert saved["event_chain_cursor"]["chain_sha256"] == valid_incremental["event_cursor"]["next_chain_sha256"]


def test_presence_cooldown_checkpoint_stops_before_deferred_chained_event(tmp_path):
    class StopAfter:
        def __init__(self, limit):
            self.waits = 0
            self.limit = limit

        def is_set(self):
            return self.waits >= self.limit

        def wait(self, _seconds):
            self.waits += 1

    game_id = "minecraft:chain-cooldown"
    history = [
        {"kind": "world.tick", "payload": {"n": 1}},
        {"kind": "task.failed", "payload": {
            "kind": "collect", "error": {"code": "collect_target_not_found"},
        }},
        {"kind": "player.connected", "payload": {}},
    ]
    path = tmp_path / "cursor.json"

    def make_plugin(stop):
        plugin = Plugin.__new__(Plugin)
        plugin.game_id = game_id
        plugin._checkpoint_path = path
        plugin.ctx = FakeContext()
        plugin.stop_event = stop
        plugin._seen_ids = set()
        plugin._last_sent = 0.0
        plugin.poll_seconds = 60
        plugin.cooldown_seconds = 10
        plugin._state = lambda: {"connected": True, "data": {}}
        return plugin

    first = make_plugin(StopAfter(2))
    calls = []
    first_pages = {
        None: _chain_snapshot(game_id, history[:1]),
        1: _chain_snapshot(game_id, history, after_id=1),
    }
    first._events = lambda after_id=None: (calls.append(after_id) or first_pages[after_id])
    sent = []
    attempts = 0

    def send(text):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            return False
        sent.append(text)
        return True

    first._send = send
    first._run()

    checkpoint = json.loads(path.read_text(encoding="utf-8"))
    assert calls == [None, 1]
    assert checkpoint["event_cursor"] == 2
    assert checkpoint["event_chain_cursor"]["event_id"] == 2
    assert sent == ["collect 没完成（collect_target_not_found），我先停下来检查。"]

    second = make_plugin(StopAfter(1))
    second._events = lambda after_id=None: (
        calls.append(after_id) or _chain_snapshot(game_id, history, after_id=after_id))
    second_sent = []
    second._send = lambda text: second_sent.append(text) or True
    second._run()

    assert calls == [None, 1, 2]
    assert second_sent == ["我上线了，先在服务器里转两圈。"]
    resumed = json.loads(path.read_text(encoding="utf-8"))
    assert resumed["event_cursor"] == 3
    assert resumed["event_chain_cursor"]["event_id"] == 3


def test_fixture_presence_rejects_stale_or_unidentified_events():
    current = {
        "connected": True,
        "fixture_id": "fixture-a",
        "world_epoch": "epoch-2",
        "data": {"fixture_phase": "ready"},
    }
    fresh = {"payload": {
        "fixture_id": "fixture-a", "world_epoch": "epoch-2", "fixture_phase": "ready",
    }}
    stale = {"payload": {
        "fixture_id": "fixture-a", "world_epoch": "epoch-1", "fixture_phase": "ready",
    }}
    legacy = {"payload": {}}
    assert Plugin._matches_current_world(fresh, current)
    assert not Plugin._matches_current_world(stale, current)
    assert not Plugin._matches_current_world(legacy, current)


def test_fixture_presence_reports_explicit_epoch_fence_but_not_other_stale_events():
    current = {
        "connected": True,
        "fixture_id": "fixture-a",
        "world_epoch": "epoch-2",
        "data": {"fixture_phase": "ready"},
    }
    fenced = {"kind": "task.failed", "payload": {
        "fixture_id": "fixture-a", "world_epoch": "epoch-1",
        "fixture_phase": "ready", "kind": "collect",
        "error": {"code": "world_identity_stale"},
    }}
    other_failure = {"kind": "task.failed", "payload": {
        "fixture_id": "fixture-a", "world_epoch": "epoch-1",
        "fixture_phase": "ready", "kind": "collect",
        "error": {"code": "collect_target_not_found"},
    }}
    assert Plugin._matches_current_world(fenced, current)
    assert not Plugin._matches_current_world(other_failure, current)
    assert Plugin._text(fenced) == "世界刚切换，旧的collect已安全废止，不会写回新世界。"


def test_fixture_presence_keeps_explicit_epoch_fence_from_previous_bot_session():
    current = {
        "connected": True,
        "fixture_id": "fixture-a",
        "world_epoch": "epoch-2",
        "data": {"fixture_phase": "ready", "bot_session": "session-new"},
    }
    fenced = {"kind": "task.failed", "payload": {
        "fixture_id": "fixture-a", "world_epoch": "epoch-1",
        "fixture_phase": "ready", "bot_session": "session-old",
        "kind": "collect", "error": {"code": "world_identity_stale"},
    }}
    assert Plugin._matches_current_world(fenced, current)


def test_fixture_presence_keeps_same_epoch_disconnect_visible_when_state_is_offline():
    current = {
        "connected": False,
        "fixture_id": "fixture-a",
        "world_epoch": "epoch-2",
        "data": {"fixture_phase": "ready"},
    }
    disconnected = {"kind": "player.disconnected", "payload": {
        "fixture_id": "fixture-a", "world_epoch": "epoch-2",
        "fixture_phase": "ready", "reason": "socketClosed",
    }}
    assert Plugin._matches_current_world(disconnected, current)


def test_fixture_presence_rejects_late_same_epoch_old_session_events():
    current = {
        "connected": True,
        "fixture_id": "fixture-a",
        "world_epoch": "epoch-2",
        "data": {"fixture_phase": "ready", "bot_session": "session-new"},
    }
    late_connected = {"kind": "player.connected", "payload": {
        "fixture_id": "fixture-a", "world_epoch": "epoch-2",
        "fixture_phase": "ready", "bot_session": "session-old",
    }}
    late_task = {"kind": "task.completed", "payload": {
        "fixture_id": "fixture-a", "world_epoch": "epoch-2",
        "fixture_phase": "ready", "bot_session": "session-old",
    }}
    assert not Plugin._matches_current_world(late_connected, current)
    assert not Plugin._matches_current_world(late_task, current)


def test_fixture_presence_allows_old_session_disconnect_while_offline():
    current = {
        "connected": False,
        "fixture_id": "fixture-a",
        "world_epoch": "epoch-2",
        "data": {"fixture_phase": "ready", "bot_session": "session-new"},
    }
    disconnect = {"kind": "player.disconnected", "payload": {
        "fixture_id": "fixture-a", "world_epoch": "epoch-2",
        "fixture_phase": "ready", "bot_session": "session-old",
    }}
    assert Plugin._matches_current_world(disconnect, current)


def test_presence_polls_disconnect_event_after_restart_while_game_is_offline(tmp_path):
    class StopAfterOnePoll:
        def __init__(self):
            self.waits = 0

        def is_set(self):
            return self.waits >= 1

        def wait(self, _seconds):
            self.waits += 1

    ctx = FakeContext()
    plugin = Plugin.__new__(Plugin)
    plugin.ctx = ctx
    plugin.game_id = "minecraft:local-test"
    plugin.groups = ("room@chatroom",)
    plugin._checkpoint_path = tmp_path / "cursor.json"
    plugin._checkpoint_path.write_text(json.dumps({
        "schema": "mc-presence-cursor-v1",
        "game_id": plugin.game_id,
        "event_cursor": 40,
    }), encoding="utf-8")
    plugin.stop_event = StopAfterOnePoll()
    plugin._seen_ids = set()
    plugin._last_sent = 0.0
    plugin.cooldown_seconds = 10
    plugin.poll_seconds = 60
    plugin._state = lambda: {
        "connected": False,
        "fixture_id": "fixture-a",
        "world_epoch": "epoch-2",
        "data": {"fixture_phase": "ready"},
    }
    event = {"id": 41, "kind": "player.disconnected", "payload": {
        "fixture_id": "fixture-a", "world_epoch": "epoch-2",
        "fixture_phase": "ready", "reason": "socketClosed",
    }}
    plugin._events = lambda after_id=None: {
        "events": [event] if after_id == 40 else [],
        "next_cursor": 41 if after_id == 40 else 40,
        "high_watermark": 41 if after_id == 40 else 40,
        "has_more": False,
    }

    plugin._run()

    assert ctx.sent == [("room@chatroom", "我掉线了，正在尝试重新连回服务器。")]
    assert plugin._seen_ids == {41}


def test_non_fixture_presence_keeps_legacy_events_compatible():
    assert Plugin._matches_current_world(
        {"payload": {}}, {"connected": True, "data": {}}
    )


def test_polling_run_sends_only_explicit_fence_from_stale_epoch():
    """Exercise the real two-poll path without sending to a real WeChat room."""
    plugin = Plugin.__new__(Plugin)
    plugin.stop_event = type("StopAfterTwoPolls", (), {
        "__init__": lambda self: setattr(self, "waits", 0),
        "is_set": lambda self: self.waits >= 2,
        "wait": lambda self, _seconds: setattr(self, "waits", self.waits + 1),
    })()
    plugin._seen_ids = set()
    plugin._last_sent = 0.0
    plugin.poll_seconds = 60
    plugin.cooldown_seconds = 10
    plugin.ctx = FakeContext()
    current = {
        "connected": True,
        "fixture_id": "fixture-a",
        "world_epoch": "epoch-2",
        "data": {"fixture_phase": "ready"},
    }
    initial = {"events": [{"id": 10, "kind": "task.updated", "payload": {}}],
               "next_cursor": 10, "high_watermark": 10, "has_more": False}
    page_one = {"events": [{"id": 11, "kind": "task.failed", "payload": {
        "fixture_id": "fixture-a", "world_epoch": "epoch-1",
        "fixture_phase": "ready", "kind": "collect",
        "error": {"code": "world_identity_stale"},
    }}], "next_cursor": 11, "high_watermark": 12, "has_more": True}
    page_two = {"events": [{"id": 12, "kind": "task.failed", "payload": {
        "fixture_id": "fixture-a", "world_epoch": "epoch-1",
        "fixture_phase": "ready", "kind": "collect",
        "error": {"code": "collect_target_not_found"},
    }}], "next_cursor": 12, "high_watermark": 12, "has_more": False}
    pages = iter([initial, page_one, page_two])
    calls = []
    def events(after_id=None):
        calls.append(after_id)
        return next(pages)
    plugin._events = events
    plugin._state = lambda: current
    sent = []
    plugin._send = sent.append

    plugin._run()

    assert sent == ["世界刚切换，旧的collect已安全废止，不会写回新世界。"]
    assert plugin._seen_ids == {10, 11, 12}
    assert calls == [None, 10, 11]


def test_polling_run_retries_event_when_chat_transport_fails():
    plugin = Plugin.__new__(Plugin)
    plugin.stop_event = type("StopAfterThreePolls", (), {
        "__init__": lambda self: setattr(self, "waits", 0),
        "is_set": lambda self: self.waits >= 3,
        "wait": lambda self, _seconds: setattr(self, "waits", self.waits + 1),
    })()
    plugin._seen_ids = set()
    plugin._last_sent = 0.0
    plugin.poll_seconds = 60
    plugin.cooldown_seconds = 10
    plugin.ctx = FakeContext()
    plugin._state = lambda: {"connected": True, "data": {}}
    initial = {"events": [], "next_cursor": 20, "high_watermark": 20, "has_more": False}
    event = {"id": 21, "kind": "task.failed", "payload": {
        "kind": "collect", "error": {"code": "transient"},
    }}
    backlog = {"events": [event], "next_cursor": 21, "high_watermark": 21, "has_more": False}
    calls = []
    def events(after_id=None):
        calls.append(after_id)
        return initial if after_id is None else backlog
    plugin._events = events
    sent = []
    attempts = 0
    def send(text):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary chat transport failure")
        sent.append(text)
    plugin._send = send

    plugin._run()

    assert len(sent) == 1
    assert "collect 没完成" in sent[0]
    assert plugin._seen_ids == {21}
    assert calls == [None, 20, 20]


def test_polling_run_preserves_events_deferred_by_cooldown():
    plugin = Plugin.__new__(Plugin)
    plugin.stop_event = type("StopAfterThreePolls", (), {
        "__init__": lambda self: setattr(self, "waits", 0),
        "is_set": lambda self: self.waits >= 3,
        "wait": lambda self, _seconds: setattr(self, "waits", self.waits + 1),
    })()
    plugin._seen_ids = set()
    plugin._last_sent = 0.0
    plugin.poll_seconds = 60
    plugin.cooldown_seconds = 10
    plugin.ctx = FakeContext()
    plugin._state = lambda: {"connected": True, "data": {}}
    initial = {"events": [], "next_cursor": 30, "high_watermark": 30, "has_more": False}
    first = {"id": 31, "kind": "player.death", "payload": {}}
    deferred = {"id": 32, "kind": "player.connected", "payload": {}}
    page = {"events": [first, deferred], "next_cursor": 32, "high_watermark": 32, "has_more": False}
    retry_page = {"events": [deferred], "next_cursor": 32, "high_watermark": 32, "has_more": False}
    pages = iter([initial, page, retry_page])
    calls = []
    def events(after_id=None):
        calls.append(after_id)
        return next(pages)
    plugin._events = events
    sent = []
    attempts = 0
    def send(text):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            return False
        sent.append(text)
        return True
    plugin._send = send

    plugin._run()

    assert sent == ["刚刚没打过，寄了。我准备重新整理一下。", "我上线了，先在服务器里转两圈。"]
    assert plugin._seen_ids == {31, 32}
    assert calls == [None, 30, 31]


def test_presence_cursor_resumes_after_plugin_restart(tmp_path):
    class StopAfter:
        def __init__(self, limit):
            self.waits = 0
            self.limit = limit

        def is_set(self):
            return self.waits >= self.limit

        def wait(self, _seconds):
            self.waits += 1

    def make_plugin(ctx, stop):
        plugin = Plugin.__new__(Plugin)
        plugin.ctx = ctx
        plugin.game_id = "minecraft:local-test"
        plugin._checkpoint_path = tmp_path / "cursor.json"
        plugin.stop_event = stop
        plugin._seen_ids = set()
        plugin._last_sent = 0.0
        plugin.poll_seconds = 60
        plugin.cooldown_seconds = 10
        return plugin

    current = {"connected": True, "data": {}}
    initial = {"events": [], "next_cursor": 10, "high_watermark": 10, "has_more": False}
    first_event = {"id": 11, "kind": "task.failed", "payload": {
        "kind": "collect", "error": {"code": "first"},
    }}
    second_event = {"id": 12, "kind": "task.failed", "payload": {
        "kind": "collect", "error": {"code": "second"},
    }}

    ctx = FakeContext()
    ctx.app_dir = tmp_path
    first = make_plugin(ctx, StopAfter(2))
    calls = []
    pages = iter([initial, {"events": [first_event], "next_cursor": 11,
                           "high_watermark": 11, "has_more": False}])
    first._state = lambda: current
    first._events = lambda after_id=None: (calls.append(after_id) or next(pages))
    sent_first = []
    first._send = lambda text: (sent_first.append(text) or True)
    first._run()
    assert sent_first and "first" in sent_first[0]
    assert json.loads((tmp_path / "cursor.json").read_text(encoding="utf-8"))["event_cursor"] == 11

    second = make_plugin(ctx, StopAfter(2))
    second_pages = iter([{"events": [second_event], "next_cursor": 12,
                          "high_watermark": 12, "has_more": False}])
    second._state = lambda: current
    second._events = lambda after_id=None: (calls.append(after_id) or next(second_pages))
    sent_second = []
    second._send = lambda text: (sent_second.append(text) or True)
    second._run()
    assert len(sent_second) == 1
    assert "second" in sent_second[0]
    assert calls[-2] == 11


def test_presence_cursor_default_path_is_isolated_per_game(tmp_path):
    ctx = FakeContext()
    ctx.app_dir = tmp_path
    ctx.config = {**FakeContext.config, "game_id": "minecraft:server-a"}
    first = Plugin.__new__(Plugin)
    first.ctx = ctx
    first.game_id = "minecraft:server-a"
    path_a = first._resolve_checkpoint_path()

    second = Plugin.__new__(Plugin)
    second.ctx = ctx
    second.game_id = "minecraft:server-b"
    path_b = second._resolve_checkpoint_path()

    token = hashlib.sha256(b"minecraft:server-a").hexdigest()[:16]
    assert path_a == tmp_path / ".runtime" / f"mc_presence_{token}.json"
    assert path_a != path_b


def test_presence_cursor_relative_override_is_anchored_to_bot_root(tmp_path):
    ctx = FakeContext()
    ctx.app_dir = tmp_path
    ctx.config = {**FakeContext.config, "cursor_file": "state/presence.json"}
    plugin = Plugin.__new__(Plugin)
    plugin.ctx = ctx
    plugin.game_id = "minecraft:local-test"
    assert plugin._resolve_checkpoint_path() == tmp_path / "state" / "presence.json"


def test_presence_rebaselines_when_core_event_watermark_rolls_back(tmp_path):
    class StopAfter:
        def __init__(self):
            self.waits = 0

        def is_set(self):
            return self.waits >= 1

        def wait(self, _seconds):
            self.waits += 1

    ctx = FakeContext()
    ctx.app_dir = tmp_path
    plugin = Plugin.__new__(Plugin)
    plugin.ctx = ctx
    plugin.game_id = "minecraft:local-test"
    plugin._checkpoint_path = tmp_path / "cursor.json"
    plugin._checkpoint_path.write_text(json.dumps({
        "schema": "mc-presence-cursor-v1",
        "game_id": plugin.game_id,
        "event_cursor": 50,
    }), encoding="utf-8")
    plugin.stop_event = StopAfter()
    plugin._seen_ids = set()
    plugin._last_sent = 0.0
    plugin.poll_seconds = 60
    plugin.cooldown_seconds = 10
    plugin._state = lambda: {"connected": True, "data": {}}
    calls = []
    pages = iter([
        {"events": [], "next_cursor": 2, "high_watermark": 2, "has_more": False},
        {"events": [{"id": 2, "kind": "task.failed", "payload": {
            "kind": "collect", "error": {"code": "old-history"},
        }}], "next_cursor": 2, "high_watermark": 2, "has_more": False},
    ])
    plugin._events = lambda after_id=None: (calls.append(after_id) or next(pages))
    sent = []
    plugin._send = lambda text: sent.append(text) or True

    plugin._run()

    assert sent == []
    assert calls == [50, None]
    assert json.loads(plugin._checkpoint_path.read_text(encoding="utf-8"))["event_cursor"] == 2
