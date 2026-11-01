"""读取供群聊接续工作的共享 MC 活动日记快照。"""
from __future__ import annotations

from pathlib import Path


CURRENT_ACTIVITY_CUES = (
    "在做什么", "在干什么", "在干嘛", "现在做什么", "现在干什么", "现在干嘛",
    "在做啥", "在干啥", "最近干什么", "最近在做什么", "最近在忙啥", "在忙什么",
    "现在忙啥", "干嘛呢", "忙什么", "忙啥", "目前在做", "当前进展", "目前进展",
    "最近进展", "进展如何", "进度怎么样", "mc状态", "mc 状态", "mc怎么样",
    "minecraft 状态", "服务器状态", "补给", "食物短缺", "安全探索", "探索进展",
)
ACTIVITY_FOLLOW_UPS = ("现在呢", "那现在呢", "接下来呢", "进展呢", "后来呢", "怎么样了")
MC_CONTEXT_CUES = ("mc", "minecraft", "agent", "日记", "探索", "补给", "食物")


def is_current_activity_query(text: str, group_context=()) -> bool:
    if not isinstance(text, str):
        return False
    normalized = "".join(text.casefold().split())
    if any("".join(cue.casefold().split()) in normalized for cue in CURRENT_ACTIVITY_CUES):
        return True
    if not any(cue in normalized for cue in ACTIVITY_FOLLOW_UPS):
        return False
    recent = group_context[-8:] if isinstance(group_context, (list, tuple)) else ()
    context_text = " ".join(row.get("text", "") for row in recent if isinstance(row, dict))
    context_text = context_text.casefold()
    return any(cue in context_text for cue in MC_CONTEXT_CUES)


def _section(lines: list[str], heading: str) -> list[str]:
    start = next((i for i, line in enumerate(lines) if line.strip() == heading), None)
    if start is None:
        return []
    result = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        if line.strip():
            result.append(line.strip())
    return result


def _current_status(lines: list[str]) -> list[str]:
    allowed = ("**更新时间：**", "**当前实例：**", "**慢思考：**", "**游戏状态：**",
               "**当前在做什么：**", "**能力边界：**", "**未证实事项：**", "**Adapter：**")
    selected = []
    for line in lines:
        if not line.startswith("- ") or not any(label in line for label in allowed):
            continue
        if "**当前实例：**" in line:
            value = line.split("**当前实例：**", 1)[1].strip()
            # 群聊只需要实例类型；丢弃本机端口、会话 UUID 等运维标识。
            value = value.split("`", 1)[0].split(";", 1)[0].strip()
            selected.append(f"- 当前实例：{value}")
        else:
            selected.append(line)
    return selected


def _live_snapshot(path: str | Path | None, *, max_bytes: int = 64_000) -> list[str]:
    """Read current status, recent verified events, and next steps from the live mirror."""
    if path is None:
        return []
    try:
        file = Path(path)
        if file.stat().st_size > max_bytes:
            return []
        raw = file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError, TypeError, ValueError):
        return []

    lines = raw.splitlines()
    status = _current_status(_section(lines, "## 当前状态"))
    # The live mirror does not have a manually maintained "当前实例" value;
    # retain only its bounded, explicitly formatted status fields.
    allowed_live = ("**更新时间：**", "**当前实例：**", "**慢思考：**",
                    "**游戏状态：**", "**当前在做什么：**", "**未证实事项：**",
                    "**Adapter：**", "- 当前实例：")
    selected = [line for line in status if any(label in line for label in allowed_live)]
    recent_activity = []
    for line in _section(lines, "## 最近已确认活动"):
        if not line.startswith("- ") or len(line) > 240:
            continue
        lowered = line.casefold()
        if any(marker in lowered for marker in (
            "127.0.0.1", "api key", "api_key", "secret", "pid ", "uuid", "session",
            ".runtime", "c:\\", "\\logs\\",
        )):
            continue
        recent_activity.append(line)
        if len(recent_activity) >= 6:
            break
    next_steps = [line for line in _section(lines, "## 下一步")
                  if line.startswith(("1.", "2."))]
    return selected + recent_activity + next_steps[:2]


def read_activity_journal(path: str | Path, *, live_path: str | Path | None = None,
                          max_bytes: int = 256_000) -> dict | None:
    """Read the hand-maintained journal and optional fresh live snapshot.

    Only status/next-step sections are exposed. Timeline, local paths, PID,
    session ids and other operational identifiers are intentionally omitted.
    """
    try:
        file = Path(path)
        if file.stat().st_size > max_bytes:
            return None
        raw = file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError, TypeError, ValueError):
        return None

    lines = raw.splitlines()
    status = _current_status(_section(lines, "## 当前状态"))
    next_steps = [line for line in _section(lines, "## 下一步") if line.startswith(("1.", "2.", "3.", "4.", "5."))]
    live = _live_snapshot(live_path)
    if live:
        # Do not feed stale manually maintained runtime lines beside the
        # newer snapshot; keep timestamped strategic context and caveats.
        status = [line for line in status if any(label in line for label in
                  ("**更新时间：**", "**当前实例：**", "- 当前实例：", "**慢思考：**",
                   "**未证实事项：**", "**能力边界：**"))]
    if not status and not live:
        return None
    result = {
        "source": "MC_AGENT_ACTIVITY_JOURNAL.md",
        "snapshot_only": True,
        "current_status": status,
        "next_steps": next_steps[:5],
    }
    if live:
        result["live_snapshot"] = live
    return result
