from wxbot_framework.llm import LLMError
from wxbot_framework.llm_router import ModelRouter


class FakeClient:
    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    def complete_json(self, _system, _user, *, session=None):
        self.calls += 1
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def test_router_falls_back_and_resets_failure_after_success():
    fast = FakeClient([LLMError("timeout"), {"reply": "fast recovered"}])
    planner = FakeClient([{"reply": "planner"}, {"reply": "planner"}])
    router = ModelRouter([{"name": "fast", "tier": "fast"}, {"name": "planner", "tier": "planner"}],
                         clients=[fast, planner], failure_threshold=2, cooldown_seconds=30)
    assert router.complete_json("s", "u")["reply"] == "planner"
    assert router.complete_json("s", "u")["reply"] == "fast recovered"
    assert fast.calls == 2
    assert planner.calls == 1
    assert router.health()[0]["failures"] == 0


def test_router_opens_a_failing_route_and_uses_planner():
    fast = FakeClient([LLMError("down"), LLMError("down")])
    planner = FakeClient([{"reply": "planner"}, {"reply": "planner"}])
    router = ModelRouter([{"name": "fast", "tier": "fast"}, {"name": "planner", "tier": "planner"}],
                         clients=[fast, planner], failure_threshold=2, cooldown_seconds=60)
    assert router.complete_json("s", "u")["reply"] == "planner"
    assert router.complete_json("s", "u")["reply"] == "planner"
    assert fast.calls == 2
    assert planner.calls == 2
    assert router.health()[0]["open"] is True


def test_same_tier_keeps_configured_preference_order():
    first = FakeClient([{"reply": "first"}])
    second = FakeClient([{"reply": "second"}])
    router = ModelRouter([{"name": "z-second", "tier": "fast"},
                          {"name": "a-first", "tier": "fast"}],
                         clients=[first, second])
    assert router.complete_json("s", "u")["reply"] == "first"
    assert first.calls == 1
    assert second.calls == 0
