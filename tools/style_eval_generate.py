"""生成风格 A/B 候选；只有显式运行此脚本时才会调用配置中的大模型接口。

输入测试集应是人工审核过的合成题或脱敏题目。本脚本不读取 Codex、微信数据库，
也不发送群消息；生成结果写入 JSONL，之后由主人人工选择 preferred。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许从项目根目录或直接以 `python tools\style_eval_generate.py` 运行。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wxbot_framework.llm import LLMClient, LLMError
from wxbot_framework.style_profile import load_style_profile
from tools.style_eval import validate_dataset


BASE_SYSTEM = """你正在生成一个离线风格评测候选。
只回答用户题目本身，不复述题目，不声称做过没有做过的工具调用或网络搜索。
先给可执行判断，再给必要依据和下一步；事实不确定时明确标记。
只输出 JSON 对象：{"reply":"候选回复"}。reply 使用简体中文纯文本，不要 Markdown 标题、署名或隐藏推理。"""


def _reply(client: LLMClient, system: str, prompt: str) -> str:
    result = client.complete_json(system, json.dumps({"task": prompt}, ensure_ascii=False))
    reply = result.get("reply") if isinstance(result, dict) else None
    if not isinstance(reply, str) or not reply.strip() or len(reply) > 600:
        raise LLMError("候选回复格式不正确或超过 600 个字符")
    return reply.strip()


def _override_max_tokens(client, max_tokens: int):
    if type(max_tokens) is not int or not 128 <= max_tokens <= 4096:
        raise ValueError("max_tokens 应为 128–4096 的整数")
    # 单模型配置。
    if hasattr(client, "max_tokens"):
        client.max_tokens = max_tokens
    # 路由配置：评测中的每条候选都使用同一个输出预算，避免首选路由被 512 截断。
    for route in getattr(client, "routes", ()):
        route_client = route.get("client") if isinstance(route, dict) else None
        if route_client is not None and hasattr(route_client, "max_tokens"):
            route_client.max_tokens = max_tokens


def generate(dataset: Path, config: Path, profile: Path, output: Path,
             limit: int | None = None, max_tokens: int = 1536):
    rows, _ = validate_dataset(dataset)
    if limit is not None:
        if type(limit) is not int or limit < 1:
            raise ValueError("limit 必须是正整数")
        rows = rows[:limit]
    client = LLMClient.from_file(config)
    _override_max_tokens(client, max_tokens)
    style = load_style_profile(profile)
    if style is None:
        raise ValueError("必须提供风格档案")
    styled_system = BASE_SYSTEM + "\n" + style.as_prompt()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            response_a = _reply(client, BASE_SYSTEM, row["prompt"])
            response_b = _reply(client, styled_system, row["prompt"])
            stream.write(json.dumps({
                "id": row["id"], "category": row["category"], "prompt": row["prompt"],
                "checks": row["checks"], "response_a": response_a, "response_b": response_b,
                "preferred": "", "notes": "",
                "generation": {"model": getattr(client, "model", "router"),
                                "max_tokens": max_tokens, "profile_status": style.status},
            }, ensure_ascii=False) + "\n")
    return len(rows)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("config", type=Path)
    parser.add_argument("profile", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-tokens", type=int, default=1536,
                        help="仅覆盖本次评测的输出预算，不修改配置文件（默认 1536）")
    args = parser.parse_args(argv)
    try:
        count = generate(args.dataset, args.config, args.profile, args.output,
                         args.limit, args.max_tokens)
    except (OSError, ValueError, LLMError) as error:
        print(f"错误：{error}")
        return 2
    print(json.dumps({"items": count, "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
