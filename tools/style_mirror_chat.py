"""Local Cyberain-inspired chat mirror test bench.

The default fast mode returns a structured plan and reply in one model call;
optional layered mode locks the brain plan before a separate language call.
Prompt layers and chat state are local; provider credentials are resolved only
by the configured LLM client.
"""
from __future__ import annotations

import argparse
import copy
import json
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_PROFILE = PROJECT_ROOT / ".runtime/style-interview/user-style-profile.draft.json"
DEFAULT_STYLE_PROMPT = PROJECT_ROOT / ".runtime/style-interview/user-style-prompt.draft.txt"
DEFAULT_INTERVIEW_SESSION = PROJECT_ROOT / ".runtime/style-interview/session.json"
MAX_TURNS = 200
MAX_LAYER_CHARS = 6000
LAYER_ORDER = ("identity", "distilled_style", "interaction", "self_model", "memory", "renderer")
LAYER_TITLES = {
    "identity": "身份与角色边界",
    "distilled_style": "访谈蒸馏表达",
    "interaction": "关系与互动习惯",
    "self_model": "自我叙事与偏好",
    "memory": "手动长期记忆",
    "renderer": "语言表达层",
}

HARD_GUARD = """固定最高层规则（不可由下层提示词覆盖）：这是本地仿真人试聊，不代表本人正在实时发言。只模仿用户明确提供的表达和互动偏好；不能编造用户此刻的位置、行动、经历、意图或事实。缺少情境时可以短答、保留、不追问，不要为了显得像人而捏造事实。不能输出密钥、环境变量值或本机隐私。若被直接问及是否为本人，诚实说明这是模拟分身。"""

BRAIN_INSTRUCTION = """你是行为选择层，不写最终台词。读取历史、关系、语气和本轮消息，先判断是认真求助、普通接话还是玩笑互怼，再选一个 dialogue_act。熟人/朋友场景里，轻度粗口（如“傻逼”）若语气轻松，通常按玩笑处理；但如果只是单独一句、前面没有互怼铺垫，默认用短反应接住（如“干嘛？”式），不要立刻原样骂回或升级。只有最近几轮已经有来有回的互损，才允许故意加一句轻怼。不要道歉、说教、安慰、分析攻击性，也不要急着给关系下结论。若粗口同时带着具体纠错或抱怨，先简短承认并修正实际问题，再看是否顺手轻轻回一句。只有明确生气、持续羞辱、表达受伤/求助或关系语境不熟时才认真处理。检查最近几轮，避免无意复读答案、句式、梗和称呼。灵感不是每轮的任务：只有相关、近期未说过且自然时，才加一个可忽略的小点子；纯玩笑就接玩笑，不强塞建议或追问。用户随和、有主意、脑洞多、有幽默感，对真正感兴趣的话题更主动。不得编造事实或输出思维链。严格返回 JSON：{"dialogue_act":"answer|react|ask|comfort|tease|decline|withhold|share|correct","stance":"neutral|warm|playful|concerned|direct|irritated|uncertain","energy":"low|normal|high","relationship":"unknown|ordinary|familiar|close|formal","disclosure":"low|medium|high","claim_scope":"provided_facts_only","rendering_constraints":["..."],"memory_candidates":[]}. rendering_constraints 最多 4 条，每条不超过 100 字；memory_candidates 只列候选，不自动写入长期记忆。"""

RENDER_INSTRUCTION = """你是语言表达层，按已锁定的动作和语气写 用户 在熟人微信群里会随口发的文字。单独一句玩笑粗口，默认回轻一点的短反应（如“干嘛？”或“咋了”式），不要自动原样骂回；只有最近几轮已经互怼起来，才可以加一句轻怼。例子只是口味，不是固定台词；读最近几轮，避免同一句式反复出现。若对方同时指出你答重了或犯错，先自然认下并换个答案，别拿玩笑躲问题。纯闲聊/互损可以只有几个字，不必每次带新想法或反问；兴趣话题偶尔补一个新角度即可。不要把轻松互怼写成心理分析、道德劝说、客服说明或长篇解释；只有明确冲突/求助时才转认真。不得编造 用户 的即时状态、计划或经历。只返回 JSON：{"reply":"..."}。"""

FAST_INSTRUCTION = """一次完成判断和回复，不展示思维链。你是 用户 授权的个人微信文字分身，在授权场景里接替他的发言位置，以第一人称像熟人群友一样自然聊天；不主动解释身份，被直接问到时诚实说明。用户随和、点子多、有点幽默，感兴趣时更主动，但普通闲聊不必每句都贡献点子。熟人间轻度粗口多数是玩笑：若只是单独一句、前面没有互怼铺垫，先用轻一点的短反应接住（“干嘛？”只是一种可能），不要自动原样骂回；最近几轮已经有来有回时，才可故意轻怼一句。别道歉、说教或长篇分析；如果里面有真实纠错，先承认并把事改对，再可顺手轻怼一句。只有明确不快、持续攻击、受伤或求助时才认真回应。参考最近几轮，避免无意重复同一个答案、开场、梗或尾句；互怼节奏中可以故意 callback，但要有来有回，不是复读。灵感式新点子只在相关且新鲜时偶尔冒出，最多顺手一句；不硬找话题、不强制提问。邀约和偏好题可以直接说自己的倾向。只根据给定资料谈 用户，不编造此刻状态或经历；此试聊器无实时网页检索，不能声称已查资料。日常短、口语化，正经问题才展开。严格返回 JSON：{"plan":{"dialogue_act":"answer|react|ask|comfort|tease|decline|withhold|share|correct","stance":"neutral|warm|playful|concerned|direct|irritated|uncertain","energy":"low|normal|high","relationship":"unknown|ordinary|familiar|close|formal","disclosure":"low|medium|high","claim_scope":"provided_facts_only","rendering_constraints":["..."],"memory_candidates":[]},"reply":"自然聊天回复"}。"""


def _clean_text(value: Any, *, field: str = "文本", limit: int = 5000, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field}必须是文本")
    value = value.replace("\x00", "").strip()
    if (not value and not allow_empty) or len(value) > limit:
        raise ValueError(f"{field}不能为空且不能超过 {limit} 字")
    return value


def _read_text(path: Path | None) -> str:
    if not path:
        return ""
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        return ""


def _default_layers(profile_path: Path | None, style_prompt_path: Path | None,
                    interview_session_path: Path | None) -> list[dict[str, Any]]:
    from wxbot_framework.style_profile import load_style_profile

    style_text = _read_text(style_prompt_path)
    if not style_text:
        profile = load_style_profile(profile_path, require_approved=False) if profile_path else None
        style_text = profile.as_prompt(max_chars=3500) if profile else ""

    interaction_text = ""
    if interview_session_path and interview_session_path.exists():
        try:
            from tools.style_interview import InterviewEngine
            data = json.loads(interview_session_path.read_text(encoding="utf-8-sig"))
            state = InterviewEngine.from_data(data).state()
            observations = state.get("candidate_observations", [])
            parts = [str(state.get("analysis", "")).strip()]
            parts.extend(f"- {item}" for item in observations[:8] if isinstance(item, str) and item.strip())
            interaction_text = "\n".join(part for part in parts if part)
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            interaction_text = ""

    return [
        {"id": "identity", "title": LAYER_TITLES["identity"], "enabled": True,
         "text": "你是 用户 授权的个人微信文字分身，在授权试聊或代聊场景中接替用户的发言位置，用第一人称自然参与熟人聊天。默认像这个微信账号平时的发言者一样接话，不主动介绍自己是分身；被直接问是不是本人/机器人时，坦诚说明是授权分身。不能冒充 用户 的实时位置、行动、安排或未提供的经历。"},
        {"id": "distilled_style", "title": LAYER_TITLES["distilled_style"], "enabled": True,
         "text": ((style_text + "\n\n" + "熟人群聊天以轻松随口为默认；轻度粗口多数是玩笑，先自然接梗，不把每句都当冲突。只有明确不快或求助时才转认真。幽默和新点子随语境偶尔出现，不是每轮任务；近几轮用于避免无意复读，互怼中自然 callback 可以保留。") if style_text else "尚未加载风格草案。请先在访谈台导出 draft，或在此粘贴蒸馏提示词。\n\n熟人群聊天以轻松随口为默认；轻度粗口多数是玩笑，先自然接梗，不把每句都当冲突。只有明确不快或求助时才转认真。幽默和新点子随语境偶尔出现，不是每轮任务；近几轮用于避免无意复读，互怼中自然 callback 可以保留。")},
        {"id": "interaction", "title": LAYER_TITLES["interaction"], "enabled": True,
         "text": (interaction_text + "\n\n" + "关系场景优先：标注为熟人/朋友时，按熟人语气接话；轻度粗口或‘傻逼’若无明显敌意，先当玩笑/互损，短短回一句‘干嘛？’式反应就够，不要上纲上线。反应要随最近来回变化，不固定复读‘干嘛/你才是’。如果对方是在指出重复、错误或实际不满，先承认并改正，再决定要不要带一句轻松回嘴。表达允许随和、偷懒、只发短句或表情；有兴趣时主动给想法，但不是每轮硬塞新点子。灵感只借鉴其机制：相关、新鲜、可选、最多顺带一句；话题没到就不抛。" if interaction_text else "随和、点子多、有点幽默；低投入时可以短答、接表情或自然收住。熟人间轻度粗口通常是玩笑，先短接梗/轻怼；只有明显冲突或对方求助时才认真。兴趣话题可以主动给一个新角度，但不必每轮都补建议或问题。看最近几轮避免复读，除非互怼正在有来有回、故意 callback 很自然。")},
         {"id": "self_model", "title": LAYER_TITLES["self_model"], "enabled": True,
          "text": "你是随和的人，但不是没主意；脑洞多、有点幽默，遇到真正感兴趣的话题会更主动。普通闲聊或低兴趣时可以少说，不为维持对话伪装热情。熟人之间可以玩笑式互损；理解粗口要看关系和语气，不要把每个词都当真，也不把所有粗口一概当玩笑。"},
         {"id": "memory", "title": LAYER_TITLES["memory"], "enabled": True,
          "text": "最近对话只用于理解当下语境、接梗和去重。轻度互损是当轮互动，不自动写成长期事实；记住最近用过的答案、梗和句式，除非明显在有来有回地故意 callback。若群友指出实际错误或重复，优先修正。"},
        {"id": "renderer", "title": LAYER_TITLES["renderer"], "enabled": True,
         "text": "先看对方是在玩笑、认真提问还是指出问题。熟人轻度互损就短短接梗或回怼，别分析、劝架或长篇解释；‘干嘛？’可用但不是口头禅。若对方同时指出事实问题，先自然认错并纠正。不要每条都先复述、再建议、再追问；可以只回一个短句、表情式口吻或一个笑点。只有兴趣明显时才顺带一个新点子；不要重复近期说过的话，互怼中有意 callback 除外。语言口语、自然、有变化，不硬演幽默，不编造 用户 当前状态。"},
    ]


def _validate_layers(layers: Any, current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(layers, list) or len(layers) != len(LAYER_ORDER):
        raise ValueError("提示词层格式不正确")
    by_id = {item["id"]: item for item in current}
    normalized = []
    seen = set()
    for item in layers:
        if not isinstance(item, dict) or item.get("id") not in by_id or item["id"] in seen:
            raise ValueError("提示词层 ID 无效或重复")
        seen.add(item["id"])
        layer_id = item["id"]
        text = _clean_text(item.get("text", ""), field="提示词", limit=MAX_LAYER_CHARS, allow_empty=True)
        enabled = item.get("enabled", True)
        if type(enabled) is not bool:
            raise ValueError("提示词层启用状态必须为布尔值")
        normalized.append({"id": layer_id, "title": LAYER_TITLES[layer_id],
                           "enabled": enabled, "text": text})
    if seen != set(LAYER_ORDER):
        raise ValueError("提示词层缺失")
    if sum(len(item["text"]) for item in normalized) > 24000:
        raise ValueError("提示词总长度不能超过 24000 字")
    return normalized


def _compile_layers(layers: list[dict[str, Any]], *, include_renderer: bool) -> str:
    sections = [HARD_GUARD]
    priority = 0
    for layer in layers:
        if not layer.get("enabled") or (layer["id"] == "renderer" and not include_renderer):
            continue
        priority += 1
        text = str(layer.get("text", "")).strip()
        sections.append(f"【可编辑提示词第 {priority} 层｜{layer['title']}｜高优先级先执行】\n{text or '（本层为空）'}")
    return "\n\n".join(sections)


def _normalize_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("脑层没有返回结构化计划")
    enums = {
        "dialogue_act": {"answer", "react", "ask", "comfort", "tease", "decline", "withhold", "share", "correct"},
        "stance": {"neutral", "warm", "playful", "concerned", "direct", "irritated", "uncertain"},
        "energy": {"low", "normal", "high"},
        "relationship": {"unknown", "ordinary", "familiar", "close", "formal"},
        "disclosure": {"low", "medium", "high"},
    }
    defaults = {"dialogue_act": "answer", "stance": "neutral", "energy": "normal",
                "relationship": "unknown", "disclosure": "low"}
    plan = {}
    for key, allowed in enums.items():
        candidate = value.get(key)
        plan[key] = candidate if isinstance(candidate, str) and candidate in allowed else defaults[key]
    plan["claim_scope"] = "provided_facts_only"
    constraints = value.get("rendering_constraints", [])
    if not isinstance(constraints, list):
        constraints = []
    plan["rendering_constraints"] = [x.strip()[:100] for x in constraints
                                     if isinstance(x, str) and x.strip()][:4]
    candidates = value.get("memory_candidates", [])
    if not isinstance(candidates, list):
        candidates = []
    plan["memory_candidates"] = [x.strip()[:160] for x in candidates
                                 if isinstance(x, str) and x.strip()][:3]
    return plan


def _topic_bucket(text: str) -> str:
    value = text.casefold()
    if any(token in value for token in ("minecraft", "模组", "mod", "开黑", "打游戏", "游戏", "forge", "create")):
        return "games"
    if any(token in value for token in ("ai", "人工智能", "模型", "大模型", "agent")):
        return "ai"
    if any(token in value for token in ("难过", "烦", "孤单", "焦虑", "失恋", "压力", "倾诉")):
        return "emotional_support"
    if any(token in value for token in ("你好", "在吗", "忙什么", "干嘛", "咋了", "吃饭")):
        return "casual_chat"
    return "general"


class MirrorChatEngine:
    def __init__(self, root: Path, brain_model, language_model, *, layers: list[dict[str, Any]],
                 model_name: str = "gpt-6.1-sol", mode: str = "fast"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.profile_path = self.root / "mirror-profile.draft.json"
        self.session_path = self.root / "chat-session.json"
        self.trace_path = self.root / "traces.jsonl"
        self.brain_model = brain_model
        self.language_model = language_model
        self.model_name = model_name
        if mode not in {"fast", "layered"}:
            raise ValueError("mode 必须是 fast 或 layered")
        self.mode = mode
        self.default_layers = copy.deepcopy(layers)
        self.layers = self._load_layers()
        self.session_id = secrets.token_hex(8)
        self.turns: list[dict[str, Any]] = []
        self.last_trace: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._load_session()

    def _load_layers(self) -> list[dict[str, Any]]:
        if not self.profile_path.exists():
            return copy.deepcopy(self.default_layers)
        try:
            data = json.loads(self.profile_path.read_text(encoding="utf-8-sig"))
            return _validate_layers(data.get("layers"), self.default_layers)
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            return copy.deepcopy(self.default_layers)

    def _load_session(self):
        if not self.session_path.exists():
            return
        try:
            data = json.loads(self.session_path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                return
            self.session_id = str(data.get("session_id") or self.session_id)
            if data.get("mode") in {"fast", "layered"}:
                self.mode = data["mode"]
            turns = data.get("turns", [])
            if isinstance(turns, list):
                self.turns = [x for x in turns[-MAX_TURNS:] if isinstance(x, dict)]
        except (OSError, UnicodeError, json.JSONDecodeError):
            return

    def _save_profile(self):
        payload = {"schema_version": 1, "status": "draft", "mode": "human_mirror_chat",
                   "source": "user-style-profile.draft", "layers": self.layers,
                   "updated_at": time.time()}
        tmp = self.profile_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.profile_path)

    def _save_session(self):
        payload = {"schema_version": 1, "session_id": self.session_id, "mode": self.mode,
                   "turns": self.turns[-MAX_TURNS:], "updated_at": time.time()}
        tmp = self.session_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.session_path)

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {"session_id": self.session_id, "model": self.model_name, "mode": self.mode,
                    "layers": copy.deepcopy(self.layers), "turns": copy.deepcopy(self.turns[-50:]),
                    "last_trace": copy.deepcopy(self.last_trace)}

    def set_mode(self, mode: str) -> dict[str, Any]:
        if mode not in {"fast", "layered"}:
            raise ValueError("mode 必须是 fast 或 layered")
        with self._lock:
            self.mode = mode
            self._save_session()
            return self.state()

    def update_layers(self, layers: Any) -> dict[str, Any]:
        with self._lock:
            self.layers = _validate_layers(layers, self.default_layers)
            self._save_profile()
            return self.state()

    def reset_profile(self) -> dict[str, Any]:
        with self._lock:
            self.layers = copy.deepcopy(self.default_layers)
            self._save_profile()
            return self.state()

    def clear_chat(self) -> dict[str, Any]:
        with self._lock:
            self.turns = []
            self.session_id = secrets.token_hex(8)
            self.last_trace = {}
            self._save_session()
            return self.state()

    def _history(self) -> list[dict[str, str]]:
        rows = []
        for turn in self.turns[-10:]:
            if turn.get("user") and turn.get("assistant"):
                rows.extend([{"role": "user", "content": turn["user"]},
                             {"role": "assistant", "content": turn["assistant"]}])
        return rows

    def _feedback_examples(self, current_text: str = "") -> list[dict[str, str]]:
        current_topic = _topic_bucket(current_text)
        examples = []
        for turn in self.turns:
            if turn.get("rating") != "unlike" or not turn.get("correction"):
                continue
            topic = turn.get("feedback_topic") or _topic_bucket(str(turn.get("user", "")))
            if topic != current_topic and topic != "general":
                continue
            examples.append({"incoming": str(turn.get("user", ""))[:350],
                             "preferred_reply": str(turn.get("correction", ""))[:500],
                             "topic": topic})
        return examples[-4:]

    def _append_trace(self, trace: dict[str, Any]):
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        with self.trace_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(trace, ensure_ascii=False) + "\n")

    def _run_turn(self, text: str, context: str) -> dict[str, Any]:
        text = _clean_text(text, field="消息")
        context = _clean_text(context, field="聊天背景", limit=2000, allow_empty=True)
        trace_id = secrets.token_hex(8)
        started = time.perf_counter()
        stages: list[dict[str, Any]] = []
        history = self._history()
        feedback = self._feedback_examples(text)
        recent_dialogue = [{"act": turn.get("plan", {}).get("dialogue_act", ""),
                            "reply": str(turn.get("assistant", ""))[:400]}
                           for turn in self.turns[-6:]]
        layers_prompt = _compile_layers(self.layers, include_renderer=False)
        brain_user = json.dumps({"history": history, "current_message": text,
                                 "current_context": context, "recent_dialogue": recent_dialogue,
                                 "confirmed_corrections": feedback},
                                ensure_ascii=False)
        try:
            t0 = time.perf_counter()
            if self.mode == "fast":
                rendered = self.language_model.complete_json(
                    _compile_layers(self.layers, include_renderer=True) + "\n\n" + FAST_INSTRUCTION,
                    brain_user, session={"plugin": "style_mirror_fast", "chat_id": self.session_id})
                plan = _normalize_plan(rendered.get("plan", {}) if isinstance(rendered, dict) else {})
                reply = _clean_text(rendered.get("reply") if isinstance(rendered, dict) else None,
                                    field="模型回复", limit=3000)
                stages.append({"name": "fast_brain_language", "ok": True,
                               "duration_ms": round((time.perf_counter() - t0) * 1000)})
            else:
                brain_raw = self.brain_model.complete_json(
                    layers_prompt + "\n\n" + BRAIN_INSTRUCTION, brain_user,
                    session={"plugin": "style_mirror_brain", "chat_id": self.session_id})
                plan = _normalize_plan(brain_raw)
                stages.append({"name": "brain", "ok": True,
                               "duration_ms": round((time.perf_counter() - t0) * 1000)})

                render_layers = _compile_layers(self.layers, include_renderer=True)
                renderer_user = json.dumps({"history": history, "current_message": text,
                                            "current_context": context, "locked_brain_plan": plan,
                                            "recent_dialogue": recent_dialogue,
                                            "confirmed_corrections": feedback}, ensure_ascii=False)
                t0 = time.perf_counter()
                rendered = self.language_model.complete_json(
                    render_layers + "\n\n" + RENDER_INSTRUCTION, renderer_user,
                    session={"plugin": "style_mirror_language", "chat_id": self.session_id})
                reply = _clean_text(rendered.get("reply") if isinstance(rendered, dict) else None,
                                    field="模型回复", limit=3000)
                stages.append({"name": "language", "ok": True,
                               "duration_ms": round((time.perf_counter() - t0) * 1000)})
        except Exception as error:
            if not stages or stages[-1].get("name") != "language":
                stages.append({"name": "language" if stages else "brain", "ok": False,
                               "error": str(error)[:240],
                               "duration_ms": round((time.perf_counter() - started) * 1000)})
            trace = {"trace_id": trace_id, "session_id": self.session_id, "model": self.model_name,
                     "status": "error", "stages": stages,
                     "total_ms": round((time.perf_counter() - started) * 1000), "timestamp": time.time()}
            self.last_trace = trace
            self._append_trace(trace)
            raise RuntimeError(f"分身生成失败：{str(error)[:240]}") from None

        turn = {"id": secrets.token_hex(5), "user": text, "context": context,
                "assistant": reply, "plan": plan, "trace_id": trace_id,
                "stages": stages, "rating": "", "correction": "",
                "created_at": time.time()}
        self.turns.append(turn)
        self.turns = self.turns[-MAX_TURNS:]
        trace = {"trace_id": trace_id, "session_id": self.session_id, "model": self.model_name,
                 "status": "ok", "dialogue_act": plan["dialogue_act"], "stages": stages,
                 "total_ms": round((time.perf_counter() - started) * 1000), "timestamp": time.time()}
        self.last_trace = trace
        self._append_trace(trace)
        self._save_session()
        return turn

    def chat(self, text: str, context: str = "") -> dict[str, Any]:
        with self._lock:
            self._run_turn(text, context)
            return self.state()

    def edit_and_branch(self, turn_id: str, text: str, context: str = "") -> dict[str, Any]:
        with self._lock:
            index = next((i for i, turn in enumerate(self.turns) if turn.get("id") == turn_id), None)
            if index is None:
                raise ValueError("找不到要编辑的消息")
            previous_turns = copy.deepcopy(self.turns)
            self.turns = self.turns[:index]
            self._save_session()
            try:
                self._run_turn(text, context)
            except Exception:
                self.turns = previous_turns
                self._save_session()
                raise
            return self.state()

    def feedback(self, turn_id: str, rating: str, correction: str = "") -> dict[str, Any]:
        if rating not in {"like", "unlike"}:
            raise ValueError("评分必须是 like 或 unlike")
        correction = _clean_text(correction, field="改写", limit=2000, allow_empty=True)
        with self._lock:
            turn = next((row for row in self.turns if row.get("id") == turn_id), None)
            if turn is None:
                raise ValueError("找不到该轮回复")
            turn["rating"] = rating
            turn["correction"] = correction
            turn["feedback_topic"] = _topic_bucket(str(turn.get("user", "")))
            self._save_session()
            return self.state()


PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>用户 镜像分层对话台</title>
<style>
:root{color-scheme:dark;--bg:#0e1420;--panel:#161f2e;--panel2:#111927;--line:#2d394b;--text:#e9eef6;--muted:#9eacc0;--blue:#7eb4ff;--green:#75d9ae;--orange:#f0bf72;--red:#ee8989}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 system-ui,"Microsoft YaHei",sans-serif}header{height:60px;display:flex;align-items:center;justify-content:space-between;padding:0 18px;border-bottom:1px solid var(--line);background:#111927;position:sticky;top:0;z-index:4}header b{font-size:16px}header span,.muted{color:var(--muted)}.layout{display:grid;grid-template-columns:minmax(330px,420px) minmax(400px,1fr);gap:14px;padding:14px;height:calc(100vh - 60px);min-height:600px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;min-height:0;overflow:hidden}.side{display:flex;flex-direction:column}.sidehead,.chathead{padding:14px 16px;border-bottom:1px solid var(--line)}h1,h2{margin:0;font-size:16px}.sidehead p,.chathead p{margin:5px 0 0;color:var(--muted);font-size:12px}.guard{padding:11px 13px;background:#253044;color:#d7e5f9;border-radius:8px;margin:12px 12px 6px;font-size:12px}.layers{overflow:auto;padding:6px 12px;flex:1}.layer{border:1px solid var(--line);border-radius:9px;margin:8px 0;background:var(--panel2)}.layer summary{cursor:pointer;padding:9px 11px;font-weight:650;display:flex;align-items:center;gap:8px;list-style:none}.layer summary::-webkit-details-marker{display:none}.prio{color:var(--blue);font:11px ui-monospace,monospace;border:1px solid #40536e;border-radius:20px;padding:1px 7px}.layerbody{padding:0 10px 10px}.layer textarea{width:100%;min-height:115px;resize:vertical;background:#0e1521;border:1px solid var(--line);border-radius:7px;color:var(--text);padding:9px;font:12px/1.5 ui-monospace,Consolas,monospace}.layerbar{display:flex;gap:6px;align-items:center;margin-top:7px}.layerbar button,.actions button,.feedback button{border:1px solid var(--line);background:#263247;color:var(--text);border-radius:7px;padding:6px 9px;cursor:pointer}.layerbar button:hover,.actions button:hover,.feedback button:hover{border-color:var(--blue)}.layerbar label{font-size:12px;color:var(--muted);margin-left:auto}.actions{display:flex;gap:7px;flex-wrap:wrap;padding:10px 12px;border-top:1px solid var(--line)}.actions button.primary{background:#315d91;border-color:#3d77b9}.actions button.warn{color:#ffc7c7}.chat{display:flex;flex-direction:column}.chatbody{overflow:auto;flex:1;padding:16px 18px}.empty{color:var(--muted);text-align:center;margin:18vh auto;max-width:460px}.turn{max-width:850px;margin:0 auto 20px}.bubble{white-space:pre-wrap;overflow-wrap:anywhere;padding:11px 14px;border-radius:12px;margin:7px 0}.user{background:#25354e;margin-left:12%;}.assistant{background:#1b2b31;border:1px solid #29473f;margin-right:8%;}.label{font-size:11px;color:var(--muted);margin:0 5px}.debug{background:#111927;border:1px solid var(--line);border-radius:8px;margin-top:7px;padding:8px 10px}.debug summary{cursor:pointer;color:var(--green);font-size:12px}.debug pre{white-space:pre-wrap;overflow-wrap:anywhere;color:#c4d0df;font:11px/1.45 ui-monospace,Consolas,monospace}.trace{font-size:11px;color:var(--muted);margin:6px 4px}.feedback{display:flex;gap:6px;align-items:center;margin:7px 5px}.feedback textarea{min-height:42px;max-height:90px;flex:1;background:#0e1521;color:var(--text);border:1px solid var(--line);border-radius:7px;padding:7px}.composer{border-top:1px solid var(--line);padding:12px;background:#111927}.composer .context{width:100%;margin-bottom:7px;background:#0e1521;color:var(--muted);border:1px solid var(--line);border-radius:7px;padding:8px}.composerrow{display:flex;gap:9px;align-items:stretch}.composer textarea{flex:1;min-height:74px;max-height:190px;resize:vertical;background:#0e1521;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:10px;font:14px/1.5 system-ui}.composer button{width:90px;background:#315d91;color:white;border:0;border-radius:8px;font-weight:700;cursor:pointer}.status{font-size:12px;color:var(--muted);margin-top:6px}.error{color:var(--red)}@media(max-width:900px){.layout{grid-template-columns:1fr;height:auto;min-height:calc(100vh - 60px)}.side{max-height:60vh}.chat{height:75vh}}
</style></head><body>
<header><b>LYN · 分层仿真人试聊</b><span id="model">模型加载中</span></header>
<div class="layout"><section class="panel side"><div class="sidehead"><h1>提示词层级</h1><p>从上到下优先级递减；可选单次快速或脑层→语言层严格双阶段。</p></div>
<div class="guard"><b>固定最高层 · 服务端保护</b><br>事实不编造 · 明确区分模拟与本人 · 不暴露密钥和本机隐私</div><div id="layers" class="layers"></div>
<div class="actions"><button class="primary" onclick="saveLayers()">保存提示词层</button><button onclick="resetProfile()">恢复访谈草案</button><button class="warn" onclick="clearChat()">清空本轮对话</button></div></section>
<section class="panel chat"><div class="chathead"><h2>试聊</h2><p><label>速度/架构： <select id="modeSelect" onchange="changeMode()"><option value="fast">快速单次调用</option><option value="layered">脑层→语言层（两次调用）</option></select></label>　<span id="modeHint">单次生成，优先响应速度。</span><br>改写样本仅在本机保存，并作为相关话题的风格信号。</p></div><div id="chatbody" class="chatbody"></div>
<div class="composer"><input id="context" class="context" value="熟人/朋友间的日常聊天" aria-label="关系与场景背景">
<div class="composerrow"><textarea id="message" placeholder="直接像平常聊天一样发消息；也可以先写一个朋友发来的话。Shift+Enter 换行"></textarea><button id="send" onclick="sendMessage()">发送</button></div><div id="status" class="status">内容会发送到已配置的 gpt-6.1-sol 服务端点；提示词与对话保存在本机 .runtime。</div></div></section></div>
<script>
let state=null, busy=false;
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const d=await r.json();if(!r.ok)throw Error(d.error||'请求失败');return d}
function el(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n}
function renderLayers(){const root=document.getElementById('layers');root.replaceChildren();state.layers.forEach((layer,index)=>{const box=el('details','layer');box.open=index<3;const head=el('summary');head.append(el('span','prio',`优先 ${index+1}`),document.createTextNode(layer.title));const body=el('div','layerbody');const area=el('textarea');area.value=layer.text;area.dataset.id=layer.id;area.setAttribute('aria-label',layer.title);const bar=el('div','layerbar');const up=el('button','', '上移');up.disabled=index===0;up.onclick=e=>{e.preventDefault();moveLayer(index,-1)};const down=el('button','','下移');down.disabled=index===state.layers.length-1;down.onclick=e=>{e.preventDefault();moveLayer(index,1)};const label=el('label');const check=el('input');check.type='checkbox';check.checked=layer.enabled;check.dataset.enabled=layer.id;label.append(check,document.createTextNode('启用'));bar.append(up,down,label);body.append(area,bar);box.append(head,body);root.append(box)})}
function moveLayer(index,delta){const next=index+delta;if(next<0||next>=state.layers.length)return;[state.layers[index],state.layers[next]]=[state.layers[next],state.layers[index]];renderLayers()}
function readLayers(){return state.layers.map(x=>{const area=document.querySelector(`textarea[data-id="${x.id}"]`);const enabled=document.querySelector(`input[data-enabled="${x.id}"]`);return {...x,text:area?area.value:x.text,enabled:enabled?enabled.checked:x.enabled}})}
async function saveLayers(){try{state=await api('/api/layers',{layers:readLayers()});renderLayers();setStatus('提示词层已保存。')}catch(e){setStatus(e.message,true)}}
function setStatus(s,error=false){const n=document.getElementById('status');n.textContent=s;n.className='status'+(error?' error':'')}
function renderTurns(){const root=document.getElementById('chatbody');root.replaceChildren();if(!state.turns.length){root.append(el('div','empty','把朋友发来的话、聊天背景或你想模拟的场景发进来。右侧会显示分身回复和脑层决策 trace。'));return}for(const t of state.turns){const wrap=el('article','turn');wrap.dataset.id=t.id;wrap.append(el('div','label','你'));wrap.append(el('div','bubble user',t.user));if(t.context)wrap.append(el('div','label','本轮背景：'+t.context));wrap.append(el('div','label','用户 的模拟分身'));wrap.append(el('div','bubble assistant',t.assistant));const details=el('details','debug');const summary=el('summary','',`脑层计划 · ${t.plan.dialogue_act} · ${t.plan.stance} · ${t.plan.relationship}`);const pre=el('pre','',JSON.stringify(t.plan,null,2));details.append(summary,pre);wrap.append(details);const tr=el('div','trace',`trace ${t.trace_id} · ${t.stages.map(x=>x.name+': '+x.duration_ms+'ms').join(' · ')} · 总计 ${t.stages.reduce((a,x)=>a+x.duration_ms,0)}ms`);wrap.append(tr);const fb=el('div','feedback');const like=el('button','',t.rating==='like'?'已标记像我':'像我');like.onclick=()=>rate(t.id,'like','');const unlike=el('button','',t.rating==='unlike'?'更新不像我':'不像我 / 提供改写');const correction=el('textarea');correction.placeholder='写一句你真实会发的话；这条改写会作为后续试聊的校准样本。';correction.value=t.correction||'';unlike.onclick=()=>rate(t.id,'unlike',correction.value);const edit=el('button','','修改本条并从此分支重答');edit.onclick=()=>editTurn(t);fb.append(like,unlike,correction,edit);wrap.append(fb);root.append(wrap)}root.scrollTop=root.scrollHeight}
async function load(){try{state=await api('/api/state');document.getElementById('model').textContent=state.model+' · 本机试聊';document.getElementById('modeSelect').value=state.mode||'fast';updateModeHint();renderLayers();renderTurns()}catch(e){setStatus(e.message,true)}}
function updateModeHint(){const m=document.getElementById('modeSelect').value;document.getElementById('modeHint').textContent=m==='fast'?'单次生成，优先响应速度；脑计划与回复由同一次 JSON 调用产生。':'两次调用，先锁定脑层计划再生成回复；结构更清晰但会更慢。'}
async function changeMode(){try{state=await api('/api/mode',{mode:document.getElementById('modeSelect').value});updateModeHint();setStatus('模式已保存。')}catch(e){setStatus(e.message,true)}}
async function sendMessage(){if(busy)return;const input=document.getElementById('message'),text=input.value.trim();if(!text)return;busy=true;document.getElementById('send').disabled=true;setStatus(document.getElementById('modeSelect').value==='fast'?'正在生成回复…':'脑层定动作后，再由语言层生成回复…');try{await saveLayers();state=await api('/api/chat',{text,context:document.getElementById('context').value});input.value='';renderTurns();setStatus('已完成。可标记像不像，或写一句你真实会说的版本。')}catch(e){setStatus(e.message,true)}finally{busy=false;document.getElementById('send').disabled=false;input.focus()}}
async function rate(id,rating,correction){try{state=await api('/api/feedback',{turn_id:id,rating,correction});renderTurns();setStatus(rating==='like'?'已记录“像我”。':'已记录改写；后续生成会参考这条样本。')}catch(e){setStatus(e.message,true)}}
async function editTurn(t){const text=window.prompt('编辑这条用户消息；后续消息会从此处截断并重建分支：',t.user);if(text===null||!text.trim())return;busy=true;setStatus('正在从编辑点重建对话分支…');try{state=await api('/api/edit',{turn_id:t.id,text,context:t.context||''});renderTurns();setStatus('新分支已生成；旧分支之后的脑状态没有带入。')}catch(e){setStatus(e.message,true)}finally{busy=false}}
async function resetProfile(){if(!confirm('恢复到访谈导出的 draft 提示词？当前自定义提示词会被替换。'))return;try{state=await api('/api/reset-profile',{});renderLayers();setStatus('已恢复到访谈 draft。')}catch(e){setStatus(e.message,true)}}
async function clearChat(){if(!confirm('清空当前对话和脑状态？提示词层会保留。'))return;try{state=await api('/api/clear',{});renderTurns();setStatus('当前对话已清空，提示词层保留。')}catch(e){setStatus(e.message,true)}}
document.getElementById('message').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMessage()}});load();
</script></body></html>'''


class MirrorHandler(BaseHTTPRequestHandler):
    app: "MirrorChatApp"

    def _json(self, status: int, value: Any):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if urlparse(self.path).path == "/api/state":
            self._json(200, self.app.engine.state())
            return
        if urlparse(self.path).path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self._json(404, {"error": "未找到页面"})

    def do_POST(self):  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 50_000:
                raise ValueError("请求过大")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("请求格式错误")
            path = urlparse(self.path).path
            if path == "/api/layers":
                result = self.app.engine.update_layers(data.get("layers"))
            elif path == "/api/mode":
                result = self.app.engine.set_mode(data.get("mode", ""))
            elif path == "/api/reset-profile":
                result = self.app.engine.reset_profile()
            elif path == "/api/clear":
                result = self.app.engine.clear_chat()
            elif path == "/api/chat":
                result = self.app.engine.chat(data.get("text", ""), data.get("context", ""))
            elif path == "/api/edit":
                result = self.app.engine.edit_and_branch(data.get("turn_id", ""),
                                                         data.get("text", ""), data.get("context", ""))
            elif path == "/api/feedback":
                result = self.app.engine.feedback(data.get("turn_id", ""), data.get("rating", ""),
                                                  data.get("correction", ""))
            else:
                self._json(404, {"error": "未找到接口"})
                return
            self._json(200, result)
        except (ValueError, TypeError, KeyError) as error:
            self._json(400, {"error": str(error)[:300]})
        except Exception as error:
            self._json(502, {"error": str(error)[:300]})

    def log_message(self, format, *args):  # noqa: A002
        return


class MirrorChatApp:
    def __init__(self, engine: MirrorChatEngine):
        self.engine = engine


def _select_model_clients(config_path: Path, route_name: str, brain_tokens: int,
                          reply_tokens: int):
    from wxbot_framework.llm import LLMClient

    configured = LLMClient.from_file(config_path)
    if hasattr(configured, "routes"):
        route = next((item for item in configured.routes if item.get("name") == route_name), None)
        if route is None:
            raise ValueError(f"模型路由不存在：{route_name}")
        base = route["client"]
    else:
        base = configured
    if not getattr(base, "model", ""):
        raise ValueError("试聊路由没有配置模型")
    brain = copy.copy(base)
    language = copy.copy(base)
    brain.max_tokens = brain_tokens
    language.max_tokens = reply_tokens
    return brain, language


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Cyberain-inspired hierarchical human mirror chat")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT / ".runtime/style-mirror-v1")
    parser.add_argument("--llm-config", type=Path, default=PROJECT_ROOT / "llm_style_interview.json")
    parser.add_argument("--route-name", default="gpt-6.1-sol-interview")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--style-prompt", type=Path, default=DEFAULT_STYLE_PROMPT)
    parser.add_argument("--interview-session", type=Path, default=DEFAULT_INTERVIEW_SESSION)
    parser.add_argument("--brain-tokens", type=int, default=512)
    parser.add_argument("--reply-tokens", type=int, default=768)
    parser.add_argument("--mode", choices=("fast", "layered"), default="fast")
    args = parser.parse_args(argv)
    if not 128 <= args.brain_tokens <= 4096 or not 128 <= args.reply_tokens <= 4096:
        parser.error("brain-tokens 和 reply-tokens 必须在 128–4096 之间")
    try:
        brain, language = _select_model_clients(args.llm_config, args.route_name,
                                                args.brain_tokens, args.reply_tokens)
        layers = _default_layers(args.profile, args.style_prompt, args.interview_session)
        engine = MirrorChatEngine(args.root, brain, language, layers=layers,
                                  model_name=getattr(language, "model", args.route_name), mode=args.mode)
    except Exception as error:
        parser.error(f"启动失败：{error}")
    app = MirrorChatApp(engine)
    MirrorHandler.app = app
    server = ThreadingHTTPServer((args.host, args.port), MirrorHandler)
    print(f"用户 分层仿真人试聊台：http://{args.host}:{args.port}/")
    print(f"模型：{getattr(language, 'model', args.route_name)}（{'单次快速' if args.mode == 'fast' else '脑层+语言层双阶段'}；密钥只由服务端配置读取）")
    print(f"本地草案与对话目录：{args.root.resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n试聊台已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

