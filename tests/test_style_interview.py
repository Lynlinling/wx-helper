import json
import tempfile
import unittest
from pathlib import Path

from tools.style_interview import ADAPTIVE_DOMAINS, App, FOLLOWUPS, QUESTIONS, InterviewEngine


class StyleInterviewTests(unittest.TestCase):
    def _finish_basic(self, engine):
        while not engine.basic_complete:
            question = engine.next_question()
            self.assertIsNotNone(question)
            if question.kind == "text":
                value = "这是我的真实回答样本。"
            elif question.kind == "multi":
                value = [question.options[0]["value"]]
            else:
                value = question.options[0]["value"]
            engine.answer(question.id, value)

    def test_basic_set_is_completed_before_adaptive_questions(self):
        engine = InterviewEngine()
        self.assertEqual(engine.next_question().id, "identity_core")
        self._finish_basic(engine)
        question = engine.next_question()
        self.assertTrue(engine.basic_complete)
        self.assertTrue(question is None or question.id.startswith("followup_") or question.id.startswith("adaptive_"))

    def test_text_answer_creates_followup_and_profile_prompt(self):
        engine = InterviewEngine()
        while engine.next_question().id != "free_example":
            question = engine.next_question()
            value = ("我是一个喜欢把想法做成系统的人。" if question.kind == "text" else
                     ([question.options[0]["value"]] if question.kind == "multi" else question.options[0]["value"]))
            engine.answer(question.id, value)
        engine.answer("free_example", "先说结论，没跑过就写没跑过。")
        self.assertIn("真实样本", engine.last_analysis)
        self.assertIn("日常聊天分身", engine.prompt())
        self.assertIn("自然接话", engine.prompt())

    def test_adaptive_chat_samples_enter_shareable_style_prompt(self):
        engine = InterviewEngine()
        self._finish_basic(engine)
        engine.set_model_review("日常聊天摘要", questions=[{
            "axis": "affect", "domain": "emotional_response", "kind": "text",
            "title": "朋友来吐槽", "prompt": "朋友说今天很糟时你会怎么回？", "options": [],
        }])
        question_id = engine.dynamic_queue[0]
        engine.answer(question_id, "先问问咋了，想说就说说。")
        profile = engine.profile()
        self.assertIn("先问问咋了，想说就说说。", profile["approved_examples"])
        self.assertIn("先问问咋了，想说就说说。", engine.prompt())

    def test_app_persists_and_exports_reviewable_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = App(root)
            state = app.json_state()
            app.handle("/api/answer", {"question_id": state["question"]["id"], "value": "short"})
            restored = App(root)
            self.assertEqual(restored.json_state()["answers"], 1)
            exported = restored.handle("/api/export", {"status": "draft"})[1]
            for key in ("profile", "prompt", "script", "self_model", "self_prompt", "self_script", "answers"):
                self.assertTrue(Path(exported[key]).exists())
            self.assertIn("draft", Path(exported["profile"]).read_text(encoding="utf-8"))
            profile = Path(exported["profile"]).read_text(encoding="utf-8")
            self.assertNotIn("self_model", profile)
            self.assertIn("casual_chat", profile)

    def test_model_review_can_add_valid_adaptive_question(self):
        engine = InterviewEngine()
        self._finish_basic(engine)
        engine.set_model_review("你重视短而有信息。", {
            "axis": "voice", "kind": "single", "title": "追问边界", "prompt": "选一个",
            "options": [{"label": "只给结论", "value": "conclusion"},
                        {"label": "结论加依据", "value": "with_reason"}],
        })
        self.assertEqual(engine.next_question().id, "adaptive_01")
        engine.answer("adaptive_01", "with_reason")
        self.assertNotEqual(engine.next_question().id, "adaptive_01")

    def test_fixed_followups_do_not_consume_adaptive_budget(self):
        engine = InterviewEngine()
        self._finish_basic(engine)
        engine.answer("followup_voice", "短一点，但要把关键信息说清楚。")
        engine.answer("followup_memory", FOLLOWUPS["memory"].options[0]["value"])
        self.assertEqual(engine.dynamic_count, 0)
        restored = InterviewEngine.from_data(engine.to_data())
        self.assertEqual(restored.dynamic_count, 0)

    def test_legacy_session_rebuilds_count_from_adaptive_answers_only(self):
        engine = InterviewEngine()
        self._finish_basic(engine)
        engine.followups_used = set(FOLLOWUPS)
        engine.used.update(question.id for question in FOLLOWUPS.values())
        domains = ["conversation_flow", "emotional_response", "humor_boundary", "relationship_distance"]
        engine.set_model_review("第一批", questions=[
            {"axis": "social", "domain": domain, "kind": "text", "title": f"场景{i}",
             "prompt": f"日常场景{i}你会怎么回复？", "options": []}
            for i, domain in enumerate(domains)
        ])
        for question_id in list(engine.dynamic_queue):
            engine.answer(question_id, "这是我的真实做法。")
        self.assertEqual(engine.dynamic_count, 4)
        legacy_data = engine.to_data()
        legacy_data["dynamic_count"] = 9  # 旧版本把五道固定追问也计入了。
        restored = InterviewEngine.from_data(legacy_data)
        self.assertEqual(restored.dynamic_count, 4)

    def test_new_fallback_batch_prefers_uncovered_chat_domains(self):
        engine = InterviewEngine()
        self._finish_basic(engine)
        engine.followups_used = set(FOLLOWUPS)
        engine.used.update(question.id for question in FOLLOWUPS.values())
        first_domains = ["conversation_flow", "emotional_response", "humor_boundary", "relationship_distance"]
        engine.set_model_review("第一批", questions=[
            {"axis": "social", "domain": domain, "kind": "text", "title": f"已答场景{i}",
             "prompt": f"已答日常场景{i}你会怎么做？", "options": []}
            for i, domain in enumerate(first_domains)
        ])
        for question_id in list(engine.dynamic_queue):
            engine.answer(question_id, "这是我的真实做法。")
        engine.set_model_review("第二批", questions=[{
            "axis": "tradeoff", "domain": "work_research", "kind": "text", "title": "工作备选",
            "prompt": "工作里遇到取舍时你会怎么做？", "options": [],
        }])
        queued = [engine.dynamic_questions[qid] for qid in engine.dynamic_queue]
        domains = [question.domain for question in queued]
        self.assertEqual(len(queued), 4)
        self.assertEqual(domains.count("work_research"), 1)
        self.assertGreaterEqual(len(set(domains) - set(first_domains) - {"work_research"}), 2)

    def test_failed_model_review_exposes_retry_and_keeps_answer_count(self):
        class RetryModel:
            def __init__(self):
                self.calls = 0

            def complete_json(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("temporary network failure")
                return {"summary": "重试成功", "observations": [], "questions": [{
                    "axis": "social", "domain": "conversation_flow", "kind": "text",
                    "title": "朋友发来新消息", "prompt": "朋友分享一件小事时，你会怎么接？写一句真实回复。",
                    "options": [],
                }]}

        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory))
            self._finish_basic(app.engine)
            app.engine.followups_used = set(FOLLOWUPS)
            app.engine.used.update(question.id for question in FOLLOWUPS.values())
            model = RetryModel()
            app.llm = model
            app.mode = "模型访谈：retry-test"
            before = len(app.engine.answers)
            _, failed = app.handle("/api/review", {})
            self.assertEqual(len(app.engine.answers), before)
            self.assertTrue(failed["retry_available"])
            self.assertIn("模型审阅失败", failed["model_error"])
            _, retried = app.handle("/api/review", {})
            self.assertEqual(model.calls, 2)
            self.assertFalse(retried["model_error"])
            self.assertEqual(retried["questions"][0]["id"], "adaptive_01")

    def test_model_can_mark_v1_sufficient_and_offer_optional_continuation(self):
        class SufficientModel:
            def complete_json(self, *args, **kwargs):
                return {"summary": "日常聊天 V1 已足够", "sufficient": True,
                        "completion_reason": "主要生活聊天场景均有自述和表达样本。",
                        "observations": [], "questions": []}

        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory))
            self._finish_basic(app.engine)
            app.engine.followups_used = set(FOLLOWUPS)
            app.engine.used.update(question.id for question in FOLLOWUPS.values())
            app.llm = SufficientModel()
            app.mode = "模型访谈：sufficient-test"
            _, finished = app.handle("/api/review", {})
            self.assertTrue(finished["sufficient"])
            self.assertTrue(finished["done"])
            self.assertTrue(finished["continue_available"])
            self.assertEqual(finished["questions"], [])

    def test_explicit_continue_overrides_model_stop_once(self):
        class ContinueModel:
            def complete_json(self, *args, **kwargs):
                return {"summary": "还可精细校准", "sufficient": True,
                        "completion_reason": "", "observations": [], "questions": [{
                            "axis": "voice", "domain": "conversation_flow", "kind": "text",
                            "title": "句尾语气", "prompt": "你发短消息时，句尾通常会加句号、表情还是直接收住？给个例子。",
                            "options": [],
                        }]}

        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory))
            self._finish_basic(app.engine)
            app.engine.followups_used = set(FOLLOWUPS)
            app.engine.used.update(question.id for question in FOLLOWUPS.values())
            app.engine.sufficient = True
            app.llm = ContinueModel()
            app.mode = "模型访谈：continue-test"
            _, continued = app.handle("/api/review", {"continue": True})
            self.assertFalse(continued["sufficient"])
            self.assertEqual(continued["questions"][0]["title"], "句尾语气")
            self.assertFalse(app.engine.continue_requested)

    def test_private_observations_do_not_enter_group_style_profile(self):
        engine = InterviewEngine()
        engine.answer("identity_core", "我曾经在某个项目里长期负责系统设计。")
        engine.set_model_review({"claim": "你重视把复杂想法做成系统。",
                                 "evidence_question_ids": ["identity_core"], "confidence": "medium"}, None)
        engine.confirmed_observations = list(engine.model_observations)
        for item in engine.model_observation_records:
            item["review_status"] = "user_confirmed"
        self.assertNotIn("复杂想法", engine.prompt())
        self.assertIn("复杂想法", engine.self_prompt("approved"))

    def test_app_model_mode_keeps_answer_when_review_fails(self):
        class BrokenModel:
            def complete_json(self, *args, **kwargs):
                raise RuntimeError("offline")

        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory))
            app.llm = BrokenModel()
            app.mode = "模型访谈：测试"
            self._finish_basic(app.engine)
            app.engine.followups_used = set(FOLLOWUPS)
            status, result = app.handle("/api/review", {})
            self.assertEqual(status, 200)
            self.assertEqual(result["answers"], len(app.engine.answers))
            self.assertIn("已保留回答", result["model_error"])

    def test_app_model_review_generates_a_followup_after_answer(self):
        class FakeModel:
            def complete_json(self, *args, **kwargs):
                return {"observation": "你要求先说结论，并明确没实测的部分。", "next_question": {
                    "axis": "evidence", "kind": "single", "title": "证据门槛追问", "prompt": "哪种验证先做？",
                    "options": [{"label": "先做离线测试", "value": "offline"},
                                {"label": "先做真实烟测", "value": "smoke"}],
                }}

        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory))
            app.llm = FakeModel()
            app.mode = "模型访谈：fake-test"
            self._finish_basic(app.engine)
            app.engine.followups_used = set(FOLLOWUPS)
            status, result = app.handle("/api/review", {})
            self.assertEqual(status, 200)
            self.assertEqual(result["question"]["id"], "adaptive_01")
            self.assertIn("先说结论", "".join(result["candidate_observations"]))
            approved = app.engine.export(Path(directory), "approved")
            prompt = Path(approved["prompt"]).read_text(encoding="utf-8")
            self.assertNotIn("先说结论", prompt)
            self.assertIn("先说结论", Path(approved["self_model"]).read_text(encoding="utf-8"))
            namespace = {}
            exec(Path(approved["script"]).read_text(encoding="utf-8"), namespace)
            self.assertIn("style_prompt", namespace)
            self.assertNotIn("先说结论", namespace["style_prompt"]())

    def test_batch_questions_wait_for_all_answers_then_review_full_history(self):
        class FakeModel:
            def __init__(self):
                self.payloads = []

            def complete_json(self, _system, user, **kwargs):
                payload = json.loads(user)
                self.payloads.append(payload)
                if len(self.payloads) == 1:
                    return {"summary": "当前摘要", "observations": [{
                        "axis": "voice", "claim": "简单状态通常短句作答。",
                        "evidence_question_ids": ["free_example"], "confidence": "medium"}],
                            "questions": [
                                {"axis": "social", "kind": "text", "title": "朋友求助时", "prompt": "朋友临时求助，你通常会怎么回应？", "options": []},
                                {"axis": "affect", "kind": "text", "title": "计划被打断时", "prompt": "计划突然被打断时，你第一步通常做什么？", "options": []},
                                {"axis": "values", "kind": "single", "title": "立场冲突时", "prompt": "熟人和你立场不同时，你通常怎么处理？", "options": [
                                    {"label": "先听对方说完", "value": "listen"},
                                    {"label": "直接讲我的判断", "value": "direct"}]}
                            ]}
                return {"summary": "结合新回答后的整体摘要", "observations": [], "questions": []}

        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory))
            self._finish_basic(app.engine)
            app.engine.followups_used = set(FOLLOWUPS)
            model = FakeModel()
            app.llm = model
            app.mode = "模型访谈：fake-test"
            _, first = app.handle("/api/review", {})
            questions = first["questions"]
            self.assertEqual(len(questions), 3)
            self.assertEqual(first["adaptive_batch_remaining"], 3)
            self.assertEqual(len(model.payloads[0]["all_answers"]), len(app.engine.answers))
            answers = [{"question_id": q["id"], "value": ("这是我的真实做法。" if q["kind"] == "text" else "listen"),
                        "note": "这样做更适合这个场景"}
                       for q in questions]
            _, updated = app.handle("/api/answer-batch", {"answers": answers})
            self.assertEqual(len(app.engine.answers), len(questions) + len(QUESTIONS))
            self.assertEqual(len(model.payloads), 2)
            self.assertEqual(len(model.payloads[1]["all_answers"]), len(app.engine.answers))
            self.assertEqual(app.engine.last_analysis, "结合新回答后的整体摘要")
            self.assertTrue(all(row.get("note") == "这样做更适合这个场景"
                                for row in app.engine.answers[-len(questions):]))

    def test_adaptive_batch_is_capped_by_remaining_question_budget(self):
        engine = InterviewEngine()
        self._finish_basic(engine)
        engine.dynamic_count = 31
        engine.set_model_review("观察", questions=[
            {"axis": "voice", "kind": "text", "title": f"追问{i}", "prompt": f"具体怎么说{i}？", "options": []}
            for i in range(3)
        ])
        self.assertEqual(len(engine.dynamic_queue), 1)

    def test_research_heavy_model_batch_is_rebalanced_to_casual_domains(self):
        engine = InterviewEngine()
        self._finish_basic(engine)
        engine.followups_used = set(FOLLOWUPS)
        engine.set_model_review("当前摘要", questions=[
            {"axis": "tradeoff", "domain": "work_research", "kind": "text",
             "title": f"实验方案{i}", "prompt": f"科研实验方案{i}不稳定时你会怎么做？", "options": []}
            for i in range(4)
        ])
        queued = [engine.dynamic_questions[qid] for qid in engine.dynamic_queue]
        domains = [question.domain for question in queued]
        self.assertEqual(len(queued), 4)
        self.assertGreaterEqual(len(set(domains)), 3)
        self.assertLessEqual(domains.count("work_research"), 1)
        self.assertTrue(any(domain in ADAPTIVE_DOMAINS for domain in domains if domain != "work_research"))
        restored = InterviewEngine.from_data(engine.to_data())
        self.assertEqual([restored.dynamic_questions[qid].domain for qid in restored.dynamic_queue], domains)

    def test_legacy_single_followup_is_expanded_across_daily_chat_domains(self):
        engine = InterviewEngine()
        self._finish_basic(engine)
        engine.set_model_review("观察", next_question={
            "axis": "evidence", "kind": "single", "title": "工作验证",
            "prompt": "这项工作选择怎么验证？",
            "options": [{"label": "先看资料", "value": "sources"},
                        {"label": "先小测", "value": "test"}],
        })
        questions = [engine.dynamic_questions[qid].public() for qid in engine.dynamic_queue]
        self.assertEqual(len(questions), 4)
        self.assertGreaterEqual(len({question["domain"] for question in questions}), 3)
        self.assertEqual(sum(question["domain"] == "work_research" for question in questions), 1)

    def test_completed_basic_session_can_start_model_adaptive_review(self):
        class FakeModel:
            def complete_json(self, *args, **kwargs):
                return {"observation": {"claim": "你会根据问题复杂度调整长度。",
                                         "evidence_question_ids": ["identity_core"],
                                         "confidence": "medium"},
                        "next_question": {"axis": "voice", "kind": "single", "title": "追问长度边界",
                                          "prompt": "熟人群里分享一个新东西时，你通常回多长？",
                                          "options": [{"label": "一句话", "value": "one_line"},
                                                      {"label": "短评后展开", "value": "expand"}]}}

        with tempfile.TemporaryDirectory() as directory:
            app = App(Path(directory))
            self._finish_basic(app.engine)
            app.engine.followups_used = set(FOLLOWUPS)
            self.assertIsNone(app.engine.next_question())
            app.llm = FakeModel()
            app.mode = "模型访谈：fake-test"
            status, result = app.handle("/api/review", {})
            self.assertEqual(status, 200)
            self.assertEqual(result["question"]["id"], "adaptive_01")
            self.assertIn("调整长度", "".join(result["candidate_observations"]))


if __name__ == "__main__":
    unittest.main()
