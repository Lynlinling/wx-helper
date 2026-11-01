"""同步并打开用户可读的 Bot → Codex 收件箱历史目录。"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wxbot_framework.codex_inbox import CodexInbox  # noqa: E402


def _load_inbox() -> CodexInbox:
    config_path = ROOT / "bot_config.json"
    if not config_path.exists():
        config_path = ROOT / "bot_config.example.json"
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    options = dict(config.get("codex_inbox", {}))
    options["reply_chat_allowlist"] = list(config.get("chat_allowlist", []))
    options["enabled"] = True
    return CodexInbox(ROOT, options)


def _open_folder(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("sync", "open"),
                        help="sync 只更新历史镜像；open 同步后打开文件夹")
    args = parser.parse_args()
    try:
        inbox = _load_inbox()
        folder = Path(inbox.archive_location())
        count = sum(1 for path in folder.glob("ci-*.md") if path.is_file())
        if args.action == "open":
            _open_folder(folder)
        print(f"Inbox archive ready: {folder} ({count} records; local-only content)")
        return 0
    except Exception as error:
        print(f"收件箱目录操作失败：{type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
