import json
import tempfile
import unittest
from pathlib import Path

from tools.check_publication import inspect


class PublicationCheckTests(unittest.TestCase):
    def test_runtime_inbox_file_is_reported_without_label_order_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / "codex-inbox"
            inbox.mkdir()
            (inbox / "task.md").write_text("private", encoding="utf-8")
            report = inspect(root)
            self.assertTrue(any(item["kind"] == "private_inbox_runtime_data" and
                                item["file"] == "codex-inbox/task.md"
                                for item in report["findings"]))

    def test_known_private_marker_is_detected_without_echoing_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.txt").write_text("public host unknown.example", encoding="utf-8")
            report = inspect(root, ("unknown.example",))
            self.assertTrue(any(item["kind"] == "known_private_marker" and
                                item["file"] == "source.txt"
                                for item in report["findings"]))
            self.assertNotIn("unknown.example", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
