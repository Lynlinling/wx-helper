import json
import sys
import stat
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from plugins.local_control import Plugin as LocalControlPlugin
from wxbot_framework.members import ConversationSnapshot
from wxbot_framework.local_control import ProposalStore, SafeLocalExecutor, _is_link_or_reparse
from wxbot_framework.personality import PersonalityState


class LocalControlTests(unittest.TestCase):
    def test_codex_snapshot_filter_rejects_windows_reparse_points(self):
        path = Path("junction")
        info = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_file_attributes=0x400)
        with patch.object(Path, "lstat", return_value=info):
            self.assertTrue(_is_link_or_reparse(path))

    def test_pending_proposal_can_be_approved_after_restart_without_plain_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            first = ProposalStore(path)
            proposal = first.create(action="launch_app", args={"app": "probe"}, reason="测试",
                                    sources=["wechat:room#1"], risk="medium",
                                    requester_id="wxid_owner", chat_id="room", source_digest="a" * 64)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertIsNone(rows[0]["token"])
            self.assertEqual(rows[0]["source_digest"], "a" * 64)
            second = ProposalStore(path)
            with self.assertRaisesRegex(ValueError, "口令不正确"):
                second.decide(proposal.id, approver_id="wxid_owner", token="wrong", approved=True,
                              expected_chat_id="room", source_message={"sequence": 2})
            approved = second.decide(proposal.id, approver_id="wxid_owner", token=proposal.token,
                                     approved=True, expected_chat_id="room", source_message={"sequence": 2})
            self.assertEqual(approved.status, "approved")
            self.assertEqual(len(proposal.token), 32)
            with self.assertRaises(ValueError):
                second.decide(proposal.id, approver_id="wxid_owner", token=proposal.token,
                              approved=True, expected_chat_id="room", source_message={"sequence": 3})

    def test_recent_codex_proposal_budget_counts_across_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            store = ProposalStore(path)
            for _ in range(3):
                store.create(action="codex_readonly", args={"workspace": "mc", "task": "查看状态"},
                             reason="检查项目", sources=["wechat:room#1"], risk="high",
                             requester_id="owner", chat_id="room")
            reloaded = ProposalStore(path)
            self.assertEqual(reloaded.count_recent("codex_readonly", chat_id="room", seconds=86400), 3)
            self.assertEqual(reloaded.count_recent("codex_readonly", chat_id="other", seconds=86400), 0)

    def test_autonomous_codex_budget_counts_one_run_across_status_updates_and_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            store = ProposalStore(path)
            args = {"run_id": "cr-123", "chat_id": "room", "source_ref": "wechat:room#4",
                    "workspace": "safe", "task_digest": "a" * 64}
            store.record_codex_run(**args, status="started")
            store.record_codex_run(**args, status="succeeded", report_id="r-123")
            self.assertEqual(store.count_recent("codex_readonly", chat_id="room", seconds=86400), 1)
            reloaded = ProposalStore(path)
            self.assertEqual(reloaded.count_recent("codex_readonly", chat_id="room", seconds=86400), 1)

    def test_approval_is_bound_to_originating_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProposalStore(Path(directory) / "audit.jsonl")
            proposal = store.create(action="launch_app", args={"app": "probe"}, reason="测试",
                                    sources=[], risk="medium", requester_id="owner", chat_id="room-a")
            with self.assertRaisesRegex(ValueError, "所属的群"):
                store.decide(proposal.id, approver_id="owner", token=proposal.token, approved=True,
                             expected_chat_id="room-b", source_message={})

    def test_allowlisted_app_uses_fixed_argv_and_no_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = []
            executor = SafeLocalExecutor({
                "apps": {"probe": {"path": sys.executable, "args": ["-c", "pass"]}},
                "autonomous_apps": [], "allowed_url_hosts": [], "codex_workspaces": {}}, audit=audit.append)
            with patch("wxbot_framework.local_control.subprocess.Popen", return_value=SimpleNamespace(pid=123)) as start:
                result = executor.execute("launch_app", {"app": "probe"})
            self.assertFalse(start.call_args.kwargs["shell"])
            self.assertEqual(start.call_args.args[0], [sys.executable, "-c", "pass"])
            self.assertEqual(result["app"], "probe")
            self.assertEqual(audit[-1]["kind"], "execution")

    def test_codex_is_read_only_bounded_and_receives_prompt_via_stdin(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            (workspace / "wxbot_framework").mkdir()
            (workspace / "wxbot_framework" / "dialogue.py").write_text("# safe source", encoding="utf-8")
            (workspace / "bot_config.json").write_text('{"chat_allowlist":["private"]}', encoding="utf-8")
            (workspace / "llm_style_interview.json").write_text("private profile", encoding="utf-8")
            (workspace / ".check_input.py").write_text("private scratch", encoding="utf-8")
            (workspace / "local_settings.py").write_text(
                'api_key = "sk-' + "A" * 32 + '"\n', encoding="utf-8")
            (workspace / ".runtime").mkdir()
            (workspace / ".runtime" / "member_data.sqlite3").write_text("private history", encoding="utf-8")
            for private_dir in ("cyberain-runs", "wechat-group-probe", "tmp-private"):
                folder = workspace / private_dir
                folder.mkdir()
                (folder / "report.txt").write_text("private run data", encoding="utf-8")
            executor = SafeLocalExecutor({"apps": {}, "autonomous_apps": [], "allowed_url_hosts": [],
                                          "codex_readonly": True, "codex_workspaces": {"test": str(workspace)}},
                                         app_dir=directory)
            completed = SimpleNamespace(stdout="analysis complete", stderr="", returncode=0)
            seen = {}
            def fake_run(command, **kwargs):
                staged = Path(command[command.index("--cd") + 1])
                seen["staged"] = staged
                self.assertTrue((staged / "wxbot_framework" / "dialogue.py").is_file())
                self.assertFalse((staged / "bot_config.json").exists())
                self.assertFalse((staged / "llm_style_interview.json").exists())
                self.assertFalse((staged / ".check_input.py").exists())
                self.assertFalse((staged / "local_settings.py").exists())
                self.assertFalse((staged / ".runtime").exists())
                self.assertFalse((staged / "cyberain-runs").exists())
                self.assertFalse((staged / "wechat-group-probe").exists())
                self.assertFalse((staged / "tmp-private").exists())
                self.assertIn("wechat:room#5", kwargs["input"])
                return completed
            with patch("wxbot_framework.local_control.shutil.which", return_value="codex.exe"), \
                    patch("wxbot_framework.local_control.subprocess.run", side_effect=fake_run) as run:
                result = executor.execute("codex_readonly", {"workspace": "test", "task": "检查架构",
                                                                "source_ref": "wechat:room#5"})
            args, kwargs = run.call_args
            self.assertIn("--ephemeral", args[0])
            self.assertIn("read-only", args[0])
            self.assertEqual(args[0][-1], "-")
            self.assertNotIn("检查架构", args[0])
            self.assertIn("检查架构", kwargs["input"])
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["timeout"], 90)
            self.assertEqual(result["summary"], "analysis complete")
            self.assertEqual(result["source_ref"], "wechat:room#5")
            self.assertEqual((Path(directory) / ".runtime" / "codex-results" /
                              f"{result['report_id']}.txt").read_text(encoding="utf-8"), "analysis complete")

    def test_owner_mention_can_autonomously_ask_codex_and_returns_cited_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            (workspace / "dialogue.py").write_text("def reply(): pass", encoding="utf-8")
            owner = "wxid_owner"
            room = "123456789@chatroom"
            members = SimpleNamespace(
                groups={room}, account="wxid_robot",
                resolve=lambda alias, _room: SimpleNamespace(wxid=owner),
                conversation_snapshot=lambda _message: ConversationSnapshot(
                    room, "2026-10-31", 0, "群友", (), "上下文为什么没接上？", ()),
                display_name=lambda _sender, _room: "群友",
            )
            model = SimpleNamespace(complete_json=lambda _system, _user: {
                "reply": "我查下实现。",
                "proposal": {"action": "codex_readonly", "args": {
                    "workspace": "test", "task": "检查上下文快照实现并引用文件行号"},
                    "reason": "需要核对项目代码", "sources": []},
            })
            sent = []
            ctx = SimpleNamespace(
                members=members,
                config={"owner_aliases": ["主人"], "state_file": ".runtime/local_control.jsonl",
                        "codex_readonly": True, "codex_readonly_autonomous": True,
                        "codex_workspaces": {"test": str(workspace)}, "codex_daily_limit": 3},
                app_dir=directory,
                register_service=lambda *_args: None,
                unregister_service=lambda *_args: None,
                logger=SimpleNamespace(info=lambda *_args: None, warning=lambda *_args: None),
                personality=lambda _message: PersonalityState(room, "鲸鱼娘"),
                llm_for=lambda *_args, **_kwargs: model,
                reply_at=lambda _message, text, **_kwargs: sent.append(text) or True,
                response_guard=lambda *_args: None,
            )
            plugin = LocalControlPlugin()
            plugin.on_load(ctx)
            message = SimpleNamespace(type=1, is_group=True, chat_id=room, sender_id=owner,
                                      sequence=7, text="@机器人 帮我看看这个插件", data={"is_at_self": True})
            completed = SimpleNamespace(stdout="结论：检查 `dialogue.py:40` 的快照字段。", stderr="", returncode=0)
            seen_prompt = {}
            def fake_run(_command, **kwargs):
                seen_prompt["text"] = kwargs["input"]
                return completed
            with patch("wxbot_framework.local_control.shutil.which", return_value="codex.exe"), \
                    patch("wxbot_framework.local_control.subprocess.run", side_effect=fake_run):
                self.assertTrue(plugin.try_reply(message, ctx))
            body = "".join(sent)
            self.assertIn("群消息 #7", seen_prompt["text"])
            self.assertNotIn(room, seen_prompt["text"])
            self.assertIn("Codex 只读求助", body)
            self.assertIn("dialogue.py:40", body)
            self.assertIn("群消息 #7", body)
            self.assertEqual(plugin.store.count_recent("codex_readonly", chat_id=room, seconds=86400), 1)

    def test_non_owner_codex_request_creates_approval_proposal_without_running_codex(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            owner = "wxid_owner"
            room = "123456789@chatroom"
            members = SimpleNamespace(
                groups={room}, account="wxid_robot",
                resolve=lambda _alias, _room: SimpleNamespace(wxid=owner),
                conversation_snapshot=lambda _message: ConversationSnapshot(
                    room, "2026-10-31", 0, "群友", (), "帮我看看代码", ()),
                display_name=lambda _sender, _room: "群友",
            )
            model = SimpleNamespace(complete_json=lambda _system, _user: {
                "reply": "我先提个只读求助。",
                "proposal": {"action": "codex_readonly", "args": {
                    "workspace": "test", "task": "检查代码"}, "reason": "需要核对实现", "sources": []},
            })
            sent = []
            ctx = SimpleNamespace(
                members=members,
                config={"owner_aliases": ["主人"], "state_file": ".runtime/local_control.jsonl",
                        "codex_readonly": True, "codex_readonly_autonomous": True,
                        "codex_workspaces": {"test": str(workspace)}, "codex_daily_limit": 3},
                app_dir=directory,
                register_service=lambda *_args: None,
                unregister_service=lambda *_args: None,
                logger=SimpleNamespace(info=lambda *_args: None, warning=lambda *_args: None),
                personality=lambda _message: PersonalityState(room, "鲸鱼娘"),
                llm_for=lambda *_args, **_kwargs: model,
                reply_at=lambda _message, text, **_kwargs: sent.append(text) or True,
                response_guard=lambda *_args: None,
            )
            plugin = LocalControlPlugin()
            plugin.on_load(ctx)
            message = SimpleNamespace(type=1, is_group=True, chat_id=room, sender_id="wxid_friend",
                                      sequence=8, text="@机器人 帮我看看代码", data={"is_at_self": True})
            with patch("wxbot_framework.local_control.shutil.which", return_value="codex.exe"), \
                    patch("wxbot_framework.local_control.subprocess.run",
                          side_effect=AssertionError("non-owner must not execute Codex")):
                self.assertTrue(plugin.try_reply(message, ctx))
            rows = [json.loads(line) for line in
                    (Path(directory) / ".runtime" / "local_control.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["action"], "codex_readonly")
            self.assertEqual(rows[0]["status"], "pending")
            self.assertIn("诏书口令", "".join(sent))

    def test_status_followup_does_not_trigger_local_or_codex_action(self):
        plugin = LocalControlPlugin()
        plugin.catalog = [{"action": "codex_readonly"}]
        message = SimpleNamespace(text="@林老板的bot 好了吗", data={"is_at_self": True})
        self.assertFalse(plugin.try_reply(message, SimpleNamespace()))

    def test_codex_refuses_workspace_outside_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            executor = SafeLocalExecutor({"apps": {}, "autonomous_apps": [], "allowed_url_hosts": [],
                                          "codex_workspaces": {}}, app_dir=directory)
            with self.assertRaisesRegex(ValueError, "不在 allowlist"):
                executor.execute("codex_readonly", {"workspace": str(workspace), "task": "检查"})


if __name__ == "__main__":
    unittest.main()

