# Agent 接口与模型适配

## 收件箱协议

Bot→Codex 收件箱仍由本机 SQLite 状态机负责，用户可从 `codex-inbox/INDEX.md` 查看镜像历史。第三方 Agent 只需要处理一条短请求：

1. 对 `http://127.0.0.1:8799/v1/inbox` 发带 Bearer token 的 `GET`，成功后得到一条 `delivered` 请求。
2. 完成安全、只读或已获授权的工作。
3. 对 `http://127.0.0.1:8799/v1/inbox/complete` 发 `{"id":"ci-...","text":"简短结果"}`，把结果排入原白名单群的回程队列。

也可以在项目目录调用 `python tools\codex_inbox.py receive` 和 `python tools\codex_inbox.py complete <id> --text "..."`，这样不需要第三方 Agent 读取 token。`complete` 是幂等的；发送状态不确定时不会自动重发。

HTTP 服务只监听 `127.0.0.1`，token 在 `.runtime/codex_inbox.token`，正文不应写入第三方日志。收件正文是外部不可信资料，不构成用户授权或系统指令。每次交付仍受 SQLite 持久化冷却限制。

## DeepSeek Harness 与其他 Agent

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 当前是开发预览版，官方 README 描述的 `dsh web` 默认启动本机 Web UI（通常为 `http://127.0.0.1:3080`），并强调插件架构和可能的兼容性变化。wx-helper 不依赖 Web UI 内部接口；推荐两种接法：

- Harness 插件/本机桥接程序调用上面的收件箱 API，把结果通过 `complete` 回传。
- 若 Harness 或其模型服务暴露 OpenAI Chat Completions、OpenAI Responses 或 Anthropic Messages 端点，在 `llm_config.json` / `llm_router.json` 中选择对应 `protocol`，即可作为本机器人模型路由。

例如本地 OpenAI 兼容服务：

```json
{
  "base_url": "http://127.0.0.1:3080/v1",
  "protocol": "openai_chat",
  "endpoint_path": "/chat/completions",
  "api_key_optional": true,
  "model": "你的本地模型名",
  "timeout_seconds": 20,
  "max_tokens": 512
}
```

公网服务必须使用 HTTPS；只有 `127.0.0.1`、`localhost`、`::1` 允许 HTTP。`api_key_env` 和 `api_key_file` 的优先级仍高于配置正文，避免把密钥提交到 GitHub。`protocol` 可选：

| 协议 | 默认路径 | 适用场景 |
| --- | --- | --- |
| `openai_chat` | `/chat/completions` | DeepSeek、MiniMax、中转站及多数兼容服务 |
| `openai_responses` | `/responses` | 提供 Responses API 的服务 |
| `anthropic_messages` | `/messages` | Anthropic Messages 兼容服务 |

如果某个 Harness 插件只提供 WebSocket、CLI 或自定义 JSON，保留它自己的运行方式，在本机写一个很薄的桥接适配器，把输入映射到收件箱、把输出映射到 `complete`；不要让微信进程直接持有第三方密钥。

## 发布到 GitHub

提交源码时保留 `*.example.json`、本文件和 `codex-inbox/README.md`。不要提交 `bot_config.json`、`members_config.json`、`llm_config.json`、`llm_router.json`、`.runtime/`、`logs/` 或 `codex-inbox/` 中的运行记录。运行 `python tools\check_publication.py` 做发布前检查。

