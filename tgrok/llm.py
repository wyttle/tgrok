"""LLM 接入层：OpenAI 兼容流式、Gemini 原生流式、工具定义与流消费。"""

import asyncio
import base64
import json
import logging
import re

import anthropic
from openai import AsyncOpenAI, BadRequestError

from . import config
from .config import (
    CLAUDE_BASE_URL, GEMINI_API_KEY, GEMINI_BASE_URL, GEMINI_NATIVE_SEARCH,
    GEMINI_SEARCH_MODEL, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_USER_AGENT,
    MAX_TOKENS,
)
from .i18n import t

logger = logging.getLogger(__name__)

llm = AsyncOpenAI(
    base_url=LLM_BASE_URL,
    api_key=LLM_API_KEY,
    default_headers={"User-Agent": LLM_USER_AGENT} if LLM_USER_AGENT else None,
    # 禁用 SDK 内建重试：它会按服务端 Retry-After 静默睡 60s+，期间用户只能干等。
    # 重试语义由 chat.stream_reply 统一控制（零输出重试一次、429 不重试）
    max_retries=0,
)

if GEMINI_NATIVE_SEARCH or GEMINI_SEARCH_MODEL:
    from google import genai as _genai
    from google.genai import types as gtypes

    gemini_client = _genai.Client(
        api_key=GEMINI_API_KEY,
        http_options={"base_url": GEMINI_BASE_URL} if GEMINI_BASE_URL else None,
    )

if config.CLAUDE_NATIVE:
    claude_client = anthropic.AsyncAnthropic(
        base_url=CLAUDE_BASE_URL or None,
        api_key=LLM_API_KEY,
        default_headers={"User-Agent": LLM_USER_AGENT} if LLM_USER_AGENT else None,
        # 与 OpenAI 客户端一致：禁用 SDK 内建重试，重试语义由 chat.stream_reply 统一控制
        max_retries=0,
    )

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            # grounding 模式下 web_search 是深度调研代理，引导主模型合并任务而非拆散小查询
            (
                "Deep research agent backed by Google Search. Give it ONE comprehensive "
                "research task in natural language — it can cover several aspects at once, "
                "and the agent will run multiple Google searches internally and return a "
                "fact summary with sources. Do NOT split one round of research into many "
                "narrow queries; one well-phrased task per round is enough."
            )
            if GEMINI_SEARCH_MODEL
            else (
                "Search the web for up-to-date information. Use this for recent events, "
                "time-sensitive facts, or anything you are unsure about."
            )
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The research task or question, in the language most likely to find good results."
                        if GEMINI_SEARCH_MODEL
                        else "The search query, in the language most likely to find good results."
                    ),
                }
            },
            "required": ["query"],
        },
    },
}

FETCH_URL_TOOL = {
    "type": "function",
    "function": {
        "name": "open_url",
        "description": (
            "Fetch a web page by URL and return its readable text. "
            "Use it to read the details behind links found via web_search."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full http(s) URL of the page to read.",
                }
            },
            "required": ["url"],
        },
    },
}

SEARCH_TOOLS = [WEB_SEARCH_TOOL, FETCH_URL_TOOL]

# 后端明确拒绝 tools 参数后置 False，进程内不再携带（bot 退化为普通对话）
tools_supported = True

# 后端明确拒绝采样参数后置 False（推理类模型常只接受默认 temperature/top_p）
sampling_supported = True

# 后端明确拒绝 LLM_EXTRA_BODY 里的厂商私有参数（thinking/reasoning_effort 等）后置 False
extra_body_supported = True


def _sampling_kwargs() -> dict:
    """按配置组装 temperature/top_p；走 config 模块属性读取，便于测试与运行时调整。"""
    kw = {}
    if config.LLM_TEMPERATURE is not None:
        kw["temperature"] = config.LLM_TEMPERATURE
    if config.LLM_TOP_P is not None:
        kw["top_p"] = config.LLM_TOP_P
    return kw

async def _drain_stream(stream, on_text) -> tuple[dict[int, dict], str]:
    """消费流式响应：正文片段逐个交给 on_text，tool_call 片段按 index 聚合。

    空闲看门狗：已有正文、且没有聚合到一半的 tool_call 时，超过
    config.STREAM_IDLE_TIMEOUT 秒没有新数据就视为生成完成、主动收尾——部分网关
    发完内容后不发结束帧，流会空挂到上游超时。
    返回（聚合后的 tool_calls, 本轮完整正文）。
    """
    if config.CLAUDE_NATIVE:
        return await _drain_claude_stream(stream, on_text)
    calls: dict[int, dict] = {}
    content = ""
    it = stream.__aiter__()
    while True:
        try:
            if content and not calls and config.STREAM_IDLE_TIMEOUT > 0:
                chunk = await asyncio.wait_for(anext(it), config.STREAM_IDLE_TIMEOUT)
            else:
                chunk = await anext(it)
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError:
            logger.warning(
                "LLM 流已有正文但 %.0fs 无新数据，视为完成（正文 %d 字）",
                config.STREAM_IDLE_TIMEOUT, len(content),
            )
            try:
                await stream.close()
            except Exception:
                pass
            break
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        if delta.content:
            content += delta.content
            await on_text(delta.content)
        for tc in delta.tool_calls or []:
            # Gemini 兼容端点的 tool_call 不带 index（整个调用一个 chunk 发全）：
            # 每次分配新槽位，避免多个调用挤进同一槽互相覆盖/拼接
            idx = tc.index if tc.index is not None else 10_000 + len(calls)
            slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": "", "extra": None})
            if tc.id:
                slot["id"] = tc.id
            if tc.function:
                if tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function.arguments:
                    slot["arguments"] += tc.function.arguments
            # Gemini 思考型模型要求回传 thought_signature（在 extra_content 里），
            # 丢失会导致下一轮请求 400
            extra = getattr(tc, "model_extra", None) or {}
            if extra.get("extra_content"):
                slot["extra"] = extra["extra_content"]
    return calls, content


def _assistant_tool_call_msg(calls: dict[int, dict], content: str) -> dict:
    """把聚合好的 tool_call 片段组装成请求格式的 assistant 消息。"""
    tool_calls = []
    for i, slot in sorted(calls.items()):
        call = {
            # 部分本地后端不回 id：合成一个，并在 tool 结果里复用以保持配对
            "id": slot["id"] or f"call_{i}",
            "type": "function",
            "function": {
                "name": slot["name"] or "web_search",
                "arguments": slot["arguments"] or "{}",
            },
        }
        if slot.get("extra"):
            # 回传 Gemini 思考型模型的 thought_signature 等厂商扩展字段
            call["extra_content"] = slot["extra"]
        tool_calls.append(call)
    return {"role": "assistant", "content": content or "", "tool_calls": tool_calls}


def _is_quota_error(e: BaseException) -> bool:
    """配额/限流类错误（429）：重试大概率无效且浪费配额，需单独处理。"""
    s = str(e)
    return "429" in s or "RESOURCE_EXHAUSTED" in s or "rate limit" in s.lower()


def _tool_args(call: dict) -> dict | None:
    """解析 tool_call 的参数；arguments 不是合法 JSON 对象时返回 None。"""
    try:
        args = json.loads(call["function"]["arguments"])
    except (json.JSONDecodeError, TypeError):
        return None
    return args if isinstance(args, dict) else None

async def create_stream(history: list[dict], use_tools: bool):
    if config.CLAUDE_NATIVE:
        return await _claude_create_stream(history, use_tools)
    global tools_supported, sampling_supported, extra_body_supported
    token_param = "max_tokens"
    include_tools = use_tools and tools_supported
    sampling = _sampling_kwargs() if sampling_supported else {}
    extra_body = config.LLM_EXTRA_BODY if extra_body_supported else None
    while True:
        kwargs = {"model": LLM_MODEL, "messages": history, "stream": True, token_param: MAX_TOKENS, **sampling}
        if extra_body:
            kwargs["extra_body"] = extra_body
        if include_tools:
            kwargs["tools"] = SEARCH_TOOLS
            kwargs["tool_choice"] = "auto"
        try:
            return await llm.chat.completions.create(**kwargs)
        except BadRequestError as e:
            err = str(e).lower()
            # OpenAI 官方较新的模型要求用 max_completion_tokens 代替 max_tokens
            if token_param == "max_tokens" and "max_completion_tokens" in err:
                token_param = "max_completion_tokens"
                continue
            # 推理类模型常只接受默认采样值：去掉 temperature/top_p 重试并粘性禁用
            if sampling and ("temperature" in err or "top_p" in err):
                logger.warning("后端拒绝采样参数，已改用后端默认值（重启进程后会再次尝试）：%s", e)
                sampling_supported = False
                sampling = {}
                continue
            # 厂商私有参数不被当前后端接受：去掉重试并粘性禁用
            if extra_body and any(k.lower() in err for k in extra_body):
                logger.warning("后端拒绝额外参数 %s，本进程内不再携带（重启后会再次尝试）：%s",
                               list(extra_body), e)
                extra_body_supported = False
                extra_body = None
                continue
            # 后端不支持 function calling：去掉 tools 重试，并在进程内粘性禁用。
            # 注意 thought_signature 缺失的 400 报错文案里也含 "tool"，不属于此类
            if include_tools and "tool" in err and "thought_signature" not in err:
                logger.warning("后端拒绝 tools 参数，联网搜索已禁用（重启进程后会再次尝试）：%s", e)
                tools_supported = False
                include_tools = False
                continue
            raise

_DATA_URL_RE = re.compile(r"^data:([^;]+);base64,(.*)$", re.S)


def _to_gemini_contents(history: list[dict]):
    """OpenAI 格式的 messages → Gemini 的 (system_instruction, contents)。

    多模态 content 数组里的 base64 data URL 图片转回字节；system 消息单独抽出。
    """
    system_text, contents = "", []
    for m in history:
        role, content = m["role"], m.get("content", "")
        if role == "system":
            if isinstance(content, str):
                system_text = content
            continue
        parts = []
        if isinstance(content, str):
            if content:
                parts.append(gtypes.Part.from_text(text=content))
        else:
            for p in content:
                if p.get("type") == "text":
                    parts.append(gtypes.Part.from_text(text=p["text"]))
                elif p.get("type") == "image_url":
                    mm = _DATA_URL_RE.match(p.get("image_url", {}).get("url", ""))
                    if mm:
                        parts.append(gtypes.Part.from_bytes(
                            data=base64.b64decode(mm.group(2)), mime_type=mm.group(1)))
        if parts:
            contents.append(gtypes.Content(role="model" if role == "assistant" else "user", parts=parts))
    return system_text, contents


async def gemini_create_stream(history: list[dict]):
    system_text, contents = _to_gemini_contents(history)
    tools = [gtypes.Tool(google_search=gtypes.GoogleSearch())]
    try:
        tools.append(gtypes.Tool(url_context=gtypes.UrlContext()))
    except AttributeError:  # 旧版 SDK 无 url_context，仅用 google_search
        pass
    gen_config = gtypes.GenerateContentConfig(
        system_instruction=system_text or None,
        max_output_tokens=MAX_TOKENS,
        # None 即未设置，SDK 序列化时会剔除
        temperature=config.LLM_TEMPERATURE,
        top_p=config.LLM_TOP_P,
        tools=tools,
    )
    return await gemini_client.aio.models.generate_content_stream(
        model=LLM_MODEL, contents=contents, config=gen_config
    )


async def _drain_gemini_stream(stream, on_text) -> tuple[list[dict], str]:
    """消费 Gemini 原生流：正文交给 on_text，聚合 grounding 引用（去重）。

    与 _drain_stream 相同的空闲看门狗语义。返回（引用列表, 正文）。
    """
    content, citations, seen = "", [], set()
    it = stream.__aiter__()
    while True:
        try:
            if content and config.STREAM_IDLE_TIMEOUT > 0:
                chunk = await asyncio.wait_for(anext(it), config.STREAM_IDLE_TIMEOUT)
            else:
                chunk = await anext(it)
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError:
            logger.warning(
                "Gemini 流已有正文但 %.0fs 无新数据，视为完成（正文 %d 字）",
                config.STREAM_IDLE_TIMEOUT, len(content),
            )
            break
        text = getattr(chunk, "text", None)
        if text:
            content += text
            await on_text(text)
        for cand in getattr(chunk, "candidates", None) or []:
            gm = getattr(cand, "grounding_metadata", None)
            for gc in getattr(gm, "grounding_chunks", None) or []:
                web = getattr(gc, "web", None)
                uri = getattr(web, "uri", None)
                if uri and uri not in seen:
                    seen.add(uri)
                    citations.append({"uri": uri, "title": getattr(web, "title", "") or uri})
    return citations, content


# ---------------------------------------------------------------------------
# Claude 原生协议（Anthropic Messages API）
# ---------------------------------------------------------------------------

# 后端拒绝超大 max_tokens 后记住其允许的上限（官方各模型 64k~128k 不等）
claude_max_tokens: int | None = None


def _anthropic_tools() -> list[dict]:
    """OpenAI function 工具定义 → Anthropic 原生工具格式。"""
    return [
        {
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "input_schema": t["function"]["parameters"],
        }
        for t in SEARCH_TOOLS
    ]


def _to_anthropic_messages(history: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI 格式的 messages → Anthropic 的 (system, messages)。

    system 单独抽出；多模态 data URL 图片转 image 块；assistant 的 tool_calls
    还原为 tool_use 块，思考块从 extra_content 原样取回前置——Anthropic 要求
    工具续传时回传 thinking 及签名，丢失会 400；连续的 tool 结果必须合并进
    同一条 user 消息，分开发会被判定 role 不交替。
    """
    system_text, out = "", []
    for m in history:
        role, content = m["role"], m.get("content", "")
        if role == "system":
            if isinstance(content, str):
                system_text = content
        elif role == "assistant" and m.get("tool_calls"):
            blocks: list[dict] = []
            first_extra = m["tool_calls"][0].get("extra_content") or {}
            blocks.extend(first_extra.get("anthropic_thinking") or [])
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            for c in m["tool_calls"]:
                blocks.append({
                    "type": "tool_use",
                    "id": c["id"],
                    "name": c["function"]["name"],
                    "input": _tool_args(c) or {},
                })
            out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            last = out[-1] if out else None
            if (last and last["role"] == "user" and isinstance(last["content"], list)
                    and last["content"] and last["content"][0].get("type") == "tool_result"):
                last["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif isinstance(content, str):
            if content:
                out.append({"role": role, "content": content})
        else:
            blocks = []
            for p in content:
                if p.get("type") == "text":
                    blocks.append({"type": "text", "text": p["text"]})
                elif p.get("type") == "image_url":
                    mm = _DATA_URL_RE.match(p.get("image_url", {}).get("url", ""))
                    if mm:
                        blocks.append({
                            "type": "image",
                            "source": {"type": "base64", "media_type": mm.group(1), "data": mm.group(2)},
                        })
            if blocks:
                out.append({"role": role, "content": blocks})
    return system_text, out


async def _claude_create_stream(history: list[dict], use_tools: bool):
    """建立 Anthropic 原生流。降级链与 create_stream 同一套路：采样参数、
    LLM_EXTRA_BODY（thinking 等）、超限的 max_tokens、tools 被拒时逐项去掉
    重试并进程内粘性禁用。"""
    global tools_supported, sampling_supported, extra_body_supported, claude_max_tokens
    system_text, messages = _to_anthropic_messages(history)
    include_tools = use_tools and tools_supported
    sampling = _sampling_kwargs() if sampling_supported else {}
    extra_body = config.LLM_EXTRA_BODY if extra_body_supported else None
    max_tokens = min(MAX_TOKENS, claude_max_tokens) if claude_max_tokens else MAX_TOKENS
    while True:
        kwargs = {"model": LLM_MODEL, "messages": messages, "stream": True,
                  "max_tokens": max_tokens, **sampling}
        if system_text:
            kwargs["system"] = system_text
        if extra_body:
            kwargs["extra_body"] = extra_body
        if include_tools:
            kwargs["tools"] = _anthropic_tools()
        try:
            return await claude_client.messages.create(**kwargs)
        except anthropic.BadRequestError as e:
            err = str(e).lower()
            # Opus 4.7+ 已移除采样参数（400），去掉重试并粘性禁用
            if sampling and ("temperature" in err or "top_p" in err):
                logger.warning("后端拒绝采样参数，已改用默认值（重启进程后会再次尝试）：%s", e)
                sampling_supported = False
                sampling = {}
                continue
            # thinking 等额外参数被拒（如 4.7+ 已移除 budget_tokens）：去掉重试
            if extra_body and any(k.lower() in err for k in extra_body):
                logger.warning("后端拒绝额外参数 %s，本进程内不再携带（重启后会再次尝试）：%s",
                               list(extra_body), e)
                extra_body_supported = False
                extra_body = None
                continue
            # max_tokens 超过模型输出上限：从报错文案里解析上限并粘性记住
            if "max_tokens" in err:
                nums = [int(n) for n in re.findall(r"\d+", err)]
                smaller = [n for n in nums if 0 < n < max_tokens]
                new = max(smaller) if smaller else 8192
                if new >= max_tokens:
                    raise
                logger.warning("后端拒绝 max_tokens=%d，降为 %d 重试：%s", max_tokens, new, e)
                claude_max_tokens = new
                max_tokens = new
                continue
            if include_tools and "tool" in err:
                logger.warning("后端拒绝 tools 参数，联网搜索已禁用（重启进程后会再次尝试）：%s", e)
                tools_supported = False
                include_tools = False
                continue
            raise


async def _drain_claude_stream(stream, on_text) -> tuple[dict[int, dict], str]:
    """消费 Anthropic 原生流：text_delta 交给 on_text，tool_use 块按 index 聚合，
    thinking 块（含签名）原样收集、挂到首个调用的扩展字段上以便续传时回传。
    与 _drain_stream 相同的空闲看门狗语义。
    """
    calls: dict[int, dict] = {}
    content = ""
    thinking_blocks: list[dict] = []
    cur_think: dict | None = None
    it = stream.__aiter__()
    while True:
        try:
            if content and not calls and config.STREAM_IDLE_TIMEOUT > 0:
                event = await asyncio.wait_for(anext(it), config.STREAM_IDLE_TIMEOUT)
            else:
                event = await anext(it)
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError:
            logger.warning(
                "Claude 流已有正文但 %.0fs 无新数据，视为完成（正文 %d 字）",
                config.STREAM_IDLE_TIMEOUT, len(content),
            )
            try:
                await stream.close()
            except Exception:
                pass
            break
        etype = getattr(event, "type", "")
        if etype == "content_block_start":
            block = event.content_block
            btype = getattr(block, "type", "")
            if btype == "tool_use":
                calls[event.index] = {"id": getattr(block, "id", "") or "",
                                      "name": getattr(block, "name", "") or "",
                                      "arguments": "", "extra": None}
            elif btype == "thinking":
                cur_think = {"type": "thinking",
                             "thinking": getattr(block, "thinking", "") or "", "signature": ""}
                thinking_blocks.append(cur_think)
            elif btype == "redacted_thinking":
                thinking_blocks.append({"type": "redacted_thinking",
                                        "data": getattr(block, "data", "") or ""})
        elif etype == "content_block_delta":
            delta = event.delta
            dtype = getattr(delta, "type", "")
            if dtype == "text_delta":
                text = getattr(delta, "text", "") or ""
                if text:
                    content += text
                    await on_text(text)
            elif dtype == "input_json_delta":
                slot = calls.get(event.index)
                if slot is not None:
                    slot["arguments"] += getattr(delta, "partial_json", "") or ""
            elif dtype == "thinking_delta" and cur_think is not None:
                cur_think["thinking"] += getattr(delta, "thinking", "") or ""
            elif dtype == "signature_delta" and cur_think is not None:
                cur_think["signature"] = getattr(delta, "signature", "") or ""
        elif etype == "message_stop":
            break
    if calls and thinking_blocks:
        # 经 _assistant_tool_call_msg 进入 extra_content，
        # 续传时由 _to_anthropic_messages 取回原样前置
        first = calls[min(calls)]
        first["extra"] = {**(first["extra"] or {}), "anthropic_thinking": thinking_blocks}
    return calls, content

