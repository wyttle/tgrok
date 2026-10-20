"""LLM 接入层：按配置装配主模型、可选备用模型和 Gemini 帮手模型的协议适配器。"""

from .. import config
from .base import RoundResult, assistant_tool_call_msg, is_quota_error, tool_args

__all__ = ["RoundResult", "assistant_tool_call_msg", "is_quota_error", "tool_args",
           "make_adapter", "adapter", "fallback_adapter", "helper_adapter"]


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
# 抢答和后台杂活（记忆整理）用的 Gemini 原生模型；未配置时为 None
_helper = config.helper_endpoint()
helper_adapter = make_adapter(_helper) if _helper is not None else None
