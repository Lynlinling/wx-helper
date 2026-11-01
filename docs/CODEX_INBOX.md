# Bot → Codex 本机收件箱

该收件箱只在 Bot 配置启用后运行，监听地址固定为 `127.0.0.1`。本机 `codex_inbox_watch.py` 每 15 秒只读检查 SQLite 状态；空队列不调用模型。发现待办后，它通过 Codex App Server 向指定会话发送一次固定唤醒指令，由 Codex 再显式调用 `receive` 读取正文。会话忙碌时监听器退避重试，不抢占当前回合，也不把消息正文直接塞进唤醒指令。监听器不连接公网、不操作微信界面，也不会自动批准操作。Codex 完成后可用 `complete` 写入结果，Bot 会通过现有发送保护回到原始白名单群，并在本机记录发送状态。

## 配置

`bot_config.json` 中启用：

```json
{
  "codex_inbox": {
    "enabled": true,
    "host": "127.0.0.1",
    "port": 8799,
    "database_file": ".runtime/codex_inbox.sqlite3",
    "token_file": ".runtime/codex_inbox.token",
    "min_interval_seconds": 600,
    "max_pending": 32
  },
  "plugins": {
    "local_control": { "enabled": true, "codex_inform": true }
  }
}
```

Bot 只接受已绑定主人的 `codex_inform` 提案；触发话语必须明确要求转告 Codex。内容限 2,000 字符，常见 API 密钥、访问令牌、微信账号标识和群 ID 模式会被拒收。消息仅保存摘要文本、来源标签、随机消息编号、时间与状态；幂等键只保存 SHA-256 摘要。待处理队列最多 32 条。

提交消息与交付给 Codex 各自有持久化的 600 秒全局冷却。HTTP 接口使用 `.runtime/codex_inbox.token` 中的 Bearer token：

- `POST http://127.0.0.1:8799/v1/inbox`：提交 `{"text":"...","source":"wechat_bot","idempotency_key":"..."}`。
- `GET http://127.0.0.1:8799/v1/inbox`：取出一条待处理消息；冷却中返回 HTTP 429。
- `POST http://127.0.0.1:8799/v1/inbox/ack`：确认已处理的编号，正文为 `{"ids":["ci-..."]}`。
- `POST http://127.0.0.1:8799/v1/inbox/complete`：完成请求并排入受白名单保护的回程队列，正文为 `{"id":"ci-...","text":"简短结果"}`。

Codex 可在自己的任务中离线读取同一 SQLite 队列：

```powershell
python tools\codex_inbox.py receive
python tools\codex_inbox.py complete ci-消息编号 --text "检查完成：发现一处配置缺口，未修改文件。"
python tools\codex_inbox.py status ci-消息编号
```

## 人工查看与历史

SQLite 仍是状态机的权威存储；用户可浏览的 Markdown 镜像位于项目根目录 `codex-inbox/`。每条请求生成一份 `ci-*.md`，记录收件正文、状态时间线与回程结果，`INDEX.md` 列出历史。旧数据库没有完整状态时间线时，镜像会标注迁移时可观察到的状态，不伪造原始变更时间。

运行下面命令可同步历史并在文件管理器中打开目录：

```powershell
python tools\wish_mailbox.py open
```

该目录中的运行记录已由 `.gitignore` 排除，避免上传到 GitHub；`README.md` 是仓库内的使用说明。不要把运行记录改为跟踪文件。

## Agent 接入

除 Codex CLI 外，其他本机 Agent 可复用同一个队列：用 `GET /v1/inbox` 接收，再用 `POST /v1/inbox/complete` 回报。两者都要求 Bearer token，服务只绑定 `127.0.0.1`。也可调用 `python tools\codex_inbox.py receive` 与 `complete`，以避免 Agent 自己处理 token。接入规则见 [`AGENT_ADAPTERS.md`](AGENT_ADAPTERS.md)。

## 本地事件监听

Windows 登录后任务 `WxHelperCodexInboxWatch` 启动本地监听器。它仅在有待办且交付冷却结束时唤醒已配置的 Codex 会话；若 Codex 会话正由其他回合写入，则等待空闲后重试。监听状态与日志保存在 `.runtime`，不含消息正文、账号或群聊标识。可手动只读检查：

```powershell
python tools\codex_inbox_watch.py --once --dry-run
```

查看 `.runtime\codex_inbox_watch.log` 可确认监听与重试状态。脚本使用本机安装的 `codex app-server` stdio 接口；若 Codex CLI 更新导致协议不兼容，监听会记录失败并退避重试，不会将收件消息标记为已交付。

`complete` 原子确认已交付请求并排入一次回程消息；结果最多 850 个 UTF-8 字节，只能发回原请求所在且仍在当前 `chat_allowlist` 的群。Bot 轮询结果队列时使用原生发送后端、发送限速和重复抑制；失败会记为 `failed`，不会自动重试，以免结果不确定时重复发群。Codex 可用 `status` 查看是否已发送。旧的 `ack` 仍可用于不需要回程的处理。

收到的正文一律视为不可信文本，不是执行授权。它不会自动触发工具、命令、文件写入或其他动作；Codex 只会在后续任务活动时看到它。回程内容同样经过敏感信息拦截，并且不能指定新的群聊目标。
