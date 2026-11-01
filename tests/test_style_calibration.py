import json
import tempfile
import unittest
from pathlib import Path

from tools.style_calibration import CALIBRATION_BATCH_SIZE, CalibrationEngine, _load_profile_prompt
from wxbot_framework.style_profile import load_style_profile


class FakeLLM:
    def __init__(self):
        self.calls = []

    def complete_json(self, system, user, *, session=None):
        self.calls.append((system, user, session))
        if len(self.calls) % (CALIBRATION_BATCH_SIZE + 1) == 0:
            return {"rules": [{"claim": "熟人群里先用简短口语接话。",
                               "evidence_ids": [1, 2, 3, 4, 999], "confidence": 0.8}]}
        return {"reply": "我最近在折腾点东西。"}


class StyleCalibrationTests(unittest.TestCase):
    def test_draft_profile_is_available_only_for_explicit_local_calibration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "status": "draft",
                "mode": "casual_chat",
                "style_rules": ["群聊里短句接话。"],
                "approved_examples": [],
            }, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                _load_profile_prompt(path)
            prompt = _load_profile_prompt(path, allow_draft=True)
            self.assertIn("群聊里短句接话。", prompt)
            self.assertIn("draft", prompt)

    def test_four_rated_turns_generate_candidates_but_do_not_approve_them(self):
        with tempfile.TemporaryDirectory() as directory:
            llm = FakeLLM()
            engine = CalibrationEngine(Path(directory), llm)
            turns = [engine.add_turn(f"群友说第{i}种情况") for i in range(1, 5)]
            for turn in turns:
                engine.rate(turn["id"], "mostly_like", rewritten="我会这么回", note="更口语")
            self.assertEqual(len(llm.calls), 5)
            self.assertEqual(engine.approved_rules, [])
            self.assertEqual(engine.candidate_rules[0]["claim"], "熟人群里先用简短口语接话。")
            self.assertEqual(engine.candidate_rules[0]["evidence_ids"], [1, 2, 3, 4])

    def test_approved_rules_export_as_separate_approved_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = CalibrationEngine(Path(directory))
            engine.candidate_rules = [{"claim": "先短句接话。", "evidence_ids": [1], "confidence": .9}]
            engine.approve_rules()
            output = engine.export()
            profile = load_style_profile(output, require_approved=True)
            self.assertEqual(profile.mode, "casual_chat")
            self.assertIn("先短句接话。", profile.as_prompt())
            self.assertTrue(output.is_file())

    def test_rejected_candidates_never_enter_approved_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = CalibrationEngine(Path(directory))
            engine.candidate_rules = [{"claim": "不适用的规则", "evidence_ids": [1], "confidence": .4}]
            engine.reject_rules()
            self.assertEqual(engine.candidate_rules, [])
            self.assertEqual(engine.approved_rules, [])

    def test_turn_feedback_persists_and_stays_out_of_prompt_until_approved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = CalibrationEngine(root, FakeLLM())
            turn = engine.add_turn("有人问我最近忙什么")
            engine.rate(turn["id"], "too_formal", rewritten="最近在瞎折腾", note="短一点")
            restored = CalibrationEngine(root)
            self.assertEqual(restored.turns[0]["rewritten"], "最近在瞎折腾")
            self.assertEqual(restored.turns[0]["rating"], "too_formal")
            self.assertEqual(restored.approved_rules, [])

    def test_rating_requires_existing_turn_and_valid_category(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = CalibrationEngine(Path(directory))
            with self.assertRaises(ValueError):
                engine.rate(1, "very_like")
            turn = engine.add_turn("对方问候")
            with self.assertRaises(ValueError):
                engine.rate(turn["id"], "unknown")


if __name__ == "__main__":
    unittest.main()
