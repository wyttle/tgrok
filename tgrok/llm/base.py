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


def error_text(e: BaseException) -> str:
    """提取结构化错误正文并转为小写；没有结构化 body 时回退异常文本。"""
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        detail = body.get("error")
        if isinstance(detail, dict):
            parts = [detail.get("message"), detail.get("param"), detail.get("type"), detail.get("code")]
        else:
            parts = [body.get("message"), body.get("param"), body.get("type"), body.get("code")]
        text = " ".join(str(part) for part in parts if part is not None)
        if text:
            return text.lower()
    return str(e).lower()


def error_param(e: BaseException) -> str:
    """提取 OpenAI 结构化错误中的 param；Anthropic 等无此字段时返回空串。"""
    body = getattr(e, "body", None)
    if not isinstance(body, dict):
        return ""
    detail = body.get("error")
    source = detail if isinstance(detail, dict) else body
    param = source.get("param")
    return str(param).lower() if param is not None else ""


_REJECT_HINTS = (
    "unsupported", "not supported", "does not support", "unrecognized",
    "unknown parameter", "no such parameter", "invalid parameter",
    "not allowed", "not available",
)
_TOOL_ERROR_CODES = {"unknown_parameter", "unsupported_parameter"}


def _error_code(e: BaseException) -> str:
    body = getattr(e, "body", None)
    if not isinstance(body, dict):
        return ""
    detail = body.get("error")
    source = detail if isinstance(detail, dict) else body
    code = source.get("code")
    return str(code).lower() if code is not None else ""


def _param_matches(param: str, name: str) -> bool:
    return param == name or param.startswith(name + ".")


def rejected_param(e: BaseException, extra_keys=()) -> str | None:
    """把 400 分类为可安全降级的参数拒绝；普通业务错误返回 None。"""
    text = error_text(e)
    if "thought_signature" in text:
        return None
    param = error_param(e)
    rejected = any(hint in text for hint in _REJECT_HINTS)

    if "max_completion_tokens" in text:
        return "token_param"
    if any(_param_matches(param, name) for name in ("temperature", "top_p")):
        return "sampling"
    if not param and rejected and any(name in text for name in ("temperature", "top_p")):
        return "sampling"

    keys = tuple(str(key).lower() for key in extra_keys)
    if any(_param_matches(param, key) for key in keys):
        return "extra"
    if not param and rejected and any(key in text for key in keys):
        return "extra"

    if _param_matches(param, "max_tokens"):
        return "max_tokens_limit"
    if not param and "max_tokens" in text:
        return "max_tokens_limit"

    tool_param = (
        param == "tools" or param.startswith("tools.") or param.startswith("tools[")
        or param == "tool_choice" or param.startswith("tool_choice.")
    )
    tool_rejected = rejected or _error_code(e) in _TOOL_ERROR_CODES
    if tool_param and tool_rejected:
        return "tools"
    if not param and rejected and ("tool" in text or "function calling" in text):
        return "tools"
    return None


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
