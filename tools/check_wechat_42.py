"""Read-only, hash-locked account preflight for the WeChat 4.2.0.12 process."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "native"))

from run_selftest import K, check, modules, wechat_candidates
from wxbot_framework.account import read_current_account_42, select_account_process
from wxbot_framework.wechat_42 import verify_wechat_42_core


def inspect_process(pid):
    core = modules(pid).get("weixin.dll")
    if not core:
        return None
    verify_wechat_42_core(core[1])
    process = check(K.OpenProcess(0x410, False, pid), "只读核对微信账号")
    try:
        def read(address, size):
            import ctypes as c
            buffer = c.create_string_buffer(size)
            count = c.c_size_t()
            check(K.ReadProcessMemory(process, address, buffer, size, c.byref(count)), "只读核对账号字段")
            return buffer.raw[:count.value]
        return read_current_account_42(read, core[0])
    finally:
        K.CloseHandle(process)


def main():
    if sys.platform != "win32":
        raise SystemExit("仅支持 Windows；本检查不会注入或发送")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, help="可选：只检查指定微信进程 PID")
    args = parser.parse_args()
    config_path = ROOT / "bot_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    expected = config.get("wechat_account") or None
    pids = [args.pid] if args.pid else wechat_candidates()
    candidates = []
    for pid in pids:
        try:
            account = inspect_process(pid)
            if account:
                candidates.append((pid, account))
        except (OSError, RuntimeError):
            continue
    try:
        select_account_process(candidates, expected)
    except RuntimeError:
        print("FAIL: no unique logged-in 4.2.0.12 account matched configuration; read-only, no injection, no send.")
        return 2
    label = "configured account" if expected else "unique logged-in account"
    print(f"PASS: exact 4.2.0.12 fingerprint and {label} verified; read-only, no injection, no send.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
