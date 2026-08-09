"""Gemini 原生协议适配器与 grounding 客户端。"""

import asyncio
import base64
import logging
import re

from .. import config
from ..config import (
    GEMINI_API_KEY, GEMINI_BASE_URL, GEMINI_NATIVE_SEARCH, GEMINI_SEARCH_MODEL,
    LLM_MODEL, MAX_TOKENS,
)
from .base import BaseAdapter, RoundResult

logger = logging.getLogger(__name__)
_DATA_URL_RE = re.compile(r"^data:([^;]+);base64,(.*)$", re.S)

if GEMINI_NATIVE_SEARCH or GEMINI_SEARCH_MODEL:
    from google import genai as _genai
    from google.genai import types as gtypes

    gemini_client = _genai.Client(
        api_key=GEMINI_API_KEY,
        http_options={"base_url": GEMINI_BASE_URL} if GEMINI_BASE_URL else None,
    )


class GeminiAdapter(BaseAdapter):
    name = "gemini"
    supports_tool_loop = False

    async def run_round(self, history, use_tools, on_text) -> RoundResult:
        stream = await self._create_stream(history)
        citations, content = await drain_stream(stream, on_text)
        return RoundResult(content=content, citations=citations)

    async def _create_stream(self, history: list[dict]):
        system_text, contents = to_gemini_contents(history)
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


def to_gemini_contents(history: list[dict]):
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


async def drain_stream(stream, on_text) -> tuple[list[dict], str]:
    """消费 Gemini 原生流：正文交给 on_text，聚合 grounding 引用（去重）。

    与 OpenAI 流消费相同的空闲看门狗语义。返回（引用列表, 正文）。
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
