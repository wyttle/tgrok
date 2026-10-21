# tgrok 架构设计文档

Telegram 群聊 AI 助手，提供类似 X 上 @grok 的引用提问体验。主模型支持 OpenAI 兼容 Chat Completions、OpenAI Responses API、Gemini 原生和 Claude 原生四种协议；bot 自带工具循环提供计算器、梗词典，以及可选的联网搜索和读网页。

## 模块划分

```text
bot.py                 入口（python bot.py）：配置控制台日志，然后运行 tgrok.app.main
configure.py           交互式配置向导和多配置档管理（独立运行，不进 Docker 镜像）
tests/                 pytest 用例（无需网络与真实 Telegram），conftest.py 固定环境变量并在每个测试后还原状态
tgrok/
|-- config.py          环境变量解析和常量；其它模块一律在用到时读 config.X
|-- i18n.py            全部界面和提示词文案，t() 与 phrases()
|-- jsonstore.py       JSON 文件读写：读取失败回退初始值，写入原子替换（白名单、记忆、梗词典共用）
|-- db.py              MEMORY_DB 的 SQLite 连接；各模块用 schema() 登记自己的表（记忆、对话历史、最近群聊、用量）
|-- logs.py            日志格式、按天滚动的日志文件、请求编号（contextvar）和请求内的累计（轮次、工具、token）
|-- usage.py           模型用量：每次调用一行（功能、模型、token、耗时、成败），/stats 按今天和近 7 天汇总
|-- tg_auth.py         白名单和管理员判定
|-- app.py             组装 python-telegram-bot 应用、注册处理函数和命令菜单、启动日志
|-- handlers.py        普通消息：要不要回、拼出对话、生成回复、记进缓存、最近群聊和记忆；静默旁听；媒体预读
|-- commands.py        斜杠命令；COMMANDS 是唯一的命令表，注册、权限检查和菜单都从它生成
|-- prompt.py          系统提示词，以及每条 user 消息的拼装（说话人、引用/转发的真正作者、记忆、梗、最近群聊、时间、图片）
|-- media.py           图片、贴纸、相册和视频语音：data URL、媒体说明（转述、media_id 或看不到）、相册缓存、媒体登记和预读
|-- viewer.py          让 GEMINI_MEDIA_MODEL 看/听媒体：watch_media 和后台预读共用，按 file_unique_id 缓存转述
|-- recent.py          每个群最近的聊天（存数据库，只留 RECENT_CHAT_MINUTES 分钟内的），提问时附上，每行写明谁说的、回复谁
|-- conversations.py   追问用的对话历史（内存层带图原样，数据库层纯文本、跨重启）和历史截断
|-- chat.py            一次回复的流程：模型轮次循环、重试、备用模型切换、工具轮、取消按钮
|-- bubble.py          一条回复在 Telegram 上的显示：占位、流式编辑、分段、进度、取消、报错、定稿
|-- render.py          模型输出 → 富文本 html：白名单标签和转义、配平、<p>/<br> 段落结构、长回复自动折叠、按段落拆分不切断标签
|-- rich.py            富文本消息的发送和编辑（sendRichMessage / editMessageText 的 rich_message，经 Bot.do_api_request），
|                      被拒绝时改发纯文本并按 4096 拆条
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
|-- autoreply.py       关键词直接回复（/kv）：整条消息正好是关键词就发存好的文字，不用 @、不调模型、不记忆
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
        （是不是提问者本人、转发的原作者、频道/匿名管理员、引用的片段，只有图也算），提问者的话单独标成【提问】
     -> 附图（提问者自己的在前，被引用消息的在后）都写明几张、是谁发的：图挂在 user 消息末尾，不写明模型会凭上下文猜
        prompt.user_turn 拼出 [记忆附注] [梗词典附注] [最近群聊] 正文 [当前时间]，有图时是多模态数组
     -> conversations.trim
  -> chat.stream_reply
     Bubble.open 后台发占位消息和取消按钮（与首轮请求并发），注册 active_generations
     -> chat._generate：最多 SEARCH_MAX_ROUNDS + 1 轮，最后一轮不挂工具
        -> chat._call_model：adapter.run_round，正文增量交给 Bubble.on_text
           零输出就出错：配了备用模型就整条换成备用模型（去掉厂商扩展字段；两边工具能力不同时
           连系统提示词的工具说明一起换），否则重试一次；429 不重试；已有正文则按完成处理
        -> 有工具调用：Bubble.take_draft 丢掉这轮草稿，tools.plan_round 排出进度行并并发执行
           -> calculate、remember_slang、memory、recall、watch_media、web_search（grounding 模式同轮合并）、open_url、send_image
           -> research：每个研究员交给一个常驻的 omp --mode rpc 进程（只开 web_search 和 read，加载 hook，空的临时目录），
              超过 RESEARCH_TIMEOUT 或回复被取消时整组结束；失败的研究员按 RESEARCH_FALLBACK_MODELS 换备选再跑一次（研究员之间同时跑，
              每个研究员的主→备先后跑）；超时不换备选；失败原因取 omp 事件流里
              接口报的错误（网关 HTML 错误页只取标题）；omp 的输出边跑边逐行读：每次开始搜索或读网页，
              研究员那一行的进度明细（ProgressLine.detail，经 tools.show_detail）就更新成「在做什么、第几步」；
              报告标上研究员和模型名交给主模型判断，全部失败时提示它自己查。什么时候查、用 research 还是 web_search
              只在 research 的说明里讲一次（问出处、辨真假、不认识的梗、严肃的事一律先用 research）；开了 research 时
              web_search 的说明只讲工具本身。「用自己的话讲、不用编号引用、链接放末尾」只写在回复格式说明里
        -> 工具结果追加到这次调用自己的 working history，进入下一轮
        -> Gemini 原生返回的引用附到正文末尾
     -> 轮次跑完还没有正文（空返回，或者最后一轮不挂工具时模型还想调工具）：chat._answer_now 追加一条
        「不能再调用工具，根据已有资料直接作答」再问一次，还没有就换备用模型再问一次
     -> Bubble.finish 定稿（富文本消息，细节过长自动折叠；被拒绝时改发纯文本）；终轮空手时回用最后一版草稿
     -> 还是没写出回答、或者出错时，这次工具已经查到了东西：提示「回复这条消息就接着答」，并通过 keep 回调
        把对话 + 工具结果（纯文本，最多 chat.FINDINGS_CHARS 字）按那条提示的消息 id 存进 conversations
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
- Claude 原生在 `LLM_EXTRA_BODY` 要了 thinking 时，流的第一个内容块不是思考块就断开重发（`claude.THINKING_ATTEMPTS` 次，最后一次不检查）：有的网关（cpa）不时丢掉 thinking，模型不思考就把说话人标签、提示词碎片或英文推理当正文吐出来，这时还没有字交给气泡。
- `calculate` 只解释白名单 AST 节点，没有名字查找和属性访问；整数位数、幂指数、阶乘和表达式长度都有上限。
- 网页正文提取运行在独立进程，避免 trafilatura 和 lxml 的 CPU 工作阻塞事件循环。
- 气泡更新单飞且在后台执行，编辑的网络错误只丢该次显示、不打断生成；上一次更新还在途时进度刷新直接跳过，不让模型轮次和工具等 Telegram（群里被限流时一等就是几十秒）。定稿、取消和报错前先等在途编辑落地。占位消息被限流时等一下重发一次；还是发不出去（例如 bot 被踢出群）才取消本次生成。
- 群聊里每条 user 消息、被引用消息的作者和记忆里的说话人都标成「名字（@用户名，id）」，同名的人靠 id 区分；频道和匿名管理员发的看 `sender_chat`，转发的看 `forward_origin`。系统提示词要求把话安在真正说的人头上。截断后的历史从 user 开始，满足严格交替的接口和本地模型模板。
- 最近群聊和媒体转述都挂在最新一条 user 消息上，前面的历史字节不变，prompt cache 照样命中。
- 只订阅 `message` 和 `callback_query` 更新：编辑旧消息不会触发重复回答。
- 进度显示用纯文本和缩进，不向群成员展示网址；`open_url` 只接受公网 HTTP(S) 地址。
- 等待反馈：出正文前进度定期刷新（私聊 2 秒、群里 4 秒），底部阶段行带从提问起的总计时，还在执行的工具行带自己的计时；同时每 4.5 秒发一次「正在输入」。所有显示更新（进度、正文编辑）在私聊至少隔 1 秒、群里至少隔 3 秒，避开 Telegram 的群消息限流；被跳过的状态由下个刷新周期补上。定稿编辑网络超时会重试一次，不会停在带光标的中间版本上。
- 对话缓存同时按条目数和近似内容字符量驱逐；字符量不是进程内存硬上限。

## 运行时状态归属

- `llm.adapter`、`llm.fallback_adapter`、`llm.helper_adapter`：主模型、备用模型和 Gemini 帮手模型（`GEMINI_HELPER_MODEL`，留空沿用 `GEMINI_MEDIA_MODEL`；记忆整理用）的适配器，各自记着自己的降级状态。`config.fallback_endpoint()` 里留空的协议、地址和 key 沿用主模型，额外参数不继承。
- `web._grounding_cooldown_until`：grounding 配额超限后的冷却截止时间。
- `tools.research._slots`：限制同时进行的调研任务数（`RESEARCH_CONCURRENCY`）的信号量，换事件循环时重建。
- `tools.research._idle`：常驻的 omp（`--mode rpc`），每个研究员模型最多一个空闲的。`app.post_init` 调 `warm_up` 先启动好；任务来了取走（还在启动就等它），用 `new_session` 开新会话接任务，读事件到 `prompt_result`；用完放回，接满 `RECYCLE_AFTER` 次换新进程。超时、出错或回复被取消时整组进程杀掉，再补一个新的预热。`RESEARCH_WARM=false` 时每个任务现启动、用完就关。`app.post_shutdown` 关掉全部。
- `chat.active_generations`：正在生成的任务，供取消按钮定位。
- `tools.send_image.outbox`：contextvar，`chat.stream_reply` 为每次回复放一个空列表，send_image 把图片攒进去；回复成功定稿后才发，被取消或失败时不发。发出去的图在返回的回复文本末尾记一句，追问时模型知道自己发过什么。
- `tg_auth.allowed_users`：运行时白名单，写入 `WHITELIST_FILE`。
- `conversations._cache`、`_sizes`、`_owners`、`_total`：追问用的对话历史（带图片原样）、字符量计数和提问者。同时写进 `MEMORY_DB` 的 `conversations` 表：去掉系统提示词、图片换成「[图片]」，保留 `CONVERSATION_KEEP_DAYS` 天；内存里没有时从表里取，系统提示词换成当前的。`/forget` 和 `/forgetme` 一起删。
- `media.album_cache`：相册聚合缓存（只在开启视觉时收集）。
- `media.watch_cache`：配了 `GEMINI_MEDIA_MODEL` 且主模型走工具循环时，bot 被叫到的那条消息和它引用的消息里的视频、语音等按（聊天, 消息 id）登记 file_id 和 bot，最多 300 条；`watch_media` 按 media_id 取出，有预读就等那一份，否则下载（官方 Bot API 上限 20MB）、整段内联发给 Gemini（`llm.gemini.gemini_client`，与 grounding 共用），转述回灌主模型。别的聊天的 media_id 取不到；重启后清空。有文字也有媒体时，文字和媒体说明都交给模型。
- `viewer._jobs`：媒体预读任务，按 file_unique_id 存最近 500 份，最多同时 4 个（`viewer._slots`，换事件循环时重建）。转述好了就直接写进媒体说明；`viewer._used` 记着哪些转述已经交给过主模型，之后的 `watch_media` 按具体问题重新看。
- `recent` 表（`MEMORY_DB`）：群消息和 bot 回复（bot 的回复每条最多 800 字），每次写入时删掉超过 `RECENT_CHAT_MINUTES` 的；注入时按 `RECENT_CHAT_MINUTES` 和 `RECENT_CHAT_COUNT` 截取，另外不管群里多热闹都带上 bot 自己最近 3 条回复，按时间排好；不含当前提问和被引用的那条。记着发消息的人（bot 的回复记提问者），`/forget`、`/forgetme` 一起删。
- `usage` 表（`MEMORY_DB`）：`chat._call_model`（回复，每轮一行，失败也记）、`tools.research`（每个研究员）、`web._search_gemini_grounded`（搜索）、`viewer._generate`（媒体）、`memory.consolidate`（记忆整理）各记一行，保留 90 天。token 来自各适配器流里的用量（`RoundResult.usage`；OpenAI 兼容接口请求 `stream_options.include_usage`，后端拒绝时去掉并记住）、Gemini 的 `usage_metadata`、omp 的事件流。
- `logs._current`：当前请求（`handlers.handle_message` 决定要回复时 `logs.begin()`），日志行带 `#编号`；子任务继承它。请求内的模型轮次、工具名和 token 累计在上面，结束时打一行汇总。日志同时写入 `LOG_DIR/bot.log`，每天零点换文件，留 `LOG_KEEP_DAYS` 天；httpx、Google SDK 的逐请求 INFO 压到 WARNING。
- `memory.store`：`MEMORY_DB` 里的三张表。`messages`（+ `messages_fts`）是原始记录，三个字以上的检索词走 trigram 全文索引，更短的退回 LIKE；`msg_id` 是 Telegram 消息 id，（聊天, msg_id）唯一，同一条消息既旁听到又被引用时只记一次；`notes` 是精选条目，`user_id` 为空是群笔记，否则是这个人在这个聊天的档案，可带到期时间；`progress` 记每个聊天整理到哪条记录。所有读写都很小，直接在事件循环线程里同步执行。
- 注入：`memory.memory_block` 只拼进一段对话的首条 user 消息：群笔记、提问者和被引用者的档案（`MEMORY_GROUP_CHARS` / `MEMORY_PROFILE_CHARS` 限制总量），加上 3 天内最近 5 条提问；每条都标距今多久，过期条目不显示。
- 写入：`memory` 工具和后台整理都走 `notes.apply`，超出字数上限时整批拒绝并把现有条目交还模型去合并或删除，不会悄悄淘汰。整理只执行模型提出的操作，没提到的条目原样保留。
- 后台维护：`app.post_init` 调用 `memory.start_maintenance`，先迁移旧的 `MEMORY_FILE`，再每分钟跑一次 `consolidate.tick`：每个聊天最多每 `MEMORY_CONSOLIDATE_INTERVAL` 分钟整理一次，攒够 `MEMORY_CONSOLIDATE_MIN` 条新记录（或最早一条放了一天）才整理；同时清掉过期条目和超过 `MEMORY_ARCHIVE_DAYS` 天的记录。整理先用帮手模型，失败再用主模型、备用模型。`consolidate._running` 防止同一聊天并发整理。
- 静默旁听：`MEMORY_OBSERVE_CHATS` 里的群，没叫到 bot 的消息只存进原始记录，不逐条请求模型，由后台整理处理。
- `slang._entries`：梗词典，按「作用域|词」存，每个词最多 3 种用法，新的在前；本群用法排在通用用法前。只把这条消息和被引用内容里出现了的词附上（最多 5 个，单字词只在 12 字以内的短句里算命中）。写入 `SLANG_FILE`，超过 `SLANG_MAX_ENTRIES` 淘汰最久没命中的。
- `autoreply` 表（`MEMORY_DB`）：关键词 → 回复，关键词按小写、合并空白匹配。`handlers.handle_message` 在记完最近群聊后、判断要不要回之前就查：整条消息（去掉 @bot）正好是关键词就把回复直接发到聊天里（不引用原消息）并返回，不管叫没叫到 bot、在不在白名单；不调模型，不进对话历史、长期记忆和 bot 的最近回复。管理员用 `/kv` 增删查。
- 找图（`send_image` 的 query）：配了 `GEMINI_MEDIA_MODEL` 时只发 Gemini 确认对题的候选；全部不对题、超时（`CHECK_TIMEOUT`）或回答看不懂就不发，告诉模型没找到。没配 Gemini 没法核对，才用排名第一的。随回复发出的图片消息 id 也记进对话历史，群友回复那张图也能接着聊。
- 回复格式：系统提示词附 `format_note`（空行分段、只用白名单 HTML 标签、短回复不加标签、长回复细节放进 `<blockquote expandable>`、链接放末尾）。富文本的 html 会把换行压成空格，所以 `render` 把空行分出的段落包成 `<p>`、段内换行变 `<br>`；可折叠引用只能装行内内容，里面的代码块改成行内代码；Telegram 自己的解析器很宽松（未闭合、未知标签都直接吞掉），白名单和转义由 `render` 负责。流式过程中每次编辑都是配平好的富文本，末尾挂光标；正文超过 `rich.RICH_CHARS` 时用 `render.cut` 在段落边界切开另起一条。对话历史留模型原文（带标签），最近群聊和原始记录存纯文本。

## 配置

全部配置通过环境变量读取，示例和逐项注释见 `.env.example`。OpenAI、Responses 和 Claude 使用 `LLM_BASE_URL`，Gemini 原生及 grounding 使用 `GEMINI_BASE_URL`。`SYSTEM_PROMPT` 中字面的 `\n` 当作换行。`configure.py` 能读跨行的引号值，写出时一律转成带 `\n` 转义的单行双引号值；向导每一步是一个 `_step_*` 函数，共享状态在 `Wizard` 里。多配置档保存在 `profiles/*.env`。

## 测试与部署

- `pip install -r requirements-dev.txt` 后运行 `python -m pytest`。用例按模块分文件：回复流程（test_reply）、工具（test_tools）、深入调研（test_research，用 tests/fake_omp.py 代替真 omp，覆盖多份报告、部分失败、超时和取消时结束进程）、适配器（test_adapters）、消息拼装（test_messages）、记忆（test_memory）、梗词典（test_slang）、显示格式（test_render）、联网（test_web）、配置和向导（test_config、test_configure）。
- 部署不属于测试。修改后先在本地跑通测试，再按项目部署方式重建服务。
