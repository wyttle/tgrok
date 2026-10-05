"""OpenAI 兼容协议适配器。"""

import asyncio
import logging

from openai import AsyncOpenAI, BadRequestError

from .. import config
from ..config import LLM_USER_AGENT, MAX_TOKENS
from .base import BaseAdapter, RoundResult, active_tools, rejected_param, sampling_kwargs

logger = logging.getLogger(__name__)


class OpenAIAdapter(BaseAdapter):
    name = "openai"

    def __init__(self, client=None, endpoint=None):
        self.endpoint = endpoint or config.primary_endpoint()
        self.model = self.endpoint.model
        self.client = client if client is not None else AsyncOpenAI(
            base_url=self.endpoint.base_url,
            api_key=self.endpoint.api_key,
            default_headers={"User-Agent": LLM_USER_AGENT} if LLM_USER_AGENT else None,
            # 禁用 SDK 内建重试：它会按服务端 Retry-After 静默睡 60s+，期间用户只能干等。
            # 重试语义由 chat.stream_reply 统一控制（零输出重试一次、429 不重试）
            max_retries=0,
        )
        self.tools_supported = True
        self.sampling_supported = True
        self.extra_body_supported = True
        self.token_param = "max_tokens"

    async def run_round(self, history, use_tools, on_text) -> RoundResult:
        stream = await self._create_stream(history, use_tools)
        calls, content = await drain_stream(stream, on_text)
        return RoundResult(calls=calls, content=content)

    async def _create_stream(self, history: list[dict], use_tools: bool):
        include_tools = use_tools and self.tools_supported
        sampling = sampling_kwargs() if self.sampling_supported else {}
        extra_body = self.endpoint.extra_body if self.extra_body_supported else None
        while True:
            kwargs = {
                "model": self.model, "messages": history, "stream": True,
                self.token_param: MAX_TOKENS, **sampling,
            }
            if extra_body:
                kwargs["extra_body"] = extra_body
            if include_tools:
                kwargs["tools"] = active_tools()
                kwargs["tool_choice"] = "auto"
            try:
                return await self.client.chat.completions.create(**kwargs)
            except BadRequestError as e:
                kind = rejected_param(e, extra_keys=extra_body or ())
                # OpenAI 官方较新的模型要求用 max_completion_tokens 代替 max_tokens
                if kind == "token_param" and self.token_param == "max_tokens":
                    self.token_param = "max_completion_tokens"
                    continue
                # 推理类模型常只接受默认采样值：去掉 temperature/top_p 重试并粘性禁用
                if kind == "sampling" and sampling:
                    logger.warning("后端拒绝采样参数，已改用后端默认值（重启进程后会再次尝试）：%s", e)
                    self.sampling_supported = False
                    sampling = {}
                    continue
                # 厂商私有参数不被当前后端接受：去掉重试并粘性禁用
                if kind == "extra" and extra_body:
                    logger.warning("后端拒绝额外参数 %s，本进程内不再携带（重启后会再次尝试）：%s",
                                   list(extra_body), e)
                    self.extra_body_supported = False
                    extra_body = None
                    continue
                # 后端明确不支持 function calling 时才禁用搜索；schema 与续传错误必须原样抛出
                if kind == "tools" and include_tools:
                    logger.warning("后端拒绝 tools 参数，联网搜索已禁用（重启进程后会再次尝试）：%s", e)
                    self.tools_supported = False
                    include_tools = False
                    continue
                raise


async def drain_stream(stream, on_text) -> tuple[dict[int, dict], str]:
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
