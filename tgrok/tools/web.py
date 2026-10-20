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
    """配置了 GEMINI_SEARCH_MODEL 时它是深度调研代理：每次调用内部已经多轮检索，
    所以同一轮的多个调用合并成一次综合调研执行，避免主模型把它当浏览器逐条调度。"""

    name = "web_search"

    @property
    def description(self) -> str:
        if config.GEMINI_SEARCH_MODEL:
            return ("Deep research agent backed by Google Search. Give it ONE comprehensive "
                    "research task in natural language — it can cover several aspects at once, "
                    "and the agent will run multiple Google searches internally and return a "
                    "fact summary with sources. Do NOT split one round of research into many "
                    "narrow queries; one well-phrased task per round is enough.")
        return ("Search the web for up-to-date information. Use this for recent events, "
                "time-sensitive facts, or anything you are unsure about.")

    @property
    def parameters(self) -> dict:
        return {
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
        }

    def enabled(self) -> bool:
        return config.SEARCH_ENABLED

    def prompt_note(self) -> str:
        note = t("search_system_prompt")
        if config.GEMINI_SEARCH_MODEL:
            note += t("search_agent_note")
        # 开了深入调研时，查梗的出处交给 research，不让 web_search 的说明把它抢走
        return note + "\n\n" + t("slang_research_prompt" if config.RESEARCH_MODELS else "slang_search_prompt")

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
