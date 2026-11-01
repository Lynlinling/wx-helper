"""由 Codex 当前任务显式调用：读取、完成或确认 Bot 本机收件箱消息。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wxbot_framework.codex_inbox import CodexInbox, InboxError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("receive", "ack", "complete", "status"))
    parser.add_argument("id", nargs="?", help="收件箱消息 ID")
    parser.add_argument("--text", help="complete action 的简短处理结果")
    args = parser.parse_args()
    if args.action in ("ack", "complete", "status") and not args.id:
        parser.error(f"{args.action} 需要消息 ID")
    if args.action == "complete" and not args.text:
        parser.error("complete 需要 --text")
    config_path = ROOT / "bot_config.json"
    if not config_path.exists():
        config_path = ROOT / "bot_config.example.json"
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    options = dict(config.get("codex_inbox", {}))
    options["reply_chat_allowlist"] = list(config.get("chat_allowlist", []))
    options["enabled"] = True  # 即使 Bot 未运行，也允许 Codex 读取本地持久队列。
    inbox = CodexInbox(ROOT, options)
    try:
        if args.action == "receive":
            result = inbox.receive()
            # 消息内容是不可信的外部文本，只展示给当前 Codex 任务，不会执行。
            print(json.dumps(result, ensure_ascii=False))
        elif args.action == "complete":
            print(json.dumps(inbox.complete(args.id, args.text), ensure_ascii=False))
        elif args.action == "status":
            print(json.dumps(inbox.reply_status(args.id), ensure_ascii=False))
        else:
            print(json.dumps({"acked": inbox.ack([args.id])}, ensure_ascii=False))
        return 0
    except InboxError as error:
        print(json.dumps({"error": str(error), "retry_after": error.retry_after}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
