# tgrok 架构设计文档

Telegram 群聊 AI 助手，提供类似 X 上 @grok 的引用提问体验。主模型支持 OpenAI 兼容 Chat Completions、OpenAI Responses API、Gemini 原生和 Claude 原生四种协议；bot 自带工具循环提供计算器、梗词典，以及可选的联网搜索和读网页。

## 模块划分

```text
bot.py                 入口（python bot.py）：配置日志，然后运行 tgrok.app.main
configure.py           交互式配置向导和多配置档管理（独立运行，不进 Docker 镜像）
tests/                 pytest 用例（无需网络与真实 Telegram），conftest.py 固定环境变量并在每个测试后还原状态
tgrok/
|-- config.py          环境变量解析和常量；其它模块一律在用到时读 config.X
|-- i18n.py            全部界面和提示词文案，t() 与 phrases()
|-- jsonstore.py       JSON 文件读写：读取失败回退初始值，写入原子替换（白名单、记忆、梗词典共用）
|-- tg_auth.py         白名单和管理员判定
|-- app.py             组装 python-telegram-bot 应用、注册处理函数和命令菜单、启动日志
|-- handlers.py        普通消息：要不要回、拼出对话、生成回复、记进缓存、最近群聊和记忆；静默旁听；媒体预读
|-- commands.py        斜杠命令；COMMANDS 是唯一的命令表，注册、权限检查和菜单都从它生成
|-- prompt.py          系统提示词，以及每条 user 消息的拼装（说话人、引用/转发的真正作者、记忆、梗、最近群聊、时间、图片）
|-- media.py           图片、贴纸、相册和视频语音：data URL、媒体说明（转述、media_id 或看不到）、相册缓存、媒体登记和预读
|-- viewer.py          让 GEMINI_MEDIA_MODEL 看/听媒体：watch_media 和后台预读共用，按 file_unique_id 缓存转述
|-- recent.py          每个群最近的聊天（内存），提问时附上，每行写明谁说的、回复谁
|-- conversations.py   追问用的对话缓存和历史截断
|-- chat.py            一次回复的流程：模型轮次循环、重试、备用模型切换、工具轮、取消按钮
|-- bubble.py          一条回复在 Telegram 上的显示：占位、流式编辑、分段、进度、取消、报错、定稿
|-- render.py          Markdown 转 MarkdownV2、流式中间态、压成一段
|-- tools/             bot 自带工具：每个工具的 schema、执行、进度文案、结果摘要和提示词说明放在一起
|   |-- __init__.py    Tool 基类、REGISTRY、schemas()/prompt_notes()、plan_round() 进度与并发执行
|   |-- calculate.py   calculate
|   |-- remember_slang.py  remember_slang
|   |-- remember.py    memory：增改删本聊天的群笔记和个人档案
|   |-- recall.py      recall：按关键词、说话人、时间查本聊天的原始记录
|   |-- watch.py       watch_media：把视频、GIF、语音、音频（没开视觉时也包括图片）交给 GEMINI_MEDIA_MODEL 看/听
|   |-- send_image.py  send_image：按直链或关键词准备图片，攒进本次回复的 outbox，正文定稿后由 Bubble.send_images 发出
|   |-- web.py         web_search（grounding 模式下同轮合并）、open_url
|   |-- research.py    research：同一任务并发交给 RESEARCH_MODELS 里的每个 omp 研究员，报告一起交回
|   `-- research_hook.ts  研究员进程加载的 omp hook：read 只准读公网 http(s) 网页
|-- calc.py            calculate 的求值器：白名单 AST 的算术、比较与日期推算
|-- web.py             多搜索源聚合、Gemini grounding、网页抓取和 Jina 兜底；失败抛 WebError
|-- memory/          按聊天保存的长期记忆
|   |-- __init__.py    记问答和旁听、注入块、/forget /forgetme、后台维护、旧 memory.json 迁移
|   |-- store.py       SQLite：原始记录（FTS5 trigram 全文索引）、精选条目、整理进度
|   |-- notes.py       条目的增改删校验、字数上限、到期和渲染
|   `-- consolidate.py 后台整理：读新记录，让模型提出增改删逐条执行
|-- slang.py           梗词典：按词保存多种带语境的用法、按消息命中注入参考
`-- llm/
    |-- __init__.py    按配置装配主模型、备用模型和 Gemini 帮手模型的 adapter
    |-- base.py        RoundResult、BaseAdapter（共用的降级重试循环）、watch_idle 空闲看门狗、错误分类
    |-- openai.py      OpenAI 兼容 Chat Completions
    |-- responses.py   OpenAI Responses API：输入转换和事件流消费
    |-- gemini.py      Gemini 原生协议和 grounding 共用客户端
    `-- claude.py      Claude 原生协议：消息转换和流消费
```

依赖方向大致是 `config -> i18n/jsonstore -> llm/web/calc/slang/memory -> tools -> prompt/render/bubble -> chat -> handlers/commands -> app`。`llm` 不依赖工具定义：工具的 schema 由调用方作为参数传给 `run_round`。`web.py` 只在执行 Gemini grounding 时在函数体内导入 `llm.gemini`，其他协议不加载 Google SDK。

## LLM 适配器接口

```python
@dataclass
class RoundResult:
    calls: dict[int, dict] = field(default_factory=dict)  # 按序号聚合的工具调用 {id, name, arguments, extra}
    content: str = ""
    citations: list[dict] = field(default_factory=list)


class BaseAdapter:
    supports_tool_loop: bool = True
    bad_request: tuple[type[BaseException], ...] = ()   # SDK 的 400 异常

    async def run_round(self, history, tools, on_text) -> RoundResult: ...  # = _create_stream + _drain
    async def _open(self, history, opts: RequestOptions): ...               # 子类：按 opts 发起流式请求
    async def _drain(self, stream, on_text) -> RoundResult: ...             # 子类：消费流
    def _degrade(self, e, kind, opts) -> bool: ...                          # 子类补充协议特有的降级
```

`tools` 是 OpenAI function 格式的工具定义，`None` 表示这轮不挂工具；Claude 和 Responses 适配器各自转换成原生格式。`BaseAdapter._create_stream` 统一处理后端明确拒绝采样参数、`LLM_EXTRA_BODY` 或 tools 的 400：去掉那一项重试，并记在适配器实例上，本进程内不再发送。协议特有的降级在子类 `_degrade` 里补充：OpenAI 的 `max_tokens` → `max_completion_tokens`，Claude 按报错里的上限降低 `max_tokens`，Responses 不认 `store`/`include` 时改为不回传推理条目。错误分类优先读 SDK 的结构化字段，只在缺少时用受约束的文本匹配，避免把普通业务错误（例如工具调用配对错误）误判成后端不支持 tools。

四个协议的流消费都用 `base.watch_idle` 做空闲看门狗：已有正文、且没有聚合到一半的工具调用时，连续 `STREAM_IDLE_TIMEOUT` 秒无新数据就按完成收尾。它不是整次请求的超时。

## 四种主模型协议

| `LLM_PROTOCOL` | 接口与地址 | 工具行为 |
|---|---|---|
| `openai`（默认） | OpenAI 兼容 `/chat/completions`；`LLM_BASE_URL` | bot 工具循环 |
| `responses` | OpenAI Responses API `/responses`；`LLM_BASE_URL` | bot 工具循环；默认 `store=False` 并回传加密推理条目。GPT-6.1 Sol 等只在 Responses 上支持工具调用的模型需要用它 |
| `gemini` | google-genai 原生接口；`GEMINI_BASE_URL`，留空连接 Google 官方 | 单轮生成，Google 服务端执行 `google_search` 和 `url_context` |
| `claude` | Anthropic Messages API；`LLM_BASE_URL`，末尾的 `/v1` 会被剥掉 | bot 工具循环，保留原生 thinking 块和签名 |

旧开关 `CLAUDE_NATIVE` / `GEMINI_NATIVE_SEARCH` 不再兼容：`.env` 里只有它们、没有 `LLM_PROTOCOL` 时启动报错并提示改法；`configure.py` 读到它们会换算成 `LLM_PROTOCOL` 写回。

## 一条消息的生命周期

```text
Telegram update
  -> handlers.handle_message
     所有消息先进 recent（最近群聊）；视频语音交给 media.prefetch 在后台转述
     路由（私聊、@bot、回复 bot）、鉴权、相册收集；没叫到 bot 的消息交给静默旁听（转述好了再记）
     -> 追问：conversations.get 接上历史，附上那条回答之后的新群聊；过期时把 bot 那条回复并入本轮 user 消息
     -> 新对话：prompt.new_conversation（系统提示词 + 首条 user 消息）；被引用的消息标明真正的发送者
        （是不是提问者本人、转发的原作者、频道/匿名管理员、引用的片段），提问者的话单独标成【提问】
        prompt.user_turn 拼出 [记忆附注] [梗词典附注] [最近群聊] 正文 [当前时间]，有图时是多模态数组
     -> conversations.trim
  -> chat.stream_reply
     Bubble.open 后台发占位消息和取消按钮（与首轮请求并发），注册 active_generations
     -> chat._generate：最多 SEARCH_MAX_ROUNDS + 1 轮，最后一轮不挂工具
        -> chat._call_model：adapter.run_round，正文增量交给 Bubble.on_text
           首轮走 chat._race：主模型 HEDGE_AFTER 秒既没出字也没跑完，就同时让 llm.helper_adapter 作答，
           两路都先攒着，先出字（或先跑完）的一路放出来、另一路取消；帮手胜出时整条换成帮手，末尾注明
           零输出就出错：配了备用模型就整条换成备用模型（去掉厂商扩展字段；两边工具能力不同时
           连系统提示词的工具说明一起换），否则重试一次；429 不重试；已有正文则按完成处理
        -> 有工具调用：Bubble.take_draft 丢掉这轮草稿，tools.plan_round 排出进度行并并发执行
           -> calculate、remember_slang、memory、recall、watch_media、web_search（grounding 模式同轮合并）、open_url、send_image
           -> research：每个研究员起一个 omp -p 子进程（只开 web_search 和 read，加载 hook，空的临时目录），
              超时或回复被取消时整组结束；失败的研究员按 RESEARCH_FALLBACK_MODELS 换备选再跑一次；
              报告标上研究员和模型名交给主模型判断，全部失败时提示它自己查。系统提示词要求问出处、辨真假、
              不认识的梗一律先用 research（开了 research 时，查梗的说明也指向它而不是 web_search）
        -> 工具结果追加到这次调用自己的 working history，进入下一轮
        -> Gemini 原生返回的引用附到正文末尾
     -> Bubble.finish 定稿（MarkdownV2，失败回退纯文本）；终轮空手时回用最后一版草稿
  -> conversations.remember 按回复的每一段消息 id 保存追问用的历史；recent.add_bot 记下 bot 的回复
  -> memory.record_exchange 把这一轮问答原样存进原始记录：被引用的原话按原作者单独记（按消息 id 去重），
     提问只记「（回复了谁的消息）问了什么」；后台整理再从里面挑出值得记的条目
```

## 关键设计决策

- 实时时间追加到最新一条 user 消息，系统提示词和历史保持字节不变，上游 prompt cache 才能命中；记忆也只注入一段对话的首条 user 消息。
- 系统提示词由 `prompt.system_prompt(with_tools)` 生成：只有当前模型走 bot 工具循环时才附各工具的使用说明（来自 `Tool.prompt_note`），Gemini 原生不提这些工具。
- 工具成功与否由工具自己返回（`Outcome.ok`），进度结果行不靠文本形状判断。`web.py` 搜不到或读不到时抛 `WebError`，`str(e)` 就是给模型看的说明。
- SDK 内建重试关闭（`max_retries=0`），重试只由 chat 控制。
- OpenAI 兼容路径回传 Gemini 思考型模型的 `thought_signature`；Claude 原生收集 thinking 和签名，续传时恢复成原生块；Responses 把带 `encrypted_content` 的推理条目挂在首个工具调用上，续传时放回 input。
- `calculate` 只解释白名单 AST 节点，没有名字查找和属性访问；整数位数、幂指数、阶乘和表达式长度都有上限。
- 网页正文提取运行在独立进程，避免 trafilatura 和 lxml 的 CPU 工作阻塞事件循环。
- 气泡更新单飞且在后台执行，编辑的网络错误只丢该次显示、不打断生成；上一次更新还在途时进度刷新直接跳过，不让模型轮次和工具等 Telegram（群里被限流时一等就是几十秒）。定稿、取消和报错前先等在途编辑落地。占位消息被限流时等一下重发一次；还是发不出去（例如 bot 被踢出群）才取消本次生成。
- 抢答（`chat._race`）只在首轮：帮手模型拿不到这次工具调用的结果，工具之后的轮次仍由主模型完成。两路都失败时抛出主模型的错误，照常走重试和备用模型；用户取消时两路一起取消。
- 群聊里每条 user 消息、被引用消息的作者和记忆里的说话人都标成「名字（@用户名，id）」，同名的人靠 id 区分；频道和匿名管理员发的看 `sender_chat`，转发的看 `forward_origin`。系统提示词要求把话安在真正说的人头上。截断后的历史从 user 开始，满足严格交替的接口和本地模型模板。
- 最近群聊和媒体转述都挂在最新一条 user 消息上，前面的历史字节不变，prompt cache 照样命中。
- 只订阅 `message` 和 `callback_query` 更新：编辑旧消息不会触发重复回答。
- 进度显示用纯文本和缩进，不向群成员展示网址；`open_url` 只接受公网 HTTP(S) 地址。
- 等待反馈：出正文前进度定期刷新（私聊 2 秒、群里 4 秒），底部阶段行带从提问起的总计时，还在执行的工具行带自己的计时；同时每 4.5 秒发一次「正在输入」。所有显示更新（进度、正文编辑）在私聊至少隔 1 秒、群里至少隔 3 秒，避开 Telegram 的群消息限流；被跳过的状态由下个刷新周期补上。定稿编辑网络超时会重试一次，不会停在带光标的中间版本上。
- 对话缓存同时按条目数和近似内容字符量驱逐；字符量不是进程内存硬上限。

## 运行时状态归属

- `llm.adapter`、`llm.fallback_adapter`、`llm.helper_adapter`：主模型、备用模型和 Gemini 帮手模型（`GEMINI_HELPER_MODEL`，留空沿用 `GEMINI_MEDIA_MODEL`；抢答和记忆整理用）的适配器，各自记着自己的降级状态。`config.fallback_endpoint()` 里留空的协议、地址和 key 沿用主模型，额外参数不继承。
- `web._grounding_cooldown_until`：grounding 配额超限后的冷却截止时间。
- `tools.research._slots`：限制同时进行的调研任务数（`RESEARCH_CONCURRENCY`）的信号量，换事件循环时重建。
- `chat.active_generations`：正在生成的任务，供取消按钮定位。
- `tools.send_image.outbox`：contextvar，`chat.stream_reply` 为每次回复放一个空列表，send_image 把图片攒进去；回复成功定稿后才发，被取消或失败时不发。发出去的图在返回的回复文本末尾记一句，追问时模型知道自己发过什么。
- `tg_auth.allowed_users`：运行时白名单，写入 `WHITELIST_FILE`。
- `conversations._cache`、`_sizes`、`_total`：追问用的对话历史和字符量计数。
- `media.album_cache`：相册聚合缓存（只在开启视觉时收集）。
- `media.watch_cache`：配了 `GEMINI_MEDIA_MODEL` 且主模型走工具循环时，bot 被叫到的那条消息和它引用的消息里的视频、语音等按（聊天, 消息 id）登记 file_id 和 bot，最多 300 条；`watch_media` 按 media_id 取出，有预读就等那一份，否则下载（官方 Bot API 上限 20MB）、整段内联发给 Gemini（`llm.gemini.gemini_client`，与 grounding 共用），转述回灌主模型。别的聊天的 media_id 取不到；重启后清空。有文字也有媒体时，文字和媒体说明都交给模型。
- `viewer._jobs`：媒体预读任务，按 file_unique_id 存最近 500 份，最多同时 4 个（`viewer._slots`，换事件循环时重建）。转述好了就直接写进媒体说明；`viewer._used` 记着哪些转述已经交给过主模型，之后的 `watch_media` 按具体问题重新看。
- `recent._chats`：每个群最近 80 条消息（最多 500 个群），只在内存里；注入时按 `RECENT_CHAT_MINUTES` 和 `RECENT_CHAT_COUNT` 截取，不含当前提问和被引用的那条。
- `memory.store`：`MEMORY_DB` 里的三张表。`messages`（+ `messages_fts`）是原始记录，三个字以上的检索词走 trigram 全文索引，更短的退回 LIKE；`msg_id` 是 Telegram 消息 id，（聊天, msg_id）唯一，同一条消息既旁听到又被引用时只记一次；`notes` 是精选条目，`user_id` 为空是群笔记，否则是这个人在这个聊天的档案，可带到期时间；`progress` 记每个聊天整理到哪条记录。所有读写都很小，直接在事件循环线程里同步执行。
- 注入：`memory.memory_block` 只拼进一段对话的首条 user 消息：群笔记、提问者和被引用者的档案（`MEMORY_GROUP_CHARS` / `MEMORY_PROFILE_CHARS` 限制总量），加上 3 天内最近 5 条提问；每条都标距今多久，过期条目不显示。
- 写入：`memory` 工具和后台整理都走 `notes.apply`，超出字数上限时整批拒绝并把现有条目交还模型去合并或删除，不会悄悄淘汰。整理只执行模型提出的操作，没提到的条目原样保留。
- 后台维护：`app.post_init` 调用 `memory.start_maintenance`，先迁移旧的 `MEMORY_FILE`，再每分钟跑一次 `consolidate.tick`：每个聊天最多每 `MEMORY_CONSOLIDATE_INTERVAL` 分钟整理一次，攒够 `MEMORY_CONSOLIDATE_MIN` 条新记录（或最早一条放了一天）才整理；同时清掉过期条目和超过 `MEMORY_ARCHIVE_DAYS` 天的记录。整理先用帮手模型，失败再用主模型、备用模型。`consolidate._running` 防止同一聊天并发整理。
- 静默旁听：`MEMORY_OBSERVE_CHATS` 里的群，没叫到 bot 的消息只存进原始记录，不逐条请求模型，由后台整理处理。
- `slang._entries`：梗词典，按「作用域|词」存，每个词最多 3 种用法，新的在前；本群用法排在通用用法前。只把这条消息和被引用内容里出现了的词附上（最多 5 个，单字词只在 12 字以内的短句里算命中）。写入 `SLANG_FILE`，超过 `SLANG_MAX_ENTRIES` 淘汰最久没命中的。
- 只回一段：`REPLY_SINGLE_PARAGRAPH` 开启时系统提示词追加要求，`render.shape` 在每次显示前把正文压成一段（代码块保留），存进对话历史的也是这一段。

## 配置

全部配置通过环境变量读取，示例和逐项注释见 `.env.example`。OpenAI、Responses 和 Claude 使用 `LLM_BASE_URL`，Gemini 原生及 grounding 使用 `GEMINI_BASE_URL`。`SYSTEM_PROMPT` 中字面的 `\n` 当作换行。`configure.py` 能读跨行的引号值，写出时一律转成带 `\n` 转义的单行双引号值；向导每一步是一个 `_step_*` 函数，共享状态在 `Wizard` 里。多配置档保存在 `profiles/*.env`。

## 测试与部署

- `pip install -r requirements-dev.txt` 后运行 `python -m pytest`。用例按模块分文件：回复流程（test_reply）、工具（test_tools）、深入调研（test_research，用 tests/fake_omp.py 代替真 omp，覆盖多份报告、部分失败、超时和取消时结束进程）、适配器（test_adapters）、消息拼装（test_messages）、记忆（test_memory）、梗词典（test_slang）、显示格式（test_render）、联网（test_web）、配置和向导（test_config、test_configure）。
- 部署不属于测试。修改后先在本地跑通测试，再按项目部署方式重建服务。
