"""LLM 接入层：按 config.LLM_PROTOCOL 装配协议适配器。"""

from .. import config
from . import base
from .base import (
    RoundResult, SEARCH_TOOLS, assistant_tool_call_msg, is_quota_error, tool_args,
)


def _make_adapter():
    if config.LLM_PROTOCOL == "claude":
        from .claude import ClaudeAdapter
        return ClaudeAdapter()
    if config.LLM_PROTOCOL == "gemini":
        from .gemini import GeminiAdapter
        return GeminiAdapter()
    from .openai import OpenAIAdapter
    return OpenAIAdapter()


adapter = _make_adapter()
