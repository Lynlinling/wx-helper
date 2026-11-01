"""HTTP contract tests for the wx-helper Minecraft plugins.

The test server mirrors Agent Core's JSON/URL contract but never connects to a
Minecraft server or a WeChat transport.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from urllib.parse import parse_qs, unquote, urlsplit

from plugins.mc_control import Plugin as ControlPlugin
from plugins.mc_presence import Plugin as PresencePlugin
from plugins.mc_status import Plugin as StatusPlugin
from wxbot_framework.sdk import Message


GAME_ID = "minecraft:cyber-fixture"
FIXTURE_ID = "fixture-contract-test"
WORLD_EPOCH = "epoch-contract-test"


class CoreContract:
    def __init__(self):
        self.tasks = []
        self.state = {
            "connected": True,
            "health": {"current": 18, "max": 20},
            "hunger": 16,
            "location": {"dimension": "overworld", "x": 2, "y": 66, "z": -3},
            "inventory": [{"name": "minecraft:oak_log", "count": 2}],
            "task": {"id": None, "name": None, "status": "idle", "detail": None},
            "state_revision": 41,
            "fixture_id": FIXTURE_ID,
            "world_epoch": WORLD_EPOCH,
            "data": {
                "bot_session": "session-contract-test",
                "fixture_phase": "ready",
                "session_identity_stale": False,
                "held_item": {"name": "minecraft:stone_axe"},
                "time_of_day": 6000,
            },
        }
        self.events = [{
            "id": 72,
            "kind": "task.completed",
            "payload": {
                "kind": "eat",
                "fixture_id": FIXTURE_ID,
                "world_epoch": WORLD_EPOCH,
                "fixture_phase": "ready",
                "result": {"action": "eat", "item": "cooked_beef"},
            },
            "occurred_at": "2026-10-25T00:00:00Z",
        }]


class FakeMembers:
    account = "wxid-bot"
    groups = {"room@chatroom"}

    @staticmethod
    def display_name(_sender, _room):
        return "测试成员"


class FakeContext:
    members = FakeMembers()

    def __init__(self, base_url):
        self.config = {"base_url": base_url, "game_id": GAME_ID, "timeout_seconds": 1}
        self.services = {}
        self.replies = []

    def register_service(self, name, value):
        self.services[name] = value

    def unregister_service(self, name, value=None):
        if self.services.get(name) is value:
            del self.services[name]

    def reply_at(self, message, text, *, display_name=None):
        self.replies.append((message.sender_id, text, display_name))


def start_core_contract():
    core = CoreContract()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _send(self, status, value):
            payload = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _game(self):
            parts = urlsplit(self.path).path.strip("/").split("/")
            return unquote(parts[2]) if len(parts) > 2 and parts[0:2] == ["v1", "games"] else ""

        def do_GET(self):
            game_id = self._game()
            if game_id != GAME_ID:
                self._send(404, {"error": "game_not_found", "game_id": game_id})
                return
            path = urlsplit(self.path).path
            if path.endswith("/state"):
                self._send(200, core.state)
            elif path.endswith("/memory"):
                self._send(200, {"memories": [{"kind": "strategy", "text": "夜间优先回家"}]})
            elif path.endswith("/events"):
                query = parse_qs(urlsplit(self.path).query)
                after_id = int(query.get("after_id", ["0"])[0])
                events = [event for event in core.events if event["id"] > after_id]
                self._send(200, {
                    "game_id": GAME_ID,
                    "events": events,
                    "next_cursor": events[-1]["id"] if events else after_id,
                    "high_watermark": core.events[-1]["id"] if core.events else 0,
                    "has_more": False,
                })
            else:
                self._send(404, {"error": "not_found"})

        def do_POST(self):
            if self._game() != GAME_ID or not urlsplit(self.path).path.endswith("/tasks"):
                self._send(404, {"error": "game_not_found"})
                return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            core.tasks.append(body)
            self._send(202, {"task": {"task_id": "contract-task"}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return core, server, thread, f"http://127.0.0.1:{server.server_port}"


def test_status_presence_and_control_share_core_http_identity_contract():
    core, server, thread, base_url = start_core_contract()
    try:
        ctx = FakeContext(base_url)

        status = StatusPlugin()
        status.on_load(ctx)
        snapshot = status.context()
        assert snapshot["game_id"] == GAME_ID
        assert snapshot["fixture_id"] == FIXTURE_ID
        assert snapshot["world_epoch"] == WORLD_EPOCH
        assert snapshot["bot_session"] == "session-contract-test"
        assert snapshot["fixture_phase"] == "ready"
        assert snapshot["memories"][0]["text"] == "夜间优先回家"

        presence = PresencePlugin.__new__(PresencePlugin)
        presence.base_url = base_url
        presence.game_id = GAME_ID
        presence.timeout = 1
        state = presence._state()
        page = presence._events(after_id=71)
        assert page["game_id"] == GAME_ID
        assert len(page["events"]) == 1
        assert PresencePlugin._matches_current_world(page["events"][0], state)

        control = ControlPlugin.__new__(ControlPlugin)
        control.base_url = base_url
        control.game_id = GAME_ID
        control.timeout = 1
        message = Message(text="/mc 走 3 66 -3", chat_id="room@chatroom", sender_id="wxid-user",
                          is_group=True, data={}, sequence=9)
        result = control._create({"kind": "move_to", "request": {"x": 3, "y": 66, "z": -3}}, message)
        assert result["task"]["task_id"] == "contract-task"
        assert len(core.tasks) == 1
        assert core.tasks[0]["expected_state_revision"] == 41
        assert core.tasks[0]["expected_fixture_id"] == FIXTURE_ID
        assert core.tasks[0]["expected_world_epoch"] == WORLD_EPOCH
        assert core.tasks[0]["preconditions"]["connected"] is True
        status.on_unload(ctx)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_presence_rejects_event_from_previous_epoch_over_http():
    core, server, thread, base_url = start_core_contract()
    try:
        core.events[0]["payload"]["world_epoch"] = "old-epoch"
        presence = PresencePlugin.__new__(PresencePlugin)
        presence.base_url = base_url
        presence.game_id = GAME_ID
        presence.timeout = 1
        page = presence._events(after_id=71)
        state = presence._state()
        assert not PresencePlugin._matches_current_world(page["events"][0], state)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
