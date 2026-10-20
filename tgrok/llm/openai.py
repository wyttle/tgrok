"""OpenAI 兼容协议（/chat/completions）适配器。"""

from openai import AsyncOpenAI, BadRequestError

from .. import config
from .base import BaseAdapter, RequestOptions, RoundResult, Usage, watch_idle


class OpenAIAdapter(BaseAdapter):
    name = "openai"
    bad_request = (BadRequestError,)

    def __init__(self, client=None, endpoint=None):
        super().__init__(endpoint)
        self.client = client if client is not None else AsyncOpenAI(
            base_url=self.endpoint.base_url,
            api_key=self.endpoint.api_key,
            default_headers={"User-Agent": config.LLM_USER_AGENT} if config.LLM_USER_AGENT else None,
            # 禁用 SDK 内建重试：它会按服务端 Retry-After 静默睡 60s+，期间用户只能干等。
            # 重试语义由 chat 统一控制（零输出重试一次、429 不重试）
            max_retries=0,
        )
        self.token_param = "max_tokens"
        self.usage_supported = True  # stream_options.include_usage：流的最后一块带 token 用量

    async def _open(self, history: list[dict], opts: RequestOptions):
        kwargs = {"model": self.model, "messages": history, "stream": True,
                  self.token_param: config.MAX_TOKENS, **opts.sampling}
        if self.usage_supported:
            kwargs["stream_options"] = {"include_usage": True}
        if opts.extra_body:
            kwargs["extra_body"] = opts.extra_body
        if opts.tools:
            kwargs["tools"] = opts.tools
            kwargs["tool_choice"] = "auto"
        return await self.client.chat.completions.create(**kwargs)

    def _degrade(self, e, kind, opts) -> bool:
        # OpenAI 官方较新的模型要求用 max_completion_tokens 代替 max_tokens
        if kind == "token_param" and self.token_param == "max_tokens":
            self.token_param = "max_completion_tokens"
            return True
        if kind == "stream_options" and self.usage_supported:
            self.usage_supported = False
            return True
        return super()._degrade(e, kind, opts)

    async def _drain(self, stream, on_text) -> RoundResult:
        usage = Usage()
        calls, content = await drain_stream(stream, on_text, usage)
        return RoundResult(calls=calls, content=content, usage=usage)


async def drain_stream(stream, on_text, usage: Usage | None = None) -> tuple[dict[int, dict], str]:
    """消费流式响应：正文片段逐个交给 on_text，tool_call 片段按 index 聚合，后端报的 token 用量写进 usage。
    返回（聚合后的 tool_calls, 本轮完整正文）。"""
    calls: dict[int, dict] = {}
    content = ""
    async for chunk in watch_idle(stream, lambda: bool(content) and not calls, "LLM"):
        reported = getattr(chunk, "usage", None)
        if usage is not None and reported is not None:
            usage.input = reported.prompt_tokens or 0
            usage.output = reported.completion_tokens or 0
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
