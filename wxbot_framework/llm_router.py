"""带超时降级和短熔断的 OpenAI 兼容模型路由。"""
from __future__ import annotations

import copy
import threading
import time

from .llm import LLMClient, LLMError


class ModelRouter:
    """按 tier 顺序尝试模型；单条路由连续失败后短暂熔断，避免拖慢每条群消息。"""

    def __init__(self, routes, *, usage_store=None, session=None, clients=None,
                 failure_threshold=2, cooldown_seconds=30):
        if not isinstance(routes, list) or not 1 <= len(routes) <= 8:
            raise ValueError("routes 必须是 1–8 条模型路由")
        if type(failure_threshold) is not int or not 1 <= failure_threshold <= 5:
            raise ValueError("failure_threshold 应为 1–5")
        if type(cooldown_seconds) not in (int, float) or not 1 <= cooldown_seconds <= 600:
            raise ValueError("cooldown_seconds 应为 1–600")
        self.session = session or {}
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = float(cooldown_seconds)
        self._lock = threading.Lock()
        self._states = {}
        self.routes = []
        if clients is not None:
            if len(clients) != len(routes):
                raise ValueError("clients 数量必须与 routes 一致")
            for index, (route, client) in enumerate(zip(routes, clients)):
                name = str(route.get("name", f"route-{index}")) if isinstance(route, dict) else f"route-{index}"
                self.routes.append({"name": name, "tier": self._tier(route), "client": client})
        else:
            for index, route in enumerate(routes):
                if not isinstance(route, dict):
                    raise ValueError("每条路由必须是 JSON 对象")
                name = route.get("name", f"route-{index}")
                if not isinstance(name, str) or not name.strip() or len(name) > 80:
                    raise ValueError("路由 name 无效")
                options = dict(route)
                options.pop("name", None)
                options.pop("tier", None)
                self.routes.append({"name": name.strip(), "tier": self._tier(route),
                                    "client": LLMClient(options, usage_store=usage_store,
                                                         session=self.session)})
        for route in self.routes:
            self._states[route["name"]] = {"failures": 0, "open_until": 0.0,
                                            "last_error": "", "successes": 0}

    @staticmethod
    def _tier(route):
        tier = route.get("tier", "default") if isinstance(route, dict) else "default"
        if tier not in ("fast", "planner", "fallback", "default"):
            raise ValueError("route.tier 应为 fast、planner、fallback 或 default")
        return tier

    @classmethod
    def from_config(cls, config, *, usage_store=None, session=None):
        routes = config.get("routes")
        options = config.get("router") or {}
        if not isinstance(options, dict):
            raise ValueError("router 必须是 JSON 对象")
        return cls(routes, usage_store=usage_store, session=session,
                   failure_threshold=options.get("failure_threshold", 2),
                   cooldown_seconds=options.get("cooldown_seconds", 30))

    def with_session(self, session):
        clone = copy.copy(self)
        clone.session = session or {}
        return clone

    def _ordered(self, task):
        preferred = {"chat": ("fast", "default", "planner", "fallback"),
                     "planner": ("planner", "fast", "default", "fallback"),
                     "fallback": ("fallback", "fast", "default", "planner")}.get(task,
                                                                              ("default", "fast", "planner", "fallback"))
        rank = {value: index for index, value in enumerate(preferred)}
        now = time.monotonic()
        with self._lock:
            available = [route for route in self.routes
                         if self._states[route["name"]]["open_until"] <= now]
            if not available:
                # 全部熔断时，选择最早恢复的路由，让系统仍能尝试而不是永久离线。
                available = sorted(self.routes, key=lambda route: self._states[route["name"]]["open_until"])[:1]
            # 同一 tier 按配置文件顺序尝试；这让实测更快的首选路由可以
            # 放在前面，也避免用名称排序意外改变故障转移策略。
            return sorted(available, key=lambda route: rank.get(route["tier"], 99))

    def _failed(self, name, error):
        with self._lock:
            state = self._states[name]
            state["failures"] += 1
            state["last_error"] = str(error)[:200]
            if state["failures"] >= self.failure_threshold:
                state["open_until"] = time.monotonic() + self.cooldown_seconds

    def _succeeded(self, name):
        with self._lock:
            state = self._states[name]
            state["failures"] = 0
            state["open_until"] = 0.0
            state["successes"] += 1

    def complete_json(self, system, user, *, session=None, task="chat"):
        errors = []
        effective_session = self.session if session is None else session
        for route in self._ordered(task):
            name, client = route["name"], route["client"]
            try:
                result = client.complete_json(system, user, session=effective_session)
                self._succeeded(name)
                return result
            except (LLMError, TimeoutError, OSError) as error:
                self._failed(name, error)
                errors.append(f"{name}: {str(error)[:120]}")
        raise LLMError("所有模型路由均失败；" + "；".join(errors))

    def health(self):
        now = time.monotonic()
        with self._lock:
            return [{"name": route["name"], "tier": route["tier"],
                     "open": self._states[route["name"]]["open_until"] > now,
                     "failures": self._states[route["name"]]["failures"],
                     "successes": self._states[route["name"]]["successes"]}
                    for route in self.routes]
