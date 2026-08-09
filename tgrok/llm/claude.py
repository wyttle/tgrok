"""Claude 原生协议适配器。"""

import asyncio
import logging
import re

import anthropic

from .. import config
from ..config import CLAUDE_BASE_URL, LLM_API_KEY, LLM_MODEL, LLM_USER_AGENT, MAX_TOKENS
from .base import BaseAdapter, RoundResult, SEARCH_TOOLS, error_text, rejected_param, sampling_kwargs, tool_args

logger = logging.getLogger(__name__)
_DATA_URL_RE = re.compile(r"^data:([^;]+);base64,(.*)$", re.S)


class ClaudeAdapter(BaseAdapter):
    name = "claude"

    def __init__(self, client=None):
        self.client = client if client is not None else anthropic.AsyncAnthropic(
            base_url=CLAUDE_BASE_URL or None,
            api_key=LLM_API_KEY,
            default_headers={"User-Agent": LLM_USER_AGENT} if LLM_USER_AGENT else None,
            # 与 OpenAI 客户端一致：禁用 SDK 内建重试，重试语义由 chat.stream_reply 统一控制
            max_retries=0,
        )
        self.tools_supported = True
        self.sampling_supported = True
        self.extra_body_supported = True
        self.max_tokens_limit: int | None = None

    async def run_round(self, history, use_tools, on_text) -> RoundResult:
        stream = await self._create_stream(history, use_tools)
        calls, content = await drain_stream(stream, on_text)
        return RoundResult(calls=calls, content=content)

    async def _create_stream(self, history: list[dict], use_tools: bool):
        """建立 Anthropic 原生流。降级链与 OpenAI 兼容路径同一套路。"""
        system_text, messages = to_anthropic_messages(history)
        include_tools = use_tools and self.tools_supported
        sampling = sampling_kwargs() if self.sampling_supported else {}
        extra_body = config.LLM_EXTRA_BODY if self.extra_body_supported else None
        max_tokens = min(MAX_TOKENS, self.max_tokens_limit) if self.max_tokens_limit else MAX_TOKENS
        while True:
            kwargs = {"model": LLM_MODEL, "messages": messages, "stream": True,
                      "max_tokens": max_tokens, **sampling}
            if system_text:
                kwargs["system"] = system_text
            if extra_body:
                kwargs["extra_body"] = extra_body
            if include_tools:
                kwargs["tools"] = anthropic_tools()
            try:
                return await self.client.messages.create(**kwargs)
            except anthropic.BadRequestError as e:
                kind = rejected_param(e, extra_keys=extra_body or ())
                # Opus 4.7+ 已移除采样参数（400），去掉重试并粘性禁用
                if kind == "sampling" and sampling:
                    logger.warning("后端拒绝采样参数，已改用默认值（重启进程后会再次尝试）：%s", e)
                    self.sampling_supported = False
                    sampling = {}
                    continue
                # thinking 等额外参数被拒（如 4.7+ 已移除 budget_tokens）：去掉重试
                if kind == "extra" and extra_body:
                    logger.warning("后端拒绝额外参数 %s，本进程内不再携带（重启后会再次尝试）：%s",
                                   list(extra_body), e)
                    self.extra_body_supported = False
                    extra_body = None
                    continue
                # max_tokens 超过模型输出上限：从报错文案里解析更小上限并粘性记住
                if kind == "max_tokens_limit":
                    nums = [int(n) for n in re.findall(r"\d+", error_text(e))]
                    smaller = [n for n in nums if 0 < n < max_tokens]
                    if not smaller:
                        raise
                    new = max(smaller)
                    logger.warning("后端拒绝 max_tokens=%d，降为 %d 重试：%s", max_tokens, new, e)
                    self.max_tokens_limit = new
                    max_tokens = new
                    continue
                if kind == "tools" and include_tools:
                    logger.warning("后端拒绝 tools 参数，联网搜索已禁用（重启进程后会再次尝试）：%s", e)
                    self.tools_supported = False
                    include_tools = False
                    continue
                raise


def anthropic_tools() -> list[dict]:
    """OpenAI function 工具定义 → Anthropic 原生工具格式。"""
    return [
        {
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "input_schema": t["function"]["parameters"],
        }
        for t in SEARCH_TOOLS
    ]


def to_anthropic_messages(history: list[dict]) -> tuple[str, list[dict]]:
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
                    "input": tool_args(c) or {},
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


async def drain_stream(stream, on_text) -> tuple[dict[int, dict], str]:
    """消费 Anthropic 原生流：text_delta 交给 on_text，tool_use 块按 index 聚合，
    thinking 块（含签名）原样收集、挂到首个调用的扩展字段上以便续传时回传。
    与 OpenAI 流消费相同的空闲看门狗语义。
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
        # 经 assistant_tool_call_msg 进入 extra_content，
        # 续传时由 to_anthropic_messages 取回原样前置
        first = calls[min(calls)]
        first["extra"] = {**(first["extra"] or {}), "anthropic_thinking": thinking_blocks}
    return calls, content
