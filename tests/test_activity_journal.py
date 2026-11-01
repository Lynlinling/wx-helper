from wxbot_framework.activity_journal import is_current_activity_query, read_activity_journal


def test_current_activity_query_detects_status_questions_only():
    assert is_current_activity_query("你现在在做什么？")
    assert is_current_activity_query("MC 状态怎么样")
    assert is_current_activity_query("最近进展呢")
    assert not is_current_activity_query("晚上一起打游戏吗")


def test_follow_up_only_uses_journal_when_recent_chat_is_about_mc():
    mc_history = [{"speaker_name": "机器人", "text": "MC Agent 在待机，食物补给还没确认。"}]
    unrelated_history = [{"speaker_name": "群友", "text": "今晚看电影。"}]
    assert is_current_activity_query("那现在呢？", mc_history)
    assert not is_current_activity_query("现在呢？", unrelated_history)


def test_reader_returns_timestamped_status_and_next_steps_without_runtime_ids(tmp_path):
    path = tmp_path / "MC_AGENT_ACTIVITY_JOURNAL.md"
    live = tmp_path / "MC_AGENT_ACTIVITY_JOURNAL_LIVE.md"
    path.write_text("""# MC Agent 活动日记

## 当前状态
- **更新时间：** 2026-10-31 20:05（Asia/Shanghai）
- **当前实例：** Paper 26.3 隔离服 `127.0.0.1:25566`; Core `127.0.0.1:8788`; bot session `session-secret`。
- **能力边界：** 已验证门禁冻结；自主探索仍未验证。
- **未证实事项：** Temurin 稳定性观察仍在进行。
- **运行情况：** PID 10552 正在运行。
- **慢思考：** 最近决策是安全探索；原因是食物短缺。
- **游戏状态：** 生命 20，饥饿 20，任务 idle，背包有 22 个泥土。
- **未证实事项：** 没有确认完成食物补给。

## 下一步
1. 观察探索航点是否扩展覆盖。
2. 验证食物来源和安全门槛。

## 时间线
- 日志路径：.runtime/private.jsonl
""" , encoding="utf-8")
    live.write_text("""# MC Agent 活动日记（实时镜像）

## 当前状态
- **更新时间：** 2026-10-31 21:43:28 +08:00
- **当前实例：** Paper 26.3 隔离服；Paper 端口未监听。
- **慢思考：** 模型请求：planner.error latency_ms=15482；动作来源：circuit_fallback goal=explore。
- **游戏状态：** 当前动作待机；生命 null，饥饿 null，背包空。
- **当前在做什么：** 慢思考请求超时，正在等待安全回退。
- **未证实事项：** 真实采集与狩猎尚未完成。

## 下一步
1. 恢复 Paper 26.3 后继续测试。

## 最近已确认活动
- 2026-10-31T14:18:01Z `agent.spawn_gate_passed` status=passed
- 2026-10-31T14:18:02Z `task.receipt` status=succeeded session-secret

## 说明
- bot session hidden-session; PID 9999; API key secret; 127.0.0.1:25566
""", encoding="utf-8")

    result = read_activity_journal(path, live_path=live)

    assert result is not None
    assert result["source"] == "MC_AGENT_ACTIVITY_JOURNAL.md"
    assert result["snapshot_only"] is True
    joined = "\n".join(result["current_status"] + result["next_steps"])
    assert "2026-10-31 20:05" in joined
    assert "Paper 26.3 隔离服" in joined
    assert "食物短缺" in joined and "没有确认完成食物补给" in joined
    assert "观察探索航点" in joined
    assert "门禁冻结" in joined and "稳定性观察仍在进行" in joined
    assert "127.0.0.1" not in joined
    assert "session-secret" not in joined
    assert "PID 10552" not in joined
    assert "private.jsonl" not in joined
    assert result["live_snapshot"]
    live_joined = "\n".join(result["live_snapshot"])
    assert "2026-10-31 21:43:28" in live_joined
    assert "Paper 端口未监听" in live_joined
    assert "慢思考请求超时，正在等待安全回退" in live_joined
    assert "15482" in live_joined and "circuit_fallback" in live_joined
    assert "agent.spawn_gate_passed" in live_joined
    assert "task.receipt" not in live_joined
    assert "session-secret" not in live_joined
    assert "hidden-session" not in live_joined
    assert "127.0.0.1" not in live_joined
    assert "API key" not in live_joined
    assert "9999" not in live_joined


def test_reader_returns_none_for_missing_or_malformed_journal(tmp_path):
    assert read_activity_journal(tmp_path / "missing.md") is None
    empty = tmp_path / "empty.md"
    empty.write_text("# no status\n", encoding="utf-8")
    assert read_activity_journal(empty) is None
