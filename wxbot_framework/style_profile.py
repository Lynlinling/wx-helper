"""可审查的主人风格档案。

档案只承载可观察的表达和任务偏好，不保存原始聊天全文，也不试图复刻
隐藏推理。它作为低优先级提示注入，事实、安全、当前任务和群人格始终优先。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class StyleProfileError(ValueError):
    pass


def _text(value: Any, *, field: str, limit: int = 800) -> str:
    if not isinstance(value, str):
        raise StyleProfileError(f"{field} 必须是字符串")
    if "\x00" in value or any(ord(ch) < 32 and ch not in "\n\r\t" for ch in value):
        raise StyleProfileError(f"{field} 含有控制字符")
    value = value.strip()
    if not value or len(value) > limit:
        raise StyleProfileError(f"{field} 不能为空且不能超过 {limit} 个字符")
    return value


@dataclass(frozen=True)
class StyleProfile:
    version: int
    status: str
    style_rules: tuple[str, ...]
    reasoning_preferences: tuple[str, ...]
    interaction_preferences: tuple[str, ...]
    avoid: tuple[str, ...]
    source_note: str = ""
    approved_examples: tuple[str, ...] = ()
    learning_notes: tuple[str, ...] = ()
    mode: str = "casual_chat"
    self_model_prompt: str = ""

    def as_prompt(self, *, max_chars: int = 4200) -> str:
        sections = [
            ("这是主人日常聊天分身的表达档案，只用于模仿可观察的说话方式和互动习惯；"
             "它不是事实数据库，也不代表当前状态。" if self.mode == "casual_chat" else
             "这是主人表达偏好的档案，只用于模仿可观察的说话和协作方式；状态仍以档案标记为准。"),
            "不要把其中的个人事实当成当前事实，不要泄露原始样本，不要声称掌握主人的隐藏思维。",
            ("日常闲聊优先自然接话、短句和口语感；不要每条消息都写成结论、依据、下一步，"
             "只有对方认真询问事实、方案或任务时才展开。" if self.mode == "casual_chat" else
             "事实、当前任务、安全规则和当前群人格优先；无法确定时明确说不确定。"),
            "涉及本人当前状态、现实行动或没有给出证据的经历时，不要编造；不确定就直接说不确定。",
        ]
        labels = (("说话方式", self.style_rules), ("思考与判断偏好", self.reasoning_preferences),
                  ("协作偏好", self.interaction_preferences), ("避免", self.avoid))
        for label, values in labels:
            if values:
                sections.append(label + "：" + "；".join(values))
        if self.approved_examples:
            sections.append("主人主动提供的表达样本（只参考语气，不照搬事实）：" + "；".join(self.approved_examples))
        if self.status == "approved" and self.learning_notes:
            sections.append("主人已确认的表达与协作观察：" + "；".join(self.learning_notes))
        if self.mode == "casual_chat" and self.self_model_prompt:
            sections.append("主人自述的长期背景（只用于理解话题，不代表当前状态）：" + self.self_model_prompt)
        result = "主人风格档案（" + self.status + "）：\n" + "\n".join(sections)
        return result[:max_chars]


def _list(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or len(value) > 32:
        raise StyleProfileError(f"{key} 必须是不超过 32 项的数组")
    return tuple(_text(item, field=f"{key}[{index}]") for index, item in enumerate(value))


def _load_self_model_prompt(path: str | Path | None) -> str:
    if not path:
        return ""
    model_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(model_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StyleProfileError(f"私密自我模型无法读取：{model_path}") from error
    if not isinstance(raw, dict) or raw.get("type") != "lyn-self-model" or raw.get("status") != "approved":
        raise StyleProfileError("私密自我模型必须是 approved 的 lyn-self-model")
    reports = raw.get("self_reports", [])
    if not isinstance(reports, list):
        raise StyleProfileError("私密自我模型的 self_reports 格式不正确")
    allowed_axes = {"identity", "life_story", "values", "drives", "goals", "cognition", "affect", "social"}
    lines = []
    for item in reports:
        if not isinstance(item, dict) or item.get("axis") not in allowed_axes:
            continue
        title, answer = item.get("title"), item.get("answer")
        if isinstance(title, str) and isinstance(answer, str) and answer.strip():
            lines.append(f"{title.strip()}：{answer.strip()[:1200]}")
    return "；".join(lines)[:6000]


def load_style_profile(path: str | Path | None, *, require_approved: bool = False,
                       self_model_path: str | Path | None = None) -> StyleProfile | None:
    if path is None or str(path).strip() == "":
        return None
    profile_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(profile_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as error:
        raise StyleProfileError(f"风格档案不存在：{profile_path}") from error
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StyleProfileError(f"风格档案无法读取：{profile_path}") from error
    if not isinstance(raw, dict):
        raise StyleProfileError("风格档案必须是 JSON 对象")
    version = raw.get("schema_version", 1)
    if type(version) is not int or version != 1:
        raise StyleProfileError("只支持 schema_version=1 的风格档案")
    status = _text(raw.get("status", "draft"), field="status", limit=32)
    if status not in ("draft", "approved", "archived"):
        raise StyleProfileError("status 必须是 draft、approved 或 archived")
    source_note = raw.get("source_note", "")
    if source_note:
        source_note = _text(source_note, field="source_note", limit=500)
    if require_approved and status != "approved":
        raise StyleProfileError("运行中的风格档案必须标记为 approved")
    mode = _text(raw.get("mode", "casual_chat"), field="mode", limit=32)
    if mode not in ("casual_chat", "task_agent", "hybrid"):
        raise StyleProfileError("mode 必须是 casual_chat、task_agent 或 hybrid")
    # A legacy self_model key is intentionally ignored. Private self reports
    # are loaded through the separate lyn-self-model artifact, never a group
    # chat style profile.
    return StyleProfile(version, status, _list(raw, "style_rules"),
                        _list(raw, "reasoning_preferences"),
                        _list(raw, "interaction_preferences"), _list(raw, "avoid"), source_note,
                        _list(raw, "approved_examples"), _list(raw, "learning_notes"), mode,
                        _load_self_model_prompt(self_model_path))
