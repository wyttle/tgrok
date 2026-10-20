"""bot 自带的工具。

每个工具的定义（给模型的 schema）、执行、进度显示文案和系统提示词说明都写在它自己的模块里，
在这里的 REGISTRY 登记一次。chat 和 prompt 只通过本模块的函数和工具打交道，加一个工具不用改别处。
"""

import asyncio
import contextvars
import time
from dataclasses import dataclass, field

from .. import llm
from ..i18n import t


@dataclass
class Outcome:
    content: str  # 回灌给模型的文本
    ok: bool
    summary: str  # 进度里工具行下面的结果行：几条结果、多少字、= 结果、失败


def failed(content: str) -> Outcome:
    return Outcome(content, False, t("res_failed"))


def human_chars(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


class Tool:
    name: str = ""
    description: str = ""
    parameters: dict = {}

    def enabled(self) -> bool:
        return True

    def schema(self) -> dict:
        """OpenAI function 格式；各协议适配器自己转换成原生格式。"""
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}

    def prompt_note(self) -> str:
        """挂载这个工具时追加到系统提示词的说明；空串表示不追加。"""
        return ""

    def label(self, args: dict) -> str:
        """进度里这次调用的一行，例如「搜索: xxx」。不显示网址。"""
        return self.name

    def batch_label(self, args_list: list[dict]) -> str | None:
        """同一轮里这个工具的全部调用合并成一行时返回那一行；None 表示每次调用各占一行。"""
        return None

    def batch_summary(self, outcomes: list[Outcome]) -> str:
        return outcomes[0].summary

    async def run(self, args: dict, chat_id: int | None) -> Outcome:
        raise NotImplementedError

    async def run_batch(self, args_list: list[dict], chat_id: int | None) -> list[Outcome]:
        """同一轮里这个工具的全部调用，默认并发执行；每个调用在自己的任务里，show_detail 写到它自己的进度行。"""
        lines = _batch_lines.get()

        async def one(i: int, args: dict) -> Outcome:
            if lines is not None:
                _line.set(lines[i])
            return await self.run(args, chat_id)

        return list(await asyncio.gather(*(one(i, a) for i, a in enumerate(args_list))))


@dataclass
class ProgressLine:
    text: str
    result: str | None = None
    started: float = field(default_factory=time.monotonic)  # 进度里给还在执行的工具计时
    detail: list[str] = field(default_factory=list)  # 执行中的实时明细（如每个研究员在做什么），出结果后不再显示


_batch_lines: contextvars.ContextVar[list[ProgressLine] | None] = contextvars.ContextVar("tool_batch_lines", default=None)
_line: contextvars.ContextVar[ProgressLine | None] = contextvars.ContextVar("tool_line", default=None)


def show_detail(rows: list[str]) -> None:
    """工具执行期间更新自己那一行下面的实时明细；气泡下一次刷新进度时显示。不在工具调用里时什么也不做。"""
    line = _line.get()
    if line is not None:
        line.detail = list(rows)


def _registry() -> dict[str, Tool]:
    from .calculate import CalculateTool
    from .recall import RecallTool
    from .remember import RememberTool
    from .remember_slang import RememberSlangTool
    from .research import ResearchTool
    from .send_image import SendImageTool
    from .watch import WatchMediaTool
    from .web import OpenUrlTool, WebSearchTool
    tools = [CalculateTool(), RememberSlangTool(), RememberTool(), RecallTool(), WatchMediaTool(), WebSearchTool(),
             OpenUrlTool(), SendImageTool(), ResearchTool()]
    return {tool.name: tool for tool in tools}


REGISTRY: dict[str, Tool] = _registry()


def active() -> list[Tool]:
    return [tool for tool in REGISTRY.values() if tool.enabled()]


def schemas() -> list[dict]:
    return [tool.schema() for tool in active()]


def prompt_notes() -> list[str]:
    return [note for note in (tool.prompt_note() for tool in active()) if note]


@dataclass
class RoundPlan:
    """一轮工具调用：先拿 lines 显示进度，再 execute() 执行并把结果行填进 lines。"""
    calls: list[dict]
    chat_id: int | None
    lines: list[ProgressLine] = field(default_factory=list)
    _line_of: list[ProgressLine] = field(default_factory=list)
    _args: list[dict | None] = field(default_factory=list)

    async def execute(self) -> list[dict]:
        """并发执行全部调用，按原顺序返回 tool 消息。"""
        outcomes: list[Outcome | None] = [None] * len(self.calls)
        groups: dict[str, list[int]] = {}
        for i, call in enumerate(self.calls):
            name = call["function"]["name"]
            if self._args[i] is None:
                outcomes[i] = failed(t("search_bad_args"))
            elif name not in REGISTRY:
                outcomes[i] = failed(t("tool_unknown", name=name))
            else:
                groups.setdefault(name, []).append(i)

        async def run_group(name: str, idxs: list[int]) -> None:
            _batch_lines.set([self._line_of[i] for i in idxs])
            results = await REGISTRY[name].run_batch([self._args[i] for i in idxs], self.chat_id)
            for i, outcome in zip(idxs, results):
                outcomes[i] = outcome

        await asyncio.gather(*(run_group(name, idxs) for name, idxs in groups.items()))
        for line in self.lines:
            idxs = [i for i, owner in enumerate(self._line_of) if owner is line]
            tool = REGISTRY.get(self.calls[idxs[0]]["function"]["name"])
            mine = [outcomes[i] for i in idxs]
            line.result = tool.batch_summary(mine) if tool and len(idxs) > 1 else mine[0].summary
        return [{"role": "tool", "tool_call_id": call["id"], "name": call["function"]["name"],
                 "content": outcome.content} for call, outcome in zip(self.calls, outcomes)]


def plan_round(calls: list[dict], chat_id: int | None) -> RoundPlan:
    """把模型这一轮请求的工具调用排成进度行（按调用顺序，可合并的同类调用并成一行）。"""
    plan = RoundPlan(calls, chat_id)
    plan._args = [llm.tool_args(call) for call in calls]
    plan._line_of = [None] * len(calls)
    for i, call in enumerate(calls):
        if plan._line_of[i] is not None:
            continue
        name, args = call["function"]["name"], plan._args[i]
        tool = REGISTRY.get(name)
        line = ProgressLine(tool.label(args or {}) if tool else name)
        if tool and args is not None:
            same = [j for j, c in enumerate(calls)
                    if c["function"]["name"] == name and plan._args[j] is not None]
            merged = tool.batch_label([plan._args[j] for j in same])
            if merged is not None:
                line.text = merged
                for j in same:
                    plan._line_of[j] = line
        plan._line_of[i] = line
        plan.lines.append(line)
    return plan
