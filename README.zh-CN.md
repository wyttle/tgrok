# tgrok — Telegram 群聊 AI 助手（类 @grok）

[English](README.md) | 简体中文

在 Telegram 群里实现类似 X 上 @grok 的体验：回复任意消息并 @bot 提问（如「这是真的吗？」），
bot 会结合被引用的消息内容，调用你本地的 LLM 模型（或任意 OpenAI 兼容 API）回答。

## 功能

- **引用提问**：回复某条消息 + @bot 提问，bot 结合该消息内容回答（核心的 @grok 用法）
- **直接提问**：在群里直接 @bot 问任何问题
- **多轮追问**：回复 bot 的回答可以继续对话，上下文自动延续
- **私聊**：私聊里直接发消息即可
- **图片理解**：搭配视觉模型可以对群里的图片和贴纸提问；没开视觉时回复 bot 一个贴纸也会接话（模型能看到贴纸对应的 emoji）
- **看视频、听语音**：配上 `GEMINI_MEDIA_MODEL` 后，视频、GIF、圆形视频、语音和音频交给 Gemini 看/听，转写和画面描述交给主模型（主模型不用支持视觉）
- **联网搜索**：模型可通过 `web_search` 工具自主联网搜索实时信息再回答、附来源（Tavily / DuckDuckGo / SearXNG / Serper 可选）
- **计算器**：内置 `calculate` 工具，模型可以用它核对算术、百分比、单位换算和日期推算，不靠心算（无需配置）
- **发图**：需要给人看图时（对方要图、问长什么样），模型调用 `send_image`，给图片直链（用 `open_url` 读过的网页会列出页面里的图片）或者给关键词让 bot 搜图；搜到的候选图配了 `GEMINI_MEDIA_MODEL` 时先让 Gemini 确认对题，图片跟在文字回复后面发出。下载只走公网地址（每次跳转都重新校验）、必须真是图片、不超过 10MB，一条回复最多 4 张
- **长期记忆**：按聊天分开，精选条目（群笔记和每个人的档案）加可检索的聊天原话，参照 Hermes Agent 和 OpenClaw
- **梗词典**：弄明白一个梗或隐喻（或者被群友纠正）后会记住真正的意思，下次直接用
- **流式输出**：回复像打字机一样逐步出现；等待期间进度行显示计时和正在执行的工具，聊天里显示「正在输入」；长生成不会被网关空闲超时掐断
- **富文本回复**：回复用 Telegram 富文本消息（`sendRichMessage`）发出：分段、加粗/斜体/行内代码、代码块、链接、剧透，长回复的细节收进可折叠引用；模型只写白名单里的几个 HTML 标签，其余一律转义成文字，Telegram 拒绝这条格式时自动改发纯文本
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
# 会在线验证 Token 有效性，并按协议（OpenAI 兼容 / Claude / Gemini）自动列出主模型和备用模型的可用模型供选择
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
| `LLM_FALLBACK_MODEL` | 备用模型；主模型还没输出正文就出错时，这条回复改用它；留空 = 不启用 | （空） |
| `LLM_FALLBACK_PROTOCOL` / `LLM_FALLBACK_BASE_URL` / `LLM_FALLBACK_API_KEY` | 备用模型的协议、地址和 key；留空沿用主模型 | （空） |
| `LLM_FALLBACK_EXTRA_BODY` | 备用模型的额外请求参数（不继承 `LLM_EXTRA_BODY`） | （空） |
| `MAX_HISTORY` | 多轮对话保留的消息条数 | `20` |
| `CONVERSATION_KEEP_DAYS` | 回复 bot 多少天以内的消息还能接上上下文（重启后也能） | `7` |
| `MEMORY_DB` | SQLite 数据库：记忆、追问用的对话历史、最近群聊和用量统计 | 白名单文件旁的 `memory.db` |
| `LOG_DIR` / `LOG_KEEP_DAYS` | 日志文件目录（每天一个文件）和保留天数；天数为 `0` 只输出到控制台 | 白名单文件旁的 `logs/` / `14` |
| `MEMORY_ENABLED` | 按聊天保存的长期记忆：精选条目 + 可检索的原始记录 | `true` |
| `MEMORY_GROUP_CHARS` | 每个聊天的群笔记字数上限 | `1500` |
| `MEMORY_PROFILE_CHARS` | 每个人在每个聊天的档案字数上限 | `500` |
| `MEMORY_ARCHIVE_DAYS` | 原始记录保留天数 | `90` |
| `MEMORY_CONSOLIDATE_INTERVAL` | 每个聊天后台整理的最短间隔（分钟） | `60` |
| `MEMORY_CONSOLIDATE_MIN` | 攒够多少条新记录才整理（放了一天也整理） | `10` |
| `MEMORY_OBSERVE_CHATS` | 静默旁听的群 id，逗号分隔；这些群里没 @ bot 的消息也存进原始记录 | （空） |
| `SLANG_ENABLED` | 梗词典：模型用 `remember_slang` 记下弄明白的梗，消息里出现时附上释义 | `true` |
| `SLANG_MAX_ENTRIES` | 梗词典条目上限，超出淘汰最久没用到的 | `500` |
| `ENABLE_VISION` | 图片理解（需模型支持视觉输入） | `false` |
| `GEMINI_MEDIA_MODEL` | 看视频、听语音用的 Gemini 模型；没开视觉时图片也交给它看。走 `GEMINI_BASE_URL` / `GEMINI_API_KEY` | （空） |
| `MEDIA_PREFETCH` | 视频语音一出现就让 Gemini 在后台转述，问到时直接带上 | `true` |
| `GEMINI_HELPER_MODEL` | 记忆整理用的 Gemini 模型；留空沿用 `GEMINI_MEDIA_MODEL` | （空） |
| `RECENT_CHAT_COUNT` | 提问时附上的最近群聊条数；`0` 关闭 | `15` |
| `RECENT_CHAT_MINUTES` | 最近群聊只取这么多分钟以内的 | `60` |
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
| `RESEARCH_MODELS` | `research` 工具用的 omp 模型名（逗号分隔），每个模型一个研究员；留空关闭 | （空） |
| `RESEARCH_FALLBACK_MODELS` | 每个研究员的备选 omp 模型（按位置对应，只写一个则共用）；研究员失败时换它再跑一次，时间用完的除外 | （空） |
| `RESEARCH_TIMEOUT` | 单个研究员最多跑多少秒 | `150` |
| `RESEARCH_THINKING` | 研究员的思考强度（`omp --thinking`） | `low` |
| `RESEARCH_CONCURRENCY` | 同时最多几个调研任务 | `2` |
| `RESEARCH_WARM` | 每个研究员模型常驻一个 omp 进程（每个约 200MB 内存），接到任务立刻开工，省掉每次 10 到 15 秒的冷启动 | `true` |
| `OMP_BIN` | omp 可执行文件 | `omp` |
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
| `/stats` | 今天和近 7 天的模型调用次数和 token，按功能（回复、调研、搜索、看视频/听语音、记忆整理）和模型分开 |
| `/memory` | 查看本聊天的记忆条目和编号（管理员；私聊里白名单用户也可用） |
| `/forget [编号]` | 删掉指定条目；不带编号则清空本聊天的记忆和聊天记录（权限同上） |
| `/forgetme` | 删掉你在所有聊天里的档案和聊天记录（白名单用户可用） |
| `/learn 词=解释\|语境` | 教 bot 这个词在本群的一种用法（语境可省略）；`/learn 通用 …` 所有群通用（白名单用户可用） |
| `/slang` | 查看记住的梗和说法（管理员；私聊里白名单用户也可用） |
| `/unlearn 词` | 删掉一个记住的说法（权限同 `/slang`） |

记忆参照 Hermes Agent 和 OpenClaw 分两层，按聊天分开，存在白名单文件旁的 `memory.db`（Docker 下在 `/data`）：

- **原始记录。** 每一轮和 bot 的问答，以及 `MEMORY_OBSERVE_CHATS` 里那些群的所有消息，原样存进 SQLite 全文索引（trigram 分词，中文可用；少于三个字的词退回慢一点的逐条匹配）。模型用 `recall` 工具按关键词、说话人或时间查，拿到带日期和说话人的原话，「上周谁说过 XX」能查到真话而不是瞎猜。超过 `MEMORY_ARCHIVE_DAYS` 天的记录会删掉。
- **精选条目。** 群笔记（约定、长期话题、群里对 bot 的要求）和每个人的档案（是谁、长期偏好、让 bot 记住的事），一条一条存、带编号、各有字数上限。模型在聊天中用 `memory` 工具增改删（「记住我生日是 5 月 3 号」当场就存）；写满时工具拒绝并列出现有条目，模型得先合并或删掉旧的，不会悄悄丢东西。后台每 `MEMORY_CONSOLIDATE_INTERVAL` 分钟最多整理一次（攒够 `MEMORY_CONSOLIDATE_MIN` 条新记录，或放了一天），读新记录提出增、改、删；没提到的条目原样保留，不会越整理越走样。一时的事带到期时间，过期后不再出现。

新对话开头会附上群笔记、提问者和被引用者的档案，以及最近几条提问，每条标着距今多久。追问沿用同一份历史，所以一段对话只注入一次，上游提示缓存照样命中。附注里写明默认忽略，只有相关时才用。情绪发泄、玩笑和一时的状态（比如「爆仓了要跳楼」）不会记成条目。首次启动时旧的 `memory.json` 会自动迁移（摘要每行变成群笔记，最近的问答放进原始记录），然后改名为 `memory.json.migrated`。

**静默旁听。** 在 `MEMORY_OBSERVE_CHATS` 里填上群 id（在群里发 `/memory` 可以看到 id），这些群里没 @ bot 的消息也会存进原始记录，bot 不回复，也不会每条消息都请求模型。Telegram 只把普通群消息推给关闭了隐私模式的 bot：在 BotFather 里 `/setprivacy` → Disable，再把 bot 移出群重新拉进来；或者把 bot 设为群管理员。

**梗词典。** 中文的梗、黑话、谐音和隐喻很少是字面意思，同一个词换个句子意思也可能完全不同。所以词典不是「一个词对应一个释义」：每个词最多留 3 种用法，每种都记下什么语境下是这个意思、当时的原句。模型弄明白一种用法后（从专门讲这个梗的页面，或者群友纠正、解释了它），会调用 `remember_slang` 工具记下来；被纠正时新说法排在最前面，旧理解不会直接覆盖。之后的消息（或它引用的消息）里出现记住的词，就把这些用法作为参考附进请求，并明确告诉模型先看这句话的语境，对不上就可能只是字面意思，别硬套。只附真正出现的词，最多 5 个；「典」「急」这类单字梗只在短句里才算出现。通用网络梗所有群共享，群内梗只在学到它的那个群里用。群友可以用 `/learn 词=解释|什么语境下是这个意思` 手动教。开了联网搜索时，还会告诉模型怎么查梗：用「某某 是什么梗 / 出处」去搜，打开萌娘百科、知乎、百度百科、B 站解读这类专门讲这个梗的页面读正文，而不是只看搜索摘要，再对照原消息判断用的是哪层意思。词典存在白名单文件旁的 `slang.json`。

在群里也可以**直接回复某人的消息**发送 `/adduser` / `/deluser`，无需手动查 ID。

白名单修改实时生效并保存到 `allowed_users.json`，重启不丢失。`.env` 里的 `ALLOWED_USER_IDS` 只在该文件不存在时（首次启动）作为初始值。

获取自己的用户 ID：私聊 bot 发 `/start`（需有权限），或使用 @userinfobot。建议先把自己的 ID 配置成管理员再启动。

## LLM 协议与云端 API

通过 `LLM_PROTOCOL` 选择主模型协议：

- `openai`（默认）：OpenAI 兼容 `/chat/completions`，地址使用 `LLM_BASE_URL`。
- `responses`：OpenAI Responses API `/responses`，地址使用 `LLM_BASE_URL`。`gpt-6.1-sol` 这类只在 Responses 上支持工具调用的模型要用它（这类模型在 Chat Completions 上只能不带工具）。bot 的工具照常可用；推理强度写在 `LLM_EXTRA_BODY={"reasoning":{"effort":"low"}}`。
- `gemini`：通过 google-genai SDK 连接 Gemini 原生接口，地址使用 `GEMINI_BASE_URL`，留空连接 Google 官方。Google Search 和 URL context 在服务端执行，因此不走 bot 自带工具循环。
- `claude`：Anthropic Messages API 原生协议，地址使用 `LLM_BASE_URL`。保留原生 thinking 块、签名与工具语义，bot 自带搜索工具仍可使用。

旧开关 `GEMINI_NATIVE_SEARCH` 和 `CLAUDE_NATIVE` 已不再读取：`.env` 里还有它们而没有 `LLM_PROTOCOL` 时，bot 启动时报错并提示该写成什么。重新运行一次 `python configure.py` 会自动改写成 `LLM_PROTOCOL`。

**备用模型。** 设置 `LLM_FALLBACK_MODEL`（或用 `python configure.py` 第 8.5 步）即可启用。每条回复都先调用主模型；主模型在输出任何正文之前出错（HTTP 错误、配额、超时、模型不存在等），这条回复剩下的部分改由备用模型完成，切换时进度行会提示，回复末尾注明由哪个备用模型回答。这条提示只用于显示，不进追问历史。主模型已经输出部分正文后才断开的，仍按原来的方式保留已有内容。备用模型可以用不同的协议、地址和 key，留空的项沿用主模型。

接入 OpenAI 官方 API 时，把 `LLM_BASE_URL` 填为 `https://api.openai.com/v1`，协议用 `LLM_PROTOCOL=openai`；需要在 Responses API 上调用工具的模型改用 `LLM_PROTOCOL=responses`。bot 会自动兼容新模型要求的 `max_completion_tokens` 参数。

`LLM_TEMPERATURE` 和 `LLM_TOP_P` 均可留空以使用后端默认值。`LLM_EXTRA_BODY` 接受 JSON 对象，可用于 reasoning effort、thinking 等厂商私有字段；OpenAI 兼容、Responses 和 Claude 原生请求都会透传，Gemini 原生不会使用。后端明确拒绝这些可选参数时，适配器会去掉参数重试，并在当前进程内保持禁用。

不用手写这段 JSON：`python configure.py` 第 8 步识别到推理型 GPT 模型（o 系列、GPT-5 及以后）时，会给出推理强度菜单，也可以直接输入自定义值；向导按所选协议写成对应字段（Responses 用 `reasoning.effort`，Chat Completions 用 `reasoning_effort`），把偏小的 `MAX_TOKENS` 默认值调到 4096，并跳过 temperature/top_p。其它额外参数用 `键=值` 填写，嵌套键用点号，如 `thinking.type=enabled, thinking.budget_tokens=1000`。

模型支持视觉输入时，把 `ENABLE_VISION` 设为 `true`（配置向导第 6 步），即可：

- 回复一张图片并 @bot 提问：「这是什么？」「图里说的是真的吗？」
- 直接发图配文字 @bot
- 单次最多附带 4 张图，过大的图片文件（>10MB）会被跳过

注意：开启后图片会发送给你配置的 LLM 服务；若用的是云端 API，意味着群内图片会离开你的服务器。

**看视频、听语音。** 主模型一般看不了视频、听不了语音（Claude 不收视频，中转站也未必转发），所以交给 Gemini 代劳：设置 `GEMINI_MEDIA_MODEL`（例如 `gemini-2.5-flash`），bot 会给主模型挂上 `watch_media` 工具。有人发了视频、GIF、圆形视频、语音或音频（也包括以文件形式发送的），并 @bot、回复 bot 或在私聊里发时，主模型会看到「发了语音（8 秒），media_id=…」这样的说明，需要内容时带着具体问题（说了什么、发生了什么、这是哪里）调用工具。Gemini 下载原文件，画面和声音一起看，返回逐字转写和画面描述，主模型再自己判断、作答。进度里会显示「查看媒体: …」。追问时同一段媒体可以换个问题再看。没开 `ENABLE_VISION` 时图片也走这条路，纯文本模型也能看图。

- 用 Gemini 原生接口（`GEMINI_BASE_URL`，留空连 Google 官方；key 用 `GEMINI_API_KEY`，留空复用 `LLM_API_KEY`），和 grounding 搜索共用
- 官方 Bot API 只能下载 20MB 以内的文件，更大的会告诉主模型「看不了」
- 主模型走 `LLM_PROTOCOL=gemini` 时没有 bot 工具循环，这个功能不生效
- 没配 `GEMINI_MEDIA_MODEL` 时，主模型会被明确告知「看不到也听不到」，不会凭说明瞎编内容
- 媒体会发给 Gemini 服务；bot 重启后旧消息里的媒体要重新发一次才能看
- **预读**：bot 收到的视频、语音等（包括没叫它的群消息）一出现就在后台让 Gemini 完整转述，同一个文件只做一次，最多同时 4 个。之后有人引用它提问，或者问「他刚才语音说啥」，转述直接写在提问里，主模型不用再调工具等一轮；转述还没好时 `watch_media` 等这一份，不重复请求。旁听群里的语音视频记进原始记录时也带上转述

**最近群聊。** 群友常常就刚才聊的内容直接 @bot。bot 收到的每条群消息（关闭隐私模式后是全部群消息）都按群存下来，连同 bot 自己的回复；提问时附上 `RECENT_CHAT_MINUTES` 分钟内最近 `RECENT_CHAT_COUNT` 条，每行写明「几点 谁 回复 谁：说了什么」，语音视频带上转述。回复 bot 追问时只附上那条回答之后的新消息。存在 `MEMORY_DB` 里，重启不丢，更早的随新消息写入时删掉。

**分清谁说的。** 回复别人的消息并 @bot 时，被引用的消息单独标成「【被引用的消息】发送者：小李（不是提问者）」，提问者的话标成「【提问】阿强 回复了上面这条消息，问你：…」；系统提示词要求模型把话安在真正说的人头上，消息里的「我」指它的作者。转发的消息写明原作者（转发的人只是转发），频道和匿名管理员发的按频道名标注，提问者只引用了一段时也会写出那一段，引用自己的消息会标明是本人。记进原始记录时，被引用的原话按原作者单独记一条（按 Telegram 消息 id 去重），提问只记「（回复了 小李 的消息）这是真的吗」，以后查记录、整理记忆时不会把别人的话算到提问者头上。

**提速。** 配了 `GEMINI_HELPER_MODEL`（或只配了 `GEMINI_MEDIA_MODEL`）后，记忆整理改用帮手模型，不占主模型的并发和额度，帮手失败时再用主模型。显示更新不会阻塞生成：Telegram 编辑慢或被群限流时，模型和工具照常往下跑；占位消息被限流时等一下重发，不会放弃这条回复。

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

## 深入调研（omp）

遇到要认真查证的问题（不认识的梗和小众黑话、有争议的说法、需要准确数字和日期的事实），模型可以调用 `research` 工具。
调研任务会同时交给 `RESEARCH_MODELS` 里的每个模型：每个模型起一个 [omp](https://omp.sh) 无界面进程，只能搜索和读公网网页，
读原文后交回事实要点和读过的来源。所有报告一起交给回复用的模型，由它对比、决定信谁的。一次约 30 秒到 2 分钟；
查个时间、价格这种快问题模型仍然用 `web_search`。

配置：

1. 运行 bot 的地方要装 omp。Docker 镜像会自动安装（版本由构建参数 `OMP_VERSION` 固定，`--build-arg OMP_VERSION=` 可跳过）。
2. 给 omp 配好模型的接口和 key。Docker 部署时 `./omp` 挂载为 omp 的配置目录，新建 `./omp/agent/models.yml`。
   每个模型用它自己的原生协议：Claude 用 `anthropic-messages`，Gemini 用 `google-generative-ai`（地址写到 `/v1beta`），
   不要让 Gemini 走 Claude 格式的转换。例如：

   ```yaml
   providers:
     myclaude:
       baseUrl: https://your-claude-relay.example.com
       apiKey: LLM_API_KEY        # 写环境变量名：从 .env 里读取，key 不用写进这个文件
       api: anthropic-messages
       auth: apiKey
       models:
         - id: claude-opus-5-5
     mygemini:
       baseUrl: https://your-gemini-relay.example.com/v1beta
       apiKey: GEMINI_API_KEY
       api: google-generative-ai
       models:
         - id: gemini-3.8-flash-high
   ```

3. 设置 `RESEARCH_MODELS=myclaude/claude-opus-5-5,mygemini/gemini-3.8-flash-high`（「接口名/模型名」，可用 `omp models` 查看）。
   想要备用接口就再配一组 provider，写进 `RESEARCH_FALLBACK_MODELS`，按位置对应：某个研究员失败时换成对应的备选再跑一次。

安全：研究员只开放 omp 的 `web_search` 和 `read`，并由 hook（`tgrok/tools/research_hook.ts`）把 `read` 限制在公网 http(s) 网页，
群友用提示词注入也读不到本地文件、omp 自己的凭据和内网地址。研究员在空的临时目录里运行，超时或回复被取消时会被结束。
解析到内网 IP 的域名（DNS rebinding）拦不住。

调研期间，进度行下面每个研究员一行，实时写明它在做什么、第几步（「A claude-opus-5-5：读 reuters.com/…（第 5 步）」），
换用备用接口、交回报告或失败也会显示；不显示百分比，因为一个任务要几步事先谁也不知道。
服务器上没有本地模型服务时，在 `./omp/agent/config.yml` 里写 `disabledProviders: [ollama, llama.cpp, lm-studio]`，
omp 每次启动就不再去探测它们。

## 部署（长期运行）

Bot 使用**轮询模式**（主动向 Telegram 拉取消息），不需要公网 IP 和域名——只要机器能访问外网，
放在家里和 LM Studio 同一台电脑上跑也完全可以。

### Docker（推荐用于服务器）

```bash
cp .env.example .env   # 编辑填好配置（或运行 configure.py）
docker compose up -d --build
docker compose logs -f # 查看日志（同时写在 ./data/logs/bot.log，每天一个文件）
```

- `restart: unless-stopped` 保证崩溃或服务器重启后自动拉起
- 白名单、记忆数据库和日志都在 `./data`，容器重建不丢失
- 每个请求有一个编号（`#12`），它的每行日志都带上；结束时一行汇总：耗时、模型轮次、用了哪些工具、token、回复字数
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
- **多轮对话失忆**：追问用的对话历史存在 `MEMORY_DB` 里，保留 `CONVERSATION_KEEP_DAYS` 天，重启不丢；更早的回复仍可继续问，只是只带上 bot 那条回答作为上下文。历史里的图片恢复后显示为「[图片]」。
