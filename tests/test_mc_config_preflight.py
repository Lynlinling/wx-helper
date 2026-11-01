import json
from urllib.error import HTTPError

from tools.check_mc_config import check_config


GAME = "minecraft:cyber-fixture"


def base_config():
    return {"plugins": {
        "mc_status": {"enabled": True, "base_url": "http://127.0.0.1:8788", "game_id": GAME},
        "mc_presence": {"enabled": True, "base_url": "http://127.0.0.1:8788", "game_id": GAME},
        "mc_control": {"enabled": True, "base_url": "http://127.0.0.1:8788", "game_id": GAME},
    }}


def ready_state():
    return {"connected": True, "fixture_id": "fixture-a", "world_epoch": "epoch-a",
            "data": {"fixture_phase": "ready", "session_identity_stale": False}}


def test_preflight_accepts_ready_fixture_and_event_contract():
    config = base_config()

    def fetch(url, _timeout):
        if url.endswith("/v1/games"):
            return {"games": [{"id": GAME}]}
        if url.endswith("/state"):
            return ready_state()
        assert "/events?" in url
        return {"game_id": GAME, "events": []}

    report = check_config(config, fetch=fetch)
    assert report["ok"] is True
    assert report["state"]["fixture_phase"] == "ready"
    assert report["errors"] == []


def test_preflight_reports_unknown_game_id_without_printing_secrets():
    config = base_config()
    requests = []

    def fetch(_url, _timeout):
        requests.append(_url)
        if _url.endswith("/v1/games"):
            return {"games": [{"id": "minecraft:other"}]}
        raise HTTPError("http://127.0.0.1/state", 404, "Not Found", {}, None)

    report = check_config(config, fetch=fetch)
    assert report["ok"] is False
    assert "未登记 game_id" in report["errors"][0]
    assert report["state"]["registered"] is False
    assert len(requests) == 1
    assert "secret" not in json.dumps(report, ensure_ascii=False).lower()


def test_preflight_reports_registered_but_disconnected_legacy_game():
    config = base_config()
    config["plugins"]["mc_presence"]["enabled"] = False
    config["plugins"]["mc_control"]["enabled"] = False

    def fetch(url, _timeout):
        if url.endswith("/v1/games"):
            return {"games": [{"id": GAME}]}
        return {"connected": False, "data": {}}

    report = check_config(config, fetch=fetch)
    assert report["ok"] is False
    assert report["state"]["registered"] is True
    assert report["state"]["connected"] is False
    assert "Core 状态显示 bot 未连接" in report["errors"]


def test_preflight_rejects_mismatched_plugin_bindings():
    config = base_config()
    config["plugins"]["mc_control"]["game_id"] = "minecraft:other"
    report = check_config(config, fetch=lambda url, _timeout: (
        {"games": [{"id": GAME}]} if url.endswith("/v1/games") else ready_state()))
    assert report["ok"] is False
    assert any("同一个" in item for item in report["errors"])


def test_preflight_rejects_partial_identity_and_not_ready_state():
    config = base_config()

    def fetch(_url, _timeout):
        if _url.endswith("/v1/games"):
            return {"games": [{"id": GAME}]}
        return {"connected": True, "fixture_id": "fixture-a", "world_epoch": None,
                "data": {"fixture_phase": "resetting", "session_identity_stale": False}}

    report = check_config(config, fetch=fetch)
    assert report["ok"] is False
    assert any("同时提供" in item for item in report["errors"])


def test_preflight_allows_explicit_legacy_control_compatibility():
    config = base_config()
    config["plugins"]["mc_control"]["allow_legacy_identity"] = True

    def fetch(url, _timeout):
        if url.endswith("/v1/games"):
            return {"games": [{"id": GAME}]}
        if url.endswith("/state"):
            return {"connected": True, "data": {}}
        return {"game_id": GAME, "events": []}

    report = check_config(config, fetch=fetch)
    assert report["ok"] is True
    assert any("legacy" in item for item in report["warnings"])
