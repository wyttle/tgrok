"""LLM 接入层：按配置装配主模型与可选备用模型的协议适配器。"""

from .. import config
from . import base
from .base import (
    RoundResult, active_tools, assistant_tool_call_msg, is_quota_error, tool_args,
)


def make_adapter(endpoint: config.LLMEndpoint):
    if endpoint.protocol == "claude":
        from .claude import ClaudeAdapter
        return ClaudeAdapter(endpoint=endpoint)
    if endpoint.protocol == "responses":
        from .responses import ResponsesAdapter
        return ResponsesAdapter(endpoint=endpoint)
    if endpoint.protocol == "gemini":
        from .gemini import GeminiAdapter
        return GeminiAdapter(endpoint=endpoint)
    from .openai import OpenAIAdapter
    return OpenAIAdapter(endpoint=endpoint)


adapter = make_adapter(config.primary_endpoint())
# 主模型出错且尚无正文输出时，本条回复改用它；未配置时为 None
_fallback = config.fallback_endpoint()
fallback_adapter = make_adapter(_fallback) if _fallback is not None else None
