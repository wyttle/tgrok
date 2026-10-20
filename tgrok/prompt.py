"""系统提示词组装与实时时间注入。"""

from datetime import datetime

from . import config, tools
from .i18n import phrases, t


def system_prompt(with_tools: bool) -> str:
    """系统提示词：人设（SYSTEM_PROMPT 或内置默认）+ 固定约定 + 挂载工具的使用说明。

    with_tools 跟着当前模型走：不走 bot 工具循环的协议（Gemini 原生）不提这些工具，
    免得模型以为自己能调用。
    """
    base = config.SYSTEM_PROMPT_OVERRIDE if config.SYSTEM_PROMPT_OVERRIDE is not None else t("system_prompt")
    # 说话人标识的约定：用 id 区分同名的人，回复里只用名字
    parts = [base, t("speaker_note")]
    if config.REPLY_SINGLE_PARAGRAPH:
        parts.append(t("single_paragraph_note"))
    if with_tools:
        parts += tools.prompt_notes()
    return "\n\n".join(parts)


def current_time_line() -> str:
    """返回一句描述当前真实时间的文本，随每次请求实时生成。

    附加到「当前这条用户消息」末尾，而非系统提示：系统提示与历史轮次保持字节
    不变，才能命中上游的 prompt 缓存；时间只挂在本来就是新内容的最新一轮上。
    """
    now = datetime.now(config.BOT_TZ)
    weekday = phrases("weekday")[now.weekday()]
    stamp = f"{now:%Y-%m-%d %H:%M} {weekday}"
    return t("current_time", time=stamp, tz=config.BOT_TZ_NAME)


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
