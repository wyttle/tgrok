"""OpenAI Responses API 适配器（/v1/responses）。

GPT-6.1 Sol 等新模型在 Chat Completions 上不支持工具调用，工具循环必须走 Responses。
对外仍然接收和产出与其它适配器相同的 OpenAI chat 格式历史，格式转换只发生在本模块内。

无状态续传：默认 store=False 并请求 reasoning.encrypted_content，推理条目原样随
工具结果回传，不依赖服务端保存会话；后端不认这两个参数时去掉重试，并不再回传推理条目。
"""

import asyncio
import logging

from openai import AsyncOpenAI, BadRequestError

from .. import config
from ..config import LLM_USER_AGENT, MAX_TOKENS
from .base import BaseAdapter, RoundResult, active_tools, error_param, error_text, rejected_param, sampling_kwargs

logger = logging.getLogger(__name__)


class ResponsesAdapter(BaseAdapter):
    name = "responses"

    def __init__(self, client=None, endpoint=None):
        self.endpoint = endpoint or config.primary_endpoint()
        self.model = self.endpoint.model
        self.client = client if client is not None else AsyncOpenAI(
            base_url=self.endpoint.base_url,
            api_key=self.endpoint.api_key,
            default_headers={"User-Agent": LLM_USER_AGENT} if LLM_USER_AGENT else None,
            # 与其它适配器一致：禁用 SDK 内建重试，重试语义由 chat.stream_reply 统一控制
            max_retries=0,
        )
        self.tools_supported = True
        self.sampling_supported = True
        self.extra_body_supported = True
        self.stateless_supported = True

    async def run_round(self, history, use_tools, on_text) -> RoundResult:
        stream = await self._create_stream(history, use_tools)
        calls, content = await drain_stream(stream, on_text)
        return RoundResult(calls=calls, content=content)

    async def _create_stream(self, history: list[dict], use_tools: bool):
        include_tools = use_tools and self.tools_supported
        sampling = sampling_kwargs() if self.sampling_supported else {}
        extra_body = self.endpoint.extra_body if self.extra_body_supported else None
        while True:
            stateless = self.stateless_supported
            instructions, items = to_responses_input(history, keep_reasoning=stateless)
            kwargs = {"model": self.model, "input": items, "stream": True,
                      "max_output_tokens": MAX_TOKENS, **sampling}
            if instructions:
                kwargs["instructions"] = instructions
            if stateless:
                kwargs["store"] = False
                kwargs["include"] = ["reasoning.encrypted_content"]
            if extra_body:
                kwargs["extra_body"] = extra_body
            if include_tools:
                kwargs["tools"] = responses_tools()
                kwargs["tool_choice"] = "auto"
            try:
                return await self.client.responses.create(**kwargs)
            except BadRequestError as e:
                kind = rejected_param(e, extra_keys=extra_body or ())
                # 推理模型不接受采样参数：去掉重试并粘性禁用
                if kind == "sampling" and sampling:
                    logger.warning("后端拒绝采样参数，已改用后端默认值（重启进程后会再次尝试）：%s", e)
                    self.sampling_supported = False
                    sampling = {}
                    continue
                if kind == "extra" and extra_body:
                    logger.warning("后端拒绝额外参数 %s，本进程内不再携带（重启后会再次尝试）：%s",
                                   list(extra_body), e)
                    self.extra_body_supported = False
                    extra_body = None
                    continue
                if kind == "tools" and include_tools:
                    logger.warning("后端拒绝 tools 参数，工具调用已禁用（重启进程后会再次尝试）：%s", e)
                    self.tools_supported = False
                    include_tools = False
                    continue
                if stateless and _rejects_stateless(e):
                    logger.warning("后端不支持 store/include 参数，改为不回传推理内容（重启后会再次尝试）：%s", e)
                    self.stateless_supported = False
                    continue
                raise


def _rejects_stateless(e: BaseException) -> bool:
    param = error_param(e)
    if param in ("store", "include") or param.startswith("include["):
        return True
    text = error_text(e)
    return "encrypted_content" in text or "'store'" in text or "'include'" in text


def responses_tools() -> list[dict]:
    """chat 格式的 function 工具 → Responses 的扁平格式。
    strict 显式关掉：Responses 默认按严格模式校验 schema，现有 schema 不满足其要求。"""
    return [
        {
            "type": "function",
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "parameters": t["function"]["parameters"],
            "strict": False,
        }
        for t in active_tools()
    ]


def to_responses_input(history: list[dict], keep_reasoning: bool = True) -> tuple[str, list[dict]]:
    """OpenAI chat 格式历史 → Responses 的 (instructions, input 条目)。

    system 抽成 instructions；多模态图片转 input_image；assistant 的 tool_calls 还原为
    function_call 条目（不带条目 id，避免要求服务端存有对应推理条目），其前置推理条目
    从 extra_content 取回；tool 结果转 function_call_output。
    """
    instructions, items = "", []
    for m in history:
        role, content = m["role"], m.get("content", "")
        if role == "system":
            if isinstance(content, str):
                instructions = content
        elif role == "assistant" and m.get("tool_calls"):
            if keep_reasoning:
                extra = m["tool_calls"][0].get("extra_content") or {}
                items.extend(extra.get("responses_reasoning") or [])
            if isinstance(content, str) and content:
                items.append({"role": "assistant", "content": content})
            for c in m["tool_calls"]:
                items.append({
                    "type": "function_call",
                    "call_id": c["id"],
                    "name": c["function"]["name"],
                    "arguments": c["function"]["arguments"] or "{}",
                })
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": m["tool_call_id"],
                          "output": m["content"]})
        elif isinstance(content, str):
            if content:
                items.append({"role": role, "content": content})
        else:
            parts = []
            for p in content:
                if p.get("type") == "text":
                    parts.append({"type": "input_text", "text": p["text"]})
                elif p.get("type") == "image_url":
                    url = (p.get("image_url") or {}).get("url", "")
                    if url:
                        parts.append({"type": "input_image", "image_url": url})
            if parts:
                items.append({"role": role, "content": parts})
    return instructions, items


def _reasoning_input(item) -> dict | None:
    """流里拿到的推理条目 → 可回传的输入条目，只保留输入 schema 接受的字段。
    store=False 时服务端不保存推理条目，没有 encrypted_content 的条目回传只会报找不到，直接丢弃。"""
    encrypted = getattr(item, "encrypted_content", None)
    if not encrypted:
        return None
    return {"type": "reasoning", "id": getattr(item, "id", None) or "",
            "encrypted_content": encrypted,
            "summary": [s.model_dump() if hasattr(s, "model_dump") else s
                        for s in (getattr(item, "summary", None) or [])]}


async def drain_stream(stream, on_text) -> tuple[dict[int, dict], str]:
    """消费 Responses 事件流：正文增量交给 on_text，function_call 按 output_index 聚合，
    推理条目收集后挂到首个调用的扩展字段，续传时由 to_responses_input 取回。
    与其它协议相同的空闲看门狗语义。
    """
    calls: dict[int, dict] = {}
    content = ""
    reasoning: list[dict] = []
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
                "Responses 流已有正文但 %.0fs 无新数据，视为完成（正文 %d 字）",
                config.STREAM_IDLE_TIMEOUT, len(content),
            )
            try:
                await stream.close()
            except Exception:
                pass
            break
        etype = getattr(event, "type", "")
        if etype in ("response.output_text.delta", "response.refusal.delta"):
            text = getattr(event, "delta", "") or ""
            if text:
                content += text
                await on_text(text)
        elif etype == "response.output_item.added":
            item = event.item
            if getattr(item, "type", "") == "function_call":
                calls[event.output_index] = {
                    "id": getattr(item, "call_id", "") or "",
                    "name": getattr(item, "name", "") or "",
                    "arguments": getattr(item, "arguments", "") or "",
                    "extra": None,
                }
        elif etype == "response.function_call_arguments.delta":
            slot = calls.get(event.output_index)
            if slot is not None:
                slot["arguments"] += getattr(event, "delta", "") or ""
        elif etype == "response.output_item.done":
            item = event.item
            itype = getattr(item, "type", "")
            if itype == "function_call":
                slot = calls.setdefault(event.output_index,
                                        {"id": "", "name": "", "arguments": "", "extra": None})
                slot["id"] = getattr(item, "call_id", "") or slot["id"]
                slot["name"] = getattr(item, "name", "") or slot["name"]
                if getattr(item, "arguments", ""):
                    slot["arguments"] = item.arguments  # 以定稿为准，增量可能被网关合并或截断
            elif itype == "reasoning":
                passback = _reasoning_input(item)
                if passback is not None:
                    reasoning.append(passback)
        elif etype in ("response.completed", "response.incomplete"):
            # incomplete 多为 max_output_tokens 用尽：保留已有正文，按完成处理
            if etype == "response.incomplete":
                logger.warning("Responses 生成未完整结束（多为 MAX_TOKENS 用尽），正文 %d 字", len(content))
            break
        elif etype == "response.failed":
            err = getattr(getattr(event, "response", None), "error", None)
            raise RuntimeError(f"Responses 生成失败：{getattr(err, 'code', '')} {getattr(err, 'message', '')}")
        elif etype == "error":
            raise RuntimeError(f"Responses 流错误：{getattr(event, 'code', '')} {getattr(event, 'message', '')}")
    if calls and reasoning:
        first = calls[min(calls)]
        first["extra"] = {**(first["extra"] or {}), "responses_reasoning": reasoning}
    return calls, content
