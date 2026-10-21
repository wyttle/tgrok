"""web_search 和 open_url：联网搜索、读取网页正文（实现在 tgrok/web.py）。"""

import re

from .. import config, web
from ..i18n import t
from . import Outcome, Tool, failed, human_chars

_RESULT_ROW_RE = re.compile(r"^\[\d+\]", re.M)


def _search_summary(content: str) -> str:
    rows = len(_RESULT_ROW_RE.findall(content))
    # grounding 综述没有 [n] 行标记，显示字数
    return t("res_results", n=rows) if rows else t("res_chars", k=human_chars(len(content)))


class WebSearchTool(Tool):
    """配置了 GEMINI_SEARCH_MODEL 时由 Gemini + Google 搜索执行：每次调用内部会检索几次并汇总，
    所以同一轮的多个调用合并成一次执行。它只是快速检索；要认真查证的事交给 research。"""

    name = "web_search"

    @property
    def description(self) -> str:
        research = " For anything serious or that needs real checking, use research instead." if config.RESEARCH_MODELS else ""
        if config.GEMINI_SEARCH_MODEL:
            return ("Quick Google lookup run by Gemini: give it one question in natural language and it returns a "
                    "short fact summary with sources. For simple lookups with one clear answer." + research)
        return "Search the web for up-to-date information: simple lookups, recent events, time-sensitive facts." + research

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The question to look up, in the language most likely to find good results."
                        if config.GEMINI_SEARCH_MODEL
                        else "The search query, in the language most likely to find good results."
                    ),
                }
            },
            "required": ["query"],
        }

    def enabled(self) -> bool:
        return config.SEARCH_ENABLED

    def prompt_note(self) -> str:
        note = t("search_system_prompt")
        if config.GEMINI_SEARCH_MODEL:
            note += t("search_agent_note")
        if config.RESEARCH_MODELS:
            return note  # 什么时候查、查梗的出处都由 research 的说明统一讲，这里不再重复
        return note + t("search_when_prompt") + "\n\n" + t("slang_search_prompt")

    def label(self, args: dict) -> str:
        return t("tool_search", q=self._query(args)[:48] or "?")

    def batch_label(self, args_list: list[dict]) -> str | None:
        if not config.GEMINI_SEARCH_MODEL or len(args_list) < 2:
            return None
        return t("tool_search", q="；".join(self._query(a) or "?" for a in args_list)[:48])

    @staticmethod
    def _query(args: dict) -> str:
        return str(args.get("query", "")).strip()

    async def run(self, args: dict, chat_id: int | None) -> Outcome:
        return await self._search(self._query(args))

    async def run_batch(self, args_list: list[dict], chat_id: int | None) -> list[Outcome]:
        queries = [q for q in map(self._query, args_list) if q]
        if not config.GEMINI_SEARCH_MODEL or len(queries) < 2:
            return await super().run_batch(args_list, chat_id)
        merged = await self._search("；".join(dict.fromkeys(queries)))
        note = Outcome(t("search_merged"), merged.ok, merged.summary)
        return [merged] + [note] * (len(args_list) - 1)

    @staticmethod
    async def _search(query: str) -> Outcome:
        try:
            content = await web.run_web_search(query)
        except web.WebError as e:
            return failed(str(e))
        return Outcome(content, True, _search_summary(content))


class OpenUrlTool(Tool):
    name = "open_url"
    description = (
        "Fetch a web page by URL and return its readable text. "
        "Use it to read the details behind links found via web_search."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The full http(s) URL of the page to read."}
        },
        "required": ["url"],
    }

    def enabled(self) -> bool:
        return config.SEARCH_ENABLED

    def label(self, args: dict) -> str:
        return t("tool_open")

    def batch_label(self, args_list: list[dict]) -> str | None:
        # 同一轮读几个网页合并成一行，结果是总字数；不向群里显示网址
        return t("tool_open") if len(args_list) == 1 else t("tool_open_n", n=len(args_list))

    def batch_summary(self, outcomes: list[Outcome]) -> str:
        ok = [o for o in outcomes if o.ok]
        if not ok:
            return t("res_failed")
        summary = t("res_chars", k=human_chars(sum(len(o.content) for o in ok)))
        fails = len(outcomes) - len(ok)
        return summary + (t("res_fail_suffix", n=fails) if fails else "")

    async def run(self, args: dict, chat_id: int | None) -> Outcome:
        try:
            content = await web.run_fetch_url(str(args.get("url", "")))
        except web.WebError as e:
            return failed(str(e))
        return Outcome(content, True, t("res_chars", k=human_chars(len(content))))
