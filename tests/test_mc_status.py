from plugins.mc_status import Plugin
from wxbot_framework.sdk import Message


class FakeMembers:
    account = "wxid_bot"
    groups = {"room@chatroom"}

    @staticmethod
    def display_name(_sender, _room):
        return "小明"


class FakeContext:
    members = FakeMembers()
    config = {"base_url": "http://127.0.0.1:8787", "game_id": "minecraft:local-test", "timeout_seconds": 1.5}

    def __init__(self):
        self.replies = []
        self.services = {}

    def register_service(self, name, service):
        self.services[name] = service

    def unregister_service(self, name, service=None):
        if self.services.get(name) is service:
            self.services.pop(name, None)

    def reply_at(self, message, text, *, display_name=None):
        self.replies.append((message.sender_id, text, display_name))


def message(text, *, at=True):
    return Message(
        text=text,
        chat_id="room@chatroom",
        sender_id="wxid_friend",
        is_group=True,
        data={"is_at_self": at, "at_members": ("wxid_bot",) if at else ()},
    )


def test_status_query_uses_local_snapshot_and_mentions_the_sender():
    plugin = Plugin()
    ctx = FakeContext()
    plugin.on_load(ctx)
    plugin._fetch = lambda path: {
        "connected": True,
        "health": {"current": 18, "max": 20},
        "hunger": 16,
        "location": {"dimension": "overworld", "x": 1, "y": 64, "z": 2},
        "task": {"name": "收集木头"},
    }

    plugin.on_message(message("@机器人\u2005状态"), ctx)

    assert len(ctx.replies) == 1
    sender, text, display = ctx.replies[0]
    assert sender == "wxid_friend"
    assert "MC 在线" in text and "18/20" in text and "收集木头" in text
    assert display == "小明"


def test_status_query_requires_real_mention_and_allowlisted_group():
    plugin = Plugin()
    ctx = FakeContext()
    plugin.on_load(ctx)
    plugin._fetch = lambda _path: (_ for _ in ()).throw(AssertionError("must not fetch"))

    plugin.on_message(message("@机器人\u2005状态", at=False), ctx)
    plugin.on_message(
        Message(text="@机器人\u2005状态", chat_id="other@chatroom", sender_id="wxid_friend",
                is_group=True, data={"is_at_self": True, "at_members": ("wxid_bot",)}),
        ctx,
    )

    assert ctx.replies == []


def test_status_endpoint_rejects_non_loopback_urls():
    plugin = Plugin()
    ctx = FakeContext()
    ctx.config = {"base_url": "http://example.com", "timeout_seconds": 1.5}

    try:
        plugin.on_load(ctx)
    except ValueError as error:
        assert "回环" in str(error)
    else:
        raise AssertionError("non-loopback URL accepted")


def test_context_returns_compact_state_for_chat():
    plugin = Plugin()
    ctx = FakeContext()
    plugin.on_load(ctx)
    plugin._fetch = lambda path: {
        "connected": True,
        "health": {"current": 20, "max": 20},
        "location": {"dimension": "overworld", "x": 1, "y": 64, "z": 2},
        "inventory": [{"name": "minecraft:oak_log", "count": 3}],
        "state_revision": 7, "fixture_id": "fixture-a", "world_epoch": "epoch-a",
        "data": {"held_item": {"name": "minecraft:stone_pickaxe"}, "time_of_day": 500,
                 "bot_session": "session-a", "fixture_phase": "ready"},
    } if path == "state" else {"memories": [{"kind": "preference", "text": "夜间先回家", "created_at": "2026-01-01T00:00:00+00:00"}]}
    value = plugin.context()
    assert value["connected"] is True
    assert value["inventory"][0]["count"] == 3
    assert value["state_revision"] == 7
    assert value["world_epoch"] == "epoch-a"
    assert value["bot_session"] == "session-a"
    assert value["memories"][0]["text"] == "夜间先回家"
    assert "verify_task_receipt" in value["memories"][0]["provenance"]
    plugin.on_unload(ctx)


def test_memory_command_formats_recent_memory():
    plugin = Plugin()
    ctx = FakeContext()
    plugin.on_load(ctx)
    plugin._fetch = lambda path: {"memories": [{"kind": "plan", "text": "白天收集木头"}]} if path == "memory" else {}

    plugin.on_message(message("@机器人\u2005记忆"), ctx)

    assert len(ctx.replies) == 1
    assert "最近记忆" in ctx.replies[0][1]
    assert "白天收集木头" in ctx.replies[0][1]


def test_unknown_game_id_reports_configuration_mismatch():
    from urllib.error import HTTPError

    plugin = Plugin()
    ctx = FakeContext()
    plugin.on_load(ctx)
    plugin._fetch = lambda _path: (_ for _ in ()).throw(
        HTTPError("http://127.0.0.1/state", 404, "Not Found", {}, None))

    plugin.on_message(message("@机器人 状态"), ctx)

    assert len(ctx.replies) == 1
    assert "minecraft:local-test" in ctx.replies[0][1]
    assert "核对" in ctx.replies[0][1]
