"""关键词直接回复：群里任何人发的一整条消息（去掉 @bot 后）正好是某个关键词，就直接把存好的内容发到聊天里。

不用叫到 bot，不看白名单，不引用对方那条消息；不调用模型，不进对话历史、长期记忆和 bot 的最近回复。
关键词不分大小写，连续空白算一个空格。管理员用 /kv 增删查，存在 MEMORY_DB 的 autoreply 表里，重启不丢。
"""

import time

from .db import db, schema

MAX_TRIGGER_CHARS = 64
MAX_REPLY_CHARS = 4000

schema("""
CREATE TABLE IF NOT EXISTS autoreply (
    key TEXT PRIMARY KEY,      -- 规范化后的关键词（小写、空白合并），用来匹配
    trigger TEXT NOT NULL,     -- 管理员写的原样，列表里显示
    reply TEXT NOT NULL,
    updated REAL NOT NULL
);
""")


def _norm(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def lookup(text: str) -> str | None:
    """这句话正好是某个关键词时返回要回的内容，否则 None。"""
    key = _norm(text)
    if not key or len(key) > MAX_TRIGGER_CHARS:
        return None
    row = db().execute("SELECT reply FROM autoreply WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def put(trigger: str, reply: str) -> str | None:
    """新增或覆盖一条。成功返回整理后的关键词；关键词为空或太长、回复为空时返回 None。"""
    trigger = " ".join((trigger or "").split())
    reply = (reply or "").strip()[:MAX_REPLY_CHARS]
    if not trigger or len(trigger) > MAX_TRIGGER_CHARS or not reply:
        return None
    db().execute("INSERT OR REPLACE INTO autoreply(key, trigger, reply, updated) VALUES (?, ?, ?, ?)",
                 (_norm(trigger), trigger, reply, time.time()))
    return trigger


def remove(trigger: str) -> bool:
    return db().execute("DELETE FROM autoreply WHERE key = ?", (_norm(trigger),)).rowcount > 0


def entries() -> list[tuple[str, str]]:
    """全部（关键词, 回复），按关键词排序。"""
    return [(r[0], r[1]) for r in db().execute("SELECT trigger, reply FROM autoreply ORDER BY key")]
