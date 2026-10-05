# tgrok 架构设计文档

Telegram 群聊 AI 助手，提供类似 X 上 @grok 的引用提问体验。主模型支持 OpenAI 兼容 Chat Completions、OpenAI Responses API、Gemini 原生和 Claude 原生四种协议；工具循环提供 `calculate` 计算器，并可选 bot 工具搜索或 Google grounding 搜索。

## 模块划分

```text
bot.py                 入口（python bot.py），只做 from tgrok.tg import main
configure.py           交互式配置向导和多配置档管理（独立运行，不进 Docker 镜像）
tests/smoke_test.py    36 项冒烟和回归测试（无需网络与真实 Telegram）
tgrok/
|-- config.py          环境变量解析、常量、日志与时区初始化
|-- i18n.py            全部界面和提示词文案，以及 t()
|-- prompt.py          SYSTEM_PROMPT 组装和实时时间注入 with_time()
|-- calc.py            calculate 工具：白名单 AST 求值的算术、比较与日期推算
|-- llm/
|   |-- __init__.py    按 LLM_PROTOCOL 装配唯一 adapter 单例并导出共享 helper
|   |-- base.py        RoundResult、BaseAdapter、工具定义与 active_tools()、错误分类和共享 helper
|   |-- openai.py      OpenAI 兼容 Chat Completions 适配器和流消费
|   |-- responses.py   OpenAI Responses API 适配器、输入转换和事件流消费
|   |-- gemini.py      Gemini 原生协议适配器、原生客户端和 grounding 共用客户端
|   `-- claude.py      Claude 原生协议适配器、消息转换和流消费
|-- web.py             多搜索源聚合、Gemini grounding、网页抓取和 Jina 兜底
|-- chat.py            进度显示、工具执行、统一的 stream_reply 轮次循环和取消回调
|-- tg_auth.py         白名单和管理员判定
`-- tg.py              Telegram 路由、图片和相册、管理命令、对话缓存及 main()
```

主要依赖方向为 `config -> i18n/prompt/llm/web -> chat -> tg`。`web.py` 仅在执行 Gemini grounding 时在函数体内延迟导入 `llm.gemini`，避免其他协议无条件加载 Google SDK。

## LLM 适配器接口

`tgrok.llm` 根据 `LLM_PROTOCOL` 创建一个进程级 `adapter`。四个协议实现相同的最小接口：

```python
@dataclass
class RoundResult:
    calls: dict[int, dict] = field(default_factory=dict)
    content: str = ""
    citations: list[dict] = field(default_factory=list)


class BaseAdapter:
    name: str = ""
    supports_tool_loop: bool = True

    async def run_round(self, history, use_tools, on_text) -> RoundResult:
        raise NotImplementedError
```

一轮调用同时完成建流和消费流。`RoundResult.calls` 保存聚合后的工具调用，`content` 保存本轮正文，`citations` 保存 Gemini 原生 grounding 引用。OpenAI、Responses 和 Claude 适配器支持 bot 自带工具循环，挂载的工具由 `base.active_tools()` 决定：`calculate` 总是挂载，`web_search` 和 `open_url` 只在配置了搜索源时挂载。Gemini 原生适配器将 `supports_tool_loop` 设为 `False`，搜索与读页由 Google 服务端完成。

后端能力降级状态属于适配器实例，例如 tools、采样参数和 `LLM_EXTRA_BODY` 是否被接受，以及协议特有的 token 参数探测结果。错误分类优先读取 SDK 的结构化错误字段，只在缺少结构化信息时使用受约束的文本回退，避免把普通业务错误误判为后端不支持 tools。

## 四种主模型协议

`LLM_PROTOCOL` 是主协议选择键：

| 值 | 接口与地址 | 工具行为 |
|---|---|---|
| `openai` | OpenAI 兼容 `/chat/completions`；使用 `LLM_BASE_URL` | 支持 bot 工具循环 |
| `responses` | OpenAI Responses API `/responses`；使用 `LLM_BASE_URL` | 支持 bot 工具循环；默认 `store=False` 并回传加密推理条目，后端不认时降级为不回传。GPT-6.1 Sol 等只在 Responses 上支持工具调用的模型需要用它 |
| `gemini` | google-genai 原生接口；使用 `GEMINI_BASE_URL`，留空连接 Google 官方地址 | 单轮生成，Google 服务端执行 `google_search` 和 `url_context` |
| `claude` | Anthropic Messages API；使用 `LLM_BASE_URL`，末尾 `/v1` 会在客户端根地址处理时剥离 | 支持 bot 工具循环，并保留原生 thinking 块和签名 |

`LLM_PROTOCOL` 留空时默认为 `openai`。`GEMINI_NATIVE_SEARCH` 和 `CLAUDE_NATIVE` 仅用于读取旧配置；新配置应只写 `LLM_PROTOCOL`。代码会把最终协议结果映射到这两个兼容属性，供尚未迁移的显示逻辑使用。

`LLM_TEMPERATURE` 和 `LLM_TOP_P` 留空时不发送，由后端使用默认值。`LLM_EXTRA_BODY` 接受 JSON 对象，OpenAI 兼容、Responses 和 Claude 原生适配器都会将其作为 `extra_body` 透传；Gemini 原生不使用该配置。后端明确拒绝可选采样参数或额外字段时，对应适配器会去掉参数重试，并在当前进程内粘性禁用。

## 一条消息的生命周期

```text
Telegram update
  -> tg.handle_message
     路由、鉴权、相册收集，组装 system、引用上下文、图片和实时时间
  -> chat.stream_reply
     后台发送占位消息和取消按钮（与首轮 LLM 请求并发），注册 active_generations
     -> 取得 llm.adapter
     -> 进入统一轮次循环
        -> 每轮调用 adapter.run_round
        -> 无输出的流中断重试一次；已有正文则按完成处理；429 不重试
        -> 若返回工具调用且本轮允许 tools，执行 chat._execute_tool_calls
           -> calculate 调用 calc.run_calculate
           -> web_search 调用 web.run_web_search
              -> 可选 Gemini grounding
              -> 或多搜索源并发聚合、去重和交错合并
           -> open_url 调用 web.run_fetch_url
              -> httpx 抓取、独立进程提取正文、必要时 Jina 兜底
        -> 工具结果追加到 working history 后进入下一轮
        -> Gemini 原生返回的 citations 在循环结束后附到正文
     -> 正文按节流间隔在后台流式编辑（中间态同样渲染 MarkdownV2），超过分段阈值时发送下一条消息
  -> 等在途编辑落地后 MarkdownV2 定稿，失败时回退纯文本
  -> tg.remember 按回复的每一段消息 id 保存追问所需的对话历史
```

OpenAI、Responses 和 Claude 最多运行 `SEARCH_MAX_ROUNDS + 1` 轮（搜索和计算共用轮数），最后一轮强制不带 tools，避免无限工具调用。Gemini 原生仍走同一段 chat 循环，但固定为一轮且不启用 bot 工具。

## 关键设计决策

- 实时时间追加到最新一条用户消息，而不是系统提示或历史消息。这样多轮间的 system prompt 与历史字节保持不变，有利于上游 prompt cache 命中。
- SDK 内建重试被禁用，OpenAI 与 Anthropic 客户端均使用 `max_retries=0`。重试语义只由 chat 的统一循环控制。
- 流消费使用空闲看门狗：仅在已有正文且没有半截工具调用时，连续 `STREAM_IDLE_TIMEOUT` 秒无新数据才按完成收尾。它不是整次请求的人为超时。
- OpenAI 兼容路径会回传 Gemini 思考型模型 tool call 中的 `thought_signature`。Claude 原生路径会收集 thinking 和签名，并在工具续传时恢复为 Anthropic 消息块。Responses 路径把带 `encrypted_content` 的推理条目挂在首个工具调用上，续传时放回 input；function_call 条目不带条目 id，不依赖服务端存储。
- `calculate` 只解释白名单 AST 节点（数字、四则与幂、比较、少量数学和日期函数），没有名字查找和属性访问；整数位数、幂指数、阶乘和表达式长度都有上限，越界返回失败文案而不是执行。
- 网页正文提取运行在独立进程，避免 trafilatura 和 lxml 的 CPU 工作阻塞事件循环。
- 气泡更新单飞且在后台执行：占位、中间编辑和进度渲染同一时刻最多一个在途请求，流消费不等待 Telegram 往返或限流退避，编辑的网络错误只丢该次显示、不打断生成。定稿、取消和报错前先等在途编辑落地，避免旧编辑覆盖新内容。占位消息发送失败（如 bot 被踢出群）会取消本次生成。
- 相册多图并发下载。
- 回复 bot 的任一段消息（带不带 @ 都算）即为追问，接上缓存的历史；群聊里每条 user 消息带说话人名字，多人接力追问时模型分得清谁在问。历史过期时把 bot 那条回复并入本轮 user 消息，不伪造 assistant 轮；`trim_history` 截断后从 user 开始，满足严格交替的接口和本地模型对话模板。
- 只订阅 `message` 和 `callback_query` 更新：编辑旧消息不会触发重复回答。
- grounding 被视为调研代理。同轮多个 `web_search` 会合并为一个综合任务，减少重复调用。
- 进度显示使用纯文本和缩进，不向群成员展示 URL。
- `open_url` 只接受公网 HTTP(S) 地址，拒绝内网和回环 IP 字面量。
- 对话缓存同时按条目数和近似内容字符量驱逐。字符预算统计文本、图片 data URL 等 content 长度，用于避免视觉历史无界增长；它不是进程内存硬上限，Python 对象开销、字符编码和共享消息对象都会使实际内存与统计值不同。

## 运行时状态归属

- `llm.adapter`：当前协议适配器及其粘性能力降级状态。
- `web._grounding_cooldown_until`：Gemini grounding 遇到配额错误后的冷却状态。
- `chat.active_generations`：正在生成的任务，供取消按钮定位。
- `tg_auth.allowed_users`：运行时白名单。
- `tg.conversations`、`tg._conv_sizes`、`tg._conv_total`：对话历史及近似内容字符预算会计。
- `tg.album_cache`：相册聚合缓存。

## 配置

全部配置通过环境变量读取，示例和逐项注释见 `.env.example`。协议地址必须区分：OpenAI、Responses 和 Claude 使用 `LLM_BASE_URL`，Gemini 原生及 Gemini grounding 使用 `GEMINI_BASE_URL`。`SYSTEM_PROMPT` 中字面的 `\n` 会被当作换行，方便写成单行；`configure.py` 能读取跨行的引号值，写出时一律转成带 `\n` 转义的单行双引号值。多配置档保存在 `profiles/*.env`，由 `configure.py` 新建、切换和删除。

## 测试与部署

- `python tests/smoke_test.py`：36 项行为级断言，覆盖流重试、空闲看门狗、取消、分段（含全部分段 id）、工具调用、四协议适配器、错误降级、引用、相册、SSRF、对话缓存预算、编辑网络错误不截断正文、历史截断从 user 开始、配置向导多行值读写和额外参数解析、calculate 计算与边界，以及 Responses 转换、降级和工具循环。
- 部署流程不属于测试的一部分。修改后应先在本地通过测试，再按项目部署方式重建服务。
