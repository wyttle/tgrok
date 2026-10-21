"""Claude 原生协议（Anthropic Messages API）适配器。"""

import logging
import re

import anthropic

from .. import config
from .base import BaseAdapter, RequestOptions, RoundResult, Usage, close_stream, error_text, tool_args, watch_idle

logger = logging.getLogger(__name__)
_DATA_URL_RE = re.compile(r"^data:([^;]+);base64,(.*)$", re.S)
# 开了 thinking 时最多发几次请求：前几次要求流先出思考块，最后一次不再检查、照单全收
THINKING_ATTEMPTS = 4


class ThinkingSkipped(Exception):
    """要了 thinking，流的第一个内容块却不是思考块。"""


class ClaudeAdapter(BaseAdapter):
    name = "claude"
    bad_request = (anthropic.BadRequestError,)

    def __init__(self, client=None, endpoint=None):
        super().__init__(endpoint)
        self.client = client if client is not None else anthropic.AsyncAnthropic(
            base_url=self.endpoint.base_url or None,
            api_key=self.endpoint.api_key,
            default_headers={"User-Agent": config.LLM_USER_AGENT} if config.LLM_USER_AGENT else None,
            max_retries=0,  # 与 OpenAI 客户端一致：重试语义由 chat 统一控制
        )
        self.max_tokens_limit: int | None = None  # 后端报出的输出上限，比 MAX_TOKENS 小时用它

    def _max_tokens(self) -> int:
        return min(config.MAX_TOKENS, self.max_tokens_limit) if self.max_tokens_limit else config.MAX_TOKENS

    async def _open(self, history: list[dict], opts: RequestOptions):
        system_text, messages = to_anthropic_messages(history)
        kwargs = {"model": self.model, "messages": messages, "stream": True,
                  "max_tokens": self._max_tokens(), **opts.sampling}
        if system_text:
            kwargs["system"] = system_text
        if opts.extra_body:
            kwargs["extra_body"] = opts.extra_body
        if opts.tools:
            kwargs["tools"] = anthropic_tools(opts.tools)
        return await self.client.messages.create(**kwargs)

    def _degrade(self, e, kind, opts) -> bool:
        # max_tokens 超过模型输出上限：从报错文案里解析更小的上限并记住
        if kind == "max_tokens_limit":
            current = self._max_tokens()
            smaller = [n for n in map(int, re.findall(r"\d+", error_text(e))) if 0 < n < current]
            if not smaller:
                return False
            self.max_tokens_limit = max(smaller)
            logger.warning("后端拒绝 max_tokens=%d，降为 %d 重试：%s", current, self.max_tokens_limit, e)
            return True
        return super()._degrade(e, kind, opts)

    def _wants_thinking(self) -> bool:
        thinking = (self.endpoint.extra_body or {}).get("thinking") if self.extra_body_supported else None
        return isinstance(thinking, dict) and thinking.get("type") not in (None, "disabled")

    async def run_round(self, history: list[dict], tools: list[dict] | None, on_text) -> RoundResult:
        # 有的网关（cpa）不时悄悄丢掉 thinking 参数，模型不思考就直接出字，常把提示词碎片、说话人标签或
        # 英文推理当正文吐出来。这种流第一个块就认得出，那时还没有字交给 on_text：断开重发。
        for attempt in range(1, THINKING_ATTEMPTS + 1):
            stream = await self._create_stream(history, tools)
            try:
                return await self._drain(stream, on_text, attempt < THINKING_ATTEMPTS and self._wants_thinking())
            except ThinkingSkipped:
                logger.warning("Claude 流没有先思考（第 %d 次），断开重发", attempt)
            finally:
                await close_stream(stream)

    async def _drain(self, stream, on_text, require_thinking: bool = False) -> RoundResult:
        usage = Usage()
        calls, content = await drain_stream(stream, on_text, usage, require_thinking)
        return RoundResult(calls=calls, content=content, usage=usage)


def anthropic_tools(tools: list[dict]) -> list[dict]:
    """OpenAI function 工具定义 → Anthropic 原生工具格式。"""
    return [
        {
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "input_schema": t["function"]["parameters"],
        }
        for t in tools
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
                _append(out, role, [{"type": "text", "text": content}])
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
                _append(out, role, blocks)
    return system_text, out


def _append(out: list[dict], role: str, blocks: list[dict]) -> None:
    """普通消息：紧跟在工具结果后的 user 消息（比如「直接作答」的提示）并进同一条，保持 role 交替；
    其余情况单独一条，纯文本仍用字符串形式。"""
    last = out[-1] if out else None
    if (role == "user" and last and last["role"] == "user" and isinstance(last["content"], list)
            and last["content"] and last["content"][0].get("type") == "tool_result"):
        last["content"].extend(blocks)
    elif len(blocks) == 1 and blocks[0]["type"] == "text":
        out.append({"role": role, "content": blocks[0]["text"]})
    else:
        out.append({"role": role, "content": blocks})


def _take_usage(reported, usage: Usage) -> None:
    """message_start 带输入（含缓存）用量，message_delta 带累计输出；有的网关把输入也放在 message_delta 里。"""
    if reported is None:
        return
    total_in = sum(getattr(reported, k, None) or 0
                   for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    usage.input = max(usage.input, total_in)
    usage.output = max(usage.output, getattr(reported, "output_tokens", None) or 0)


async def drain_stream(stream, on_text, usage: Usage | None = None,
                       require_thinking: bool = False) -> tuple[dict[int, dict], str]:
    """消费 Anthropic 原生流：text_delta 交给 on_text，tool_use 块按 index 聚合，
    thinking 块（含签名）原样收集、挂到首个调用的扩展字段上以便续传时回传；token 用量写进 usage。
    require_thinking 时第一个内容块不是思考块就抛 ThinkingSkipped（此时还没有正文交给 on_text）。
    """
    calls: dict[int, dict] = {}
    content = ""
    thinking_blocks: list[dict] = []
    cur_think: dict | None = None
    usage = usage if usage is not None else Usage()
    async for event in watch_idle(stream, lambda: bool(content) and not calls, "Claude"):
        etype = getattr(event, "type", "")
        if etype == "message_start":
            _take_usage(getattr(getattr(event, "message", None), "usage", None), usage)
        elif etype == "message_delta":
            _take_usage(getattr(event, "usage", None), usage)
        elif etype == "content_block_start":
            block = event.content_block
            btype = getattr(block, "type", "")
            if require_thinking and btype not in ("thinking", "redacted_thinking"):
                raise ThinkingSkipped(btype)
            require_thinking = False
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
                if text and require_thinking:  # 没有块开头就直接来正文，同样是没思考
                    raise ThinkingSkipped("text")
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
