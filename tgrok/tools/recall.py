"""recall：按关键词、说话人、时间查这个聊天以前的聊天原话。"""

import time
from datetime import datetime

from .. import config
from ..i18n import t
from ..memory import store
from . import Outcome, Tool, failed

MAX_LIMIT = 30
TEXT_CHARS = 1200  # 单条记录回灌给模型的字数上限


class RecallTool(Tool):
    name = "recall"
    description = (
        "Search this chat's earlier messages word for word: people's questions, your answers, and overheard group "
        "chat. Returns matching messages with time and speaker, oldest first. Every keyword must appear; use words of "
        "at least 2-3 characters. Leave query empty to browse by person or time."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keywords separated by spaces, e.g. 生日 or 周五 开黑."},
            "user_id": {"type": "integer", "description": "Optional: only messages from this person (numeric id)."},
            "days": {"type": "integer", "description": "Optional: only the last N days (default: everything kept)."},
            "limit": {"type": "integer", "description": f"Optional: how many of the newest matches, default 15, max {MAX_LIMIT}."},
        },
    }

    def enabled(self) -> bool:
        return config.MEMORY_ENABLED

    def prompt_note(self) -> str:
        return t("recall_prompt", days=config.MEMORY_ARCHIVE_DAYS)

    def label(self, args: dict) -> str:
        what = str(args.get("query", "")).strip() or (f"id {args['user_id']}" if args.get("user_id") else "")
        return t("tool_recall", q=what[:48] or "?")

    async def run(self, args: dict, chat_id: int | None) -> Outcome:
        if chat_id is None:
            return failed(t("recall_none", days=config.MEMORY_ARCHIVE_DAYS))
        try:
            days = min(int(args.get("days") or config.MEMORY_ARCHIVE_DAYS), config.MEMORY_ARCHIVE_DAYS)
            limit = max(1, min(int(args.get("limit") or 15), MAX_LIMIT))
            user_id = int(args["user_id"]) if args.get("user_id") not in (None, "") else None
        except (TypeError, ValueError):
            return failed(t("search_bad_args"))
        found = store.search_messages(chat_id, str(args.get("query", "")), user_id=user_id,
                                      since=time.time() - days * 86400, limit=limit)
        if not found:
            return Outcome(t("recall_none", days=days), True, t("res_found", n=0))
        lines = []
        for m in found:
            when = datetime.fromtimestamp(m.ts, config.BOT_TZ).strftime("%Y-%m-%d %H:%M")
            text = m.text[:TEXT_CHARS]
            lines.append(t("recall_reply_line", when=when, text=text) if m.kind == "reply"
                         else t("recall_line", when=when, who=m.who, text=text))
        return Outcome("\n".join(lines), True, t("res_found", n=len(found)))
