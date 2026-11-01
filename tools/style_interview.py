"""Lyn 风格主动访谈台：本机网页界面、主动选题和可回滚导出。

默认只使用确定性规则分析答案，不联网、不读取 Codex/微信历史，也不做心理诊断。
只有显式传入 --open 才会尝试打开浏览器；答案和导出文件都留在本机。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from urllib.parse import urlparse


AXES = {
    "identity": "自我身份与角色",
    "life_story": "经历与转折",
    "values": "价值观与底线",
    "drives": "动机与欲望",
    "goals": "目标与未来",
    "cognition": "思考与决策",
    "affect": "情绪与压力",
    "social": "关系与沟通",
    "behavior": "行动习惯",
    "voice": "说话方式",
    "structure": "回答结构",
    "evidence": "证据与事实",
    "autonomy": "推进与自主性",
    "tradeoff": "取舍与风险",
    "correction": "纠偏方式",
    "conflict": "质疑与冲突",
    "memory": "记忆边界",
    "initiative": "主动性",
    "humor": "幽默与情绪",
    "permission": "授权边界",
}

# These dimensions are useful to the private self model but unsafe to copy
# into a small group-chat persona prompt by default.
PRIVATE_AXES = {"identity", "life_story", "values", "drives", "goals", "affect"}

MODEL_REVIEW_SYSTEM = """你是“日常聊天分身”的主动学习审阅器。目标是模拟用户平常和朋友、群友聊天时可观察的说话、接话、情绪回应、幽默、关系距离和聊天节奏；不是把用户训练成研究/项目助理。只根据用户明确提供的回答总结可观察倾向，不作心理诊断、不推断敏感属性、不复述隐私原文。
每轮综合 all_answers、既有观察、覆盖度、历史追问题目和矛盾点，不能只追着 latest_answer 问。先简要更新当前画像，再给 0–3 条有证据的候选观察；如仍有会明显影响分身表现的未知点，生成 3–4 道互相独立、具体、能区分未知点的问题（剩余额度不足时减少）。问题应从“覆盖领域”转向精细校准：真实口头禅/语气词、句长和标点、表情使用、接话/追问/建议比例、话题切换和收尾、熟人间调侃强度、对不同关系的语气差异；优先用 A/B 两种具体回复让用户选择并修改成自己的话。不要为凑题重复已经有明确答案的场景。每批至少覆盖 3 个不同的 domain，且最多 1 题属于 work_research。domain 只能是 conversation_flow、emotional_response、humor_boundary、relationship_distance、self_disclosure、chat_rhythm、daily_interests、conflict_repair、values_decision、work_research。
优先探索：闲聊如何开场和接话；对方分享开心/烦恼时会怎么回；玩笑和调侃的尺度；熟人、普通群友和不熟的人之间如何调整距离；会主动分享什么日常；消息、沉默和主动联系的节奏；兴趣和生活话题；误会或玩笑过界后的修复。研究、工程、工作偏好可以保留，但此前已经覆盖较多，除非存在明确冲突或关键盲点，否则不要再问；每批最多一题。
问题必须问真实场景中“你会怎么做/怎么说”，最好要求写一句真实会发的话；不要用抽象人格标签、心理量表式评价、职场/科研场景连续占题，也不要预设用户应当怎样。选择题的选项互斥且中性，应允许用补充说明纠正选项；开放题保持简短。把 known_adaptive_questions 视为禁重清单：不能换几个近义词重问相同情境；只有新题明确测试此前没覆盖的行为边界或语言细节时，才可沿用同一大领域，并在 prompt 中说明与旧题的区别。候选观察必须带真实答案题号，不把推测写成事实。
只有同时满足以下条件才设置 sufficient=true：日常闲聊、情绪回应、幽默边界、关系远近、主动联系/收尾、自我分享和日常兴趣都有具体自述；至少有数条可直接模仿的自然表达样本；不同关系和场景下的稳定差异已基本清楚；没有会明显改变聊天结果的重大矛盾或盲区。若仍有高价值缺口，sufficient=false 并继续提问；用户明确要求额外一轮时，即使已接近标准，也必须再给一批有区分力的校准题并返回 sufficient=false。若 sufficient=true，questions 必须为空数组，并简述可部署为 V1 的依据及剩余低优先级未知点。不要为了追求心理层面的“完全了解”无限提问；目标是足够像的日常聊天 V1。
严格返回 JSON：{"summary":"...","sufficient":true,"completion_reason":"...","observations":[{"axis":"...","claim":"...","evidence_question_ids":["..."],"confidence":"low|medium|high"}],"questions":[{"axis":"...","domain":"conversation_flow|emotional_response|humor_boundary|relationship_distance|self_disclosure|chat_rhythm|daily_interests|conflict_repair|values_decision|work_research","kind":"single|text","title":"...","prompt":"...","options":[{"label":"...","value":"..."}]}]}。axis 必须是 identity、life_story、values、drives、goals、cognition、affect、social、behavior、voice、structure、evidence、autonomy、tradeoff、correction、conflict、memory、initiative、humor、permission 之一。evidence_question_ids 必须来自 all_answers。single 提供 2–5 个选项，value 只能由 ASCII 字母、数字和下划线组成；text 的 options 为空数组。不要输出思维链。"""

ADAPTIVE_DOMAINS = {
    "conversation_flow": "闲聊开场与接话",
    "emotional_response": "情绪回应与支持",
    "humor_boundary": "幽默与玩笑边界",
    "relationship_distance": "关系距离与熟悉度",
    "self_disclosure": "自我分享与话题选择",
    "chat_rhythm": "聊天节奏与主动联系",
    "daily_interests": "兴趣与日常生活",
    "conflict_repair": "误会、分歧与修复",
    "values_decision": "日常判断与选择",
    "work_research": "工作、科研与工程",
}

CASUAL_FALLBACK_QUESTIONS = (
    {"axis": "social", "domain": "conversation_flow", "kind": "text", "title": "群聊安静时怎么接话",
     "prompt": "熟人群安静了一阵，你看到一件有趣的小事。你会直接发出来、等话题，还是先铺垫？写一句你真会发的话。", "options": []},
    {"axis": "affect", "domain": "emotional_response", "kind": "text", "title": "朋友来吐槽时",
     "prompt": "朋友在群里说今天过得很糟，但没明确求建议。你通常会怎么回？请写一句真实会发的话。", "options": []},
    {"axis": "humor", "domain": "humor_boundary", "kind": "text", "title": "玩笑开过头时",
     "prompt": "熟人拿你开玩笑，你觉得有点过了，但又不想把气氛弄僵。你会怎么接？写出你可能发的原句。", "options": []},
    {"axis": "social", "domain": "relationship_distance", "kind": "text", "title": "面对不太熟的群友",
     "prompt": "一个不太熟的群友私聊你聊日常，你通常会聊到什么程度、怎样判断要不要继续聊？按你的真实习惯回答。", "options": []},
    {"axis": "voice", "domain": "self_disclosure", "kind": "text", "title": "别人问你最近怎样",
     "prompt": "群友随口问“最近在忙啥/过得咋样”，你通常会分享哪一层？写一句你平时可能会发的回答。", "options": []},
    {"axis": "initiative", "domain": "chat_rhythm", "kind": "single", "title": "看到消息但暂时没话说",
     "prompt": "你看见熟人发来一条不急着处理的消息，但一时不知道怎么接。你通常会怎么做？",
     "options": [{"label": "先回个简短反应，之后再接着聊", "value": "brief_then_return"},
                 {"label": "想好内容再回复，不刻意占位", "value": "reply_when_ready"},
                 {"label": "看关系和话题，有时表情回应、有时先放着", "value": "context_dependent"}]},
    {"axis": "drives", "domain": "daily_interests", "kind": "text", "title": "自然聊起来的话题",
     "prompt": "不聊工作或项目时，什么日常话题最容易让你多说几句？有没有你会主动丢进群里的东西？", "options": []},
    {"axis": "values", "domain": "values_decision", "kind": "text", "title": "日常小选择怎么表达",
     "prompt": "朋友群里几个人选电影、餐馆或周末安排，大家问你偏好时，你通常会怎么选、怎么说？写一句真实会发的话。", "options": []},
    {"axis": "conflict", "domain": "conflict_repair", "kind": "text", "title": "一句话被误会时",
     "prompt": "你发了句玩笑或简短回复，对方明显理解偏了。你会怎么补一句把意思说清、又不显得过度解释？", "options": []},
)

AXIS_DEFAULT_DOMAINS = {
    "voice": "conversation_flow", "structure": "conversation_flow", "social": "relationship_distance",
    "affect": "emotional_response", "humor": "humor_boundary", "memory": "self_disclosure",
    "initiative": "chat_rhythm", "drives": "daily_interests", "conflict": "conflict_repair",
    "values": "values_decision", "cognition": "values_decision", "identity": "self_disclosure",
    "life_story": "self_disclosure", "goals": "values_decision", "behavior": "daily_interests",
    "evidence": "work_research", "autonomy": "work_research", "tradeoff": "work_research",
    "correction": "conflict_repair", "permission": "work_research",
}


def _question_domain(axis: str, raw: Any = None) -> str:
    """Return a known adaptive domain while keeping old JSON responses compatible."""
    if isinstance(raw, str) and raw in ADAPTIVE_DOMAINS:
        return raw
    return AXIS_DEFAULT_DOMAINS.get(axis, "conversation_flow")

ADAPTIVE_QUESTION_LIMIT = 32
ADAPTIVE_BATCH_MAX = 4


@dataclass(frozen=True)
class Question:
    id: str
    axis: str
    kind: str
    title: str
    prompt: str
    options: tuple[dict[str, Any], ...] = ()
    required: bool = True
    followup_for: str | None = None
    domain: str | None = None

    def public(self) -> dict[str, Any]:
        result = {"id": self.id, "axis": self.axis, "axis_label": AXES[self.axis],
                "kind": self.kind, "title": self.title, "prompt": self.prompt,
                "options": list(self.options), "required": self.required}
        if self.domain in ADAPTIVE_DOMAINS:
            result["domain"] = self.domain
            result["domain_label"] = ADAPTIVE_DOMAINS[self.domain]
        return result


def option(label: str, value: str, **signals: float) -> dict[str, Any]:
    return {"label": label, "value": value, "signals": signals}


QUESTIONS: tuple[Question, ...] = (
    Question("identity_core", "identity", "text", "先说你是谁", "如果把你介绍给一个长期合作的 Agent，你希望它先知道哪三件关于你的事？", (), required=False),
    Question("identity_roles", "identity", "multi", "你现在扮演哪些角色？", "可多选。哪些角色会明显影响你的判断和说话？", (
        option("探索者/研究者", "explorer", cognition=.5, drives=.5),
        option("建造者/工程师", "builder", behavior=.5, drives=.5),
        option("决策者/负责人", "owner", autonomy=.5, permission=.5),
        option("学习者/学生", "learner", cognition=.5, goals=.5),
    )),
    Question("life_turning", "life_story", "text", "经历过什么转折", "讲一个真实的转折点：发生了什么，它怎样改变了你现在的做事方式？不想展开可以跳过。", (), required=False),
    Question("values_core", "values", "multi", "你真正看重什么？", "最多选四项。哪些价值一旦被牺牲，你会觉得事情做错了？", (
        option("真实/诚实", "truth", values=1, evidence=.5),
        option("自由/自主", "freedom", values=1, autonomy=.5),
        option("创造/新意", "creation", values=1, drives=.5),
        option("可靠/负责", "reliability", values=1, tradeoff=.5),
    )),
    Question("values_conflict", "values", "single", "价值冲突时", "当“做得更快”和“做得更可靠”冲突，你更不能接受哪种代价？", (
        option("为了快而把没验证的事说成完成", "false_certainty", values=1, evidence=1),
        option("为了稳而长期不行动", "paralysis", values=1, initiative=1),
        option("两者都不想牺牲，先拆成低风险试验", "split_risk", values=1, tradeoff=1),
    )),
    Question("drives_core", "drives", "single", "什么会让你持续投入？", "下面哪种最能解释你会长期做一件事？", (
        option("好奇心和发现新东西", "curiosity", drives=1, cognition=.5),
        option("把想法变成真实东西", "making", drives=1, behavior=.5),
        option("掌控复杂系统并看到结果", "mastery", drives=1, autonomy=.5),
        option("被理解、被认可或对别人有用", "meaning", drives=1, social=.5),
    )),
    Question("goals_future", "goals", "text", "你想成为什么样", "如果一年后回看这次访谈，什么变化会让你觉得自己真的往前走了？", (), required=False),
    Question("decision_process", "cognition", "single", "你怎么做重要决定？", "面对没有标准答案的选择，你通常先做什么？", (
        option("先搜集事实和案例", "evidence_first", cognition=1, evidence=.5),
        option("先凭直觉选一个方向再试", "intuition_first", cognition=1, initiative=.5),
        option("先拆约束和取舍，再做小实验", "model_then_test", cognition=1, tradeoff=1),
        option("先听信任的人怎么判断", "social_input", cognition=.7, social=1),
    )),
    Question("mind_change", "cognition", "text", "什么能改变你的看法", "描述一次你改变原先判断的真实例子，或者说什么证据通常足以让你改主意。", (), required=False),
    Question("stress_response", "affect", "single", "压力来了会怎样", "当事情连续失败或失控时，你最常见的第一反应是什么？", (
        option("继续拆问题，先解决最小的一块", "decompose", affect=.5, behavior=1),
        option("先停下来恢复，再决定是否继续", "recover", affect=1, behavior=.5),
        option("找人讨论，把卡点说出来", "talk", affect=1, social=1),
        option("会先烦躁/逃避，但过后仍会回来", "avoid_then_return", affect=1, correction=.5),
    )),
    Question("social_conflict", "social", "single", "别人误解你时", "在合作中别人误解了你的意图，你更希望怎样处理？", (
        option("直接澄清事实和边界", "clarify", social=1, conflict=.5),
        option("先理解对方为什么这样理解", "understand", social=1, conflict=.5),
        option("如果不影响结果，就不纠正", "let_go", social=.5, autonomy=.5),
    )),
    Question("behavior_momentum", "behavior", "single", "怎么进入状态", "面对一个重要但不想开始的任务，什么最能让你动起来？", (
        option("先做一个五分钟能完成的小动作", "tiny_start", behavior=1, initiative=1),
        option("先把环境、工具和计划准备好", "setup", behavior=1, structure=.5),
        option("找人约定结果或截止时间", "accountability", behavior=.7, social=.7),
        option("等真正有兴趣/有压力时自然启动", "pressure_start", behavior=.5, affect=.5),
    )),
    Question("self_blindspot", "identity", "text", "你可能看错自己的地方", "你觉得自己最可能在哪件事上自我误判？这是自我描述，不要求我替你下诊断。", (), required=False),
    Question("voice_length", "voice", "single", "先定一个基调", "你希望 Agent 平时说话有多长？", (
        option("短脆，能一句说清就不展开", "short", voice=1, structure=.5),
        option("中等，结论后给关键依据", "medium", voice=.5, structure=1),
        option("按问题复杂度展开", "adaptive", voice=.5, structure=1, evidence=.5),
    )),
    Question("answer_order", "structure", "single", "你更认哪种回答顺序？", "同一件事下面哪种更像你？", (
        option("先给判断，再给依据和下一步", "conclusion_first", structure=1, voice=.5),
        option("先把背景和问题讲完整，再下判断", "context_first", structure=.5, evidence=1),
        option("简单问题直接答，复杂问题再分层", "adaptive", structure=1, voice=.5),
    )),
    Question("evidence_scale", "evidence", "scale", "证据门槛", "下面哪句最接近你的要求？", (
        option("有合理推测就可以先试", "explore", evidence=.2, initiative=1),
        option("关键结论要有来源或实测", "verify", evidence=1, tradeoff=.5),
        option("没有真实验证就不能说完成", "strict", evidence=1, correction=.5),
    )),
    Question("autonomy", "autonomy", "single", "什么时候应该自己推进？", "任务已经说清楚但细节还不完整时，Agent 应该怎么做？", (
        option("先按推荐路线推进，遇到真正阻塞再问", "push", autonomy=1, initiative=1),
        option("先列方案，等我逐项选择", "ask", autonomy=.2, permission=.5),
        option("低风险直接做，高风险先给我一个决策点", "risk_gated", autonomy=1, permission=1, tradeoff=1),
    )),
    Question("uncertainty", "evidence", "single", "不确定时怎么说？", "当资料不完整或接口没有实测时，你更希望看到什么？", (
        option("直接给最可能的判断，别拖", "best_guess", initiative=1, evidence=.2),
        option("把已知、推测、待验证分开", "label", evidence=1, structure=1),
        option("先停下来问我，不要自行假设", "clarify", evidence=.5, permission=.5),
    )),
    Question("correction", "correction", "single", "发现自己说错了时", "Agent 把计划说成完成了，你希望它怎么改？", (
        option("直接承认，按事实重报并继续推进", "own_and_fix", correction=1, autonomy=1),
        option("简短道歉后给正确答案", "brief_apology", correction=.7, voice=1),
        option("解释为什么会错，再给修正方案", "explain", correction=.5, evidence=.5),
    )),
    Question("challenge", "conflict", "single", "你接受怎样的反驳？", "Agent 认为你的方案有硬伤时，最合适的表达是？", (
        option("直接指出硬伤，给替代路线", "direct", conflict=1, evidence=1),
        option("先说可取处，再说问题和边界", "balanced", conflict=.7, voice=.5),
        option("只在有强证据时反驳", "evidence_only", conflict=.5, evidence=1),
    )),
    Question("tradeoff", "tradeoff", "single", "冲突时怎么取舍？", "创新性、可靠性、速度冲突时，你通常优先什么？", (
        option("先做能验证的最小闭环", "validate_first", tradeoff=1, initiative=1),
        option("先追求理论或产品上限", "ambition", tradeoff=.5, initiative=1),
        option("按风险分层：低风险快试，高风险稳做", "risk_split", tradeoff=1, evidence=1),
    )),
    Question("memory", "memory", "multi", "哪些东西值得长期记住？", "可多选。你希望 Agent 长期保留哪些记忆？", (
        option("稳定的说话与协作偏好", "style", memory=1),
        option("已经确认的项目事实", "facts", memory=1, evidence=.5),
        option("一次任务里的临时决定", "temporary", memory=.2),
        option("群聊里的完整原文", "raw_chat", memory=.1, permission=.5),
    )),
    Question("initiative", "initiative", "scale", "主动性边界", "Agent 多久主动汇报一次，才不会打扰？", (
        option("只有完成、失败或需要我决策时", "milestone", initiative=.2, voice=1),
        option("有明显进展就报，平时安静", "progress", initiative=.7),
        option("可以主动发现机会并提出行动", "proactive", initiative=1, permission=.5),
    )),
    Question("humor", "humor", "single", "人设和干活的关系", "你希望有趣的语气怎么和实际工作配合？", (
        option("先把事办对，再少量卖萌", "after_work", humor=1, evidence=1),
        option("闲聊可以放开，任务时收敛", "mode_split", humor=1, structure=.5),
        option("全程保持克制，准确更重要", "restrained", humor=.2, evidence=1),
    )),
    Question("permission", "permission", "single", "哪些动作要你的诏书？", "启动程序、读文件、发群消息、改代码时，怎么分级？", (
        option("低风险只读自动做，外部副作用先问", "side_effect_gate", permission=1, autonomy=1),
        option("只要涉及本机就先问", "local_gate", permission=1, autonomy=.2),
        option("给过一次范围授权后，在范围内自动做", "scoped", permission=.7, autonomy=1),
    )),
    Question("pair_status", "voice", "pair", "哪条更像你？", "同样是汇报进展，你更偏向哪一种？", (
        option("A：接口已连通，2 题烟测通过；完整集还没跑，下一步提高预算后再测。", "a", voice=1, evidence=1, structure=1),
        option("B：整体方案已经跑通，后面再把一些细节优化一下。", "b", voice=.2, evidence=.1),
    )),
    Question("free_example", "voice", "text", "给我一段真实样本", "请写一条你平时会发给协作者的项目消息，主题随意。越像平时说话越有用。", (), required=False),
)


FOLLOWUPS = {
    "voice": Question("followup_voice", "voice", "text", "追问你的语气边界", "刚才的选择还不能区分“短但有信息”和“短到只下命令”。请写一句你满意的短回复，或说明你讨厌哪种说法。", followup_for="voice"),
    "evidence": Question("followup_evidence", "evidence", "single", "追问证据边界", "如果实测成本较高，但方向看起来很有希望，你希望 Agent 先怎么做？", (
        option("先做便宜的只读/离线验证，再决定是否实测", "cheap_probe", evidence=1, tradeoff=1),
        option("直接做小规模真实烟测，尽快得到反馈", "smoke_test", initiative=1, tradeoff=.7),
        option("没有完整证据就先不动", "wait", evidence=1, autonomy=.2),
    ), followup_for="evidence"),
    "autonomy": Question("followup_autonomy", "autonomy", "single", "追问自主推进", "当你说“自己持续推进”时，哪种情况才算真正阻塞？", (
        option("缺少不可替代的账号、授权或关键输入", "hard_block", autonomy=1, permission=1),
        option("有多个路线但无法判断哪条更好", "choice_block", autonomy=.5),
        option("任何可能产生外部副作用的动作", "side_effect", permission=1, autonomy=.7),
    ), followup_for="autonomy"),
    "memory": Question("followup_memory", "memory", "single", "追问记忆撤销", "如果 Agent 记住了一条偏好，后来你说“这次例外”，它应该怎么处理？", (
        option("只对当前任务覆盖，长期偏好不变", "temporary_override", memory=1),
        option("把旧偏好降权，等我重新确认", "reconfirm", memory=.7, correction=.5),
        option("直接更新长期偏好", "replace", memory=.5),
    ), followup_for="memory"),
    "permission": Question("followup_permission", "permission", "text", "写一条授权规则", "请用一句话写出你愿意给 Agent 的本机授权范围，最好包含允许、禁止和需要诏书的动作。", followup_for="permission"),
}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _set_max_tokens(client: Any, max_tokens: int) -> None:
    if type(max_tokens) is not int or not 128 <= max_tokens <= 4096:
        raise ValueError("max_tokens 应为 128–4096 的整数")
    if hasattr(client, "max_tokens"):
        client.max_tokens = max_tokens
    for route in getattr(client, "routes", ()):
        route_client = route.get("client") if isinstance(route, dict) else None
        if route_client is not None and hasattr(route_client, "max_tokens"):
            route_client.max_tokens = max_tokens


class InterviewEngine:
    """主动学习状态机：按覆盖率和不确定度选择下一题。"""

    def __init__(self, *, session_id: str | None = None):
        self.session_id = session_id or secrets.token_hex(8)
        self.started_at = time.time()
        self.answers: list[dict[str, Any]] = []
        self.used: set[str] = set()
        self.axis_scores: dict[str, list[float]] = {axis: [] for axis in AXES}
        self.axis_values: dict[str, list[str]] = {axis: [] for axis in AXES}
        self.examples: list[str] = []
        self.followups_used: set[str] = set()
        self.dynamic_questions: dict[str, Question] = {}
        self.dynamic_queue: list[str] = []
        self.dynamic_question_id: str | None = None
        self.dynamic_count = 0
        self.dynamic_serial = 0
        self.dynamic_priority = False
        self.model_observations: list[str] = []
        self.model_observation_records: list[dict[str, Any]] = []
        self.confirmed_observations: list[str] = []
        self.model_error = ""
        self.sufficient = False
        self.completion_reason = ""
        self.continue_requested = False
        self.last_analysis = "先回答几题，我会根据不确定项调整问题。"

    def question_map(self) -> dict[str, Question]:
        return ({q.id: q for q in QUESTIONS} | {q.id: q for q in FOLLOWUPS.values()} |
                self.dynamic_questions)

    def _tailored_followup(self, axis: str) -> Question:
        question = FOLLOWUPS[axis]
        prior = next((item for item in reversed(self.answers)
                      if item.get("axis") == axis and not item.get("skipped")), None)
        if prior:
            source = self._question(prior["question_id"])
            chosen = next((item["label"] for item in source.options
                           if item["value"] == prior.get("value")), "")
            if chosen:
                return replace(question, prompt=f"你刚才选了“{chosen}”。{question.prompt}")
            if axis == "voice" and prior.get("kind") == "text":
                return replace(question, prompt="我看过你刚才给的真实说话样本。" + question.prompt)
        return question

    def _question(self, question_id: str) -> Question:
        try:
            return self.question_map()[question_id]
        except KeyError as exc:
            raise ValueError("未知问题") from exc

    def _signals(self, question: Question, value: Any) -> tuple[dict[str, float], str]:
        if question.kind == "text":
            text = str(value).strip()
            if not text or len(text) > 1200:
                raise ValueError("文字回答需要 1–1200 个字符")
            return {}, text
        if question.kind == "multi":
            if not isinstance(value, list) or not value or len(value) > 4:
                raise ValueError("至少选择一项，最多选择四项")
            selected = {str(item) for item in value}
            options = {item["value"]: item for item in question.options}
            if not selected <= set(options):
                raise ValueError("选项无效")
            signals: dict[str, float] = {}
            for item in selected:
                for axis, score in options[item].get("signals", {}).items():
                    signals[axis] = signals.get(axis, 0) + float(score)
            return signals, ",".join(sorted(selected))
        value = str(value).strip()
        found = next((item for item in question.options if item["value"] == value), None)
        if found is None:
            raise ValueError("选项无效")
        return {axis: float(score) for axis, score in found.get("signals", {}).items()}, value

    def answer(self, question_id: str, value: Any, *, note: str = "") -> dict[str, Any]:
        question = self._question(question_id)
        if question_id in self.used:
            raise ValueError("这题已经回答过了")
        if not isinstance(note, str) or len(note) > 1200:
            raise ValueError("补充说明最多 1200 个字符")
        signals, normalized = self._signals(question, value)
        self.used.add(question_id)
        if question.followup_for:
            self.followups_used.add(question.followup_for)
        label_map = {item["value"]: item["label"] for item in question.options}
        answer_label = label_map.get(normalized, normalized) if question.kind != "multi" else \
            "；".join(label_map.get(item, item) for item in normalized.split(","))
        answer_row = {"question_id": question_id, "axis": question.axis,
                      "title": question.title, "prompt": question.prompt,
                      "kind": question.kind, "value": normalized, "answer": answer_label,
                      "signals": signals, "at": time.time()}
        if note.strip():
            answer_row["note"] = note.strip()
        self.answers.append(answer_row)
        for axis, score in signals.items():
            if axis in self.axis_scores:
                self.axis_scores[axis].append(_clamp(score))
                self.axis_values[axis].append(normalized)
        if question.kind == "text":
            self.examples.append(normalized)
        if question_id in self.dynamic_questions or question_id.startswith("adaptive_"):
            if question_id in self.dynamic_questions:
                self.dynamic_queue = [item for item in self.dynamic_queue if item != question_id]
                self.dynamic_question_id = self.dynamic_queue[0] if self.dynamic_queue else None
                self.dynamic_priority = bool(self.dynamic_queue)
            self.dynamic_count += 1
        self.last_analysis = self._analysis_after(question, normalized)
        return self.state()

    def skip(self, question_id: str) -> dict[str, Any]:
        """Mark the currently displayed question as skipped.

        Skipping still advances the fixed basic set.  It deliberately does not
        create an observation or an axis score, so a skipped dimension remains
        visible as uncertain in the exported profile.
        """
        question = self._question(question_id)
        if question_id in self.used:
            raise ValueError("这题已经处理过了")
        self.used.add(question_id)
        if question.followup_for:
            self.followups_used.add(question.followup_for)
        if question_id in self.dynamic_questions or question_id.startswith("adaptive_"):
            if question_id in self.dynamic_questions:
                self.dynamic_queue = [item for item in self.dynamic_queue if item != question_id]
                self.dynamic_question_id = self.dynamic_queue[0] if self.dynamic_queue else None
                self.dynamic_priority = bool(self.dynamic_queue)
            self.dynamic_count += 1
        self.answers.append({"question_id": question_id, "axis": question.axis,
                             "title": question.title, "prompt": question.prompt,
                             "kind": question.kind, "skipped": True, "at": time.time()})
        self.last_analysis = "这题已跳过；我会把它保留为未知，不用猜测代替你的自述。"
        return self.state()

    def set_model_review(self, observation: str | dict[str, Any],
                         next_question: dict[str, Any] | None = None,
                         *, summary: str = "", observations: list[dict[str, Any]] | None = None,
                         questions: list[dict[str, Any]] | None = None,
                         sufficient: bool | None = None, completion_reason: str = "") -> None:
        """合并一轮整体审阅，并把模型返回的问题放入队列。

        兼容旧模型的 observation/next_question 格式，但新格式一次生成一批问题。
        队列中的问题全部回答完之前不再次调用模型，避免“只根据上一题追问”。
        """
        records = observations if isinstance(observations, list) else []
        if not records:
            if isinstance(observation, dict):
                records = [observation]
            elif isinstance(observation, str) and observation.strip():
                records = [{"claim": observation, "confidence": "unspecified"}]
        answered_ids = {str(row.get("question_id")) for row in self.answers if not row.get("skipped")}
        fallback_source = str(self.answers[-1].get("question_id", "")) if self.answers else ""
        for item in records[:8]:
            if isinstance(item, str):
                item = {"claim": item, "confidence": "unspecified"}
            if not isinstance(item, dict):
                continue
            claim = item.get("claim", "")
            if not isinstance(claim, str) or not claim.strip():
                continue
            raw_sources = item.get("evidence_question_ids", [])
            sources = [source for source in raw_sources if isinstance(source, str) and source in answered_ids][:8] \
                if isinstance(raw_sources, list) else []
            if not sources and fallback_source:
                sources = [fallback_source]
            confidence = item.get("confidence", "low")
            if confidence not in ("low", "medium", "high", "unspecified"):
                confidence = "low"
            source_axis = next((row.get("axis") for row in self.answers
                                if str(row.get("question_id")) in sources), "")
            claim = claim.strip()[:500]
            self.model_observations.append(claim)
            self.model_observation_records.append({"claim": claim, "axis": item.get("axis", source_axis),
                                                  "evidence_question_ids": sources,
                                                  "confidence": confidence, "review_status": "candidate",
                                                  "source": "model_observation"})
        self.model_observations = self.model_observations[-50:]
        self.model_observation_records = self.model_observation_records[-50:]
        if summary and isinstance(summary, str):
            self.last_analysis = summary.strip()[:800]
        elif self.model_observations:
            self.last_analysis = self.model_observations[-1]
        # A model must explicitly declare that the V1 chat clone is sufficient;
        # missing/legacy fields stay open for another calibration round.
        self.sufficient = bool(sufficient) if isinstance(sufficient, bool) else False
        self.completion_reason = (completion_reason.strip()[:500]
                                  if isinstance(completion_reason, str) else "")
        if self.continue_requested:
            self.sufficient = False
        if self.sufficient and (questions or isinstance(next_question, dict)):
            self.sufficient = False
        if not self.basic_complete or self.dynamic_count >= ADAPTIVE_QUESTION_LIMIT or self.sufficient:
            return
        raw_questions = questions if isinstance(questions, list) else []
        if not raw_questions and isinstance(next_question, dict):
            raw_questions = [next_question]
        if not raw_questions:
            return
        existing_prompts = {q.prompt for q in self.dynamic_questions.values()}
        batch_cap = min(ADAPTIVE_BATCH_MAX, ADAPTIVE_QUESTION_LIMIT - self.dynamic_count)
        candidates: list[dict[str, Any]] = []
        candidate_prompts = set(existing_prompts)
        for item in raw_questions:
            if not isinstance(item, dict):
                continue
            axis, kind = item.get("axis"), item.get("kind")
            title, prompt = item.get("title"), item.get("prompt")
            if (axis not in AXES or kind not in ("single", "text") or
                    not isinstance(title, str) or not title.strip() or len(title) > 100 or
                    not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 700 or
                    prompt.strip() in existing_prompts):
                continue
            options: tuple[dict[str, Any], ...] = ()
            if kind == "single":
                raw_options = item.get("options")
                if not isinstance(raw_options, list) or not 2 <= len(raw_options) <= 5:
                    continue
                clean, seen = [], set()
                for option_item in raw_options:
                    if not isinstance(option_item, dict):
                        clean = []
                        break
                    label, value = option_item.get("label"), option_item.get("value")
                    if (not isinstance(label, str) or not label.strip() or len(label) > 120 or
                            not isinstance(value, str) or not value.isascii() or
                            not value.replace("_", "").isalnum() or len(value) > 40 or value in seen):
                        clean = []
                        break
                    seen.add(value)
                    clean.append({"label": label.strip(), "value": value, "signals": {axis: .5}})
                if not clean:
                    continue
                options = tuple(clean)
            domain = _question_domain(axis, item.get("domain"))
            candidates.append({"axis": axis, "domain": domain, "kind": kind,
                               "title": title.strip(), "prompt": prompt.strip(), "options": options})
            candidate_prompts.add(prompt.strip())

        # Keep the model's useful candidates, but never let one batch become
        # another all-work interview. Fill missing slots from everyday-chat
        # scenarios so the UI always samples several distinct blind spots.
        selected: list[dict[str, Any]] = []
        selected_domains: set[str] = set()
        covered_domains = {
            question.domain or _question_domain(question.axis)
            for question_id, question in self.dynamic_questions.items()
            if question_id in self.used
        }
        for candidate in candidates:
            domain = candidate["domain"]
            if domain in selected_domains:
                continue
            if domain == "work_research" and "work_research" in selected_domains:
                continue
            selected.append(candidate)
            selected_domains.add(domain)
            if len(selected) >= batch_cap:
                break
        minimum_domains = min(3, batch_cap)
        if len(selected_domains) < minimum_domains or not selected:
            for fallback in CASUAL_FALLBACK_QUESTIONS:
                if len(selected) >= batch_cap:
                    break
                domain, prompt = fallback["domain"], fallback["prompt"]
                if domain in selected_domains or domain in covered_domains or prompt in candidate_prompts:
                    continue
                selected.append({**fallback, "options": tuple(fallback.get("options", []))})
                selected_domains.add(domain)
                candidate_prompts.add(prompt)

        # If prior rounds covered most everyday domains, allow a less-preferred
        # fallback as a last resort rather than returning an empty batch.
        if selected and len(selected_domains) < minimum_domains:
            for fallback in CASUAL_FALLBACK_QUESTIONS:
                if len(selected) >= batch_cap or len(selected_domains) >= minimum_domains:
                    break
                domain, prompt = fallback["domain"], fallback["prompt"]
                if domain in selected_domains or prompt in candidate_prompts:
                    continue
                selected.append({**fallback, "options": tuple(fallback.get("options", []))})
                selected_domains.add(domain)
                candidate_prompts.add(prompt)

        for candidate in selected[:batch_cap]:
            self.dynamic_serial += 1
            question_id = f"adaptive_{self.dynamic_serial:02d}"
            self.dynamic_questions[question_id] = Question(
                question_id, candidate["axis"], candidate["kind"], candidate["title"],
                candidate["prompt"], candidate["options"], False, None, candidate["domain"])
            self.dynamic_queue.append(question_id)
        if self.dynamic_question_id is None and self.dynamic_queue:
            self.dynamic_question_id = self.dynamic_queue[0]
        self.dynamic_priority = bool(self.dynamic_queue)

    def _analysis_after(self, question: Question, value: str) -> str:
        labels = AXES[question.axis]
        if question.kind == "text":
            return f"我先把这段作为“{labels}”的真实样本，不会直接把临时措辞当长期规则。"
        if question.axis == "evidence" and value in ("verify", "strict", "label", "cheap_probe"):
            return "目前看，你对事实边界和实测证据的要求偏高；我会继续区分“要来源”和“必须真实跑过”。"
        if question.axis == "autonomy" and value in ("push", "risk_gated", "hard_block"):
            return "目前看，你希望任务已授权后持续推进，只有权限、关键输入或外部副作用才算阻塞。"
        if question.axis == "voice":
            return "目前先记为表达偏好，后面会用真实句子和 A/B 选择校准，避免只靠自我描述。"
        return f"我先记录了你在“{labels}”上的选择；下一题会优先检查相邻边界，而不是重复问同一件事。"

    def _uncertainty(self, axis: str) -> float:
        values = self.axis_scores[axis]
        if not values:
            return 1.0
        mean = sum(values) / len(values)
        spread = sum((item - mean) ** 2 for item in values) / len(values)
        return (1.0 / (1 + len(values))) + min(.5, math.sqrt(spread))

    def next_question(self) -> Question | None:
        # The basic questionnaire is a fixed, auditable sequence.  This is
        # intentional: adaptive questions must never replace a missing base
        # dimension merely because an LLM proposed one early.
        for question in QUESTIONS:
            if question.id not in self.used:
                return question
        answered_axes = {row.get("axis") for row in self.answers if not row.get("skipped")}
        self.dynamic_queue = [question_id for question_id in self.dynamic_queue
                              if question_id in self.dynamic_questions and question_id not in self.used]
        if self.dynamic_queue:
            self.dynamic_question_id = self.dynamic_queue[0]
            self.dynamic_priority = True
            return self.dynamic_questions.get(self.dynamic_question_id)
        if self.dynamic_question_id and self.dynamic_question_id not in self.used:
            question = self.dynamic_questions.get(self.dynamic_question_id)
            if question is not None:
                self.dynamic_queue = [self.dynamic_question_id]
                return question
        if self.sufficient:
            return None
        # Active-learning pass: ask about the least certain already-observed dimension.
        if self.dynamic_count >= ADAPTIVE_QUESTION_LIMIT:
            return None
        followup_axes = [axis for axis in FOLLOWUPS if axis in answered_axes and axis not in self.followups_used
                         and FOLLOWUPS[axis].id not in self.used]
        if followup_axes:
            axis = max(followup_axes, key=lambda item: (self._uncertainty(item), -list(FOLLOWUPS).index(item)))
            return self._tailored_followup(axis)
        return None

    @property
    def basic_complete(self) -> bool:
        return all(question.id in self.used for question in QUESTIONS)

    def _self_model(self, status: str = "draft") -> dict[str, Any]:
        """Build the private, user-facing self model from self-reports only."""
        reports = []
        for row in self.answers:
            if row.get("skipped"):
                continue
            value = str(row.get("answer") or row.get("value") or "").strip()
            if not value:
                continue
            reports.append({
                "question_id": str(row.get("question_id", "")),
                "axis": str(row.get("axis", "")),
                "title": str(row.get("title", "")),
                "answer": value[:1600],
                "source": "user_self_report",
                "review_status": "user_approved" if status == "approved" else "pending_review",
            })
        records = list(self.model_observation_records[-24:])
        if not records:
            records = [{"claim": item, "evidence_question_ids": [], "confidence": "unspecified",
                        "review_status": "candidate"} for item in self.model_observations[-12:]]
        hypotheses = [{**item, "source": "model_observation"} for item in records
                      if item.get("review_status") == "candidate"]
        confirmed = [{**item, "source": "model_observation"} for item in records
                     if item.get("review_status") == "user_confirmed"]
        known_confirmed = {item.get("claim") for item in confirmed}
        confirmed.extend({"claim": item, "evidence_question_ids": [], "confidence": "unspecified",
                          "review_status": "user_confirmed", "source": "legacy_model_observation"}
                         for item in self.confirmed_observations[-12:] if item not in known_confirmed)
        return {
            "schema_version": 1,
            "type": "lyn-self-model",
            "status": status,
            "source_note": "私密自我模型：自述与模型候选观察分层保存。候选观察不应当被当作事实，导出批准版前应逐条审核。",
            "self_reports": reports[-100:],
            "candidate_hypotheses": hypotheses,
            "confirmed_hypotheses": confirmed,
            "coverage": {axis: len(self.axis_scores[axis]) for axis in AXES},
            "uncertainty": {axis: round(self._uncertainty(axis), 3) for axis in AXES},
        }

    def profile(self, status: str = "draft") -> dict[str, Any]:
        def values(axis: str) -> list[str]:
            return self.axis_values[axis][-4:]
        style_rules = []
        if any(v in values("voice") for v in ("short", "medium")):
            style_rules.append("中文为主，表达直接；能短说清时不堆背景。")
        if any(v in values("structure") for v in ("conclusion_first", "adaptive")):
            style_rules.append("先给判断，再给必要依据和下一步；复杂问题再分层。")
        if "adaptive" in values("voice") or "adaptive" in values("structure"):
            style_rules.append("回复长度按问题复杂度调整，避免固定模板。")
        reasoning = []
        if any(v in values("evidence") for v in ("verify", "strict", "label", "cheap_probe")):
            reasoning.append("区分已验证、推测和待验证；关键结论给来源或实测证据。")
        if any(v in values("tradeoff") for v in ("validate_first", "risk_split")):
            reasoning.append("优先做可验证的最小闭环，并明确风险、停止点和回退路径。")
        interaction = []
        if any(v in values("autonomy") for v in ("push", "risk_gated", "hard_block")):
            interaction.append("任务已授权时持续推进，只有关键输入、权限或外部副作用才提问。")
        if any(v in values("correction") for v in ("own_and_fix", "brief_apology")):
            interaction.append("发现错误就按事实纠正，不把计划写成完成。")
        avoid = []
        if any(v in values("voice") for v in ("short", "medium")):
            avoid.append("机械复述、长篇客服腔和无必要的铺垫。")
        if any(v in values("evidence") for v in ("strict", "label")):
            avoid.append("把推测、搜索结果或模拟写成真实完成。")
        # Keep user-authored samples for the shareable chat style.  Adaptive
        # text answers are especially valuable here: they show how the user
        # actually responds to a friend, joke or everyday topic.  They remain
        # samples rather than facts and are still subject to the draft/approved
        # review gate at runtime.
        examples = [str(row.get("answer", ""))[:500] for row in self.answers
                    if not row.get("skipped") and row.get("axis") == "voice"
                    and row.get("kind") == "text"]
        chat_domains = set(ADAPTIVE_DOMAINS) - {"work_research"}
        for row in self.answers:
            if row.get("skipped") or row.get("kind") != "text":
                continue
            question = self.dynamic_questions.get(str(row.get("question_id", "")))
            if question is None or question.domain not in chat_domains:
                continue
            answer = str(row.get("answer", "")).strip()
            if answer:
                examples.append(answer[:500])
        examples = list(dict.fromkeys(examples))[-12:]
        return {
            "schema_version": 1, "status": status, "mode": "casual_chat",
            "source_note": "由本机主动访谈生成；仅包含主人主动回答的可观察表达与协作偏好，导出前应人工审核。",
            "style_rules": style_rules, "reasoning_preferences": reasoning,
            "interaction_preferences": interaction, "avoid": avoid,
            "approved_examples": examples,
            "learning_notes": [item["claim"] for item in self.model_observation_records[-24:]
                               if item.get("review_status") == "user_confirmed"
                               and item.get("axis") not in PRIVATE_AXES][-12:],
            "coverage": {axis: len(self.axis_scores[axis]) for axis in AXES},
            "uncertainty": {axis: round(self._uncertainty(axis), 3) for axis in AXES},
        }

    def prompt(self, status: str = "draft") -> str:
        profile = self.profile(status)
        lines = [
            "你是 Lyn 的日常聊天分身。以下规则只模仿可观察的表达和互动习惯，不代表当前事实，也不能覆盖安全、权限和事实核验。",
            "平常聊天先自然接话，短句、口语和上下文优先；不要把每条消息都写成项目汇报。只有对方认真询问事实、方案或任务时，才使用证据、取舍和下一步结构。",
            "不能编造 Lyn 当前在哪里、正在做什么、见过谁或已经完成了什么；没有实时依据时直接说不确定。",
        ]
        groups = (("说话方式", profile["style_rules"]), ("判断偏好", profile["reasoning_preferences"]),
                  ("协作偏好", profile["interaction_preferences"]), ("避免", profile["avoid"]))
        for title, items in groups:
            if items:
                lines.append(title + "：" + "；".join(items))
        if profile["approved_examples"]:
            lines.append("主人主动提供的表达样本，仅用于语气参考：" + "；".join(profile["approved_examples"]))
        return "\n".join(lines)

    def self_prompt(self, status: str = "draft") -> str:
        model = self._self_model(status)
        lines = [
            "这是 Lyn 的私密自我模型，只能用于获得本人授权的私聊或本地 Agent。",
            "自述是本人提供的上下文；候选假设不是事实，除非 confirmed_hypotheses 中明确标为已确认。",
        ]
        for item in model["self_reports"]:
            lines.append(f"自述·{item['title']}：{item['answer']}")
        for item in model["confirmed_hypotheses"]:
            lines.append("主人确认的观察：" + item["claim"])
        if status != "approved":
            lines.append("这是草案，使用前必须由主人逐条审核。")
        return "\n".join(lines)

    def script(self, status: str = "draft") -> str:
        profile = self.profile(status)
        payload = json.dumps(profile, ensure_ascii=False, indent=2)
        return '''"""由 Lyn 风格访谈台生成的可审查风格策略。"""\n\nSTYLE_PROFILE = ''' + payload + '''\n\n\ndef style_prompt():\n    """把风格规则作为低优先级提示注入；事实和安全规则必须由调用方放在更高优先级。"""\n    sections = [\n        "只模仿可观察的表达和协作偏好，不泄露原始样本，不声称掌握隐藏推理。",\n        "事实、当前任务、工具结果和安全规则优先。",\n    ]\n    for key, label in (("style_rules", "说话方式"), ("reasoning_preferences", "判断偏好"),\n                       ("interaction_preferences", "协作偏好"), ("avoid", "避免")):\n        if STYLE_PROFILE.get(key):\n            sections.append(label + "：" + "；".join(STYLE_PROFILE[key]))\n    return "\\n".join(sections)\n'''

    def state(self) -> dict[str, Any]:
        question = self.next_question()
        profile = self.profile()
        queued = [self.dynamic_questions[question_id].public() for question_id in self.dynamic_queue
                  if question_id in self.dynamic_questions and question_id not in self.used]
        questions = queued or ([question.public()] if question else [])
        return {"session_id": self.session_id, "answers": len(self.answers),
                "total_questions": len(QUESTIONS) + ADAPTIVE_QUESTION_LIMIT,
                "phase": "基础访谈" if not self.basic_complete else "自适应追问",
                "basic_total": len(QUESTIONS),
                "basic_completed": sum(question.id in self.used for question in QUESTIONS),
                "adaptive_count": self.dynamic_count,
                "adaptive_limit": ADAPTIVE_QUESTION_LIMIT,
                "adaptive_batch_remaining": len(queued),
                "questions": questions,
                "question": question.public() if question else None,
                "analysis": self.last_analysis, "profile": profile,
                "candidate_observations": self.model_observations[-12:],
                "candidate_hypotheses": self._self_model("draft")["candidate_hypotheses"],
                "confirmed_observations": self.confirmed_observations[-12:],
                "confirmed_hypotheses": self._self_model("draft")["confirmed_hypotheses"],
                "self_model_preview": self._self_model("draft"),
                "prompt_preview": self.prompt(), "done": question is None and self.sufficient,
                "sufficient": self.sufficient, "completion_reason": self.completion_reason,
                "updated_at": time.time()}

    def to_data(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "started_at": self.started_at,
                "answers": self.answers, "used": sorted(self.used),
                "axis_scores": self.axis_scores, "axis_values": self.axis_values,
                "examples": self.examples, "followups_used": sorted(self.followups_used),
                "dynamic_questions": {key: value.public() for key, value in self.dynamic_questions.items()},
                "dynamic_queue": list(self.dynamic_queue),
                "dynamic_question_id": self.dynamic_question_id, "dynamic_count": self.dynamic_count,
                "dynamic_serial": self.dynamic_serial,
                "dynamic_priority": self.dynamic_priority,
                "sufficient": self.sufficient, "completion_reason": self.completion_reason,
                "continue_requested": self.continue_requested,
                "model_observations": self.model_observations, "model_error": self.model_error,
                "model_observation_records": self.model_observation_records,
                "confirmed_observations": self.confirmed_observations,
                "last_analysis": self.last_analysis}

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "InterviewEngine":
        if not isinstance(data, dict) or not isinstance(data.get("answers", []), list):
            raise ValueError("访谈会话文件格式不正确")
        engine = cls(session_id=str(data.get("session_id") or secrets.token_hex(8)))
        engine.started_at = float(data.get("started_at", time.time()))
        engine.answers = data.get("answers", [])[-100:]
        engine.used = {str(value) for value in data.get("used", []) if isinstance(value, str)}
        known = set(AXES)
        engine.axis_scores = {axis: [float(x) for x in data.get("axis_scores", {}).get(axis, [])[-20:]]
                              for axis in known}
        engine.axis_values = {axis: [str(x)[:1200] for x in data.get("axis_values", {}).get(axis, [])[-20:]]
                              for axis in known}
        engine.examples = [str(x)[:1200] for x in data.get("examples", [])[-10:]]
        engine.followups_used = {str(value) for value in data.get("followups_used", []) if value in FOLLOWUPS}
        for key, raw in data.get("dynamic_questions", {}).items():
            if (not isinstance(raw, dict) or raw.get("axis") not in known or
                    raw.get("kind") not in ("single", "text")):
                continue
            options = raw.get("options", [])
            if not isinstance(options, list):
                continue
            engine.dynamic_questions[str(key)] = Question(
                str(key), raw["axis"], raw["kind"], str(raw.get("title", "追问"))[:80],
                str(raw.get("prompt", ""))[:600], tuple(options), False, None,
                _question_domain(raw["axis"], raw.get("domain")))
        dynamic_id = data.get("dynamic_question_id")
        engine.dynamic_question_id = dynamic_id if dynamic_id in engine.dynamic_questions else None
        raw_queue = data.get("dynamic_queue", [])
        if not isinstance(raw_queue, list):
            raw_queue = []
        engine.dynamic_queue = [str(item) for item in raw_queue
                                if item in engine.dynamic_questions and item not in engine.used]
        if not engine.dynamic_queue and engine.dynamic_question_id and engine.dynamic_question_id not in engine.used:
            engine.dynamic_queue = [engine.dynamic_question_id]
        # Older sessions counted fixed follow-ups in ``dynamic_count``.  That
        # made the adaptive budget appear exhausted after a couple of rounds,
        # hiding the retry action even though fewer than twelve adaptive
        # questions had been answered.  Rebuild the counter from adaptive IDs;
        # unanswered queued questions do not consume budget.
        adaptive_answer_ids = {
            str(row.get("question_id")) for row in engine.answers
            if not row.get("skipped") and str(row.get("question_id", "")).startswith("adaptive_")
        }
        adaptive_skipped_ids = {
            str(row.get("question_id")) for row in engine.answers
            if row.get("skipped") and str(row.get("question_id", "")).startswith("adaptive_")
        }
        known_adaptive_ids = set(engine.dynamic_questions)
        consumed_adaptive = (adaptive_answer_ids | adaptive_skipped_ids) & known_adaptive_ids
        if not consumed_adaptive:
            # Keep compatibility with a hand-created/very old session that
            # did not persist dynamic question objects but did persist rows.
            consumed_adaptive = adaptive_answer_ids | adaptive_skipped_ids
        engine.dynamic_count = max(0, min(ADAPTIVE_QUESTION_LIMIT, len(consumed_adaptive)))
        engine.dynamic_serial = max(0, min(100, int(data.get("dynamic_serial", len(engine.dynamic_questions)))))
        engine.dynamic_priority = bool(data.get("dynamic_priority", False))
        engine.sufficient = data.get("sufficient") is True
        engine.completion_reason = str(data.get("completion_reason", ""))[:500]
        engine.continue_requested = data.get("continue_requested") is True
        engine.model_observations = [str(x)[:500] for x in data.get("model_observations", [])[-50:]]
        engine.model_observation_records = [item for item in data.get("model_observation_records", [])[-50:]
                                            if isinstance(item, dict) and isinstance(item.get("claim"), str)]
        engine.confirmed_observations = [str(x)[:500] for x in data.get("confirmed_observations", [])[-50:]]
        engine.model_error = str(data.get("model_error", ""))[:300]
        engine.last_analysis = str(data.get("last_analysis", "访谈已恢复。"))[:500]
        return engine

    def export(self, output_dir: Path, status: str = "draft") -> dict[str, Any]:
        if status not in ("draft", "approved"):
            raise ValueError("status 必须是 draft 或 approved")
        output_dir.mkdir(parents=True, exist_ok=True)
        profile = self.profile(status)
        prefix = "lyn-style-profile.approved" if status == "approved" else "lyn-style-profile.draft"
        profile_path = output_dir / f"{prefix}.json"
        prompt_path = output_dir / ("lyn-style-prompt.approved.txt" if status == "approved" else "lyn-style-prompt.draft.txt")
        script_path = output_dir / ("lyn-style-policy.approved.py" if status == "approved" else "lyn-style-policy.draft.py")
        answers_path = output_dir / "lyn-style-interview.answers.jsonl"
        profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        prompt_path.write_text(self.prompt(status) + "\n", encoding="utf-8")
        script_path.write_text(self.script(status), encoding="utf-8")
        private_model_path = output_dir / ("lyn-self-model.approved.json" if status == "approved" else "lyn-self-model.draft.json")
        private_prompt_path = output_dir / ("lyn-self-prompt.approved.txt" if status == "approved" else "lyn-self-prompt.draft.txt")
        private_script_path = output_dir / ("lyn-self-policy.approved.py" if status == "approved" else "lyn-self-policy.draft.py")
        private_model_path.write_text(json.dumps(self._self_model(status), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        private_prompt_path.write_text(self.self_prompt(status) + "\n", encoding="utf-8")
        private_script_path.write_text(
            '"""由主动访谈生成的私密自我模型；仅供本人授权的本地/私聊 Agent 使用。"""\n\n'
            + "SELF_MODEL = " + json.dumps(self._self_model(status), ensure_ascii=False, indent=2) + "\n\n"
            + "def self_prompt():\n    return " + repr(self.self_prompt(status)) + "\n",
            encoding="utf-8")
        with answers_path.open("w", encoding="utf-8", newline="\n") as stream:
            for row in self.answers:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        return {"profile": str(profile_path), "prompt": str(prompt_path),
                "script": str(script_path), "self_model": str(private_model_path),
                "self_prompt": str(private_prompt_path), "self_script": str(private_script_path),
                "answers": str(answers_path), "status": status}


HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lyn 风格主动访谈台</title>
<style>
:root{color-scheme:dark;--bg:#111827;--panel:#1f2937;--muted:#9ca3af;--text:#f3f4f6;--accent:#60a5fa;--good:#34d399;--line:#374151}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 system-ui,-apple-system,"Microsoft YaHei",sans-serif}
main{max-width:1100px;margin:0 auto;padding:28px 18px 60px}.top{display:flex;justify-content:space-between;gap:20px;align-items:start}.eyebrow{color:var(--accent);font-weight:700;letter-spacing:.08em}.muted{color:var(--muted)}h1{margin:3px 0 8px;font-size:28px}h2{font-size:18px;margin:0 0 12px}.grid{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(300px,.7fr);gap:16px;margin-top:20px}.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px}.progress{height:8px;background:#111827;border-radius:99px;overflow:hidden;margin:18px 0}.progress i{display:block;height:100%;background:var(--accent);width:0;transition:width .25s}.tag{display:inline-block;border:1px solid #2563eb;color:#bfdbfe;border-radius:99px;padding:2px 9px;font-size:12px}.option{display:block;width:100%;text-align:left;background:#111827;border:1px solid var(--line);border-radius:10px;color:var(--text);padding:12px;margin:9px 0;cursor:pointer}.option:hover,.option.selected{border-color:var(--accent);background:#172554}.option small{display:block;color:var(--muted);margin-top:2px}.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}button{border:0;border-radius:9px;padding:9px 14px;cursor:pointer;background:var(--accent);color:#08111f;font-weight:700}button.secondary{background:#374151;color:var(--text)}button.danger{background:#7f1d1d;color:#fecaca}.analysis{border-left:3px solid var(--good);padding:9px 12px;background:#064e3b22;margin-top:16px}.axis{margin:10px 0}.axis-head{display:flex;justify-content:space-between;font-size:13px}.bar{height:7px;background:#111827;border-radius:99px;overflow:hidden}.bar i{display:block;height:100%;background:var(--good)}textarea{width:100%;min-height:130px;background:#111827;color:var(--text);border:1px solid var(--line);border-radius:9px;padding:11px;resize:vertical}.check{display:flex;gap:8px;align-items:start;padding:8px 0}.preview{white-space:pre-wrap;background:#111827;border-radius:9px;padding:12px;font-size:13px;max-height:330px;overflow:auto}.notice{padding:10px 12px;border-radius:8px;background:#451a03;color:#fed7aa;margin-top:12px;font-size:13px}.hidden{display:none}@media(max-width:800px){.grid{grid-template-columns:1fr}.top{display:block}}
</style></head><body><main>
<div class="top"><div><div class="eyebrow">LYN STYLE LAB</div><h1>日常聊天分身访谈台</h1><div class="muted">目标是还原平常和朋友、群友聊天的方式；自适应追问会主动覆盖接话、情绪、幽默、关系、兴趣和聊天节奏。</div></div><div class="muted"><div id="session">本机会话</div><div id="mode"></div></div></div>
<div class="notice" id="privacy">这里收集表达、协作和可观察的决策偏好，不是心理诊断。不要输入密码、密钥等敏感信息；答案保存在本机输出目录。</div>
<div class="progress"><i id="progress"></i></div>
<div class="grid"><section class="card"><div id="phase"></div><div id="question"></div><div class="analysis" id="analysis"></div><div class="actions"><button class="secondary" onclick="skipQuestion()">跳过这题</button><button class="secondary" onclick="resetSession()">清空重来</button></div></section>
<aside class="card"><h2>基础题进度</h2><div id="coverage"></div><h2 style="margin-top:20px">候选观察</h2><div class="preview" id="candidates"></div><h2 style="margin-top:20px">已确认观察</h2><div class="preview" id="confirmed"></div><div class="actions"><button class="secondary" onclick="confirmObservations()">确认当前候选</button></div><h2 style="margin-top:20px">群聊提示词预览</h2><div class="preview" id="preview"></div><h2 style="margin-top:20px">私密自我模型预览</h2><div class="preview" id="selfPreview"></div><div class="actions"><button onclick="exportFiles('draft')">导出草案</button><button onclick="exportFiles('approved')">确认并导出</button></div><div class="muted" id="exported"></div></aside></div>
</main><script>
let state=null, selected=[];
async function api(path, opts={}){let r=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});let d=await r.json();if(!r.ok)throw Error(d.error||'请求失败');return d}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function formatHypotheses(items){return (items||[]).map(x=>`[${esc(x.confidence||'unknown')}] ${esc(x.claim||'')}（证据：${esc((x.evidence_question_ids||[]).join(', ')||'未标注')}）`).join('\\n')||'暂无。'}
function render(){if(!state)return;document.getElementById('session').textContent=`本机会话 ${state.session_id} · 已答 ${state.answers} 题`;document.getElementById('mode').textContent=state.mode||'';document.getElementById('phase').innerHTML=`<span class="tag">${esc(state.phase||'访谈')}</span> <span class="muted">基础题 ${state.basic_completed}/${state.basic_total} · 自适应追问 ${state.adaptive_count}/${state.adaptive_limit}</span>`;document.getElementById('privacy').textContent=state.mode&&state.mode.startsWith('模型访谈')?'模型访谈已启用：提交后，全部已答访谈、既有观察和覆盖情况会发送到已配置模型服务，以分析画像并生成一批覆盖不同生活聊天场景的问题。不要输入密钥或与风格无关的敏感信息。':'本机模式：答案只留在本机，不会发送到模型服务。这里收集表达、协作和可观察的聊天习惯，不是心理诊断。';document.getElementById('progress').style.width=Math.min(100,(state.basic_completed+state.adaptive_count)/(state.total_questions||24)*100)+'%';document.getElementById('analysis').textContent=state.analysis+(state.model_error?' '+state.model_error:'');document.getElementById('preview').textContent=state.prompt_preview;document.getElementById('candidates').textContent=formatHypotheses(state.candidate_hypotheses);document.getElementById('confirmed').textContent=formatHypotheses(state.confirmed_hypotheses);document.getElementById('selfPreview').textContent=JSON.stringify(state.self_model_preview||{},null,2);let c=state.profile.coverage;document.getElementById('coverage').innerHTML=Object.entries(c).map(([k,n])=>`<div class="axis"><div class="axis-head"><span>${esc(axisLabel(k))}</span><span>${n?`已采样 ${n}`:'待了解'}</span></div><div class="bar"><i style="width:${Math.min(100,n*32)}%"></i></div></div>`).join('');let q=state.question;if(!q){let retryable=!!state.model_error;let continuing=!!state.continue_available;let canReview=!!state.retry_available||continuing||(state.phase==='自适应追问'&&!state.sufficient&&state.basic_completed===state.basic_total&&state.adaptive_count<state.adaptive_limit&&state.mode&&state.mode.startsWith('模型访谈'));let reviewTitle=retryable?'模型分析未完成':(continuing?'当前 V1 已完成':'基础答案已完成');let reviewText=retryable?(state.adaptive_count>=state.adaptive_limit?'回答已保存。追问额度已用完；重试只更新画像，不会新增问题。':'回答已保存且未重复计数。点击重试，重新综合全部答案并生成下一批问题。'):(continuing?(state.completion_reason||'当前画像已达到可用标准；如果你愿意，可以继续校准更细的口头禅、节奏和关系差异。'):'点击后会把全部已答访谈和覆盖情况发送给配置的模型，分析尚未摸清的日常聊天场景，并生成一批不同领域的问题。');let reviewButton=retryable?'重试模型分析':(continuing?'继续校准一轮':'分析答案并生成下一批问题');document.getElementById('question').innerHTML=canReview?`<h2>${esc(reviewTitle)}</h2><p>${esc(reviewText)}</p><button onclick="continueAdaptive(${continuing?'true':'false'})">${esc(reviewButton)}</button>`:'<h2>主动访谈完成</h2><p>基础题和可用追问已完成。先检查候选观察，再导出草案或批准版。</p>';return}selected=[];let body=`<span class="tag">${esc(q.axis_label)}</span><h2 style="margin-top:10px">${esc(q.title)}</h2><p>${esc(q.prompt)}</p>`;if(q.kind==='text'){body+=`<textarea id="textAnswer" placeholder="按你平时说话就行，临时决定可以标记为例外。"></textarea><div class="actions"><button onclick="submitText()">提交回答</button></div>`}else if(q.kind==='multi'){body+=q.options.map(o=>`<label class="check"><input type="checkbox" value="${esc(o.value)}" onchange="toggle(this)"><span>${esc(o.label)}</span></label>`).join('')+`<div class="actions"><button onclick="submitMulti()">提交选择</button></div>`}else{body+=q.options.map(o=>`<button class="option" data-value="${esc(o.value)}" onclick="submitChoice('${esc(o.value)}')">${esc(o.label)}</button>`).join('')}document.getElementById('question').innerHTML=body}
const renderSingleQuestion=render;
render=function(){
  renderSingleQuestion();
  const questions=state&&state.questions||[];
  if(questions.length<1||(questions.length===1&&!questions[0].id.startsWith('adaptive_')))return;
  const blocks=questions.map((q,i)=>{
    const answer=q.kind==='text'
      ? `<textarea id="batch_${esc(q.id)}" placeholder="按你的真实做法回答；想不清楚可以留空跳过"></textarea>`
      : q.options.map(o=>`<label class="check"><input type="radio" name="batch_${esc(q.id)}" value="${esc(o.value)}"><span>${esc(o.label)}</span></label>`).join('');
    const note=q.kind==='text'?'':`<textarea id="batch_note_${esc(q.id)}" placeholder="可选：补一句你为什么这样做/这样说"></textarea>`;
    return `<div class="card" style="margin:12px 0"><span class="tag">${esc(q.domain_label||q.axis_label)}</span><h2 style="margin-top:8px">${i+1}. ${esc(q.title)}</h2><p>${esc(q.prompt)}</p>${answer}${note}</div>`;
  }).join('');
  document.getElementById('question').innerHTML=`<h2>本轮自适应追问：${questions.length} 题</h2><p>这些问题综合了你的全部访谈回答和当前不确定点。可留空跳过，答完后再统一分析下一轮。</p>${blocks}<div class="actions"><button onclick="submitAdaptiveBatch()">提交本轮答案</button></div>`;
};
async function submitAdaptiveBatch(){
  const questions=state.questions||[];
  const answers=questions.map(q=>{
    let value=q.kind==='text'?document.getElementById('batch_'+q.id).value.trim():
      (document.querySelector(`input[name="batch_${q.id}"]:checked`)||{}).value||'';
    return {question_id:q.id,value:value||null,skip:!value,note:(document.getElementById('batch_note_'+q.id)||{}).value||''};
  });
  if(answers.every(x=>x.skip)&&!confirm('本批都留空并跳过吗？'))return;
  document.getElementById('analysis').textContent='正在保存本批回答并综合全部访谈内容…';
  try{state=await api('/api/answer-batch',{method:'POST',body:JSON.stringify({answers})});render()}
  catch(e){document.getElementById('analysis').textContent=e.message}
}
function axisLabel(k){return {'identity':'自我身份与角色','life_story':'经历与转折','values':'价值观与底线','drives':'动机与欲望','goals':'目标与未来','cognition':'思考与决策','affect':'情绪与压力','social':'关系与沟通','behavior':'行动习惯','voice':'说话方式','structure':'回答结构','evidence':'证据与事实','autonomy':'推进与自主性','tradeoff':'取舍与风险','correction':'纠偏方式','conflict':'质疑与冲突','memory':'记忆边界','initiative':'主动性','humor':'幽默与情绪','permission':'授权边界'}[k]||k}
function toggle(el){if(el.checked)selected.push(el.value);else selected=selected.filter(x=>x!==el.value)}
async function answer(v){document.getElementById('analysis').textContent=state.mode&&state.mode.startsWith('模型访谈')?'正在分析这条回答并生成追问…':'正在更新画像…';try{state=await api('/api/answer',{method:'POST',body:JSON.stringify({question_id:state.question.id,value:v,note:(document.getElementById('answerNote')||{}).value||''})});render()}catch(e){alert(e.message)}}
function submitChoice(v){answer(v)}function submitMulti(){answer(selected)}function submitText(){let v=document.getElementById('textAnswer').value.trim();if(v)answer(v);else alert('先写一点内容，或点击跳过。')}
async function skipQuestion(){try{state=await api('/api/skip',{method:'POST',body:JSON.stringify({question_id:state.question.id})});render()}catch(e){alert(e.message)}}
async function confirmObservations(){try{state=await api('/api/confirm-observations',{method:'POST'});render()}catch(e){alert(e.message)}}
async function continueAdaptive(force=false){document.getElementById('analysis').textContent=state.model_error?'正在重试模型审阅，已保存回答不会重复计数…':(force?'正在继续校准新盲点…':'正在综合全部回答并生成针对性追问…');try{state=await api('/api/review',{method:'POST',body:JSON.stringify({continue:force})});render()}catch(e){document.getElementById('analysis').textContent=e.message}}
async function resetSession(){if(confirm('清空本机会话和答案？')){state=await api('/api/reset',{method:'POST'});render()}}
async function exportFiles(status){if(status==='approved'&&!confirm('确认将当前画像、访谈观察和你亲自填写的样本标记为 approved？这不会自动修改或重启微信机器人。'))return;try{let d=await api('/api/export',{method:'POST',body:JSON.stringify({status})});document.getElementById('exported').textContent='已导出：'+Object.values(d).join(' | ');alert(status==='approved'?'已导出批准版；接入微信前请检查生成的 JSON 和提示词。':'已导出草案。')}catch(e){alert(e.message)}}
api('/api/state').then(d=>{state=d;render()}).catch(e=>alert(e.message));
</script></body></html>'''


class App:
    def __init__(self, output_dir: Path, *, llm_config: Path | None = None, max_tokens: int = 2048):
        self.output_dir = output_dir.resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.session_path = self.output_dir / "session.json"
        try:
            self.engine = InterviewEngine.from_data(json.loads(self.session_path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            self.engine = InterviewEngine()
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            self.engine = InterviewEngine()
        self.lock = threading.RLock()
        self.llm = None
        self.mode = "本机模式：答案不发送到模型服务"
        if llm_config is not None:
            from wxbot_framework.llm import LLMClient
            self.llm = LLMClient.from_file(llm_config)
            _set_max_tokens(self.llm, max_tokens)
            model_name = getattr(self.llm, "model", "路由模型")
            self.mode = f"模型访谈：答案会发送到已配置服务（{model_name}）"

    def _state(self) -> dict[str, Any]:
        state = self.engine.state()
        state["mode"] = self.mode
        state["model_error"] = self.engine.model_error
        state["retry_available"] = bool(
            self.llm is not None and self.engine.basic_complete and
            self.engine.next_question() is None and
            not self.engine.sufficient and
            (self.engine.dynamic_count < ADAPTIVE_QUESTION_LIMIT or bool(self.engine.model_error))
        )
        state["continue_available"] = bool(
            self.llm is not None and self.engine.basic_complete and
            self.engine.dynamic_count < ADAPTIVE_QUESTION_LIMIT and
            self.engine.sufficient and self.engine.next_question() is None
        )
        return state

    def _review_answer(self):
        if self.llm is None or not self.engine.answers:
            return
        payload = {
            "latest_answer": self.engine.answers[-1],
            "all_answers": self.engine.answers[-80:],
            "known_observations": self.engine.model_observation_records[-20:],
            "profile_draft": self.engine.profile(),
            "private_self_model_draft": self.engine._self_model("draft"),
            "answered_dimensions": sorted({row.get("axis") for row in self.engine.answers
                                            if not row.get("skipped")}),
            "coverage": self.engine.profile().get("coverage", {}),
            "remaining_adaptive_questions": max(0, ADAPTIVE_QUESTION_LIMIT - self.engine.dynamic_count),
            "user_requested_continuation": self.engine.continue_requested,
            "known_adaptive_questions": [
                {"question_id": q.id, "title": q.title, "domain": q.domain or _question_domain(q.axis),
                 "prompt": q.prompt, "answered": q.id in self.engine.used}
                for q in self.engine.dynamic_questions.values()
            ],
        }
        system = MODEL_REVIEW_SYSTEM
        # 路由配置里把 planner 留给复杂的整体画像审阅；直接 LLMClient 不接受 task 参数。
        use_planner = hasattr(self.llm, "routes")
        requested_continuation = self.engine.continue_requested
        try:
            kwargs = {"session": {"plugin": "style_interview", "chat_id": self.engine.session_id}}
            if use_planner:
                kwargs["task"] = "planner"
            try:
                response = self.llm.complete_json(system, json.dumps(payload, ensure_ascii=False), **kwargs)
            except Exception as first_error:
                # 中转站常用的快速路由预算太小；同一请求自动提高到至少 2048，
                # 只重试一次，避免把截断误判成“模型追问失效”。
                if "截断" not in str(first_error) and "truncated" not in str(first_error).lower():
                    raise
                _set_max_tokens(self.llm, 4096)
                response = self.llm.complete_json(system, json.dumps(payload, ensure_ascii=False), **kwargs)
            if not isinstance(response, dict):
                raise ValueError("模型未返回 JSON 对象")
            observations = response.get("observations", [])
            if not isinstance(observations, list):
                observations = []
            observation = response.get("observation", "")
            questions = response.get("questions", [])
            if not isinstance(questions, list):
                questions = []
            # 兼容旧格式，且允许模型只返回一条观察。
            if not observations and isinstance(observation, (str, dict)):
                observations = [observation]
            if not questions and isinstance(response.get("next_question"), dict):
                questions = [response["next_question"]]
            remaining_before_review = max(0, ADAPTIVE_QUESTION_LIMIT - self.engine.dynamic_count)
            self.engine.model_error = ""
            self.engine.set_model_review(observation, response.get("next_question"),
                                         summary=response.get("summary", ""),
                                         observations=observations, questions=questions,
                                         sufficient=response.get("sufficient"),
                                         completion_reason=response.get("completion_reason", ""))
            if requested_continuation and not self.engine.dynamic_queue:
                raise ValueError("你要求继续校准，但模型没有生成下一批问题；可以重试")
            if questions and not self.engine.dynamic_queue:
                if remaining_before_review <= 0:
                    self.engine.last_analysis = (self.engine.last_analysis or "审阅完成") + \
                        "（追问题数已达上限，本次只更新画像，没有新增问题。）"
                    self.engine.sufficient = True
                    self.engine.completion_reason = "已达到追问安全上限；画像可作为当前版本使用。"
                else:
                    raise ValueError("模型返回了问题，但题目格式未通过校验或与已有题目重复；可点击重试")
            if self.engine.dynamic_count >= ADAPTIVE_QUESTION_LIMIT:
                self.engine.sufficient = True
                if not self.engine.completion_reason:
                    self.engine.completion_reason = "已达到追问安全上限；画像可作为当前版本使用。"
            self.engine.continue_requested = False
        except Exception as error:
            # A failed analysis must never discard the user's answer.
            self.engine.model_error = f"模型审阅失败，已保留回答；检查 API、预算后可重试：{str(error)[:180]}"
            self.engine.last_analysis = "模型这次没接住，我保留了回答；请检查 API、预算或稍后重试。"

    def _save_session(self):
        temp = self.session_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(self.engine.to_data(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(self.session_path)

    def json_state(self) -> dict[str, Any]:
        with self.lock:
            return self.engine.state()

    def handle(self, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        body = body or {}
        with self.lock:
            if path == "/api/state":
                return 200, self._state()
            if path == "/api/review":
                if self.llm is None:
                    raise ValueError("启用 --llm-config 后才能让模型分析基础回答并生成自适应追问")
                if not self.engine.basic_complete:
                    raise ValueError("请先完成全部基础题")
                if self.engine.next_question() is not None:
                    raise ValueError("请先回答当前显示的追问")
                if body.get("continue") is True:
                    if self.engine.dynamic_count >= ADAPTIVE_QUESTION_LIMIT:
                        raise ValueError(f"已达到 {ADAPTIVE_QUESTION_LIMIT} 道自适应题的安全上限")
                    self.engine.continue_requested = True
                    self.engine.sufficient = False
                if self.engine.sufficient and body.get("continue") is not True:
                    raise ValueError("模型判断当前画像已足够；如需再找一轮盲点，请使用继续校准按钮")
                if (self.engine.dynamic_count >= ADAPTIVE_QUESTION_LIMIT and
                        not self.engine.model_error):
                    raise ValueError(f"本轮最多进行 {ADAPTIVE_QUESTION_LIMIT} 道自适应追问")
                self._review_answer()
                self._save_session()
                return 200, self._state()
            if path == "/api/answer":
                if body.get("question_id") != (self.engine.next_question().id if self.engine.next_question() else None):
                    raise ValueError("请先回答当前显示的问题")
                result = self.engine.answer(body.get("question_id", ""), body.get("value"),
                                            note=body.get("note", ""))
                if self.engine.basic_complete and not self.engine.dynamic_queue:
                    self._review_answer()
                self._save_session()
                return 200, self._state()
            if path == "/api/answer-batch":
                entries = body.get("answers")
                if not self.engine.basic_complete or not isinstance(entries, list) or not self.engine.dynamic_queue:
                    raise ValueError("当前没有可提交的自适应问题批次")
                expected = list(self.engine.dynamic_queue)
                if not all(isinstance(item, dict) for item in entries):
                    raise ValueError("每条回答都必须是对象")
                submitted = [str(item.get("question_id", "")) for item in entries]
                if submitted != expected:
                    raise ValueError("请一次提交当前批次里的全部问题")
                normalized = []
                for item, question_id in zip(entries, expected):
                    question = self.engine._question(question_id)
                    value = item.get("value")
                    is_skip = item.get("skip") is True or value is None or value == ""
                    if not is_skip:
                        self.engine._signals(question, value)  # validate the full batch before mutating state
                    note = item.get("note", "")
                    if not isinstance(note, str) or len(note) > 1200:
                        raise ValueError("补充说明最多 1200 个字符")
                    normalized.append((question_id, value, is_skip, note))
                for question_id, value, is_skip, note in normalized:
                    if is_skip:
                        self.engine.skip(question_id)
                    else:
                        self.engine.answer(question_id, value, note=note)
                if self.engine.basic_complete and not self.engine.dynamic_queue:
                    self._review_answer()
                self._save_session()
                return 200, self._state()
            if path == "/api/skip":
                qid = body.get("question_id", "")
                if qid != (self.engine.next_question().id if self.engine.next_question() else None):
                    raise ValueError("只能跳过当前显示的问题")
                self.engine.skip(qid)
                if self.engine.basic_complete and not self.engine.dynamic_queue:
                    self._review_answer()
                self._save_session()
                return 200, self._state()
            if path == "/api/confirm-observations":
                self.engine.confirmed_observations = self.engine.model_observations[-50:]
                for item in self.engine.model_observation_records:
                    if item.get("claim") in self.engine.confirmed_observations:
                        item["review_status"] = "user_confirmed"
                self._save_session()
                return 200, self._state()
            if path == "/api/reset":
                self.engine = InterviewEngine()
                self._save_session()
                return 200, self._state()
            if path == "/api/export":
                return 200, self.engine.export(self.output_dir, str(body.get("status", "draft")))
            return 404, {"error": "接口不存在"}


class Handler(BaseHTTPRequestHandler):
    app: App

    def _send(self, status: int, data: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if urlparse(self.path).path == "/":
            self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if urlparse(self.path).path == "/api/state":
            status, data = self.app.handle("/api/state")
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 100_000:
                raise ValueError("请求过大")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("请求格式错误")
            status, data = self.app.handle(path, body)
        except Exception as exc:  # UI receives safe error text; no stack trace to browser.
            status, data = 400, {"error": str(exc)[:300]}
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def log_message(self, format, *args):  # noqa: A002
        return


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="本机 Lyn 风格主动访谈台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output-dir", default=".runtime/style-interview")
    parser.add_argument("--open", action="store_true", help="启动后打开默认浏览器")
    parser.add_argument("--llm-config", type=Path,
                        help="显式启用模型主动访谈；回答会发送到该配置中的模型服务")
    parser.add_argument("--max-tokens", type=int, default=2048,
                        help="本次模型访谈的输出预算，不修改配置文件（默认 2048）")
    args = parser.parse_args(argv)
    output_dir = Path(args.output_dir).expanduser()
    if not output_dir.is_absolute():
        output_dir = Path.cwd() / output_dir
    try:
        app = App(output_dir, llm_config=args.llm_config, max_tokens=args.max_tokens)
    except Exception as error:
        parser.error(f"无法启动访谈服务：{error}")
    Handler.app = app
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"风格主动访谈台已启动：{url}")
    print(app.mode)
    print(f"导出目录：{output_dir.resolve()}")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
