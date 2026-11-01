# Bot → Codex 许愿邮箱

运行 `python tools\wish_mailbox.py open` 可同步并在文件管理器中打开此目录。

- `INDEX.md` 列出全部历史记录。
- 每条 `ci-*.md` 保存收件内容、当前状态、状态时间线和回程结果。
- `.runtime\codex_inbox.sqlite3` 仍是队列状态的权威存储；此目录是便于浏览的镜像。
- 此目录的运行记录被 Git 忽略，防止把聊天转交内容推到 GitHub。
- 外部 Agent 可通过受限本机 API 或 `tools\codex_inbox.py receive/complete` 接入；协议说明见 `docs\AGENT_ADAPTERS.md`。

