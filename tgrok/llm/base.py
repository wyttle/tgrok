"""协议适配器共享接口、工具定义与辅助函数。"""

import json
from dataclasses import dataclass, field

from .. import config


@dataclass
class RoundResult:
    """一轮生成的产物。calls 槽位格式与现有流消费逻辑完全一致。"""
    calls: dict[int, dict] = field(default_factory=dict)
    content: str = ""
    citations: list[dict] = field(default_factory=list)


class BaseAdapter:
    """协议适配器：一轮 = 建流 + 消费流。粘性降级状态放实例属性上。"""
    name: str = ""
    supports_tool_loop: bool = True

    async def run_round(self, history: list[dict], use_tools: bool, on_text) -> RoundResult:
        raise NotImplementedError


WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            # grounding 模式下 web_search 是深度调研代理，引导主模型合并任务而非拆散小查询
            (
                "Deep research agent backed by Google Search. Give it ONE comprehensive "
                "research task in natural language — it can cover several aspects at once, "
                "and the agent will run multiple Google searches internally and return a "
                "fact summary with sources. Do NOT split one round of research into many "
                "narrow queries; one well-phrased task per round is enough."
            )
            if config.GEMINI_SEARCH_MODEL
            else (
                "Search the web for up-to-date information. Use this for recent events, "
                "time-sensitive facts, or anything you are unsure about."
            )
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The research task or question, in the language most likely to find good results."
                        if config.GEMINI_SEARCH_MODEL
                        else "The search query, in the language most likely to find good results."
                    ),
                }
            },
            "required": ["query"],
        },
    },
}

FETCH_URL_TOOL = {
    "type": "function",
    "function": {
        "name": "open_url",
        "description": (
            "Fetch a web page by URL and return its readable text. "
            "Use it to read the details behind links found via web_search."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full http(s) URL of the page to read.",
                }
            },
            "required": ["url"],
        },
    },
}

SEARCH_TOOLS = [WEB_SEARCH_TOOL, FETCH_URL_TOOL]


def sampling_kwargs() -> dict:
    """按配置组装 temperature/top_p；走 config 模块属性读取，便于测试与运行时调整。"""
    kw = {}
    if config.LLM_TEMPERATURE is not None:
        kw["temperature"] = config.LLM_TEMPERATURE
    if config.LLM_TOP_P is not None:
        kw["top_p"] = config.LLM_TOP_P
    return kw


def assistant_tool_call_msg(calls: dict[int, dict], content: str) -> dict:
    """把聚合好的 tool_call 片段组装成请求格式的 assistant 消息。"""
    tool_calls = []
    for i, slot in sorted(calls.items()):
        call = {
            # 部分本地后端不回 id：合成一个，并在 tool 结果里复用以保持配对
            "id": slot["id"] or f"call_{i}",
            "type": "function",
            "function": {
                "name": slot["name"] or "web_search",
                "arguments": slot["arguments"] or "{}",
            },
        }
        if slot.get("extra"):
            # 回传 Gemini 思考型模型的 thought_signature 等厂商扩展字段
            call["extra_content"] = slot["extra"]
        tool_calls.append(call)
    return {"role": "assistant", "content": content or "", "tool_calls": tool_calls}


def is_quota_error(e: BaseException) -> bool:
    """配额/限流类错误（429）：重试大概率无效且浪费配额，需单独处理。"""
    s = str(e)
    return "429" in s or "RESOURCE_EXHAUSTED" in s or "rate limit" in s.lower()


def tool_args(call: dict) -> dict | None:
    """解析 tool_call 的参数；arguments 不是合法 JSON 对象时返回 None。"""
    try:
        args = json.loads(call["function"]["arguments"])
    except (json.JSONDecodeError, TypeError):
        return None
    return args if isinstance(args, dict) else None
