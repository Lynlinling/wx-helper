from plugins.mc_control import Plugin
from wxbot_framework.sdk import Message


def test_parse_accepts_only_bounded_structured_actions():
    plugin = object.__new__(Plugin)
    assert plugin._parse("/mc 走 1 64 -2")["kind"] == "move_to"
    assert plugin._parse("mc 采集 oak_log 4")["request"]["count"] == 4
    assert plugin._parse("mc 回家 0 64 0")["kind"] == "return_home"
    try:
        plugin._parse("mc 运行 powershell.exe")
    except ValueError as error:
        assert "只支持" in str(error)
    else:
        raise AssertionError("arbitrary action accepted")


def test_parse_rejects_unbounded_coordinates_and_chat():
    plugin = object.__new__(Plugin)
    try:
        plugin._parse("mc 走 999999999 64 0")
    except ValueError as error:
        assert "范围" in str(error)
    else:
        raise AssertionError("unbounded coordinate accepted")
    try:
        plugin._parse("mc 说 " + "x" * 201)
    except ValueError as error:
        assert "200" in str(error)
    else:
        raise AssertionError("oversized chat accepted")


def test_create_binds_task_to_current_state_identity_and_revision(monkeypatch):
    plugin = object.__new__(Plugin)
    plugin.game_id = "minecraft:fixture"
    plugin.base_url = "http://127.0.0.1:8787"
    plugin.timeout = 1.5
    plugin._fetch_state = lambda _encoded: {
        "connected": True, "state_revision": 12,
        "fixture_id": "fixture-a", "world_epoch": "epoch-a",
        "data": {"fixture_phase": "ready", "session_identity_stale": False},
    }
    captured = {}

    class Response:
        status = 202

        @staticmethod
        def read(_limit):
            return b'{"task":{"task_id":"t-1"}}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return Response()

    import json
    monkeypatch.setattr("plugins.mc_control.urlopen", fake_urlopen)
    message = Message(text="mc 走 1 64 2", chat_id="room@chatroom", sender_id="wxid",
                      is_group=True, data={}, sequence=3)
    result = plugin._create({"kind": "move_to", "request": {"x": 1, "y": 64, "z": 2}}, message)
    assert result["task"]["task_id"] == "t-1"
    assert captured["body"]["expected_state_revision"] == 12
    assert captured["body"]["expected_fixture_id"] == "fixture-a"
    assert captured["body"]["expected_world_epoch"] == "epoch-a"
    assert captured["body"]["preconditions"]["connected"] is True


def test_create_refuses_partial_or_non_ready_fixture_identity(monkeypatch):
    import pytest

    plugin = object.__new__(Plugin)
    plugin.game_id = "minecraft:fixture"
    plugin.base_url = "http://127.0.0.1:8787"
    plugin.timeout = 1.5
    message = Message(text="mc 走 1 64 2", chat_id="room@chatroom", sender_id="wxid",
                      is_group=True, data={}, sequence=4)
    task = {"kind": "move_to", "request": {"x": 1, "y": 64, "z": 2}}
    monkeypatch.setattr("plugins.mc_control.urlopen",
                        lambda *_args, **_kwargs: pytest.fail("must not POST an unsafe task"))

    for state in (
        {"connected": True, "fixture_id": "fixture-a", "world_epoch": None},
        {"connected": True, "fixture_id": "fixture-a", "world_epoch": "epoch-a",
         "data": {"fixture_phase": "resetting"}},
        {"connected": True, "fixture_id": "fixture-a", "world_epoch": "epoch-a",
         "data": {"fixture_phase": "ready", "session_identity_stale": True}},
    ):
        plugin._fetch_state = lambda _encoded, state=state: state
        with pytest.raises(ValueError):
            plugin._create(task, message)


def test_create_refuses_legacy_identity_by_default(monkeypatch):
    import pytest

    plugin = object.__new__(Plugin)
    plugin.game_id = "minecraft:legacy"
    plugin.base_url = "http://127.0.0.1:8787"
    plugin.timeout = 1.5
    plugin._fetch_state = lambda _encoded: {"connected": True, "state_revision": 2}
    monkeypatch.setattr("plugins.mc_control.urlopen",
                        lambda *_args, **_kwargs: pytest.fail("must not POST without identity"))
    message = Message(text="mc 走 1 64 2", chat_id="room@chatroom", sender_id="wxid",
                      is_group=True, data={}, sequence=5)
    with pytest.raises(ValueError, match="fixture 身份"):
        plugin._create({"kind": "move_to", "request": {"x": 1, "y": 64, "z": 2}}, message)
