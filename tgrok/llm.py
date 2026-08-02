"""LLM 接入层：OpenAI 兼容流式、Gemini 原生流式、工具定义与流消费。"""

import asyncio
import base64
import json
import logging
import re

from openai import AsyncOpenAI, BadRequestError

from . import config
from .config import (
    GEMINI_API_KEY, GEMINI_BASE_URL, GEMINI_NATIVE_SEARCH, GEMINI_SEARCH_MODEL,
    LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_USER_AGENT, MAX_TOKENS,
)
from .i18n import t

logger = logging.getLogger(__name__)

llm = AsyncOpenAI(
    base_url=LLM_BASE_URL,
    api_key=LLM_API_KEY,
    default_headers={"User-Agent": LLM_USER_AGENT} if LLM_USER_AGENT else None,
)

if GEMINI_NATIVE_SEARCH or GEMINI_SEARCH_MODEL:
    from google import genai as _genai
    from google.genai import types as gtypes

    gemini_client = _genai.Client(
        api_key=GEMINI_API_KEY,
        http_options={"base_url": GEMINI_BASE_URL} if GEMINI_BASE_URL else None,
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

async def _drain_stream(stream, on_text) -> tuple[dict[int, dict], str]:
    """消费流式响应：正文片段逐个交给 on_text，tool_call 片段按 index 聚合。

    空闲看门狗：已有正文、且没有聚合到一半的 tool_call 时，超过
    config.STREAM_IDLE_TIMEOUT 秒没有新数据就视为生成完成、主动收尾——部分网关
    发完内容后不发结束帧，流会空挂到上游超时。
    返回（聚合后的 tool_calls, 本轮完整正文）。
    """
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
    global tools_supported
    token_param = "max_tokens"
    include_tools = use_tools and tools_supported
    while True:
        kwargs = {"model": LLM_MODEL, "messages": history, "stream": True, token_param: MAX_TOKENS}
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

