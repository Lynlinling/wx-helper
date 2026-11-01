import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


TOOLS_FILE = Path(__file__).resolve().parents[1] / "tools" / "style_eval.py"
SPEC = importlib.util.spec_from_file_location("style_eval", TOOLS_FILE)
STYLE_EVAL = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(STYLE_EVAL)
GENERATOR_SPEC = importlib.util.spec_from_file_location(
    "style_eval_generate", Path(__file__).resolve().parents[1] / "tools" / "style_eval_generate.py")
STYLE_EVAL_GENERATE = importlib.util.module_from_spec(GENERATOR_SPEC)
assert GENERATOR_SPEC.loader is not None
GENERATOR_SPEC.loader.exec_module(STYLE_EVAL_GENERATE)


class StyleEvalTests(unittest.TestCase):
    def test_override_max_tokens_supports_single_client_and_router(self):
        class Client:
            max_tokens = 512

        class Router:
            def __init__(self):
                self.routes = [{"client": Client()}, {"client": Client()}]

        router = Router()
        STYLE_EVAL_GENERATE._override_max_tokens(router, 1536)
        self.assertEqual([route["client"].max_tokens for route in router.routes], [1536, 1536])
        single = Client()
        STYLE_EVAL_GENERATE._override_max_tokens(single, 1024)
        self.assertEqual(single.max_tokens, 1024)

    def test_extract_users_redacts_local_identifiers_and_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            output = root / "users.jsonl"
            source.write_text(json.dumps({
                "role": "user",
                "source": "manual",
                "text": r"路径 C:\Home\sample\secret.txt wxid_robot 12345@chatroom api_key=sk-test-value",
            }, ensure_ascii=False) + "\n", encoding="utf-8")

            self.assertEqual(STYLE_EVAL.extract_users(source, output), 1)
            row = json.loads(output.read_text(encoding="utf-8"))
            self.assertNotIn("C:\\Users", row["text"])
            self.assertNotIn("wxid_robot", row["text"])
            self.assertNotIn("12345@chatroom", row["text"])
            self.assertNotIn("sk-test-value", row["text"])

    def test_export_pairs_only_emits_complete_selected_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "session.jsonl"
            output = root / "pairs.jsonl"
            rows = [
                {"id": "style_one", "category": "表达", "prompt": "题目",
                 "response_a": "短答", "response_b": "长答", "preferred": "a", "notes": "私密评语"},
                {"id": "style_two", "category": "表达", "prompt": "题目2",
                 "response_a": "", "response_b": "候选", "preferred": "b", "notes": ""},
                {"id": "style_three", "category": "表达", "prompt": "题目3",
                 "response_a": "甲", "response_b": "乙", "preferred": "tie", "notes": ""},
            ]
            session.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

            result = STYLE_EVAL.export_preference_pairs(session, output)
            self.assertEqual(result["pairs"], 1)
            self.assertEqual(result["skipped"], {"missing_response": 1, "tie": 1})
            pair = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(pair["chosen"], "短答")
            self.assertEqual(pair["rejected"], "长答")
            self.assertNotIn("notes", pair)


if __name__ == "__main__":
    unittest.main()
