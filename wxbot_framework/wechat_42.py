"""Strict fingerprint gate for read-only WeChat 4.2.0.12 inspection."""
from __future__ import annotations

import hashlib
from pathlib import Path

WECHAT_42_VERSION = "4.2.0.12"
WECHAT_42_CORE_SHA256 = "2b3677c3f932daaa4ef84c8b2acd0aee12cbe5a779ff9e740c370761e5c291ce"


def verify_wechat_42_core(path):
    """Accept only the exact core DLL whose read-only layout was inspected."""
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as core:
            for block in iter(lambda: core.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise RuntimeError("微信核心 DLL 无法读取；拒绝检查固定偏移") from error
    fingerprint = digest.hexdigest()
    if fingerprint != WECHAT_42_CORE_SHA256:
        raise RuntimeError("微信核心 DLL 指纹不匹配；4.2 只读账号预检已拒绝")
    return fingerprint
