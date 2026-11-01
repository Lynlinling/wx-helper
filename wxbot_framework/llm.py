"""通过配置接入 OpenAI 兼容的大模型接口，不记录密钥或聊天正文。"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .usage import provider_name

logger = logging.getLogger("LLM")


class LLMError(RuntimeError):
    pass


class _RejectRedirectHandler(HTTPRedirectHandler):
    """Never forward a credential-bearing model request to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urlopen(request, timeout):
    return build_opener(_RejectRedirectHandler()).open(request, timeout=timeout)


def _first_int(*values):
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            return number
    return 0


class LLMClient:
    def __init__(self, options, *, usage_store=None, session=None):
        if not isinstance(options, dict):
            raise ValueError("大模型配置必须是 JSON 对象")
        base_url = options.get("base_url", "https://api.example.com/v1")
        if not isinstance(base_url, str):
            raise ValueError("大模型 base_url 必须是地址字符串")
        self.base_url = base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        if not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("大模型 base_url 应为不含认证信息的地址")
        # 公网请求仍必须 HTTPS；仅允许本机回环 HTTP，便于接入 DeepSeek
        # Harness、llama.cpp 等本地 OpenAI 兼容服务。
        if parsed.scheme != "https":
            local_hosts = {"127.0.0.1", "localhost", "::1"}
            if parsed.scheme != "http" or (parsed.hostname or "").lower() not in local_hosts:
                raise ValueError("大模型 base_url 应为 HTTPS；本机回环服务可使用 HTTP")
        self.model = options.get("model", "your-model")
        self.protocol = options.get("protocol", "openai_chat")
        if not isinstance(self.protocol, str) or self.protocol not in {
                "openai_chat", "openai_responses", "anthropic_messages"}:
            raise ValueError("protocol 应为 openai_chat、openai_responses 或 anthropic_messages")
        default_path = {"openai_chat": "/chat/completions", "openai_responses": "/responses",
                        "anthropic_messages": "/messages"}[self.protocol]
        self.endpoint_path = options.get("endpoint_path", default_path)
        endpoint = urlsplit(self.endpoint_path) if isinstance(self.endpoint_path, str) else None
        if (endpoint is None or not self.endpoint_path.startswith("/") or self.endpoint_path.startswith("//") or
                endpoint.scheme or endpoint.netloc or endpoint.query or endpoint.fragment or
                len(self.endpoint_path) > 200 or any(ord(ch) < 32 for ch in self.endpoint_path)):
            raise ValueError("endpoint_path 应为不带查询参数的绝对路径")
        self.api_key_optional = options.get("api_key_optional", False)
        if type(self.api_key_optional) is not bool:
            raise ValueError("api_key_optional 必须是布尔值")
        self._api_key = self._resolve_api_key(options)
        if not isinstance(self._api_key, str):
            raise ValueError("api_key 必须是字符串")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("大模型 model 不能为空")
        if (not self.api_key_optional and
                not self._api_key.strip()):
            raise ValueError("请在大模型配置文件中填写 api_key")
        self.timeout = options.get("timeout_seconds", 20)
        self.max_tokens = options.get("max_tokens", 512)
        if type(self.timeout) not in (int, float) or not math.isfinite(self.timeout) or not 1 <= self.timeout <= 60:
            raise ValueError("timeout_seconds 应为 1–60 秒")
        if type(self.max_tokens) is not int or not 128 <= self.max_tokens <= 4096:
            raise ValueError("max_tokens 应为 128–4096 的整数")
        self.context_limit = options.get("context_limit", 128000)
        if type(self.context_limit) is not int or not 1000 <= self.context_limit <= 10_000_000:
            raise ValueError("context_limit 应为 1000–10000000 的整数")
        temperature_max = 1 if self.protocol == "anthropic_messages" else 2
        self.temperature = self._optional_number(options, "temperature", 0, temperature_max)
        self.top_p = self._optional_number(options, "top_p", 0, 1, exclusive_min=True)
        self.presence_penalty = self._optional_number(options, "presence_penalty", -2, 2)
        self.frequency_penalty = self._optional_number(options, "frequency_penalty", -2, 2)
        pricing = options.get("pricing") or {}
        if not isinstance(pricing, dict):
            raise ValueError("pricing 必须是 JSON 对象")
        self.pricing = {}
        for key in ("input_per_million", "output_per_million",
                    "cache_read_per_million", "cache_write_per_million"):
            value = pricing.get(key, 0)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"pricing.{key} 应为非负有限数值")
            self.pricing[key] = float(value)
        currency = pricing.get("currency", "USD")
        if (not isinstance(currency, str) or not currency.strip() or
                len(currency.strip()) > 12 or any(ord(ch) < 32 for ch in currency)):
            raise ValueError("pricing.currency 应为 1–12 个字符的币种名")
        self.pricing["currency"] = currency.strip()
        self.provider = provider_name(self.base_url)
        self.usage_store = usage_store
        self.session = session or {}

    @staticmethod
    def _optional_number(options, key, minimum, maximum, *, exclusive_min=False):
        value = options.get(key)
        if value is None:
            return None
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"{key} 必须是有限数值或 null")
        if (value <= minimum if exclusive_min else value < minimum) or value > maximum:
            bound = f"{minimum}<" if exclusive_min else f"{minimum}≤"
            raise ValueError(f"{key} 应满足 {bound}x≤{maximum}")
        return float(value)

    @staticmethod
    def _resolve_api_key(options):
        """从环境变量或独立文件取密钥，避免把真实密钥放进配置和日志。"""
        env_name = options.get("api_key_env")
        if env_name is not None:
            if not isinstance(env_name, str) or not env_name.strip():
                raise ValueError("api_key_env 必须是非空环境变量名")
            value = os.environ.get(env_name.strip(), "")
            if value.strip():
                return value.strip()
        file_name = options.get("api_key_file")
        if file_name is not None:
            if not isinstance(file_name, str) or not file_name.strip() or "\0" in file_name:
                raise ValueError("api_key_file 必须是非空文件路径")
            try:
                value = Path(file_name).expanduser().read_text(encoding="utf-8-sig").strip()
            except (OSError, UnicodeError) as error:
                raise ValueError("api_key_file 无法读取") from error
            if value:
                return value
        value = options.get("api_key", "")
        return value if isinstance(value, str) else value

    @classmethod
    def from_file(cls, path, *, usage_store=None, session=None):
        options = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if isinstance(options, dict) and isinstance(options.get("routes"), list):
            from .llm_router import ModelRouter
            return ModelRouter.from_config(options, usage_store=usage_store, session=session)
        return cls(options, usage_store=usage_store, session=session)

    def with_session(self, session):
        clone = copy.copy(self)
        clone.session = session or {}
        return clone

    def _cost(self, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, usage):
        direct = usage.get("cost", usage.get("total_cost")) if isinstance(usage, dict) else None
        if type(direct) in (int, float) and math.isfinite(direct) and direct >= 0:
            return float(direct)
        p = self.pricing
        # 缓存读取通常包含在 input_tokens 中，按缓存价计入；缓存写入按单独价格计入。
        billable_input = max(0, input_tokens - cache_read_tokens)
        return ((billable_input * p["input_per_million"] +
                 cache_read_tokens * p["cache_read_per_million"] +
                 cache_write_tokens * p["cache_write_per_million"] +
                 output_tokens * p["output_per_million"]) / 1_000_000.0)

    def _record_usage(self, usage, session):
        if self.usage_store is None:
            return
        usage = usage if isinstance(usage, dict) else {}
        prompt_details = usage.get("prompt_tokens_details") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        input_tokens = _first_int(usage.get("prompt_tokens"), usage.get("input_tokens"))
        output_tokens = _first_int(usage.get("completion_tokens"), usage.get("output_tokens"))
        reasoning_tokens = _first_int(completion_details.get("reasoning_tokens"),
                                      usage.get("reasoning_tokens"))
        cache_read = _first_int(prompt_details.get("cached_tokens"),
                                usage.get("prompt_cache_hit_tokens"),
                                usage.get("cache_read_input_tokens"))
        cache_write = _first_int(prompt_details.get("cache_creation_tokens"),
                                 usage.get("cache_creation_input_tokens"),
                                 usage.get("cache_write_tokens"))
        total_tokens = _first_int(usage.get("total_tokens")) or input_tokens + output_tokens
        cost = self._cost(input_tokens, output_tokens, cache_read, cache_write, usage)
        session = session if session is not None else self.session
        if isinstance(session, str):
            plugin, chat_id = session, ""
        elif isinstance(session, dict):
            plugin, chat_id = str(session.get("plugin") or ""), str(session.get("chat_id") or "")
        else:
            plugin, chat_id = "", ""
        key = f"{plugin or '未命名'}:{chat_id or '-'}"
        try:
            self.usage_store.record(
                session=key, plugin=plugin, chat_id=chat_id, provider=self.provider,
                model=self.model, context_limit=self.context_limit,
                input_tokens=input_tokens, output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens, cache_read_tokens=cache_read,
                cache_write_tokens=cache_write, total_tokens=total_tokens,
                cost=cost, currency=self.pricing["currency"])
        except Exception:
            # 统计失败不能影响机器人回复；只写一条不含正文的告警。
            logger.warning("大模型用量统计写入失败", exc_info=True)

    def complete_json(self, system, user, *, session=None, _format_retry=False):
        request_max_tokens = (self.max_tokens if not _format_retry else
                              min(4096, max(2048, self.max_tokens * 2)))
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        headers = {"Content-Type": "application/json"}
        if self.protocol != "anthropic_messages" and self._api_key:
            headers["Authorization"] = "Bearer " + self._api_key
        if self.protocol == "openai_responses":
            payload = {"model": self.model, "input": messages,
                       "text": {"format": {"type": "json_object"}},
                       "max_output_tokens": request_max_tokens, "stream": False}
        elif self.protocol == "anthropic_messages":
            payload = {"model": self.model, "system": system,
                       "messages": [{"role": "user", "content": user}],
                       "max_tokens": request_max_tokens, "stream": False}
            if self._api_key:
                headers["x-api-key"] = self._api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            payload = {"model": self.model, "messages": messages,
                       "response_format": {"type": "json_object"}, "max_tokens": request_max_tokens,
                       "stream": False}
        # 仅发送用户明确配置的采样参数，兼容不接受未知字段的第三方网关。
        optional = {"temperature": self.temperature, "top_p": self.top_p}
        if self.protocol == "openai_chat":
            optional.update({"presence_penalty": self.presence_penalty,
                             "frequency_penalty": self.frequency_penalty})
        payload.update({key: value for key, value in optional.items() if value is not None})
        request = Request(self.base_url + self.endpoint_path,
                          data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise LLMError("大模型响应过大")
            result = json.loads(raw)
            if self.protocol == "openai_responses":
                finish_reason = result.get("status") == "incomplete"
                content = result.get("output_text", "")
                if not content:
                    for item in result.get("output", []):
                        for part in item.get("content", []):
                            if isinstance(part, dict) and isinstance(part.get("text"), str):
                                content += part["text"]
            elif self.protocol == "anthropic_messages":
                finish_reason = result.get("stop_reason") == "max_tokens"
                content = "".join(part.get("text", "") for part in result.get("content", [])
                                  if isinstance(part, dict) and isinstance(part.get("text"), str))
            else:
                choice = result["choices"][0]
                finish_reason = choice.get("finish_reason") == "length"
                content = choice["message"]["content"]
            if finish_reason:
                if not _format_retry:
                    retry_system = (str(system) +
                                    "\nStrict formatting retry: keep the JSON reply concise and return exactly one complete valid JSON object.")
                    return self.complete_json(retry_system, user, session=session,
                                              _format_retry=True)
                raise LLMError("大模型响应被截断，请增加 max_tokens")
            if not isinstance(content, str) or not content.strip():
                raise LLMError("大模型返回了空结果")
            # 个别兼容端会在 JSON 外包一层思考标签或代码围栏；只剥离包装，
            # 不放宽为任意自然语言解析，避免把模型闲聊误当成结构化结果。
            content = content.strip()
            if "</think>" in content:
                content = content.rsplit("</think>", 1)[1].strip()
            if content.startswith("```") and content.endswith("```"):
                content = content[3:-3].strip()
                if content.startswith("json"):
                    content = content[4:].lstrip()
            data = json.loads(content)
            if not isinstance(data, dict):
                raise LLMError("大模型结果应为 JSON 对象")
            self._record_usage(result.get("usage"), session)
            return data
        except HTTPError as error:
            # 不输出响应正文或请求头，避免服务器错误回显密钥。
            raise LLMError(f"大模型接口 HTTP {error.code}；请核对密钥、余额和模型配置") from None
        except (URLError, TimeoutError, OSError):
            raise LLMError("大模型连接失败或超时，请稍后再试") from None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            # MiniMax-compatible gateways occasionally ignore response_format and
            # return a short natural-language answer. Retry once with an explicit
            # JSON-only reminder; callers still receive one stable LLMError if
            # the second response is malformed.
            if not _format_retry:
                retry_system = (str(system) +
                                "\nStrict formatting retry: return exactly one valid JSON object, with no prose, Markdown, code fences, or reasoning text.")
                return self.complete_json(retry_system, user, session=session,
                                          _format_retry=True)
            raise LLMError("大模型响应格式不正确，请重试") from None
