"""WeChat 4.2 read-only preflight fingerprint tests."""
from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wxbot_framework.wechat_42 import verify_wechat_42_core


class WeChat42PreflightTests(unittest.TestCase):
    def test_matching_core_fingerprint_is_accepted(self):
        data = b"verified fixture"
        with tempfile.TemporaryDirectory() as directory:
            core = Path(directory) / "Weixin.dll"
            core.write_bytes(data)
            with patch("wxbot_framework.wechat_42.WECHAT_42_CORE_SHA256", hashlib.sha256(data).hexdigest()):
                self.assertEqual(verify_wechat_42_core(core), hashlib.sha256(data).hexdigest())

    def test_mismatched_fingerprint_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            core = Path(directory) / "Weixin.dll"
            core.write_bytes(b"different build")
            with self.assertRaisesRegex(RuntimeError, "指纹不匹配"):
                verify_wechat_42_core(core)

    def test_missing_core_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "无法读取"):
                verify_wechat_42_core(Path(directory) / "missing.dll")


if __name__ == "__main__":
    unittest.main()
