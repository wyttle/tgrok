"""每个群最近的聊天：群友常常就刚才聊的内容直接 @bot，提问时附上这段背景。

bot 收到的群消息都记（关闭隐私模式后是全部群消息，否则只有叫到 bot 的），也记 bot 自己的回复。
存在数据库里，重启后接着用；超过 RECENT_CHAT_MINUTES 的随写随删。
每行都写明谁说的、回复的是谁，模型才不会把话安错人。语音、视频等媒体在渲染时取预读好的转述。
"""

import time
from dataclasses import dataclass
from datetime import datetime

from telegram import Message

from . import config, media, prompt, viewer
from .db import db, schema
from .i18n import t

LINE_CHARS = 600
BOT_LINE_CHARS = 800  # bot 自己的回复截短了，群友追问「你刚才说的」时模型就只记得开头
BOT_KEEP = 3  # 不管群里多热闹，都带上 bot 自己最近几条回复

schema("""
CREATE TABLE IF NOT EXISTS recent (
    chat_id INTEGER NOT NULL,
    msg_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    user_id INTEGER,          -- 发消息的人；bot 的回复记提问者。/forgetme 时一起删
    who TEXT NOT NULL,
    text TEXT NOT NULL,
    reply_to TEXT NOT NULL,   -- 回复的那条消息的发送者标识；不是回复时为空串
    media_uid TEXT,
    media_what TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS recent_ts ON recent(ts);
""")


@dataclass
class Entry:
    msg_id: int
    ts: float
    who: str
    text: str
    reply_to: str
    media_uid: str | None = None
    media_what: str = ""


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _save(chat_id: int, user_id: int | None, e: Entry) -> None:
    conn = db()
    conn.execute("INSERT OR REPLACE INTO recent(chat_id, msg_id, ts, user_id, who, text, reply_to, media_uid, media_what) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (chat_id, e.msg_id, e.ts, user_id, e.who, e.text, e.reply_to, e.media_uid, e.media_what))
    conn.execute("DELETE FROM recent WHERE ts < ?", (time.time() - config.RECENT_CHAT_MINUTES * 60,))


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
    author = m.from_user.id if m.from_user is not None and m.sender_chat is None else None
    _save(m.chat_id, author, Entry(
        m.message_id, m.date.timestamp() if m.date else time.time(), who, _clip(text, LINE_CHARS),
        _reply_label(m, bot_id), entry["uid"] if entry else None, media.describe(entry) if entry else ""))


def add_bot(chat_id: int, msg_ids: list[int], text: str, asker: str, asker_id: int) -> None:
    """记下 bot 自己的一条回复（分段时记第一段的 id，其余段也算在内）。私聊（chat_id 为正）不记。"""
    if not config.RECENT_CHAT_COUNT or chat_id > 0 or not msg_ids:
        return
    _save(chat_id, asker_id, Entry(msg_ids[0], time.time(), t("recent_you"), _clip(text, BOT_LINE_CHARS), asker))


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
    """当前提问之前、since 之后的最近几条群聊（不含 skip 里的消息，比如被引用的那条）；没有返回空串。
    群里热闹时最近 RECENT_CHAT_COUNT 条可能只盖住几分钟，所以 bot 自己最近的 BOT_KEEP 条回复不管多早都带上
    （仍限 RECENT_CHAT_MINUTES 以内），群友问「你刚才说的」时接得上。"""
    if not config.RECENT_CHAT_COUNT:
        return ""
    since = max(since, time.time() - config.RECENT_CHAT_MINUTES * 60)
    query = ("SELECT msg_id, ts, who, text, reply_to, media_uid, media_what FROM recent "
             "WHERE chat_id = ? AND ts >= ? AND msg_id < ? {} ORDER BY msg_id DESC LIMIT ?")
    rows = db().execute(query.format(""), (chat_id, since, before_id, config.RECENT_CHAT_COUNT + len(skip))).fetchall()
    mine = db().execute(query.format("AND who = ?"),
                        (chat_id, since, before_id, t("recent_you"), BOT_KEEP + len(skip))).fetchall()
    picked = [Entry(**dict(r)) for r in rows if r["msg_id"] not in skip][:config.RECENT_CHAT_COUNT]
    seen = {e.msg_id for e in picked}
    picked += [Entry(**dict(r)) for r in mine if r["msg_id"] not in skip and r["msg_id"] not in seen][:BOT_KEEP]
    if not picked:
        return ""
    picked.sort(key=lambda e: e.msg_id)
    return "[" + t("recent_header") + "\n" + "\n".join(_line(e) for e in picked) + "]"


def forget_chat(chat_id: int) -> int:
    return db().execute("DELETE FROM recent WHERE chat_id = ?", (chat_id,)).rowcount


def forget_user(user_id: int) -> int:
    return db().execute("DELETE FROM recent WHERE user_id = ?", (user_id,)).rowcount


def clear() -> None:
    db().execute("DELETE FROM recent")
