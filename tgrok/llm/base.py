"""协议适配器共享接口与辅助函数。"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from .. import config

logger = logging.getLogger(__name__)


@dataclass
class RoundResult:
    """一轮生成的产物。calls 是按序号聚合的工具调用槽位 {id, name, arguments, extra}。"""
    calls: dict[int, dict] = field(default_factory=dict)
    content: str = ""
    citations: list[dict] = field(default_factory=list)


@dataclass
class RequestOptions:
    """一次建流请求的可选部分；后端拒绝某一项时去掉它重试。"""
    tools: list[dict] | None
    sampling: dict
    extra_body: dict | None


class BaseAdapter:
    """协议适配器：一轮 = 建流 + 消费流。

    建流时后端明确拒绝采样参数、额外参数或 tools（400）时，去掉那一项重试，并记在实例上，
    本进程内这个适配器不再发送（重启后会再次尝试）。协议特有的降级在子类的 _degrade 里补充。
    """
    name: str = ""
    supports_tool_loop: bool = True
    bad_request: tuple[type[BaseException], ...] = ()  # SDK 的 400 异常类型

    def __init__(self, endpoint: config.LLMEndpoint | None = None):
        self.endpoint = endpoint or config.primary_endpoint()
        self.model = self.endpoint.model
        self.tools_supported = True
        self.sampling_supported = True
        self.extra_body_supported = True

    async def run_round(self, history: list[dict], tools: list[dict] | None, on_text) -> RoundResult:
        """跑一轮：tools 是 OpenAI function 格式的工具定义，None 表示这轮不挂工具。"""
        stream = await self._create_stream(history, tools)
        return await self._drain(stream, on_text)

    async def _create_stream(self, history: list[dict], tools: list[dict] | None):
        opts = RequestOptions(
            tools=tools if tools and self.tools_supported else None,
            sampling=sampling_kwargs() if self.sampling_supported else {},
            extra_body=self.endpoint.extra_body if self.extra_body_supported else None,
        )
        while True:
            try:
                return await self._open(history, opts)
            except self.bad_request as e:
                if not self._degrade(e, rejected_param(e, extra_keys=opts.extra_body or ()), opts):
                    raise

    async def _open(self, history: list[dict], opts: RequestOptions):
        """按 opts 发起流式请求，返回 SDK 的流对象。"""
        raise NotImplementedError

    async def _drain(self, stream, on_text) -> RoundResult:
        raise NotImplementedError

    def _degrade(self, e: BaseException, kind: str | None, opts: RequestOptions) -> bool:
        """处理一次 400：能去掉某个参数重试就改 opts 并返回 True；业务错误返回 False 原样抛出。"""
        if kind == "sampling" and opts.sampling:
            logger.warning("后端拒绝采样参数，改用后端默认值（重启进程后会再次尝试）：%s", e)
            self.sampling_supported, opts.sampling = False, {}
            return True
        if kind == "extra" and opts.extra_body:
            logger.warning("后端拒绝额外参数 %s，本进程内不再携带（重启后会再次尝试）：%s", list(opts.extra_body), e)
            self.extra_body_supported, opts.extra_body = False, None
            return True
        if kind == "tools" and opts.tools:
            # 只有后端明确不支持 function calling 才禁用；schema 和续传错误是业务 400，必须原样抛出
            logger.warning("后端拒绝 tools 参数，工具调用已禁用（重启进程后会再次尝试）：%s", e)
            self.tools_supported, opts.tools = False, None
            return True
        return False


async def watch_idle(stream, quiet_ok: Callable[[], bool], label: str) -> AsyncIterator:
    """逐个产出流里的事件。

    空闲看门狗：quiet_ok() 为真（已有正文、且没有聚合到一半的工具调用）时，超过
    config.STREAM_IDLE_TIMEOUT 秒没有新数据就视为生成完成、主动收尾——部分网关发完内容后
    不发结束帧，流会空挂到上游超时。
    """
    it = stream.__aiter__()
    while True:
        timeout = config.STREAM_IDLE_TIMEOUT
        try:
            if timeout > 0 and quiet_ok():
                item = await asyncio.wait_for(anext(it), timeout)
            else:
                item = await anext(it)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            logger.warning("%s 流已有正文但 %.0fs 无新数据，视为完成", label, timeout)
            close = getattr(stream, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass
            return
        yield item


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
