# tgrok — Telegram 群聊 AI 助手（类 @grok）

[English](README.md) | 简体中文

在 Telegram 群里实现类似 X 上 @grok 的体验：回复任意消息并 @bot 提问（如「这是真的吗？」），
bot 会结合被引用的消息内容，调用你本地的 LLM 模型（或任意 OpenAI 兼容 API）回答。

## 功能

- **引用提问**：回复某条消息 + @bot 提问，bot 结合该消息内容回答（核心的 @grok 用法）
- **直接提问**：在群里直接 @bot 问任何问题
- **多轮追问**：回复 bot 的回答可以继续对话，上下文自动延续
- **私聊**：私聊里直接发消息即可
- **图片理解**：搭配视觉模型可以对群里的图片提问
- **联网搜索**：模型可通过 `web_search` 工具自主联网搜索实时信息再回答、附来源（Tavily / DuckDuckGo / SearXNG / Serper 可选）
- **计算器**：内置 `calculate` 工具，模型可以用它核对算术、百分比、单位换算和日期推算，不靠心算（无需配置）
- **流式输出**：回复像打字机一样逐步出现；长生成不会被网关空闲超时掐断
- **权限控制**：管理员用命令管理白名单，无权限用户静默忽略
- **双语**：bot 消息和配置向导支持中文/英文（`BOT_LANG`）
- **四种 LLM 协议**：通过 `LLM_PROTOCOL` 选择 OpenAI 兼容 Chat Completions、OpenAI Responses API、Gemini 原生或 Claude 原生；Claude 原生保留 Anthropic thinking 与工具调用语义
- 可连接本地模型服务、云端中转、OpenAI 官方、Google Gemini 和 Anthropic API

## 1. 创建 Telegram Bot

1. 在 Telegram 里找 [@BotFather](https://t.me/BotFather)，发送 `/newbot`
2. 按提示取名字和用户名（用户名必须以 `bot` 结尾，如 `my_local_ai_bot`）
3. 拿到形如 `123456:ABC-xxxx` 的 **Bot Token**
4. **关闭隐私模式**（重要，否则群里 @ 它可能收不到消息）：
   - 给 BotFather 发 `/setprivacy` → 选择你的 bot → 选 **Disable**
   - 警告：如果在改隐私模式之前已经把 bot 拉进了群，需要把它**移出群再重新拉进来**才生效

## 2. 启动本地 LLM 服务

以 LM Studio 为例：打开 **Developer / Local Server** 页面，加载模型并启动服务（默认 `http://localhost:1234/v1`），记下页面上显示的模型名称。

vLLM 示例：

```bash
vllm serve Qwen/Qwen2.5-14B-Instruct --port 8000
# 接口地址为 http://localhost:8000/v1，模型名为 Qwen/Qwen2.5-14B-Instruct
```

## 3. 配置并运行 Bot

```bash
# 安装依赖（建议先创建虚拟环境）
python -m venv .venv
.venv\Scripts\activate        # Windows（Linux/macOS 用 source .venv/bin/activate）
pip install -r requirements.txt

# 交互式配置向导：逐项询问并生成 .env（启动时可选界面语言）
# 会在线验证 Token 有效性、自动列出 LLM 服务的可用模型供选择
python configure.py

# （也可以手动配置：复制 .env.example 为 .env 后编辑）

# 运行
python bot.py
```

把 bot 拉进群组，然后回复任意一条消息并输入 `@你的bot用户名 这是真的吗？` 试试。

## 配置项说明（.env）

| 变量 | 说明 | 默认值 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather 给的 token | （必填） |
| `BOT_LANG` | bot 消息语言：`zh` 或 `en` | `zh` |
| `LLM_PROTOCOL` | 主模型协议：`openai`、`responses`、`gemini` 或 `claude` | `openai` |
| `LLM_BASE_URL` | OpenAI 兼容、Responses 或 Claude 接口地址；Claude 可填根地址或以 `/v1` 结尾的地址 | `http://localhost:1234/v1` |
| `GEMINI_BASE_URL` | Gemini 原生回复和 grounding 的接口地址；留空连接 Google 官方 | （空） |
| `LLM_MODEL` | 模型名称 | `local-model` |
| `LLM_API_KEY` | API key；本地服务一般随便填 | `not-needed` |
| `LLM_USER_AGENT` | 自定义请求 UA（部分云端网关会校验） | SDK 默认值 |
| `SYSTEM_PROMPT` | 系统提示词；写在一行里，用 `\n` 表示换行（建议用双引号包起来） | 内置默认值 |
| `MAX_TOKENS` | 单次回答最大 token 数 | `1024` |
| `LLM_TEMPERATURE` | 采样温度；留空使用后端默认值，后端拒绝时自动去掉 | （空） |
| `LLM_TOP_P` | 核采样 top_p；留空使用后端默认值，后端拒绝时自动去掉 | （空） |
| `LLM_EXTRA_BODY` | JSON 对象；OpenAI 兼容、Responses 与 Claude 原生请求会透传，Gemini 原生忽略 | （空） |
| `MAX_HISTORY` | 多轮对话保留的消息条数 | `20` |
| `ENABLE_VISION` | 图片理解（需模型支持视觉输入） | `false` |
| `MAX_IMAGES` | 单次请求最多附带的图片数 | `4` |
| `SEARCH_PROVIDER` | 逗号分隔搜索源：`tavily`、`duckduckgo`、`searxng`、`serper`；留空关闭 | （空） |
| `TAVILY_API_KEY` | Tavily API key | （空） |
| `SERPER_API_KEY` | Serper API key | （空） |
| `SEARXNG_BASE_URL` | SearXNG 实例地址 | （空） |
| `GEMINI_SEARCH_MODEL` | 混合搜索使用的 Gemini grounding 模型 | （空） |
| `GEMINI_API_KEY` | Gemini/grounding key；留空复用 `LLM_API_KEY` | （空） |
| `SEARCH_MAX_RESULTS` | 单次搜索回灌给模型的结果条数 | `5` |
| `FETCH_CHAR_LIMIT` | 单次 `open_url` 回灌给模型的网页正文字数上限 | `3500` |
| `JINA_FALLBACK` | 直接提取网页失败时使用 Jina Reader 兜底 | `true` |
| `JINA_API_KEY` | 可选 Jina API key，用于提高速率限制 | （空） |
| `ADMIN_USER_IDS` | 超级管理员 ID（逗号分隔），可用命令管理白名单 | （空） |
| `ALLOWED_USER_IDS` | 白名单初始值，仅首次启动生效 | （空） |

## 权限控制

**三种模式：**

- `ADMIN_USER_IDS` 和白名单都为空：开放模式，所有人可用
- 配置了 `ADMIN_USER_IDS`：受控模式，只有**管理员 + 白名单内的用户**可用
- 无权限的用户被**完全静默忽略**（群聊、私聊、/start 均无任何反馈），仅在 bot 日志中记录

**管理员命令**（仅管理员可用，其他人发送会被静默忽略）：

| 命令 | 说明 |
|---|---|
| `/adduser 123456789` | 添加用户到白名单（可空格分隔多个 ID） |
| `/deluser 123456789` | 从白名单移除用户 |
| `/listusers` | 查看当前白名单 |

在群里也可以**直接回复某人的消息**发送 `/adduser` / `/deluser`，无需手动查 ID。

白名单修改实时生效并保存到 `allowed_users.json`，重启不丢失。`.env` 里的 `ALLOWED_USER_IDS` 只在该文件不存在时（首次启动）作为初始值。

获取自己的用户 ID：私聊 bot 发 `/start`（需有权限），或使用 @userinfobot。建议先把自己的 ID 配置成管理员再启动。

## LLM 协议与云端 API

通过 `LLM_PROTOCOL` 选择主模型协议：

- `openai`（默认）：OpenAI 兼容 `/chat/completions`，地址使用 `LLM_BASE_URL`。
- `responses`：OpenAI Responses API `/responses`，地址使用 `LLM_BASE_URL`。`gpt-6.1-sol` 这类只在 Responses 上支持工具调用的模型要用它（这类模型在 Chat Completions 上只能不带工具）。bot 的工具照常可用；推理强度写在 `LLM_EXTRA_BODY={"reasoning":{"effort":"low"}}`。
- `gemini`：通过 google-genai SDK 连接 Gemini 原生接口，地址使用 `GEMINI_BASE_URL`，留空连接 Google 官方。Google Search 和 URL context 在服务端执行，因此不走 bot 自带工具循环。
- `claude`：Anthropic Messages API 原生协议，地址使用 `LLM_BASE_URL`。保留原生 thinking 块、签名与工具语义，bot 自带搜索工具仍可使用。

`GEMINI_NATIVE_SEARCH` 和 `CLAUDE_NATIVE` 仅用于兼容旧配置；新配置只应设置 `LLM_PROTOCOL`。

接入 OpenAI 官方 API 时，把 `LLM_BASE_URL` 填为 `https://api.openai.com/v1`，协议用 `LLM_PROTOCOL=openai`；需要在 Responses API 上调用工具的模型改用 `LLM_PROTOCOL=responses`。bot 会自动兼容新模型要求的 `max_completion_tokens` 参数。

`LLM_TEMPERATURE` 和 `LLM_TOP_P` 均可留空以使用后端默认值。`LLM_EXTRA_BODY` 接受 JSON 对象，可用于 reasoning effort、thinking 等厂商私有字段；OpenAI 兼容、Responses 和 Claude 原生请求都会透传，Gemini 原生不会使用。后端明确拒绝这些可选参数时，适配器会去掉参数重试，并在当前进程内保持禁用。

不用手写这段 JSON：`python configure.py` 第 8 步识别到推理型 GPT 模型（o 系列、GPT-5 及以后）时，会给出推理强度菜单，也可以直接输入自定义值；向导按所选协议写成对应字段（Responses 用 `reasoning.effort`，Chat Completions 用 `reasoning_effort`），把偏小的 `MAX_TOKENS` 默认值调到 4096，并跳过 temperature/top_p。其它额外参数用 `键=值` 填写，嵌套键用点号，如 `thinking.type=enabled, thinking.budget_tokens=1000`。

模型支持视觉输入时，把 `ENABLE_VISION` 设为 `true`（配置向导第 6 步），即可：

- 回复一张图片并 @bot 提问：「这是什么？」「图里说的是真的吗？」
- 直接发图配文字 @bot
- 单次最多附带 4 张图，过大的图片文件（>10MB）会被跳过

注意：开启后图片会发送给你配置的 LLM 服务；若用的是云端 API，意味着群内图片会离开你的服务器。

## 联网搜索

模型本身没有联网能力，被问到实时信息时会说「我无法联网」或凭旧知识乱答。开启联网搜索后，
bot 会给模型挂载 `web_search` 工具：模型自主判断何时需要搜索，bot 代为执行并把结果回灌，
模型再用自己的话转述搜索结果作答，需要时在末尾附一两个关键来源链接（搜索期间消息会显示搜索状态）。

配置 `SEARCH_PROVIDER` 选择搜索源（或重跑 `python configure.py`，向导第 7 步）：

| 搜索源 | 额外配置 | 说明 |
|---|---|---|
| `tavily` | `TAVILY_API_KEY` | 托管服务，结果为 LLM 优化，质量最好；免费额度约 1000 次/月（[tavily.com](https://tavily.com) 注册） |
| `duckduckgo` | 无 | 零配置无需 key（走 `ddgs` 包）；稳定性一般，可能被限流 |
| `searxng` | `SEARXNG_BASE_URL` | 自建元搜索引擎，完全免费、隐私最好；实例需开启 JSON 输出格式 |
| `serper` | `SERPER_API_KEY` | 托管 Google 搜索结果，可与其他搜索源组合 |

说明：

- 需要模型/后端支持 **function calling**（工具调用）。主流云端模型和 LM Studio / vLLM /
  llama.cpp（需 `--jinja`）加载的较新开源模型均支持；后端不支持时 bot 自动退化为普通对话，不影响使用。
- 单次回答最多进行 3 轮工具调用（`SEARCH_MAX_ROUNDS` 可调，搜索和 `calculate` 共用），即最多 4 次模型调用，需要用工具的回答会稍慢。

## 部署（长期运行）

Bot 使用**轮询模式**（主动向 Telegram 拉取消息），不需要公网 IP 和域名——只要机器能访问外网，
放在家里和 LM Studio 同一台电脑上跑也完全可以。

### Docker（推荐用于服务器）

```bash
cp .env.example .env   # 编辑填好配置（或运行 configure.py）
docker compose up -d --build
docker compose logs -f # 查看日志
```

- `restart: unless-stopped` 保证崩溃或服务器重启后自动拉起
- 白名单持久化在 `./data/allowed_users.json`，容器重建不丢失
- 容器内访问宿主机上的 LLM 服务要用 `http://host.docker.internal:端口/v1`（容器内 `localhost` 不通）

### systemd（Linux 裸机替代方案）

`/etc/systemd/system/tgbot.service`：

```ini
[Unit]
Description=Telegram LLM Bot
After=network-online.target

[Service]
WorkingDirectory=/opt/tgrok
ExecStart=/opt/tgrok/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now tgbot
journalctl -u tgbot -f
```

## 常见问题

- **群里 @ 它没反应**：检查是否关闭了隐私模式（见上文步骤 4），且改完后重新拉群。
- **模型说自己无法联网**：默认它确实不能。配置 `SEARCH_PROVIDER` 开启联网搜索（见上文「联网搜索」）。
- **提示调用模型失败**：确认模型服务在运行，并检查所选协议对应的地址和模型：OpenAI/Claude 使用 `LLM_BASE_URL`，Gemini 原生使用 `GEMINI_BASE_URL`，同时确认 `LLM_MODEL`。
- **多轮对话失忆**：对话历史保存在内存中，bot 重启后会丢失；此时回复 bot 消息仍可继续问，只是只带上 bot 上一条回答作为上下文。
