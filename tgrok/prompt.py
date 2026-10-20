"""发给模型的内容怎么拼：系统提示词，以及每条 user 消息（说话人、引用、记忆、梗、时间、图片）。"""

from datetime import datetime

from telegram import Message

from . import config, llm, media, memory, slang, tools
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



def person_label(user) -> str:
    """给模型看的说话人标识：名字 + @用户名（有的话）+ 数字 id。
    昵称可以重名、随时改，id 唯一不变，模型和记忆靠它区分同名的人。"""
    if user.username:
        return t("person_label_handle", name=user.full_name, username=user.username, uid=user.id)
    return t("person_label", name=user.full_name, uid=user.id)


def sender_label(m) -> str:
    """一条消息真正的发送者：频道或匿名管理员发的消息 from_user 是 Telegram 的占位账号，要看 sender_chat。"""
    chat = getattr(m, "sender_chat", None)
    if chat is not None:
        if chat.id == getattr(m, "chat_id", None):
            return t("anon_admin")
        return t("chat_label", title=chat.title or chat.effective_name or "", cid=chat.id)
    return person_label(m.from_user) if m.from_user else t("someone")


def origin_label(origin) -> str:
    """转发消息的原作者（MessageOrigin 的四种）。"""
    if getattr(origin, "sender_user", None) is not None:
        return person_label(origin.sender_user)
    if getattr(origin, "sender_user_name", None):
        return t("hidden_user", name=origin.sender_user_name)
    chat = getattr(origin, "sender_chat", None) or getattr(origin, "chat", None)
    if chat is not None:
        return t("chat_label", title=chat.title or chat.effective_name or "", cid=chat.id)
    return t("someone")


def _same_sender(m, asker) -> bool:
    return (getattr(m, "sender_chat", None) is None and m.from_user is not None
            and asker is not None and m.from_user.id == asker.id)


def asked_by(msg: Message, text: str) -> str:
    """群里多人可以轮流接着同一段对话聊：每条 user 消息都带上说话人，模型才分得清谁在问。
    提问消息本身是转发来的时候，标明那段内容的原作者，转发的人只是转发。"""
    origin = getattr(msg, "forward_origin", None)
    if origin is not None:
        text = t("forwarded_by_asker", origin=origin_label(origin), text=text)
    if msg.chat.type == "private":
        return text
    return t("question_from", name=person_label(msg.from_user), question=text)


def quoted_context(replied: Message, asker=None, quote: str | None = None) -> str | None:
    """被引用的那条消息给模型的描述：写明发送者（是不是提问者本人）、转发的原作者、
    文字和媒体说明，以及提问者特意引用的片段；什么内容都没有时返回 None。"""
    content = "\n".join(p for p in (replied.text or replied.caption, media.media_note(replied)) if p)
    if not content and not quote:
        return None
    who = sender_label(replied)
    lines = [t("quoted_self" if _same_sender(replied, asker) else "quoted_other", author=who)]
    origin = getattr(replied, "forward_origin", None)
    if origin is not None:
        lines.append(t("quoted_forward", origin=origin_label(origin), author=who))
    if content:
        lines.append(t("quoted_body", content=content))
    if quote:
        lines.append(t("quoted_part", quote=quote))
    return "\n".join(lines)


def external_context(info, quote: str | None) -> str | None:
    """回复的是别的聊天或频道里的消息（external_reply）：只有来源和引用片段。"""
    if info is None:
        return None
    lines = [t("quoted_external", origin=origin_label(info.origin))]
    if quote:
        lines.append(t("quoted_part", quote=quote))
    return "\n".join(lines)


def user_turn(text: str, images: list[str], chat_id: int, slang_scan: str,
              memory_for: list[int] | None = None, recent_chat: str = "") -> dict:
    """一条 user 消息：[记忆附注] [梗词典附注] [最近群聊] 正文 [当前时间]，有图时是多模态数组。

    memory_for 给出要附上档案的人（提问者、被引用者），None 表示不附记忆。记忆只在一段对话的
    第一条 user 消息里注入一次：之后的追问沿用同一份历史，前缀字节不变，上游 prompt 缓存仍能命中。
    梗词典只附上 slang_scan 里真正出现了的词。recent_chat 是这条提问之前群里最近的聊天，
    只挂在最新这一条上，同样不影响缓存。
    """
    notes = [slang.slang_block(slang_scan, chat_id), recent_chat]
    if memory_for is not None:
        notes.insert(0, memory.memory_block(chat_id, memory_for))
    content = "\n\n".join([n for n in notes if n] + [text])
    return {"role": "user", "content": with_time(media.build_content(content, images))}


def new_conversation(turn: dict) -> list[dict]:
    return [{"role": "system", "content": system_prompt(llm.adapter.supports_tool_loop)}, turn]