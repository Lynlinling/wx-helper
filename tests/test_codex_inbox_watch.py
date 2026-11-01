import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from tools.codex_inbox_watch import read_signal


def _make_inbox(root: Path, *, pending_id: str | None = None, last_delivered: float | None = None):
    (root / ".runtime").mkdir(parents=True, exist_ok=True)
    db_path = root / ".runtime" / "inbox.sqlite3"
    with closing(sqlite3.connect(db_path)) as db:
        db.execute("CREATE TABLE codex_inbox (seq INTEGER PRIMARY KEY, id TEXT, status TEXT, text TEXT)")
        db.execute("CREATE TABLE codex_inbox_meta (name TEXT PRIMARY KEY, value TEXT)")
        if pending_id:
            db.execute("INSERT INTO codex_inbox(seq,id,status,text) VALUES(1,?,'pending','secret body never read')",
                       (pending_id,))
        if last_delivered is not None:
            db.execute("INSERT INTO codex_inbox_meta(name,value) VALUES('last_delivered',?)",
                       (str(last_delivered),))
        db.commit()
    config_path = root / "bot_config.json"
    config_path.write_text(json.dumps({
        "codex_inbox": {
            "enabled": True,
            "database_file": ".runtime/inbox.sqlite3",
            "min_interval_seconds": 600,
        }
    }), encoding="utf-8")
    return db_path, config_path


def test_read_signal_is_read_only_and_returns_only_a_fingerprint(tmp_path, monkeypatch):
    db_path, config_path = _make_inbox(tmp_path, pending_id="ci-test-private-id")
    monkeypatch.setattr("tools.codex_inbox_watch.time.time", lambda: 1_800_000_600.0)

    result = read_signal(tmp_path, config_path)

    assert result["status"] == "pending"
    assert result["retry_after"] == 0
    assert "ci-test-private-id" not in json.dumps(result)
    assert "secret body" not in json.dumps(result)
    with closing(sqlite3.connect(db_path)) as db:
        assert db.execute("SELECT status FROM codex_inbox").fetchone()[0] == "pending"


def test_read_signal_honors_persistent_delivery_cooldown(tmp_path, monkeypatch):
    _, config_path = _make_inbox(tmp_path, pending_id="ci-pending", last_delivered=1_800_000_599.0)
    monkeypatch.setattr("tools.codex_inbox_watch.time.time", lambda: 1_800_000_600.0)

    result = read_signal(tmp_path, config_path)

    assert result["status"] == "pending"
    assert result["retry_after"] == 599


def test_read_signal_returns_empty_without_creating_database(tmp_path):
    _, config_path = _make_inbox(tmp_path)
    db_path = tmp_path / ".runtime" / "inbox.sqlite3"
    db_path.unlink()

    result = read_signal(tmp_path, config_path)

    assert result["status"] == "empty"
    assert not db_path.exists()


def test_read_signal_rejects_database_paths_outside_helper_root(tmp_path):
    outside = tmp_path.parent / "outside.sqlite3"
    config_path = tmp_path / "bot_config.json"
    config_path.write_text(json.dumps({
        "codex_inbox": {"enabled": True, "database_file": str(outside)}
    }), encoding="utf-8")

    with pytest.raises(RuntimeError, match="必须位于 wx-helper 目录内"):
        read_signal(tmp_path, config_path)
