"""只读检查 wx-helper 的 Minecraft 插件是否绑定到可用的 Agent Core。

该工具只发 loopback GET 请求，不创建任务、不发送微信消息，也不读取密钥。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


MC_PLUGINS = ("mc_status", "mc_presence", "mc_control")


def _valid_loopback(base_url: str) -> bool:
    parsed = urlsplit(base_url)
    return (
        parsed.scheme == "http"
        and parsed.hostname in ("127.0.0.1", "localhost", "::1")
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


def _default_fetch(url: str, timeout: float):
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    with urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise URLError(f"status={response.status}")
        value = json.loads(response.read(256 * 1024).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Core 响应不是 JSON 对象")
    return value


def _state_url(base_url: str, game_id: str) -> str:
    return f"{base_url.rstrip('/')}/v1/games/{quote(game_id, safe='')}/state"


def _games_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/v1/games"


def _events_url(base_url: str, game_id: str) -> str:
    return f"{base_url.rstrip('/')}/v1/games/{quote(game_id, safe='')}/events?after_id=0&limit=1"


def check_config(config: dict, *, fetch: Callable[[str, float], dict] = _default_fetch) -> dict:
    """Return a non-sensitive preflight report.

    ``ok`` is false when an enabled MC plugin cannot safely use the configured
    Core. The returned report intentionally contains no account IDs, group IDs,
    URLs beyond the loopback origin, or secret material.
    """
    errors: list[str] = []
    warnings: list[str] = []
    enabled: list[dict] = []
    plugins = config.get("plugins", {})
    if not isinstance(plugins, dict):
        return {"ok": False, "enabled": [], "errors": ["plugins 必须是对象"], "warnings": []}

    for name in MC_PLUGINS:
        options = plugins.get(name) or {}
        if not isinstance(options, dict) or not options.get("enabled", False):
            continue
        base_url = str(options.get("base_url", "")).rstrip("/")
        game_id = str(options.get("game_id", "")).strip()
        row = {"plugin": name, "base_url": base_url, "game_id": game_id}
        enabled.append(row)
        if not _valid_loopback(base_url):
            errors.append(f"{name}: base_url 必须是本机回环 HTTP 地址")
        if not game_id or len(game_id) > 160 or any(ch in game_id for ch in " /\\?#\t\r\n"):
            errors.append(f"{name}: game_id 无效")

    if not enabled:
        return {"ok": not errors, "enabled": [], "errors": errors, "warnings": warnings}

    bindings = {(row["base_url"], row["game_id"]) for row in enabled}
    if len(bindings) != 1:
        errors.append("启用的 MC 插件必须使用同一个 base_url 和 game_id")
        return {"ok": False, "enabled": enabled, "errors": errors, "warnings": warnings}

    base_url, game_id = next(iter(bindings))
    if errors:
        return {"ok": False, "enabled": enabled, "errors": errors, "warnings": warnings}

    timeout_values = [
        float((plugins.get(row["plugin"]) or {}).get("timeout_seconds", 1.5))
        for row in enabled
    ]
    timeout = max(0.2, min(5.0, max(timeout_values)))
    try:
        games_response = fetch(_games_url(base_url), timeout)
        games = games_response.get("games")
        registered = (
            isinstance(games, list)
            and any(isinstance(item, dict) and item.get("id") == game_id for item in games)
        )
        if not registered:
            errors.append(f"Core 未登记 game_id「{game_id}」；请核对实例配置")
            return {"ok": False, "enabled": enabled,
                    "state": {"registered": False},
                    "errors": errors, "warnings": warnings}
    except HTTPError as error:
        errors.append(f"Core games 请求失败：HTTP {error.code}")
        return {"ok": False, "enabled": enabled, "errors": errors, "warnings": warnings}
    except (OSError, ValueError, TypeError) as error:
        errors.append(f"Core games 请求失败：{error}")
        return {"ok": False, "enabled": enabled, "errors": errors, "warnings": warnings}

    try:
        state = fetch(_state_url(base_url, game_id), timeout)
    except HTTPError as error:
        if error.code == 404:
            errors.append(f"Core 找不到 game_id「{game_id}」；请核对实例配置")
        else:
            errors.append(f"Core state 请求失败：HTTP {error.code}")
        return {"ok": False, "enabled": enabled, "errors": errors, "warnings": warnings}
    except (OSError, ValueError, TypeError) as error:
        errors.append(f"Core state 请求失败：{error}")
        return {"ok": False, "enabled": enabled, "errors": errors, "warnings": warnings}

    fixture_id = state.get("fixture_id")
    world_epoch = state.get("world_epoch")
    data = state.get("data") if isinstance(state.get("data"), dict) else {}
    if not state.get("connected"):
        errors.append("Core 状态显示 bot 未连接")
    if fixture_id is None and world_epoch is None:
        for row in enabled:
            options = plugins.get(row["plugin"]) or {}
            if row["plugin"] == "mc_control" and not options.get("allow_legacy_identity", False):
                errors.append("mc_control: Core 未提供 fixture 身份；默认拒绝无 fencing 任务")
            else:
                warnings.append(f"{row['plugin']}: Core 未提供 fixture_id/world_epoch（legacy）")
    elif not (isinstance(fixture_id, str) and fixture_id.strip()
              and isinstance(world_epoch, str) and world_epoch.strip()):
        errors.append("Core fixture_id/world_epoch 必须同时提供且非空")
    else:
        if data.get("fixture_phase") != "ready":
            errors.append(f"Core fixture_phase 不是 ready：{data.get('fixture_phase')!r}")
        if data.get("session_identity_stale"):
            errors.append("Core session_identity_stale=true")

    if any(row["plugin"] == "mc_presence" for row in enabled):
        try:
            events = fetch(_events_url(base_url, game_id), timeout)
            response_game_id = events.get("game_id")
            if response_game_id is not None and response_game_id != game_id:
                errors.append("Core events 响应的 game_id 与配置不一致")
            if not isinstance(events.get("events"), list):
                errors.append("Core events 响应缺少 events 数组")
        except HTTPError as error:
            errors.append(f"Core events 请求失败：HTTP {error.code}")
        except (OSError, ValueError, TypeError) as error:
            errors.append(f"Core events 请求失败：{error}")

    summary = {
        "registered": True,
        "connected": bool(state.get("connected")),
        "fixture_id_present": isinstance(fixture_id, str) and bool(fixture_id.strip()),
        "world_epoch_present": isinstance(world_epoch, str) and bool(world_epoch.strip()),
        "fixture_phase": data.get("fixture_phase"),
        "session_identity_stale": bool(data.get("session_identity_stale")),
    }
    return {"ok": not errors, "enabled": enabled, "state": summary,
            "errors": errors, "warnings": warnings}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="只读检查 wx-helper Minecraft/Core 配置")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "bot_config.json")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        report = check_config(config)
    except (OSError, ValueError, TypeError) as error:
        report = {"ok": False, "enabled": [], "errors": [f"读取配置失败：{error}"], "warnings": []}
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
