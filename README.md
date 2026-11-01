# wx-helper

一个面向 Windows 桌面环境的本地微信机器人框架。项目把消息接收、群聊上下文、人格提示、模型适配、记忆和可选插件拆开，适合先用只读监听验证，再按需启用模型回复或本机集成。

本仓库不预置机器人账号、群 ID、API 密钥或私有模型服务地址。首次运行后在本机配置服务商、微信会话和权限范围。

> 微信连接采用非官方桌面适配方式，兼容性取决于本机客户端版本和桥接指纹。请先用测试账号验证；项目无法保证账号不受平台限制。未验证的 UI Automation 路径只读，不会替你发送消息。

## 功能

- Windows 图形控制台：配置后端、模型、插件、群白名单和发送保护。
- 多种消息后端：版本锁定的原生文本桥接；未匹配时可用数据库只读接收或 UI Automation 只读降级。
- 群聊 @ 对话：结合发言者历史和近期群上下文，默认使用“DeepSeek//鲸鱼娘·大肥鱼”人格。
- 私聊对话、群聊总结、成语接龙、群玩法、成员绑定和三层记忆。
- 可选 Minecraft 状态/控制插件、白名单本机动作、Codex 收件箱。
- 单模型或分层路由；支持 OpenAI Chat Completions、OpenAI Responses、Anthropic Messages 风格接口。
- 本地调用统计、发送节奏和重复消息保护。

## 快速开始

### 环境

- Windows 10/11 x64；桌面交互后端需要已登录的 Windows 桌面会话。
- Python 3.12 x64；源码启动需要 tkinter。
- 微信版本需与 `native/compatibility.json` 中的受支持指纹匹配，才可使用对应原生收发能力。其他版本可能只能接收或不能连接。
- 需要 AI 功能时，准备一个与所选协议兼容的模型服务和密钥。

### 安装与启动

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python tools\init_config.py
python gui.py
```

GitHub 源码发布不包含编译后的微信桥接 DLL。若要启用不依赖鼠标点击的原生文本收发，请在 **Visual Studio x64 Native Tools Command Prompt** 中进入项目目录并构建桥接：

```powershell
Set-Location <项目目录>
.\native\build_bridges.ps1 -Target all
```

构建需要 Visual Studio 2022 C++ x64 工具链；生成的 DLL 留在本机且不会纳入 Git。原生桥接仅匹配 `native/compatibility.json` 指定的微信版本与指纹。其他版本不会自动切换到点击发送；可用后端和限制见 [`native/README.md`](native/README.md)。

也可使用控制台运行：

```powershell
python main.py
```

首次运行会从 `*.example.json` 建立本地配置。使用 GUI 前，先在 `bot_config.json` 选择后端并设置白名单，再到“群与成员”页面确认要启用的群。`uia_text` 只读取当前打开的目标会话，需要填写 `uia_chat_name`，且目标群必须唯一配置。

### 配置模型

将 `llm_config.example.json` 复制为 `llm_config.json`，填写服务商提供的 Base URL、协议、模型名和密钥来源。不要把真实密钥写进仓库；推荐使用环境变量：

```json
{
  "base_url": "https://api.example.com/v1",
  "protocol": "openai_chat",
  "endpoint_path": "/chat/completions",
  "api_key_env": "WX_HELPER_API_KEY",
  "api_key": "",
  "model": "your-model",
  "timeout_seconds": 20,
  "max_tokens": 512,
  "context_limit": 128000,
  "temperature": null,
  "top_p": null,
  "presence_penalty": null,
  "frequency_penalty": null
}
```

`api.example.com` 和 `your-model` 是占位值，必须替换为你自己的服务配置。公网请求必须使用 HTTPS；本机服务可以使用受限的回环地址。密钥也可通过 `api_key_file` 读取。

生成参数均可留空，留空时不发送该字段，由服务商使用默认值。`temperature` 范围为 0–2；Anthropic Messages 按其接口范围限制为 0–1。`top_p` 范围为 0–1；`presence_penalty` 和 `frequency_penalty` 仅用于 OpenAI Chat 风格请求。不同模型对这些参数的支持不完全相同，报参数错误时先清空不支持的项。日常聊天可从 temperature 0.8–0.9 试起；这只调节表达多样性，不会代替人格提示和对话上下文。

可选模型路由见 `llm_router.example.json`。按 `fast`、`planner`、`fallback` 分层时，同一层按文件顺序尝试；失败达到阈值后短暂熔断。每个 route 的模型、密钥变量和生成参数都在本地配置中单独指定。

## 人格与提示词

提示词由框架统一合成，默认群人格为“鲸鱼娘”；可在群里用 `/切换人格 嘴臭`、`/切换人格 卑微`、`/切换人格 夸夸` 临时切换，`/切换人格 取消` 回到配置的默认人格。多人格是可选覆盖，不影响事实、安全和工具权限。

从高到低的约束顺序为：事实/安全/授权 → 工具与任务要求 → 输出格式 → 人格身份与语气 → 本轮轻量情绪 → 可选风格档案 → 聊天上下文。闲聊倾向短句、接梗和自然回应；认真求助时再展开。情绪每轮根据上下文轻微变化，不记录虚构的长期心情。完整设计见 [`docs/PROMPT_ARCHITECTURE.md`](docs/PROMPT_ARCHITECTURE.md)。

所有聊天相关插件通过统一人格包装器调用模型。提示词中的聊天记录和昵称视为不可信资料，不能改变系统权限或输出格式；人格也不能让机器人声称做过未实际执行的操作。

## 记忆与数据

| 层 | 保存内容 | 默认行为 |
| --- | --- | --- |
| 热 | 当天群聊原文 | @ 对话使用当前发言者最近最多 10 条及群内最近最多 20 条有效消息；可按设置每日清理 |
| 温 | 主人主动整理的近期摘要 | 默认保留 30 天；由已配置模型生成，可查看和删除 |
| 冷 | 主人明确保存的稳定事实、偏好和边界 | 每群最多 50 条，直到手动删除 |

群聊记忆存于本机 SQLite，不会自动抽取冷记忆。触发 AI 插件时，相关上下文会发送到你选择的模型服务商。细节与群内命令见 [`docs/MEMORY_ARCHITECTURE.md`](docs/MEMORY_ARCHITECTURE.md) 和 [`PRIVACY.md`](PRIVACY.md)。

## 插件

| 插件 | 用途 |
| --- | --- |
| `mention_chat` | 群聊 @ 对话与近期上下文 |
| `dm_chat` | 白名单好友私聊对话 |
| `chat_records` / `chat_summary` | 群记录、按成员或全群总结 |
| `memory` / `member_identity` | 三层记忆和成员绑定 |
| `personality` | 按群切换聊天人格 |
| `idiom_chain` / `group_fun` | 成语接龙、签到、积分、排行和抽奖 |
| `mc_status` / `mc_presence` / `mc_control` | 接入已配置的 Minecraft Agent 状态与受限控制 |
| `local_control` | 白名单应用、URL 或只读 Codex 操作；默认关闭，按本机配置启用 |
| `echo` / `id_responder` | 框架联调与状态检查 |

插件通过 `plugins/<name>/plugin.json` 声明元数据和默认设置。新增插件可复制 `plugins/_template`。

## Agent 与本机控制

Codex 收件箱以本机数据库作为队列，可选提供 loopback HTTP 适配入口，并在 `codex-inbox/` 生成便于用户查看的历史。收件内容始终是不可信文本，不构成执行授权。外部 Agent 适配方式、确认状态和回程队列说明见 [`docs/AGENT_ADAPTERS.md`](docs/AGENT_ADAPTERS.md) 与 [`docs/CODEX_INBOX.md`](docs/CODEX_INBOX.md)。

本机控制插件默认关闭。只读查询、应用启动和网页打开受独立白名单及确认流程约束；不要把任意 shell、群聊消息或模型提案直接执行为命令。启用前先检查插件说明与示例配置。

## 开发与测试

```powershell
python -m pip install -r requirements.txt
python -m pytest -q
python tools\check_publication.py
```

测试使用临时数据和模拟模型，不需要微信账号或真实 API 密钥。构建原生桥接或 EXE 时才需要 `requirements-build.txt` 和 Visual Studio C++ Build Tools：

```powershell
python -m pip install -r requirements-build.txt
.\native\build_bridges.ps1
.\tools\build_exe.ps1
```

先运行完整测试，再检查打包后的目标文件；桥接二进制只应由你信任的构建流程产生。

## 发布检查

`*.example.json` 只能包含占位 URL、空密钥、空群列表和通用别名。禁止提交：

- `bot_config.json`、`members_config.json`、`llm_config.json`、`llm_router.json`；
- `.runtime/`、`logs/`、SQLite/CSV 导出和聊天记录；
- 密钥文件、环境文件、个人风格问卷与回答、收件箱运行历史；
- 含账号、群 ID、昵称、私人服务地址或本机绝对路径的截图与文档。

发布前执行 `python tools\check_publication.py`。该工具只报告问题类别和文件位置，不回显匹配到的秘密内容。Git 忽略规则只是最后一道防线，仍需人工检查暂存区和历史记录。

## 项目结构

```text
wx-helper/
├─ gui.py, main.py                 # 桌面控制台与命令行入口
├─ bot_config.example.json         # 微信后端、插件与权限模板
├─ llm_config.example.json         # 单模型模板
├─ llm_router.example.json         # 分层路由模板
├─ members_config.example.json     # 群记录、成员与默认人格模板
├─ wxbot_framework/                # 消息、模型、记忆、人格和后端抽象
├─ plugins/                        # 可独立启停的插件
├─ native/                         # 原生适配源码、兼容性信息与第三方源码
├─ tools/                          # 初始化、测试、蒸馏、发布检查工具
├─ tests/                           # 自动化测试
└─ docs/                            # 设计与数据流说明
```

## 免责声明

本项目是个人学习和研究用途的软件，不是微信官方产品。使用者需自行确认当地法律、平台规则、账号权限和模型服务条款；项目按原样提供，不保证第三方平台或客户端升级后的兼容性。
