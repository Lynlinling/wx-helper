import json
import tempfile
import unittest
from pathlib import Path

from tools.style_mirror_chat import (
    FAST_INSTRUCTION,
    LAYER_ORDER,
    MirrorChatEngine,
    _compile_layers,
    _default_layers,
    _normalize_plan,
    _validate_layers,
)


class FakeLLM:
    def __init__(self):
        self.calls = []
        self.max_tokens = 512
        self.model = "gpt-6.1-sol"

    def complete_json(self, system, user, *, session=None):
        self.calls.append((system, user, session))
        if "一次完成结构化判断" in system:
            return {"plan": {"dialogue_act": "react", "stance": "playful", "energy": "low",
                             "relationship": "familiar", "disclosure": "low",
                             "rendering_constraints": ["短一点"]},
                    "reply": "行，晚点再说"}
        if "结构化脑层" in system:
            return {"dialogue_act": "react", "stance": "playful", "energy": "low",
                    "relationship": "familiar", "disclosure": "low",
                    "claim_scope": "provided_facts_only",
                    "rendering_constraints": ["短一点", "可以带点玩笑"],
                    "memory_candidates": []}
        return {"reply": "行，晚点再说"}


class StyleMirrorChatTests(unittest.TestCase):
    def test_layers_have_cyberain_order_and_fixed_guard(self):
        layers = _default_layers(None, None, None)
        self.assertEqual([item["id"] for item in layers], list(LAYER_ORDER))
        self.assertIn("接替用户的发言位置", layers[0]["text"])
        self.assertIn("个人微信文字分身", layers[0]["text"])
        self.assertIn("轻度粗口", layers[2]["text"])
        self.assertIn("熟人间轻度粗口通常是玩笑", layers[2]["text"])
        compiled = _compile_layers(layers, include_renderer=True)
        self.assertIn("固定最高层规则", compiled)
        self.assertIn("语言表达层", compiled)

    def test_fast_mode_treats_interest_as_a_cue_for_initiative(self):
        self.assertIn("个人微信文字分身", FAST_INSTRUCTION)
        self.assertIn("熟人间轻度粗口多数是玩笑", FAST_INSTRUCTION)
        self.assertIn("灵感式新点子只在相关且新鲜时偶尔冒出", FAST_INSTRUCTION)

    def test_normalize_plan_is_strict_and_defaults_unknown_values(self):
        plan = _normalize_plan({"dialogue_act": "made_up", "stance": "playful",
                                "rendering_constraints": ["a", 3, "b"]})
        self.assertEqual(plan["dialogue_act"], "answer")
        self.assertEqual(plan["stance"], "playful")
        self.assertEqual(plan["claim_scope"], "provided_facts_only")
        self.assertEqual(plan["rendering_constraints"], ["a", "b"])

    def test_default_fast_mode_uses_one_model_call_and_persists_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            llm = FakeLLM()
            engine = MirrorChatEngine(root, llm, llm, layers=_default_layers(None, None, None))
            state = engine.chat("今晚打游戏不？", "熟人群")
            self.assertEqual(len(state["turns"]), 1)
            self.assertEqual(state["turns"][0]["assistant"], "行，晚点再说")
            self.assertEqual([x["name"] for x in state["turns"][0]["stages"]], ["fast_brain_language"])
            self.assertEqual(len(llm.calls), 1)
            self.assertTrue((root / "chat-session.json").is_file())
            self.assertTrue((root / "traces.jsonl").is_file())

    def test_layered_mode_locks_brain_before_language(self):
        with tempfile.TemporaryDirectory() as directory:
            llm = FakeLLM()
            engine = MirrorChatEngine(Path(directory), llm, llm,
                                      layers=_default_layers(None, None, None), mode="layered")
            state = engine.chat("今晚打游戏不？", "熟人群")
            self.assertEqual(len(llm.calls), 2)
            self.assertEqual([x["name"] for x in state["turns"][0]["stages"]], ["brain", "language"])

    def test_feedback_is_used_as_correction_and_edit_branches_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            llm = FakeLLM()
            engine = MirrorChatEngine(root, llm, llm, layers=_default_layers(None, None, None))
            engine.chat("第一句")
            first = engine.state()["turns"][0]["id"]
            engine.feedback(first, "unlike", "别这么正式")
            engine.chat("第二句")
            second = engine.state()["turns"][1]["id"]
            engine.edit_and_branch(second, "第二句改写")
            turns = engine.state()["turns"]
            self.assertEqual([x["user"] for x in turns], ["第一句", "第二句改写"])
            self.assertEqual(turns[0]["correction"], "别这么正式")
            payload = json.loads((root / "chat-session.json").read_text(encoding="utf-8"))
            self.assertEqual(len(payload["turns"]), 2)

    def test_topic_specific_correction_does_not_leak_into_unrelated_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            llm = FakeLLM()
            engine = MirrorChatEngine(Path(directory), llm, llm,
                                      layers=_default_layers(None, None, None))
            engine.chat("我们挑个 Minecraft mod 吧")
            turn_id = engine.state()["turns"][0]["id"]
            engine.feedback(turn_id, "unlike", "我更想加生物和工业模组")
            engine.chat("在吗")
            casual_payload = json.loads(llm.calls[-1][1])
            self.assertEqual(casual_payload["confirmed_corrections"], [])
            engine.chat("Minecraft里再加点什么模组？")
            game_payload = json.loads(llm.calls[-1][1])
            self.assertEqual(len(game_payload["confirmed_corrections"]), 1)

    def test_invalid_layer_order_or_length_is_rejected(self):
        layers = _default_layers(None, None, None)
        with self.assertRaises(ValueError):
            _validate_layers(layers[:-1], layers)
        bad = [dict(item) for item in layers]
        bad[0]["text"] = "x" * 6001
        with self.assertRaises(ValueError):
            _validate_layers(bad, layers)


if __name__ == "__main__":
    unittest.main()

