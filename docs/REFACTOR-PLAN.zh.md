# tgrok 重构设计文档（协议适配器化）

本文档是重构的完整设计与执行指令。执行者按阶段顺序操作，每个阶段结束必须满足该阶段的
验收标准后再进入下一阶段。设计决策已经定死，执行时不要自行发明新的抽象或改变方案；
发现设计与现实代码冲突、无法按文档执行时，停下来说明冲突点，不要自作主张绕过。

## 0. 背景与目标

现状问题（按优先级）：

1. `tgrok/llm.py`（552 行）混装三种协议（OpenAI 兼容 / Gemini 原生 / Claude 原生）的
   建流、流消费、历史格式转换，靠 `if config.CLAUDE_NATIVE` 在函数内部分发；
   `tgrok/chat.py` 的 `stream_reply` 里 Gemini 原生又是独立分支，把「无输出重试一次、
   正文到手掐流按完成、429 不重试」的循环复制了一份。
2. 粘性降级标志（`tools_supported` / `sampling_supported` / `extra_body_supported` /
   `claude_max_tokens`）是 llm 模块级可变全局，三条协议路径共用，语义含混，测试要手工还原。
3. `config.py` 里 `_gemini_search_blocked_until = [0.0]` 用单元素 list 冒充可变全局，
   且这是 web 层的运行时状态，不属于配置模块。
4. 降级判定靠报错文案子串匹配（`"tool" in err` 等 5 处），OpenAI/Anthropic 的 400 都有
   结构化字段可用时应优先用。
5. `prompt.py` 直接 `os.getenv("SYSTEM_PROMPT")`，绕过了 config 集中读取。
6. `tg.py` 的 `conversations` 对话缓存按条数（500）驱逐，视觉模式下历史含 base64 图片，
   字节数不受控。
7. 文档（`docs/ARCHITECTURE.zh.md`、`README.md`、`README.zh-CN.md`）没有反映
   三协议 / `LLM_PROTOCOL` / 采样参数 / `LLM_EXTRA_BODY`。

目标：llm 拆成协议适配器包，chat 只留一个轮次循环；可变状态收进适配器实例；
错误判定结构化优先；配置读取归位；对话缓存加字节预算；文档同步。

## 1. 硬性约束（任何阶段都不可破坏）

这些是踩过坑换来的行为约定，重构是「搬家不改行为」，以下每条都有回归测试或明确原因：

- **提示词缓存字节不变**：system prompt 与历史消息在多轮间保持字节一致；实时时间只由
  `prompt.with_time` 追加到最新一条用户消息。重构不得改动任何消息构造逻辑。
- **不加人为 LLM 超时**：唯一的超时是空闲看门狗（`STREAM_IDLE_TIMEOUT`，已有正文且无
  半个 tool_call 时才生效）。不要引入 `asyncio.wait_for` 包整个请求之类的东西。
- **SDK 内建重试必须禁用**：OpenAI 与 Anthropic 客户端都 `max_retries=0`。
  重试语义由 chat 层统一：本轮无输出的流中断重试一次；正文已到手的中断按完成处理；
  429/配额错误绝不重试并显示配额文案。
- **PTB 并发**：`Application.builder().concurrent_updates(True)` 保留（取消按钮依赖）。
- **进度显示**：纯文本、只靠缩进分层、无任何图标字符；思考阶段行原地替换；
  不向群成员暴露 URL。所有用户可见文案走 `i18n.t`。
- **Gemini 思考签名回传**（OpenAI 兼容路径）与 **Claude 思考块签名回传**（原生路径）
  的机制原样保留，丢了会 400。
- **注释风格**：中文、解释「为什么」而不是「做了什么」；不写「本次修改」类的评审注释。
- **禁止脚本化批量改写代码**：逐文件手改。
- **每个阶段结束运行 `python tests/smoke_test.py`，22 项全过才能提交。**
  测试本身允许按第 6 节的对照表适配，但断言语义不得删弱。
- 不部署、不改服务器配置；只保证本地测试通过并逐阶段 git commit。
- `configure.py` 本次不动（除非阶段 4 的 `SYSTEM_PROMPT` 迁移需要，实际不需要——它只写 .env）。

## 2. 现状地图（耦合点清单）

`tgrok/llm.py` 当前对外符号及使用方：

| 符号 | 使用方 |
|---|---|
| `create_stream(history, use_tools)` | chat.stream_reply；测试 1-7 整体替换、16/17 直调 |
| `_drain_stream(stream, on_text)` | chat.stream_reply；测试 8 直调 |
| `_assistant_tool_call_msg(calls, content)` | chat.stream_reply；测试 8/19 |
| `_tool_args(call)` | chat（3 处）；llm 内部 |
| `_is_quota_error(e)` | chat（2 处）、web（1 处） |
| `gemini_create_stream` / `_drain_gemini_stream` | chat 的 Gemini 原生分支 |
| `gemini_client` / `gtypes` | web 的 grounding 检索 |
| `llm`（OpenAI 客户端实例） | 测试 13（max_retries==0）、16/17（替换注入假客户端） |
| `claude_client` | 测试 20（替换注入） |
| `_to_anthropic_messages` / `_drain_claude_stream` / `_claude_create_stream` | 测试 18/19/20 |
| `tools_supported` / `sampling_supported` / `extra_body_supported` / `claude_max_tokens` | llm 内部；测试 16/17/20 断言与还原 |
| `SEARCH_TOOLS` / `WEB_SEARCH_TOOL` / `FETCH_URL_TOOL` | llm 内部 |
| `MAX_TOKENS`（模块级 from config import） | 测试 20 猴补丁 |

`chat.py` 的 `stream_reply` 结构：Gemini 原生独立分支（约 327-360 行）＋
OpenAI/Claude 共用轮次循环（约 360-410 行），两处各有一份相同语义的
两次尝试（attempt）循环。

## 3. 目标架构

### 3.1 包结构

`tgrok/llm.py` 变为包 `tgrok/llm/`：

```
tgrok/llm/
  __init__.py    # 按 config.LLM_PROTOCOL 装配唯一的 adapter 单例；re-export 共享 helpers
  base.py        # RoundResult、BaseAdapter、工具定义、共享 helpers、错误解析
  openai.py      # OpenAI 兼容协议适配器（原 create_stream/_drain_stream 及其降级链）
  gemini.py      # Gemini 原生适配器 + genai 客户端（grounding 也用它）
  claude.py      # Claude 原生适配器（原 _claude_* 三件套）
```

### 3.2 接口定义（写入 base.py，签名照抄）

```python
from dataclasses import dataclass, field

@dataclass
class RoundResult:
    """一轮生成的产物。calls 槽位格式与现有 _drain_stream 完全一致：
    {index: {"id": str, "name": str, "arguments": str, "extra": dict | None}}"""
    calls: dict[int, dict] = field(default_factory=dict)
    content: str = ""
    citations: list[dict] = field(default_factory=list)  # [{"uri","title"}]，仅 Gemini 原生非空


class BaseAdapter:
    """协议适配器：一轮 = 建流 + 消费流。粘性降级状态放实例属性上。"""
    name: str = ""
    # False 表示该协议的搜索在服务端完成，bot 自带工具循环不适用（Gemini 原生）
    supports_tool_loop: bool = True

    async def run_round(self, history: list[dict], use_tools: bool, on_text) -> RoundResult:
        raise NotImplementedError
```

共享内容全部放 base.py（从现 llm.py 原样搬入，仅去掉下划线前缀改为公开名）：

- `WEB_SEARCH_TOOL` / `FETCH_URL_TOOL` / `SEARCH_TOOLS`（含 grounding 模式的描述切换逻辑）
- `tool_args(call)`（原 `_tool_args`）
- `assistant_tool_call_msg(calls, content)`（原 `_assistant_tool_call_msg`）
- `is_quota_error(e)`（原 `_is_quota_error`）
- `sampling_kwargs()`（原 `_sampling_kwargs`）
- 新增 `error_text(e)`——见 3.6

### 3.3 openai.py

```python
class OpenAIAdapter(BaseAdapter):
    name = "openai"

    def __init__(self, client=None):
        # client 可注入：测试用假对象替换，缺省按 config 构造真实 AsyncOpenAI。
        # 用 is not None 判空而非 or：假客户端可能自定义真值
        self.client = client if client is not None else AsyncOpenAI(..., max_retries=0)
        self.tools_supported = True
        self.sampling_supported = True
        self.extra_body_supported = True
        self.token_param = "max_tokens"   # 改进：探测到 max_completion_tokens 后粘性记住

    async def run_round(self, history, use_tools, on_text) -> RoundResult:
        stream = await self._create_stream(history, use_tools)
        calls, content = await drain_stream(stream, on_text)
        return RoundResult(calls=calls, content=content)
```

- `_create_stream`：原 `create_stream` 的 OpenAI 部分，`global` 声明全部删除，
  改读写 `self.*`。降级链顺序不变：max_completion_tokens → 采样参数 → extra_body → tools
  （thought_signature 例外保留）。`token_param` 升级为实例属性后跨请求粘住
  （现状每次请求都从 "max_tokens" 重新探测，多付一次 400，这是本重构唯一允许的行为改良）。
- `drain_stream(stream, on_text)`：原 `_drain_stream` 去掉开头的 CLAUDE_NATIVE 分发后
  原样搬入，保持为**模块级函数**（测试 8 直接调它喂假流）。看门狗语义一字不动。

### 3.4 claude.py

```python
class ClaudeAdapter(BaseAdapter):
    name = "claude"

    def __init__(self, client=None):
        self.client = client if client is not None else anthropic.AsyncAnthropic(..., max_retries=0)
        self.tools_supported = True
        self.sampling_supported = True
        self.extra_body_supported = True
        self.max_tokens_limit: int | None = None   # 原全局 claude_max_tokens
```

- `_create_stream`：原 `_claude_create_stream`，全局改实例属性。
- `drain_stream(stream, on_text)`：原 `_drain_claude_stream`，模块级函数（测试直调）。
  思考块收集、挂到首个调用 `extra["anthropic_thinking"]` 的机制原样保留。
- `to_anthropic_messages(history)`、`anthropic_tools()`：原 `_to_anthropic_messages` /
  `_anthropic_tools`，模块级函数（测试直调）。

### 3.5 gemini.py

- **地址语义裁定**：Gemini 原生协议使用 `GEMINI_BASE_URL`，不用 `LLM_BASE_URL`。
  理由：原生端点与 grounding 共用同一个 genai 客户端与同一地址；中转站的 Gemini
  原生地址（根地址）与 OpenAI 兼容地址（带 /v1）不同；configure.py 向导与既有
  profiles 都已按 `GEMINI_BASE_URL` 写档。据此，阶段 1 必须**修正 config.py 中
  LLM_PROTOCOL 注释里「接口地址统一用 LLM_BASE_URL」的错误表述**为：
  openai/claude 用 `LLM_BASE_URL`；gemini 用 `GEMINI_BASE_URL`（留空 = Google 官方）。
- genai 客户端与 `gtypes` 从现 llm.py 顶部搬入，创建条件不变
  （`GEMINI_NATIVE_SEARCH or GEMINI_SEARCH_MODEL` 时才 import google.genai 并建客户端）。
  grounding（web.py）与原生适配器共用这里的 `gemini_client` / `gtypes`。
- `GeminiAdapter`：`supports_tool_loop = False`；`run_round` = 原
  `gemini_create_stream` + `_drain_gemini_stream`，返回
  `RoundResult(calls={}, content=..., citations=...)`。
- `to_gemini_contents(history)`（原 `_to_gemini_contents`）留在本模块。
- `drain_stream(stream, on_text)` 保持模块级函数（只用 getattr 访问 chunk，
  不依赖 genai 类型，可用假对象直测）。

### 3.6 结构化错误分类（base.py）

替换所有「对 `str(e).lower()` 做子串匹配」的降级判定。核心问题：普通业务 400
（例如 Anthropic 的 "unexpected `tool_use_id` found in `tool_result` blocks"，多由我们
自己的消息构造 bug 引起）也可能含 "tool" 字样，绝不能触发粘性禁用搜索。因此判定
分两层：结构化 param 精确匹配优先；纯文本回退必须同时命中「拒绝措辞」关键词。

```python
def error_text(e) -> str:
    """从 BadRequestError 提取判定用文本（小写）。优先结构化 body：
    OpenAI 形如 {"error": {"message":..., "param":..., "type":...}}，
    Anthropic 形如 {"type":"error","error":{"type":...,"message":...}}。
    拿不到结构化 body 时回退 str(e)。"""
    ...

def error_param(e) -> str:
    """结构化 body 里的 error.param（OpenAI 有，Anthropic 无），小写；没有返回 ""。"""
    ...

# 拒绝措辞：后端明确表示「参数不被支持/不认识」时才允许粘性降级。
# 注意不要加 "unexpected"——Anthropic 的 tool_use/tool_result 配对错误就含它，属业务 400
_REJECT_HINTS = ("unsupported", "not supported", "does not support", "unrecognized",
                 "unknown parameter", "no such parameter", "invalid parameter",
                 "not allowed", "not available")


def rejected_param(e, extra_keys=()) -> str | None:
    """把 400 分类为被拒的参数类别，供适配器降级链使用。返回：
    "token_param"      - 需改用 max_completion_tokens（文本含该词即判定，无需关键词把关）
    "sampling"         - temperature/top_p 被拒
    "extra"            - LLM_EXTRA_BODY 中某个键被拒（extra_keys 传入当前键名集合）
    "max_tokens_limit" - max_tokens 超过模型输出上限（Claude 路径用于解析上限并夹紧）
    "tools"            - 后端不支持 function calling
    None               - 不是参数拒绝，调用方应原样抛出
    判定顺序固定如上。规则：
    - 文本含 "thought_signature" 直接返回 None（Gemini 兼容端点的续传错误，含 "tool" 但非参数拒绝）
    - "sampling" / "extra" / "max_tokens_limit"：param 命中（等于参数名或以 "参数名." 开头）
      即判定，不需要关键词——这三类的降级动作只是丢弃可选参数或夹紧数值，误判代价温和
    - "tools" 例外：**即使 param 命中也必须**同时满足「文本含 _REJECT_HINTS 之一」或
      「error.code 属于 {"unknown_parameter", "unsupported_parameter"}」。
      因为 param="tools[0].function.parameters..." 的 schema 校验错误（我们自己的工具
      定义 bug）也会带 tools 前缀的 param，而禁用 tools 的代价是整只搜索功能静默消失，
      必须要求后端明确表达「不支持」才降级
    - 无 param 时：子串命中 且 文本含任一 _REJECT_HINTS 才判定；
      唯二例外是 "token_param"（"max_completion_tokens" 足够特异）和
      "max_tokens_limit"（需配合数字解析，误报会被"解析不出更小上限"挡住）
    """
    ...
```

`token_param` 的适配器侧守卫：只允许从 "max_tokens" 切换到 "max_completion_tokens"
**一次**——处理时先检查 `self.token_param == "max_tokens"`，不满足则视为未分类、原样
抛出，防止措辞奇特的后端让降级链在两个参数名之间打转。

各适配器的降级链改为 `kind = base.rejected_param(e, extra_keys=...)` 后按 kind 分发，
动作不变（粘性置 False / 夹紧 max_tokens / continue 重试），顺序天然由分类器统一。
必须保留的既有行为：测试 16/17/20 构造的假异常（`body=None` 走文本回退）文案分别含
"unsupported"、"unrecognized"、"not supported"，在新规则下判定结果不变。

### 3.7 __init__.py（装配）

```python
"""LLM 接入层：按 config.LLM_PROTOCOL 装配协议适配器。"""
from . import base
from .base import (RoundResult, SEARCH_TOOLS, assistant_tool_call_msg,
                   is_quota_error, tool_args)

def _make_adapter():
    if config.LLM_PROTOCOL == "claude":
        from .claude import ClaudeAdapter
        return ClaudeAdapter()
    if config.LLM_PROTOCOL == "gemini":
        from .gemini import GeminiAdapter
        return GeminiAdapter()
    from .openai import OpenAIAdapter
    return OpenAIAdapter()

adapter = _make_adapter()
```

延迟 import 各协议模块：openai 协议下不 import anthropic/genai（保持现在
「条件重依赖」的性质，anthropic 例外——它在 requirements 里，允许顶层 import，
但保持在 claude.py 内）。

### 3.8 chat.py 的单一循环

删除 Gemini 原生独立分支，`stream_reply` 内改为：

```python
adapter = llm.adapter
citations: list[dict] = []
rounds = config.SEARCH_MAX_ROUNDS + 1 if adapter.supports_tool_loop else 1
for round_idx in range(rounds):
    use_tools = (config.SEARCH_ENABLED and adapter.supports_tool_loop
                 and round_idx < config.SEARCH_MAX_ROUNDS)
    # 阶段行更新逻辑原样
    for attempt in range(2):
        out_before = len(finalized) + len(segment.strip())
        t0 = time.monotonic()
        try:
            result = await adapter.run_round(working, use_tools, on_text)
            break
        except Exception as e:
            elapsed = time.monotonic() - t0
            if len(finalized) + len(segment.strip()) != out_before:
                # 正文已到手、流在收尾被掐：按完成处理（原语义）
                result = llm.RoundResult(content=segment)
                break
            if attempt or llm.is_quota_error(e):
                raise
            # 无输出重试一次（原语义，日志文案保留）
            await asyncio.sleep(1.5)
    if result.citations:
        citations = result.citations
    if not result.calls or not use_tools:
        break
    assistant_msg = llm.assistant_tool_call_msg(result.calls, result.content)
    # 进度条目、工具执行、working 追加：原样
if citations and segment.strip():
    # 原 Gemini 分支的 sources 拼接逻辑原样移到这里（segment += "\n\n" + t("sources") + 链接）
```

注意：

- 两处 attempt 循环合并为一处后，原 Gemini 分支的日志行
  （"Gemini 原生轮完成…引用 N 条"）合入统一的轮次日志即可，不必区分协议。
- `rounds` 的语义变化：Gemini 原生从「rounds=0 + 前置分支」变成「rounds=1 且
  use_tools 恒 False」，网络行为完全等价。
- `_round_entries` / `_execute_tool_calls` / 进度渲染不动，只把 `llm._tool_args`
  改成 `llm.tool_args`。

### 3.9 web.py

- `from . import llm` 的用法改为：`llm.is_quota_error`。grounding 对 genai 的访问
  **必须留在函数体内**：在 `_search_gemini_grounded` 内部
  `from .llm import gemini as llm_gemini`，再用 `llm_gemini.gemini_client` /
  `llm_gemini.gtypes`——不得提升到模块顶部，否则任何协议下 import web 都会加载
  gemini 模块，破坏「不用 Gemini 就不 import google.genai」的延迟加载性质。
  该函数只在配置了 GEMINI_SEARCH_MODEL 时到达，函数内 import 的开销可忽略
  （模块首次加载后走 sys.modules 缓存）。
- 冷却状态从 config 搬来：删除 config 的 `_gemini_search_blocked_until`，
  在 web.py 模块级加 `_grounding_cooldown_until = 0.0`，读写处加 `global`。
  `GEMINI_SEARCH_COOLDOWN` 常量留在 config。

### 3.10 config.py / prompt.py

- config 新增：`SYSTEM_PROMPT_OVERRIDE = os.getenv("SYSTEM_PROMPT")`——注意用
  **None 哨兵**：未设置返回 None，显式设为空串表示「不要系统提示词」，两者语义不同，
  必须保持与现状（`os.getenv("SYSTEM_PROMPT", 内置默认)`）逐字节一致。
- prompt.py 改为：

```python
SYSTEM_PROMPT = (config.SYSTEM_PROMPT_OVERRIDE
                 if config.SYSTEM_PROMPT_OVERRIDE is not None else t("system_prompt"))
```

  删除 `import os`。拼接搜索段落的逻辑不变。

### 3.11 对话缓存内容量预算（tg.py）

定位要诚实：这是**近似的内容字符量预算**，不是进程内存硬上限。会计规则是「对每条
历史的 content 求字符长度之和」——CJK 文本的实际 UTF-8/内部存储可达每字符 2-4 字节，
Python 对象另有开销；反方向上，追问链的各条缓存共享同一批消息 dict（`history + [...]`
只新建外层 list），同一张 base64 图片会被多条缓存重复计数，导致高估。高估意味着
提前驱逐，方向是安全的。目的只有一个：防止视觉模式下 base64 图片让缓存无界膨胀。

- config 新增常量：`CONVERSATION_CONTENT_BUDGET = 64_000_000`
  （所有缓存对话的 content 字符量之和上限，注释写明上述近似性质与动机）。
- tg.py：

```python
_conv_sizes: dict[tuple[int, int], int] = {}
_conv_total = 0

def _history_chars(history) -> int:
    """近似内容量：文本取字符长度，多模态取各块文本/data URL 长度之和。"""
    total = 0
    for m in history:
        c = m.get("content", "")
        if isinstance(c, str):
            total += len(c)
        else:
            for p in c:
                total += len(p.get("text", "")) + len((p.get("image_url") or {}).get("url", ""))
    return total

def remember(chat_id, message_id, history):
    # 覆盖同 key 时先扣旧值，写入后必须 conversations.move_to_end(key)——
    # OrderedDict 对已存在 key 的赋值不改变位置，不挪到队尾的话刚更新的对话
    # 仍会以「最旧」身份被驱逐；随后按「条数超限或总量超预算」从最旧开始驱逐，
    # 但至少保留刚插入的这条（单条超预算也不驱逐自己）
```

驱逐循环条件：`while len(conversations) > 1 and (len(conversations) > CONVERSATION_CACHE_SIZE
or _conv_total > CONVERSATION_CONTENT_BUDGET)`，弹出最旧 key 时同步扣 `_conv_sizes` 与
`_conv_total`。tg.py 以 `from .config import CONVERSATION_CONTENT_BUDGET` 方式引入常量，
使测试可以在 tg 模块上临时调小（见阶段 4 用例要求），用完还原。

## 4. 分阶段执行计划

每阶段一个 commit，提交信息用中文一行概括 + 空行 + 要点。

（修订说明：原计划的「阶段 1 纯搬迁 + 兼容层」已取消——测试通过模块属性赋值和
`global` 写回来打桩（`llm.llm = 假客户端`、`llm.sampling_supported`），代码搬进子模块后
函数读的是子模块自己的全局，包级 re-export 骗不过赋值；且 `extra_body_supported` 被
两条协议共用，纯搬迁本身无法保持单一全局。风险改由阶段 1 内部的固定子步骤顺序控制。）

### 阶段 1：适配器落地（原阶段 1+2 合并）

按以下固定顺序执行，中途状态不要求测试通过，全部完成后一次性跑套件：

1. 建 `tgrok/llm/base.py`：搬入工具定义与共享 helpers（3.2 的清单），改公开名。
2. 建 `tgrok/llm/openai.py`：`OpenAIAdapter`（3.3），粘性标志改实例属性，
   `drain_stream` 保持模块级函数。
3. 建 `tgrok/llm/claude.py`：`ClaudeAdapter`（3.4），同上。
4. 建 `tgrok/llm/gemini.py`：genai 客户端 + `GeminiAdapter`（3.5）。
5. 建 `tgrok/llm/__init__.py`（3.7），删除旧 `tgrok/llm.py`。
6. 改 `chat.py`（3.8 单循环）与 `web.py`（3.9，含冷却状态迁移与函数内延迟 import）；
   修正 config.py 中 LLM_PROTOCOL 注释的地址表述（见 3.5 的地址语义裁定）。
7. 按第 6 节对照表适配 `tests/smoke_test.py`：只改打桩目标与 import 路径，
   断言语义不得删弱。测试文件头部把 `os.environ.setdefault` 换成**强制赋值**
   `os.environ["LLM_PROTOCOL"] = "openai"`（config 会 load_dotenv，本地 .env 或
   shell 里残留的协议设置不得影响套件对默认适配器的假设）。
8. 补 Gemini 主链的两个用例（现套件对 Gemini 原生零覆盖）：
   - chat 层：把 `llm.adapter` 临时换成 `supports_tool_loop=False`、返回
     `RoundResult(content=..., citations=[...])` 的桩适配器，跑 `chat.stream_reply`，
     断言只执行一轮、最终输出末尾带「来源」链接行；
   - llm 层：用假 chunk 对象直调 `gemini.drain_stream`，断言正文透传、
     grounding 引用按 uri 去重。

验收：`python tests/smoke_test.py` 24 项全过；
`grep -n "GEMINI_NATIVE_SEARCH\|CLAUDE_NATIVE" tgrok/chat.py` 无结果
（chat 不再感知具体协议）；`tgrok/llm.py` 文件已不存在。

### 阶段 2：结构化错误分类

base.py 加 `error_text` / `error_param` / `rejected_param`（3.6），三个适配器的
降级链改用分类器。新增两个回归用例：

- 正向：构造 `body={"error": {"param": "temperature", "message": "unsupported"}}` 的
  BadRequestError，断言走 param 路径触发采样降级；
- 反向：构造 message 为 "unexpected `tool_use_id` found in `tool_result` blocks"、
  `body=None` 的 BadRequestError，断言 `rejected_param` 返回 None（业务 400 不得
  触发 tools 粘性禁用）。

验收：26 项全过。

### 阶段 3：配置归位

按 3.10 迁移 SYSTEM_PROMPT 读取（None 哨兵语义）。验收：26 项全过；
`grep -rn "os.getenv" tgrok/ --include="*.py"` 只在 config.py 有结果。

### 阶段 4：对话缓存内容量预算

按 3.11 实施。新增回归用例：往 `tg.remember` 塞入多条含大 content 的历史
（把 `tg.CONVERSATION_CONTENT_BUDGET` 临时调成很小的值以便触发，结束后还原），断言
超预算后最旧条目被驱逐、`_conv_total` 与逐条重算结果一致、单条超大历史自身不被驱逐、
覆盖同 key 不重复计数、**覆盖同 key 后该条排到队尾**（不会紧接着被当最旧驱逐）。

验收：27 项全过。

### 阶段 5：文档同步

- `docs/ARCHITECTURE.zh.md`：更新模块图（llm 包五文件、适配器接口、chat 单循环）、
  三协议说明（`LLM_PROTOCOL`）、数据流描述；`GEMINI_NATIVE_SEARCH`/`CLAUDE_NATIVE`
  只作为兼容旧键提及。
- `README.md` / `README.zh-CN.md`：功能清单补 Claude 原生协议、`LLM_PROTOCOL`、
  采样参数（`LLM_TEMPERATURE`/`LLM_TOP_P`）、`LLM_EXTRA_BODY`；配置表与 `.env.example` 对齐。
- 文档不用图标字符。

验收：文档中出现的每个配置键都能在 config.py 找到对应读取。

## 5. 明确不做的事

- 不迁移 pytest（smoke_test 是重构安全网，结构保持；迁移另立项目）。
- 不改 configure.py 的交互流程与写出的键。
- 不动 i18n、tg_auth、进度显示、取消按钮、分段发送逻辑。
- 不做部署；不碰服务器 .env 与 profiles。
- 不为「未来可能的第四种协议」添加多余的钩子——三个实现 + 一个接口，够了。

## 6. 测试适配对照表（阶段 1 使用）

| 现用例 | 现挂钩 | 适配后 |
|---|---|---|
| 1-7（重试/掐流/429/看门狗/取消） | 整体替换 `llm.create_stream` | 替换 `llm.adapter` 为临时 `OpenAIAdapter(client=假客户端)`，假客户端的 `chat.completions.create` 返回假流；或直接给 `llm.adapter.run_round` 打补丁。看门狗用例（4）继续走真 `openai.drain_stream`：保持「假流 + 真 drain」的组合，即替换客户端而非 run_round |
| 8（签名回传/空 index） | `llm._drain_stream` | `from tgrok.llm.openai import drain_stream` 直调 |
| 9（同轮搜索合并） | `chat._execute_tool_calls` | 不变 |
| 13（SDK 重试禁用） | `llm.llm.max_retries == 0` | `OpenAIAdapter().client.max_retries == 0`；同时补 `ClaudeAdapter` 同断言（构造需 anthropic 已安装，直接实例化即可） |
| 16/17（采样/extra_body 降级） | `orig_create_stream` + 替换 `llm.llm`，断言 `llm.sampling_supported` 等 | `a = OpenAIAdapter(client=假客户端)` 后调 `a.run_round(...)` 或 `a._create_stream(...)`，断言 `a.sampling_supported is False` 等；无需全局还原 |
| 18（Claude 历史转换） | `llm._to_anthropic_messages` | `from tgrok.llm.claude import to_anthropic_messages` |
| 19（Claude 流消费） | `llm._drain_claude_stream` + `llm._assistant_tool_call_msg` | `claude.drain_stream` + `llm.assistant_tool_call_msg` |
| 20（Claude 降级链） | 替换 `llm.claude_client`、补 `llm.MAX_TOKENS` | `a = ClaudeAdapter(client=假客户端)`，MAX_TOKENS 补丁改在 `tgrok.llm.claude` 模块上；断言 `a.max_tokens_limit == 128000` |

测试文件顶部的 `config.STREAM_IDLE_TIMEOUT = 0`、`config.GEMINI_NATIVE_SEARCH = False`
保留；`orig_create_stream` 捕获不再需要（改用局部适配器实例后没有全局污染）。

## 7. 风险点备忘（执行时重点自查）

- chat 的取消（`asyncio.CancelledError`）路径：合并循环后 cancel 必须仍能在
  `run_round` 的 await 点打断，`finally` 里的清理顺序不变。用例 5-7 覆盖。
- Gemini 原生的 citations 拼接时机从「循环前的分支内」变为「循环后」，注意它拼在
  `segment` 上、必须发生在最终 `push(segment, final=True)` 之前（保持现在的顺序）。
- `token_param` 粘性化后，若后端同时拒绝 max_completion_tokens 与其他参数，
  降级链的 `continue` 顺序保持原样（先 token 参数，再采样，再 extra_body，再 tools）。
- 阶段 1 体量最大且中途不可测，严格按 4.1 的子步骤顺序推进；如果卡在测试适配，
  优先对照第 6 节表格逐条核对打桩目标，不要为了让测试通过而改断言语义。

## 8. 修订记录

针对执行前评审提出的四个硬冲突的裁定：

1. **纯搬迁阶段取消**：测试用「模块属性赋值 + 函数内 `global` 写回」打桩，包级
   re-export 无法拦截赋值；共享可变全局也无法在拆包后保持单一。原阶段 1/2 合并为
   现阶段 1，用固定子步骤顺序代替中间安全网。
2. **SYSTEM_PROMPT 语义**：迁移后用 None 哨兵区分「未设置」（用内置默认）与
   「显式空值」（无系统提示词），与现状逐字节等价，不采用 `or` 回退。
3. **缓存预算定位修正**：改为「近似内容字符量预算」（`CONVERSATION_CONTENT_BUDGET`），
   明确不是内存硬上限；共享消息导致的重复计数按「高估→提前驱逐→安全」接受。
4. **误禁 tools 防护**：降级判定升级为 param 精确匹配优先 + 文本回退需命中拒绝措辞
   关键词；关键词表明确排除 "unexpected"（Anthropic 的 tool_use/tool_result 配对类
   业务 400 含该词），并新增反向回归用例锁住这条边界。

第二轮评审的五项裁定：

5. **Gemini 地址语义**：原生协议用 `GEMINI_BASE_URL`（与 grounding 共用客户端与端点，
   向导与既有 profiles 已按此写档）；config.py 的 LLM_PROTOCOL 注释中「地址统一用
   LLM_BASE_URL」是错误表述，阶段 1 修正为按协议区分。
6. **tools 类别收紧**：param 命中不再免检——schema 校验错误的 param 也带 tools 前缀，
   而禁用 tools 代价最高；必须叠加拒绝措辞或 error.code ∈
   {unknown_parameter, unsupported_parameter}。`token_param` 增加「仅允许从
   max_tokens 切换一次」守卫。
7. **Gemini 主链补测**：阶段 1 增加 chat 层（桩适配器 + citations 输出）与 llm 层
   （drain_stream 假 chunk 去重）两个用例；web.py 对 gemini 模块的 import 必须留在
   grounding 函数体内，保住延迟加载。
8. **缓存覆盖语义**：同 key 覆盖后必须 `move_to_end`（OrderedDict 赋值不改位置，
   否则刚更新的对话会被当最旧驱逐）；预算用例通过临时调小
   `tg.CONVERSATION_CONTENT_BUDGET` 触发驱逐。
9. **测试环境加固**：套件头部对 `LLM_PROTOCOL` 用强制赋值而非 setdefault（防本地
   .env/shell 残留干扰）；适配器构造器判空用 `is not None`，不用 `or`。
