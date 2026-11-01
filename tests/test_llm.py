"""大模型协议和错误处理验证；使用假响应，不消耗实际 API。"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from wxbot_framework.llm import LLMClient, LLMError, _RejectRedirectHandler
from plugins.idiom_chain.judge import IdiomJudge


class JSONClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        return self.result


class LLMTests(unittest.TestCase):
    def setUp(self):
        self.options = {"api_key": "dummy-test-key", "model": "test-model"}
        self.client = LLMClient(self.options)

    def response(self, content, **extra):
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}, **extra}]}).encode())

    def test_official_json_request_and_utf8(self):
        with patch("wxbot_framework.llm.urlopen", return_value=self.response('{"idiom":"一心一意"}')) as call:
            self.assertEqual(self.client.complete_json("只返回 JSON", "中文"), {"idiom": "一心一意"})
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.example.com/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer dummy-test-key")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["messages"][1]["content"], "中文")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertNotIn("thinking", payload)
        self.assertEqual(call.call_args.kwargs["timeout"], 20)

    def test_config_from_utf8_bom_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "llm.json"
            path.write_text(json.dumps(self.options), encoding="utf-8-sig")
            self.assertEqual(LLMClient.from_file(path).model, "test-model")

    def test_api_key_can_come_from_file_or_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api-key.txt"
            path.write_text("file-test-key\n", encoding="utf-8")
            client = LLMClient({"model": "test", "api_key_file": str(path)})
            self.assertEqual(client._api_key, "file-test-key")
        with patch.dict("os.environ", {"WX_HELPER_TEST_KEY": "env-test-key"}):
            client = LLMClient({"model": "test", "api_key_env": "WX_HELPER_TEST_KEY"})
            self.assertEqual(client._api_key, "env-test-key")

    def test_invalid_configuration(self):
        for change in ({"api_key": ""}, {"model": 1}, {"base_url": None},
                       {"base_url": "http://example.com"}, {"base_url": "https://user:pass@example.com"},
                       {"timeout_seconds": True}, {"timeout_seconds": float("nan")},
                       {"max_tokens": 1}, {"max_tokens": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                LLMClient({**self.options, **change})

    def test_server_and_connection_errors_hide_sensitive_details(self):
        for error in (HTTPError("https://example.com", 401, "dummy-test-key", {}, None),
                      URLError("dummy-test-key"), TimeoutError("dummy-test-key")):
            with self.subTest(error=type(error).__name__), patch("wxbot_framework.llm.urlopen", side_effect=error):
                with self.assertRaises(LLMError) as caught:
                    self.client.complete_json("JSON", "测试")
                self.assertNotIn("dummy-test-key", str(caught.exception))

    def test_model_requests_refuse_http_redirects(self):
        request = Request("https://example.com/v1/chat/completions")
        handler = _RejectRedirectHandler()
        self.assertIsNone(handler.redirect_request(
            request, None, 307, "Temporary Redirect", {}, "https://example.com/other"))

    def test_responses_protocol_uses_responses_shape_and_auth(self):
        client = LLMClient({**self.options, "protocol": "openai_responses",
                            "base_url": "https://api.example.com/v1"})
        payload = {"status": "completed", "output_text": '{"reply":"ok"}',
                   "usage": {"input_tokens": 12, "output_tokens": 4}}
        with patch("wxbot_framework.llm.urlopen",
                   return_value=io.BytesIO(json.dumps(payload).encode())) as call:
            self.assertEqual(client.complete_json("system", "user"), {"reply": "ok"})
        request = call.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.example.com/v1/responses")
        self.assertEqual(request.get_header("Authorization"), "Bearer dummy-test-key")
        self.assertEqual(body["input"], [{"role": "system", "content": "system"},
                                         {"role": "user", "content": "user"}])
        self.assertEqual(body["text"]["format"]["type"], "json_object")

    def test_anthropic_messages_protocol_uses_its_native_headers_and_shape(self):
        client = LLMClient({**self.options, "protocol": "anthropic_messages",
                            "base_url": "https://api.example.com/v1"})
        payload = {"stop_reason": "end_turn", "content": [{"type": "text", "text": '{"reply":"ok"}'}],
                   "usage": {"input_tokens": 12, "output_tokens": 4}}
        with patch("wxbot_framework.llm.urlopen",
                   return_value=io.BytesIO(json.dumps(payload).encode())) as call:
            self.assertEqual(client.complete_json("system", "user"), {"reply": "ok"})
        request = call.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.example.com/v1/messages")
        self.assertEqual(request.get_header("X-api-key"), "dummy-test-key")
        self.assertEqual(request.get_header("Anthropic-version"), "2023-06-01")
        self.assertEqual(body["system"], "system")
        self.assertEqual(body["messages"], [{"role": "user", "content": "user"}])

    def test_local_openai_compatible_service_can_omit_api_key(self):
        client = LLMClient({"base_url": "http://127.0.0.1:3080/v1", "model": "local-model",
                            "api_key_optional": True})
        with patch("wxbot_framework.llm.urlopen", return_value=self.response('{"reply":"ok"}')) as call:
            self.assertEqual(client.complete_json("s", "u"), {"reply": "ok"})
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:3080/v1/chat/completions")
        self.assertIsNone(request.get_header("Authorization"))
        for url in ("http://example.com/v1", "https://user:pass@example.com/v1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                LLMClient({"base_url": url, "model": "x", "api_key_optional": True})

    def test_protocol_and_endpoint_path_validation(self):
        for change in ({"protocol": []}, {"protocol": "custom"},
                       {"endpoint_path": "//evil.example/path"},
                       {"endpoint_path": "/chat/completions?secret=x"},
                       {"api_key_optional": "yes"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                LLMClient({**self.options, **change})

    def test_optional_generation_controls_are_sent_only_when_configured(self):
        options = {**self.options, "temperature": 0.85, "top_p": 0.9,
                   "presence_penalty": 0.15, "frequency_penalty": 0.25}
        client = LLMClient(options)
        with patch("wxbot_framework.llm.urlopen", return_value=self.response('{"reply":"ok"}')) as call:
            client.complete_json("s", "u")
        payload = json.loads(call.call_args.args[0].data)
        self.assertEqual(payload["temperature"], 0.85)
        self.assertEqual(payload["top_p"], 0.9)
        self.assertEqual(payload["presence_penalty"], 0.15)
        self.assertEqual(payload["frequency_penalty"], 0.25)

        with patch("wxbot_framework.llm.urlopen", return_value=self.response('{"reply":"ok"}')) as call:
            self.client.complete_json("s", "u")
        default_payload = json.loads(call.call_args.args[0].data)
        for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
            self.assertNotIn(key, default_payload)

    def test_generation_controls_validate_ranges_and_protocol_compatibility(self):
        for change in ({"temperature": True}, {"temperature": 2.1}, {"temperature": float("nan")},
                       {"top_p": 0}, {"top_p": 1.1}, {"presence_penalty": -2.1},
                       {"frequency_penalty": 2.1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                LLMClient({**self.options, **change})
        with self.assertRaises(ValueError):
            LLMClient({**self.options, "protocol": "anthropic_messages", "temperature": 1.1})

        client = LLMClient({**self.options, "protocol": "anthropic_messages",
                            "temperature": 0.8, "top_p": 0.9,
                            "presence_penalty": 0.1, "frequency_penalty": 0.2})
        response = {"stop_reason": "end_turn", "content": [{"type": "text", "text": '{"reply":"ok"}'}]}
        with patch("wxbot_framework.llm.urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as call:
            client.complete_json("s", "u")
        payload = json.loads(call.call_args.args[0].data)
        self.assertEqual(payload["temperature"], 0.8)
        self.assertEqual(payload["top_p"], 0.9)
        self.assertNotIn("presence_penalty", payload)
        self.assertNotIn("frequency_penalty", payload)

    def test_malformed_or_truncated_model_results(self):
        for content in ("", "not json", "[]", None):
            with self.subTest(content=content), patch("wxbot_framework.llm.urlopen", return_value=self.response(content)):
                with self.assertRaises(LLMError):
                    self.client.complete_json("JSON", "测试")
        with patch("wxbot_framework.llm.urlopen",
                   side_effect=[self.response("{}", finish_reason="length"),
                                self.response("{}", finish_reason="length")]):
            with self.assertRaisesRegex(LLMError, "截断"):
                self.client.complete_json("JSON", "测试")
        with patch("wxbot_framework.llm.urlopen", return_value=io.BytesIO(b'{"choices":[null]}')):
            with self.assertRaises(LLMError):
                self.client.complete_json("JSON", "测试")

    def test_malformed_json_retries_once_with_stricter_format_reminder(self):
        with patch("wxbot_framework.llm.urlopen",
                   side_effect=[self.response("not json"),
                                self.response('{"reply":"ok"}')]) as call:
            self.assertEqual(self.client.complete_json("JSON", "测试"),
                             {"reply": "ok"})
        self.assertEqual(call.call_count, 2)
        retry_payload = json.loads(call.call_args_list[1].args[0].data)
        self.assertIn("Strict formatting retry",
                      retry_payload["messages"][0]["content"])

    def test_persistently_malformed_json_stops_after_one_retry(self):
        with patch("wxbot_framework.llm.urlopen",
                   side_effect=[self.response("not json"), self.response("still not json")]) as call:
            with self.assertRaisesRegex(LLMError, "响应格式不正确"):
                self.client.complete_json("JSON", "测试")
        self.assertEqual(call.call_count, 2)

    def test_think_tag_and_json_fence_wrappers_are_removed(self):
        for content in ('<think>internal note</think>{"reply":"收到"}',
                        '```json\n{"reply":"收到"}\n```'):
            with self.subTest(content=content), patch(
                    "wxbot_framework.llm.urlopen", return_value=self.response(content)):
                self.assertEqual(self.client.complete_json("JSON", "测试"), {"reply": "收到"})

    def test_truncated_json_retries_with_larger_budget(self):
        with patch("wxbot_framework.llm.urlopen",
                   side_effect=[self.response("", finish_reason="length"),
                                self.response('{"reply":"收到"}')]) as call:
            self.assertEqual(self.client.complete_json("JSON", "测试"), {"reply": "收到"})
        self.assertEqual(call.call_count, 2)
        first = json.loads(call.call_args_list[0].args[0].data)
        second = json.loads(call.call_args_list[1].args[0].data)
        self.assertEqual(first["max_tokens"], self.client.max_tokens)
        self.assertEqual(second["max_tokens"],
                         min(4096, max(2048, self.client.max_tokens * 2)))


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.client = JSONClient({"is_idiom": True, "reason": "常见成语", "next_idiom": "发扬光大"})
        self.judge = IdiomJudge(self.client)

    def test_local_rejections_do_not_call_model(self):
        for previous, word, used in (("一心一意", "不是四字成语", ()),
                                     ("一心一意", "异想天开", ()),
                                     ("一心一意", "意气风发", ("意气风发",))):
            self.assertFalse(self.judge.evaluate(previous, word, used).accepted)
        self.assertEqual(self.client.calls, [])

    def test_success_sends_only_game_words(self):
        result = self.judge.evaluate("一心一意", "意气风发", ("一心一意",))
        self.assertTrue(result.accepted)
        self.assertEqual(result.next_idiom, "发扬光大")
        self.assertEqual(json.loads(self.client.calls[0][1]),
                         {"previous": "一心一意", "candidate": "意气风发", "used": ["一心一意", "意气风发"]})

    def test_model_can_reject_fake_four_character_word(self):
        self.client.result = {"is_idiom": False, "reason": "不是成语", "next_idiom": ""}
        result = self.judge.evaluate("一心一意", "意意意意", ("一心一意",))
        self.assertFalse(result.accepted)

    def test_invalid_model_next_word_never_advances(self):
        for word in ("假的", "一心一意", "发扬光大", "意气风发"):
            self.client.result["next_idiom"] = word
            with self.subTest(word=word), self.assertRaises(LLMError):
                self.judge.evaluate("一心一意", "意气风发", ("一心一意", "发扬光大"))

    def test_model_boolean_is_strict(self):
        self.client.result["is_idiom"] = "true"
        with self.assertRaises(LLMError):
            self.judge.evaluate("一心一意", "意气风发", ())

    def test_opening_shape(self):
        self.client.result = {"idiom": "一心一意"}
        self.assertEqual(self.judge.opening(), "一心一意")
        self.client.result = {"idiom": "假词"}
        with self.assertRaises(LLMError):
            self.judge.opening()
