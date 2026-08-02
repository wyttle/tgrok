"""系统提示词组装与实时时间注入。"""

import os
from datetime import datetime

from . import config
from .config import BOT_TZ, BOT_TZ_NAME, SEARCH_ENABLED, GEMINI_SEARCH_MODEL, BOT_LANG
from .i18n import STRINGS, t

SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", t("system_prompt"))
if SEARCH_ENABLED:
    # 明确告知模型它拥有联网搜索能力，避免它声称"我无法联网"
    SYSTEM_PROMPT += "\n\n" + t("search_system_prompt")
    if GEMINI_SEARCH_MODEL:
        SYSTEM_PROMPT += t("search_agent_note")


def current_time_line() -> str:
    """返回一句描述当前真实时间的文本，随每次请求实时生成。

    附加到「当前这条用户消息」末尾，而非系统提示：系统提示与历史轮次保持字节
    不变，才能命中上游的 prompt 缓存；时间只挂在本来就是新内容的最新一轮上。
    """
    now = datetime.now(BOT_TZ)
    weekday = STRINGS[BOT_LANG]["weekday"][now.weekday()]
    stamp = f"{now:%Y-%m-%d %H:%M} {weekday}"
    return t("current_time", time=stamp, tz=BOT_TZ_NAME)


def with_time(content):
    """把当前时间行拼到用户消息内容末尾，兼容纯文本与多模态 content 数组。"""
    line = current_time_line()
    if isinstance(content, str):
        return f"{content}\n\n[{line}]"
    # 多模态：追加到文本块（首个 text 块），没有则插一个
    for part in content:
        if part.get("type") == "text":
            part["text"] = f"{part['text']}\n\n[{line}]"
            return content
    return [{"type": "text", "text": f"[{line}]"}] + content
