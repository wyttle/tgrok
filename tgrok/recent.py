"""每个群最近的聊天（只在内存里）：群友常常就刚才聊的内容直接 @bot，提问时附上这段背景。

bot 收到的群消息都记（关闭隐私模式后是全部群消息，否则只有叫到 bot 的），也记 bot 自己的回复。
每行都写明谁说的、回复的是谁，模型才不会把话安错人。语音、视频等媒体在渲染时取预读好的转述。
"""

import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime

from telegram import Message

from . import config, media, prompt, viewer
from .i18n import t

KEEP_PER_CHAT = 80
KEEP_CHATS = 500
LINE_CHARS = 300
BOT_LINE_CHARS = 200


@dataclass
class Entry:
    msg_id: int
    ts: float
    who: str
    text: str
    reply_to: str  # 回复的那条消息的发送者标识；不是回复时为空串
    media_uid: str | None = None
    media_what: str = ""


_chats: "OrderedDict[int, deque[Entry]]" = OrderedDict()


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _queue(chat_id: int) -> deque:
    q = _chats.get(chat_id)
    if q is None:
        q = _chats[chat_id] = deque(maxlen=KEEP_PER_CHAT)
        while len(_chats) > KEEP_CHATS:
            _chats.popitem(last=False)
    _chats.move_to_end(chat_id)
    return q


def _reply_label(m: Message, bot_id: int) -> str:
    replied = m.reply_to_message
    if replied is None:
        return ""
    if replied.from_user is not None and replied.from_user.id == bot_id and replied.sender_chat is None:
        return t("recent_you")
    return prompt.sender_label(replied)


def add(m: Message, bot_id: int) -> None:
    """记下一条群消息（私聊不记）。"""
    if not config.RECENT_CHAT_COUNT or m.chat.type == "private":
        return
    entry = media.media_entry(m)
    text = m.text or m.caption or ""
    if not entry and m.sticker:
        text = " ".join(p for p in (t("observed_sticker", emoji=f" {m.sticker.emoji}" if m.sticker.emoji else ""),
                                    text) if p)
    elif not entry and media.image_entry(m):
        text = " ".join(p for p in (t("observed_image"), text) if p)
    who = prompt.sender_label(m)
    origin = getattr(m, "forward_origin", None)
    if origin is not None:
        text = t("recent_forward", origin=prompt.origin_label(origin)) + text
    _queue(m.chat_id).append(Entry(
        m.message_id, m.date.timestamp() if m.date else time.time(), who, _clip(text, LINE_CHARS),
        _reply_label(m, bot_id), entry["uid"] if entry else None, media.describe(entry) if entry else ""))


def add_bot(chat_id: int, msg_ids: list[int], text: str, asker: str) -> None:
    """记下 bot 自己的一条回复（分段时记第一段的 id，其余段也算在内）。"""
    if not config.RECENT_CHAT_COUNT or chat_id not in _chats or not msg_ids:
        return
    _queue(chat_id).append(Entry(msg_ids[0], time.time(), t("recent_you"), _clip(text, BOT_LINE_CHARS), asker))


def _line(e: Entry) -> str:
    when = datetime.fromtimestamp(e.ts, config.BOT_TZ).strftime("%H:%M")
    body = e.text
    if e.media_uid:
        heard = viewer.ready(e.media_uid)
        tag = t("recent_media_heard", what=e.media_what, text=_clip(heard, LINE_CHARS)) if heard \
            else t("observed_media", what=e.media_what)
        body = " ".join(p for p in (tag, body) if p)
    if e.reply_to:
        return t("recent_reply_line", when=when, who=e.who, to=e.reply_to, text=body)
    return t("recent_line", when=when, who=e.who, text=body)


def block(chat_id: int, before_id: int, since: float, skip: set[int] = frozenset()) -> str:
    """当前提问之前、since 之后的最近几条群聊（不含 skip 里的消息，比如被引用的那条）；没有返回空串。"""
    q = _chats.get(chat_id)
    if not q or not config.RECENT_CHAT_COUNT:
        return ""
    since = max(since, time.time() - config.RECENT_CHAT_MINUTES * 60)
    picked = [e for e in q if e.ts >= since and e.msg_id < before_id and e.msg_id not in skip]
    picked = picked[-config.RECENT_CHAT_COUNT:]
    if not picked:
        return ""
    return "[" + t("recent_header") + "\n" + "\n".join(_line(e) for e in picked) + "]"


def clear() -> None:
    _chats.clear()
