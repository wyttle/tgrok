"""Gemini 原生协议适配器与 grounding 客户端。

google_search 和 url_context 由 Google 服务端执行，所以不走 bot 的工具循环（supports_tool_loop=False）。
"""

import base64
import re

from .. import config
from .base import BaseAdapter, RequestOptions, RoundResult, watch_idle

_DATA_URL_RE = re.compile(r"^data:([^;]+);base64,(.*)$", re.S)

_genai = None
gtypes = None


def make_client(api_key: str, base_url: str):
    """按需加载 google-genai SDK 并建客户端：只有用到 Gemini 的配置才导入它。"""
    global _genai, gtypes
    if _genai is None:
        from google import genai
        from google.genai import types
        _genai, gtypes = genai, types
    return _genai.Client(api_key=api_key, http_options={"base_url": base_url} if base_url else None)


# grounding 搜索、watch_media 和媒体预读共用的客户端；主模型走 Gemini 原生时适配器另建自己的客户端
gemini_client = (make_client(config.GEMINI_API_KEY, config.GEMINI_BASE_URL)
                 if config.LLM_PROTOCOL == "gemini" or config.GEMINI_SEARCH_MODEL or config.GEMINI_MEDIA_MODEL
                 else None)


class GeminiAdapter(BaseAdapter):
    name = "gemini"
    supports_tool_loop = False

    def __init__(self, client=None, endpoint=None):
        super().__init__(endpoint)
        self.client = client if client is not None else make_client(self.endpoint.api_key, self.endpoint.base_url)

    async def _open(self, history: list[dict], opts: RequestOptions):
        system_text, contents = to_gemini_contents(history)
        tools = [gtypes.Tool(google_search=gtypes.GoogleSearch())]
        try:
            tools.append(gtypes.Tool(url_context=gtypes.UrlContext()))
        except AttributeError:  # 旧版 SDK 无 url_context，仅用 google_search
            pass
        gen_config = gtypes.GenerateContentConfig(
            system_instruction=system_text or None,
            max_output_tokens=config.MAX_TOKENS,
            # None 即未设置，SDK 序列化时会剔除
            temperature=opts.sampling.get("temperature"),
            top_p=opts.sampling.get("top_p"),
            tools=tools,
        )
        return await self.client.aio.models.generate_content_stream(
            model=self.model, contents=contents, config=gen_config
        )

    async def _drain(self, stream, on_text) -> RoundResult:
        citations, content = await drain_stream(stream, on_text)
        return RoundResult(content=content, citations=citations)


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
    """消费 Gemini 原生流：正文交给 on_text，聚合 grounding 引用（去重）。返回（引用列表, 正文）。"""
    content, citations, seen = "", [], set()
    async for chunk in watch_idle(stream, lambda: bool(content), "Gemini"):
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
