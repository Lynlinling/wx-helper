import json
import tempfile
import unittest
from pathlib import Path

from wxbot_framework.personality import PersonalityState, system_with_personality
from wxbot_framework.style_profile import StyleProfileError, load_style_profile


class StyleProfileTests(unittest.TestCase):
    def write(self, directory, value):
        path = Path(directory) / "profile.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def test_load_injects_only_group_safe_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, {
                "schema_version": 1, "status": "draft",
                "style_rules": ["短句直接"],
                "reasoning_preferences": ["要证据"],
                "interaction_preferences": ["完成后汇报"],
                "avoid": ["不要复述"],
                "approved_examples": ["先给结论，没跑过就说没跑过。"],
                "learning_notes": ["重视可验证证据"],
                "self_model": {"自我身份与角色": ["我是一个喜欢把想法做成系统的人。"]}
            })
            profile = load_style_profile(path)
            self.assertEqual(profile.mode, "casual_chat")
            prompt = system_with_personality("功能", PersonalityState("room"), profile)
            self.assertIn("短句直接", prompt)
            self.assertIn("要证据", prompt)
            self.assertIn("不要泄露原始样本", prompt)
            self.assertIn("主人风格档案（draft）", prompt)
            self.assertIn("先给结论", prompt)
            self.assertNotIn("重视可验证证据", prompt)
            self.assertNotIn("系统的人", prompt)
            self.assertIn("日常闲聊优先自然接话", prompt)

    def test_approved_profile_injects_reviewed_style_notes_but_not_private_self_model(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, {
                "schema_version": 1, "status": "approved",
                "style_rules": ["短句直接"], "learning_notes": ["重视可验证证据"],
                "self_model": {"身份": ["我是一个喜欢把想法做成系统的人。"]}
            })
            profile = load_style_profile(path, require_approved=True)
            prompt = system_with_personality("功能", PersonalityState("room"), profile)
            self.assertIn("重视可验证证据", prompt)
            self.assertNotIn("系统的人", prompt)

    def test_rejects_invalid_status_and_control_characters(self):
        with tempfile.TemporaryDirectory() as directory:
            for value in ({"status": "active"}, {"style_rules": ["坏\x00文本"]}):
                with self.subTest(value=value):
                    with self.assertRaises(StyleProfileError):
                        load_style_profile(self.write(directory, value))

    def test_runtime_loader_requires_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, {"status": "draft", "style_rules": ["短句"]})
            with self.assertRaises(StyleProfileError):
                load_style_profile(path, require_approved=True)

    def test_rejects_unknown_profile_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, {"status": "approved", "mode": "unknown"})
            with self.assertRaises(StyleProfileError):
                load_style_profile(path)

    def test_optional_approved_self_model_is_injected_only_when_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            profile_path = self.write(directory, {"status": "approved", "mode": "casual_chat"})
            self_path = Path(directory) / "self.json"
            self_path.write_text(json.dumps({
                "type": "lyn-self-model", "status": "approved",
                "self_reports": [{"axis": "identity", "title": "身份", "answer": "喜欢做系统"},
                                  {"axis": "permission", "title": "授权", "answer": "仅测试"}]
            }, ensure_ascii=False), encoding="utf-8")
            without = load_style_profile(profile_path)
            with_model = load_style_profile(profile_path, self_model_path=self_path)
            self.assertNotIn("喜欢做系统", without.as_prompt())
            self.assertIn("喜欢做系统", with_model.as_prompt())
            self.assertNotIn("仅测试", with_model.as_prompt())

    def test_empty_profile_keeps_default_personality(self):
        state = PersonalityState("room")
        prompt = system_with_personality("功能", state)
        self.assertIn("当前人格：鲸鱼娘", prompt)
        self.assertIn("功能", prompt)


if __name__ == "__main__":
    unittest.main()
