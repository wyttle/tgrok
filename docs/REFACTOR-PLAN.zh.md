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
        # client 可注入：测试用假对象替换，缺省按 config 构造真实 AsyncOpenAI
        self.client = client or AsyncOpenAI(..., max_retries=0)   # 参数同现有
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
        self.client = client or anthropic.AsyncAnthropic(..., max_retries=0)  # 参数同现有
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

- genai 客户端与 `gtypes` 从现 llm.py 顶部搬入，创建条件不变
  （`GEMINI_NATIVE_SEARCH or GEMINI_SEARCH_MODEL` 时才 import google.genai 并建客户端）。
  grounding（web.py）与原生适配器共用这里的 `gemini_client` / `gtypes`。
- `GeminiAdapter`：`supports_tool_loop = False`；`run_round` = 原
  `gemini_create_stream` + `_drain_gemini_stream`，返回
  `RoundResult(calls={}, content=..., citations=...)`。
- `to_gemini_contents(history)`（原 `_to_gemini_contents`）留在本模块。

### 3.6 结构化错误解析（base.py）

替换所有「对 `str(e).lower()` 做子串匹配」的判定入口：

```python
def error_text(e) -> str:
    """从 BadRequestError 提取判定用文本（小写）。优先结构化 body：
    OpenAI 形如 {"error": {"message":..., "param":..., "type":...}}，
    Anthropic 形如 {"type":"error","error":{"type":...,"message":...}}；
    param 字段单独拼在最前面，使参数名判定不受 message 措辞影响。
    拿不到结构化 body 时回退 str(e)。"""
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        parts = [str(err.get(k) or "") for k in ("param", "message", "type")]
        joined = " ".join(p for p in parts if p).strip()
        if joined:
            return joined.lower()
    return str(e).lower()
```

各适配器降级链把 `err = str(e).lower()` 改为 `err = error_text(e)`，判定子串本身
（"temperature"、"top_p"、"max_completion_tokens"、"tool"、"thought_signature"、
extra_body 键名、Claude 的 max_tokens 数字解析）不变。测试 16/17/20 构造的假异常
`body=None`，走回退路径，不需要改断言。

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

- `from . import llm` 的用法改为：`llm.is_quota_error`；grounding 部分改
  `from .llm import gemini as llm_gemini` 后用 `llm_gemini.gemini_client` /
  `llm_gemini.gtypes`（访问时机不变：只有配置了 GEMINI_SEARCH_MODEL 才会走到）。
- 冷却状态从 config 搬来：删除 config 的 `_gemini_search_blocked_until`，
  在 web.py 模块级加 `_grounding_cooldown_until = 0.0`，读写处加 `global`。
  `GEMINI_SEARCH_COOLDOWN` 常量留在 config。

### 3.10 config.py / prompt.py

- config 新增：`SYSTEM_PROMPT_OVERRIDE = os.getenv("SYSTEM_PROMPT", "")`。
- prompt.py 改为 `SYSTEM_PROMPT = config.SYSTEM_PROMPT_OVERRIDE or t("system_prompt")`，
  删除 `import os`。拼接搜索段落的逻辑不变。

### 3.11 对话缓存字节预算（tg.py）

- config 新增常量：`CONVERSATION_MAX_BYTES = 64 * 1024 * 1024`
  （64MB，含 base64 图片的历史总量上限；注释说明动机）。
- tg.py：

```python
_conv_sizes: dict[tuple[int, int], int] = {}
_conv_total = 0

def _history_bytes(history) -> int:
    """近似字节量：文本取长度，多模态取各块文本/data URL 长度之和。"""
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
    # 覆盖同 key 时先扣旧值；随后按「条数超限或总字节超限」从最旧开始驱逐，
    # 但至少保留刚插入的这条（单条超预算也不驱逐自己）
```

驱逐循环条件：`while len(conversations) > 1 and (len(conversations) > CONVERSATION_CACHE_SIZE
or _conv_total > CONVERSATION_MAX_BYTES)`，弹出最旧 key 时同步扣 `_conv_sizes`。

## 4. 分阶段执行计划

每阶段一个 commit，提交信息用中文一行概括 + 空行 + 要点。

### 阶段 1：llm.py → llm/ 包（纯搬迁）

把 llm.py 按 3.1 拆成五个文件，**只搬代码与改名，不改任何逻辑**（`error_text` 此阶段
还不引入；全局标志此阶段先原样搬进各协议模块的模块级——降级为中间态可以接受，
阶段 2 收进实例）。`__init__.py` 先用兼容层把旧名字全部 re-export
（`create_stream`、`_drain_stream`、`_tool_args`……指向新位置），chat.py、web.py、
测试**一行都不改**。

验收：`python tests/smoke_test.py` 22 项全过，chat/web/测试零改动。

### 阶段 2：适配器接口 + chat 单循环 + 状态收编

- base.py 落 `RoundResult` / `BaseAdapter`；三个适配器类按 3.3-3.5 成形，
  粘性标志改实例属性；`__init__.py` 按 3.7 装配 `adapter` 单例，删除阶段 1 的兼容层，
  只保留 3.7 列出的公开名。
- chat.py 按 3.8 改单循环；web.py 按 3.9 改 import 与冷却状态。
- 测试按第 6 节对照表适配。

验收：22 项全过（其中原 16/17/20 的降级断言改为断言适配器实例属性）；
`grep -rn "GEMINI_NATIVE_SEARCH" tgrok/chat.py` 无结果（chat 不再感知具体协议）。

### 阶段 3：结构化错误解析

base.py 加 `error_text`（3.6），三个适配器的降级链改用它。新增一个回归用例：
构造带结构化 body 的 BadRequestError（`body={"error": {"param": "temperature",
"message": "unsupported"}}`），断言采样降级触发——证明 param 路径生效。

验收：23 项全过。

### 阶段 4：配置归位

按 3.10 迁移 SYSTEM_PROMPT 读取。验收：全过；`grep -rn "os.getenv" tgrok/ --include="*.py"`
只在 config.py 有结果。

### 阶段 5：对话缓存字节预算

按 3.11 实施。新增回归用例：往 `tg.remember` 塞入多条含大 content 的历史，断言
超预算后最旧条目被驱逐、`_conv_total` 与实际一致、单条超大历史自身不被驱逐。

验收：24 项全过。

### 阶段 6：文档同步

- `docs/ARCHITECTURE.zh.md`：更新模块图（llm 包五文件、适配器接口、chat 单循环）、
  三协议说明（`LLM_PROTOCOL`）、数据流描述；删除 `GEMINI_NATIVE_SEARCH` 作为配置项的表述
  （只作为兼容旧键提及）。
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

## 6. 测试适配对照表（阶段 2 使用）

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
- 阶段 1 的「纯搬迁 + 兼容层」是安全网，别跳过它直接一步到位。
